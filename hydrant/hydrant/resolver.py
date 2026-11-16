"""Context resolution: walk the graph from the task's words to the few tools the task needs.

One resolve is a short walk, every hop concurrent with its siblings:

1. Term nodes for the task's words (point reads).
2. Term-[:MEANS]->Term (learned aliases), Term-[:NAMES]->Server, Term-[:DENOTES]->Entity.
3. Term-[:MENTIONS]->Tool for the words and their aliases, Server-[:PROVIDES]->Tool.
4. From the best tools: Tool-[:NEEDS]->Resource and Tool-[:THEN]->Tool through the batched adjacency read,
   then Resource-[:YIELDED_BY]->Tool and the THEN weights for the edges that exist.
5. Definitions for the tools that made it.

Nothing is preloaded and no embedding is computed. The graph is the index.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field

from .ingest import idf
from .store import Store
from .text import count_tokens, nid, terms


@dataclass
class Config:
    k_seed: int = 8            # most tools picked on lexical evidence
    tau: float = 0.25          # a seed must score at least tau x the best seed
    k_max: int = 12            # hard cap on tools in one context
    max_df_frac: float = 0.25  # words that mention more than this share of tools carry no signal
    server_boost: float = 0.6  # multiplier bonus for tools on a server the task names
    server_base: float = 0.5   # floor score for a named server's tools with no lexical hit
    expand_top: int = 5        # seeds whose requirements and workflows are followed
    prereq_min: float = 0.5    # a lookup tool is pulled in while its YIELDED_BY weight is at least this
    then_min: float = 1.0      # a learned workflow needs this many observations ...
    then_frac: float = 0.4     # ... and to follow its source tool this often
    alias_min: float = 2.0     # a learned alias or server association needs this many observations ...
    alias_frac: float = 0.5    # ... and to hold this share of the times the word was seen
    alias_gain: float = 0.7    # weight of an alias relative to the word itself
    expand: bool = True        # follow NEEDS / YIELDED_BY / THEN (off = terms only, the ablation)
    aliases: bool = True       # follow learned MEANS and learned NAMES edges
    cache_ttl: float = 30.0    # seconds a word's MENTIONS edges are reused; 0 reads HydraDB every time
    entities: bool = True      # use resolved-entity memory


@dataclass
class Picked:
    id: int
    key: str
    role: str                  # seed | prereq | workflow
    score: float
    via: list[dict]
    tok: int = 0
    name: str = ""
    server: str = ""
    summary: str = ""
    definition: dict | None = None


@dataclass
class Context:
    task: str
    tools: list[Picked]
    entities: list[dict]
    tokens: int
    queries: int
    ms: float
    terms: dict[int, str]                                  # every query term id -> text
    known: dict[int, dict]                                 # term id -> {df, srv, ent} for terms in the graph
    hits: dict[int, dict[int, float]] = field(default_factory=dict)   # term id -> tool id -> weight
    ranked: list[tuple[int, float]] = field(default_factory=list)     # lexical ranking, best first
    yielders: list[dict] = field(default_factory=list)                # every NEEDS->YIELDED_BY row seen
    keys: dict[int, str] = field(default_factory=dict)
    server_terms: dict[str, list[str]] = field(default_factory=dict)  # server -> the words that named it
    entity_terms: dict[str, list[str]] = field(default_factory=dict)  # entity key -> the words that denoted it

    def trace(self, top_terms: int = 3) -> dict:
        """The subgraph this resolve walked, as nodes and typed edges, for display."""
        nodes: dict[str, dict] = {}
        edges: list[dict] = []

        def node(kind: str, key: str, **extra) -> str:
            i = f"{kind}:{key}"
            nodes.setdefault(i, {"id": i, "kind": kind, "label": key, **extra})
            return i

        for t in self.tools:
            tid = node("tool", t.key, role=t.role, server=t.server, name=t.name)
            lex = sorted((v for v in t.via if "term" in v), key=lambda v: -v["score"])[:top_terms]
            for v in lex:
                a = node("term", v["term"])
                if "alias" in v:
                    b = node("alias", v["alias"])
                    edges.append({"from": a, "to": b, "type": "MEANS"})
                    edges.append({"from": b, "to": tid, "type": "MENTIONS", "w": v["weight"]})
                else:
                    edges.append({"from": a, "to": tid, "type": "MENTIONS", "w": v["weight"]})
            for v in t.via:
                if "server" in v:
                    s = node("server", v["server"])
                    edges.append({"from": s, "to": tid, "type": "PROVIDES"})
                    for term in self.server_terms.get(v["server"], []):
                        edges.append({"from": node("term", term), "to": s, "type": "NAMES"})
                if "needs" in v:
                    r = node("resource", str(v["needs"]))
                    edges.append({"from": f"tool:{v['for']}", "to": r, "type": "NEEDS"})
                    edges.append({"from": r, "to": tid, "type": "YIELDED_BY", "w": v.get("weight")})
                if "after" in v:
                    edges.append({"from": f"tool:{v['after']}", "to": tid, "type": "THEN", "w": v.get("seen")})
        for e in self.entities:
            key = f"{e.get('server')}:{e['kind']}:{e['name'].lower()}"
            eid = node("entity", key, name=e["name"], ref=e["ref"])
            for term in self.entity_terms.get(key, []):
                edges.append({"from": node("term", term), "to": eid, "type": "DENOTES"})
        seen, uniq = set(), []
        for e in edges:
            k = (e["from"], e["to"], e["type"])
            if k not in seen and e["from"] in nodes and e["to"] in nodes:
                seen.add(k)
                uniq.append(e)
        return {"nodes": list(nodes.values()), "edges": uniq}

    def keyset(self) -> set[str]:
        return {t.key for t in self.tools}

    def to_dict(self, definitions: bool = True) -> dict:
        return {
            "task": self.task,
            "tools": [
                {
                    "key": t.key, "server": t.server, "name": t.name, "role": t.role, "score": round(t.score, 3),
                    "tokens": t.tok, "summary": t.summary, "via": t.via,
                    **({"definition": t.definition} if definitions else {}),
                }
                for t in self.tools
            ],
            "entities": self.entities,
            "tokens": self.tokens,
            "stats": {
                "queries": self.queries, "ms": round(self.ms, 2),
                "terms_known": [self.terms[i] for i in self.known],
                "terms_unknown": [t for i, t in self.terms.items() if i not in self.known],
            },
        }


class Resolver:
    def __init__(self, store: Store, cfg: Config | None = None) -> None:
        self.store = store
        self.cfg = cfg or Config()
        self._n: tuple[float, int] | None = None
        self._res: tuple[float, dict[int, str]] | None = None
        self._post: dict[int, tuple[float, list[tuple[int, float]]]] = {}

    def forget(self, term_ids) -> None:
        """Drop cached MENTIONS edges for words the learner just rewrote."""
        for t in term_ids:
            self._post.pop(t, None)

    async def mentions(self, term_ids: list[int], ttl: float) -> list[dict]:
        """Term-[:MENTIONS {w}]->Tool for each word. A word's edges are reused for `ttl` seconds and dropped
        the moment an outcome rewrites them; everything else in a resolve is read from HydraDB every time."""
        now = time.monotonic()
        rows, miss = [], []
        for t in term_ids:
            hit = self._post.get(t) if ttl else None
            if hit and hit[0] > now:
                rows += [{"src": t, "dst": d, "w": w} for d, w in hit[1]]
            else:
                miss.append(t)
        if miss:
            fresh = await self.store.out("Term", miss, "MENTIONS", ("w",), ())
            by: dict[int, list[tuple[int, float]]] = {t: [] for t in miss}
            for r in fresh:
                by[r["src"]].append((r["dst"], r.get("w") or 0.0))
            if ttl:
                if len(self._post) > 50_000:
                    self._post.clear()
                for t, edges in by.items():
                    self._post[t] = (now + ttl, edges)
            rows += fresh
        return rows

    async def resources(self, ttl: float = 30.0) -> dict[int, str]:
        """Resource id -> key, for readable traces. Cached briefly."""
        now = time.monotonic()
        if self._res is None or now - self._res[0] > ttl:
            self._res = (now, {r["id"]: f"{r['server']}:{r.get('name') or r['kind']}"
                               for r in await self.store.scan("Resource", ("server", "kind", "name"))})
        return self._res[1]

    async def n_tools(self, ttl: float = 30.0) -> int:
        """Catalog size, for idf. Cached briefly; one label scan when stale."""
        now = time.monotonic()
        if self._n is None or now - self._n[0] > ttl:
            self._n = (now, len(await self.store.scan("Tool", ())))
        return self._n[1]

    async def resolve(self, task: str, cfg: Config | None = None) -> Context:
        cfg = cfg or self.cfg
        st = self.store
        t0, q0 = time.perf_counter(), st.queries
        q = {nid("term", t): t for t in terms(task)}
        n = await self.n_tools()

        known = await st.get("Term", q, ("df", "srv", "ent", "al", "seen"))
        cap = cfg.max_df_frac * n
        lexical = [i for i in known if 0 < (known[i]["df"] or 0) <= cap] or [i for i in known if known[i]["df"]]
        srv_terms = [i for i in known if known[i].get("srv")]
        ent_terms = [i for i in known if known[i].get("ent")] if cfg.entities else []
        al_terms = [i for i in known if known[i].get("al")] if cfg.aliases else []

        means_rows, names_rows, ent_rows = await asyncio.gather(
            st.out("Term", al_terms, "MEANS", ("c",), ("key", "df")),
            st.out("Term", srv_terms, "NAMES", ("p", "c"), ("key",)),
            st.out("Term", ent_terms, "DENOTES", ("w",), ("key", "name", "kind", "ref", "res", "nterms", "server")),
        )

        def held(count, term) -> float:
            """Share of a word's sightings that an association accounts for, 0 when it is not yet trusted."""
            seen = known[term].get("seen") or 0
            c = count or 0
            return c / seen if c >= cfg.alias_min and seen and c / seen >= cfg.alias_frac else 0.0

        alias: dict[int, tuple[int, float, str, int]] = {}      # alias term id -> (source term, gain, key, df)
        for r in means_rows:
            share = held(r.get("c"), r["src"])
            if share and r["dst"] not in q and share > alias.get(r["dst"], (0, 0.0))[1]:
                alias[r["dst"]] = (r["src"], min(1.0, share) * cfg.alias_gain, r["key"], r.get("df") or 0)
        servers: dict[int, str] = {}
        server_terms: dict[str, list[str]] = defaultdict(list)
        for r in names_rows:
            if r.get("p") or (cfg.aliases and held(r.get("c"), r["src"])):
                servers[r["dst"]] = r["key"]
                server_terms[r["key"]].append(q[r["src"]])

        fan = lexical + [a for a in alias if alias[a][3]]
        hits_rows, srv_rows = await asyncio.gather(
            self.mentions(fan, cfg.cache_ttl),
            st.reach(list(servers), "PROVIDES"),
        )

        score: dict[int, float] = defaultdict(float)
        via: dict[int, list[dict]] = defaultdict(list)
        hits: dict[int, dict[int, float]] = defaultdict(dict)
        for r in hits_rows:
            w = r.get("w") or 0.0
            if w <= 0:
                continue
            if r["src"] in alias:
                src, gain, key, df = alias[r["src"]]
                s = idf(n, df) * w * gain
                via[r["dst"]].append({"term": q[src], "alias": key, "weight": round(w, 3), "score": round(s, 3)})
            else:
                s = idf(n, known[r["src"]]["df"] or 0) * w
                hits[r["src"]][r["dst"]] = w
                via[r["dst"]].append({"term": q[r["src"]], "weight": round(w, 3), "score": round(s, 3)})
            score[r["dst"]] += s
        for r in srv_rows:
            tool = r["dst"]
            if tool in score:
                score[tool] *= 1 + cfg.server_boost
            else:
                score[tool] = cfg.server_base
            via[tool].append({"server": servers[r["src"]]})

        ranked = sorted(score.items(), key=lambda x: (-x[1], x[0]))
        seeds = [t for t, s in ranked[: cfg.k_seed] if s >= cfg.tau * ranked[0][1]] if ranked else []

        ent_hit: dict[int, dict] = {}
        ent_count: dict[int, int] = defaultdict(int)
        entity_terms: dict[str, list[str]] = defaultdict(list)
        for r in ent_rows:
            ent_hit[r["dst"]] = r
            ent_count[r["dst"]] += 1
            entity_terms[r["key"]].append(q[r["src"]])
        matched = [e for i, e in ent_hit.items() if ent_count[i] >= (e.get("nterms") or 1)]
        satisfied = {e["res"]: e for e in matched}

        picked: dict[int, Picked] = {t: Picked(t, "", "seed", score[t], via[t]) for t in seeds}
        ctx_entities: dict[str, dict] = {}
        yielders: list[dict] = []
        if cfg.expand and seeds:
            top = seeds[: cfg.expand_top]
            # topology first, through the batched adjacency read: which seeds need what, which have a workflow
            need_rows, then_topo = await asyncio.gather(st.reach(top, "NEEDS"), st.reach(top, "THEN"))
            open_res = list(dict.fromkeys(r["dst"] for r in need_rows if r["dst"] not in satisfied))
            then_srcs = list(dict.fromkeys(r["src"] for r in then_topo))
            # then weights, only for the edges that exist
            y_rows, then, info = await asyncio.gather(
                st.out("Resource", open_res, "YIELDED_BY", ("w",), ("key",)),
                st.out("Tool", then_srcs, "THEN", ("w",), ("key",)),
                st.get("Tool", then_srcs, ("uses",)),
            )
            by_res: dict[int, list[dict]] = defaultdict(list)
            for y in y_rows:
                by_res[y["src"]].append(y)
            by_need: dict[tuple[int, int], list[dict]] = {}
            for r in need_rows:
                rows = [{"src": r["src"], "mid": r["dst"], "dst": y["dst"], "w": y.get("w"), "key": y["key"]}
                        for y in by_res.get(r["dst"], [])]
                by_need[(r["src"], r["dst"])] = rows
                yielders += rows
            extra: list[Picked] = []
            for (seed, res), rows in by_need.items():
                if res in satisfied:
                    e = satisfied[res]
                    ctx_entities[e["key"]] = {
                        "kind": e["kind"], "name": e["name"], "ref": e["ref"], "server": e.get("server"),
                    }
                    continue
                if not rows:
                    continue
                best = max(rows, key=lambda r: (r.get("w") or 0, -r["dst"]))
                if (best.get("w") or 0) >= cfg.prereq_min and best["dst"] not in picked:
                    extra.append(
                        Picked(best["dst"], best["key"], "prereq", score[seed] * 0.5 + (best.get("w") or 0) * 0.01,
                               [{"needs": res, "for": seed, "weight": best.get("w")}])
                    )
            for r in then:
                w = r.get("w") or 0
                uses = (info.get(r["src"]) or {}).get("uses") or 0
                if w >= cfg.then_min and w >= cfg.then_frac * uses and r["dst"] not in picked:
                    extra.append(Picked(r["dst"], r["key"], "workflow", score[r["src"]] * 0.6 + w * 0.01,
                                        [{"after": r["src"], "seen": w}]))
            for p in sorted(extra, key=lambda p: -p.score):
                if p.id not in picked and len(picked) < cfg.k_max:
                    picked[p.id] = p
        defs = await st.get("Tool", picked, ("key", "name", "server", "summary", "defn", "tok"))
        # an entity whose words all matched is worth a line when the context touches its server
        live = {d["server"] for d in defs.values()}
        for e in matched:
            if e.get("server") in live:
                ctx_entities.setdefault(
                    e["key"], {"kind": e["kind"], "name": e["name"], "ref": e["ref"], "server": e.get("server")}
                )
        tools: list[Picked] = []
        for i, p in picked.items():
            d = defs.get(i)
            if not d:
                continue
            p.key, p.name, p.server, p.summary, p.tok = d["key"], d["name"], d["server"], d.get("summary") or "", d["tok"]
            p.definition = json.loads(d["defn"])
            tools.append(p)
        keys = {p.id: p.key for p in tools}
        res_keys = await self.resources() if any(p.role == "prereq" for p in tools) else {}
        for p in tools:
            for v in p.via:
                for f in ("for", "after"):
                    if f in v:
                        v[f] = keys.get(v[f], v[f])
                if "needs" in v:
                    v["needs"] = res_keys.get(v["needs"], v["needs"])
        ents = list(ctx_entities.values())
        tokens = sum(p.tok for p in tools) + sum(count_tokens(json.dumps(e)) for e in ents)
        return Context(
            task=task, tools=tools, entities=ents, tokens=tokens, queries=st.queries - q0,
            ms=(time.perf_counter() - t0) * 1000, terms=q, known=known, hits=dict(hits), ranked=ranked[:25],
            yielders=yielders, keys=keys, server_terms=dict(server_terms), entity_terms=dict(entity_terms),
        )
