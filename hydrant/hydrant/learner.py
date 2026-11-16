"""The learning loop: every finished task rewrites a little of the graph.

What an outcome changes
-----------------------
edge reweighting      Term-[:MENTIONS]->Tool weights move toward the tools that were actually used and away
                      from tools that were offered instead of them. Updates are error-driven: a tool that was
                      already ranked where it should be is left alone, so weights do not drift.
gap-driven ingestion  A word the graph had never seen, used in a task whose tool it missed, becomes a new
                      Term node wired to that tool.
path compression      Tools used one after another get a direct Tool-[:THEN]->Tool edge, so the two-hop
                      Tool->Resource->Tool walk (or no path at all) becomes one hop next time.
lookup pruning        Resource-[:YIELDED_BY]->Tool weights rise for lookups that were used and decay for
                      lookups that were offered and ignored, until they stop being offered.
alias learning        Words that keep arriving with tools named something else become Term-[:MEANS]->Term
                      aliases ("pr" means "pull_request"), and words that keep arriving with one server become
                      Term-[:NAMES]->Server edges ("pod" names kubernetes). One alias sharpens every tool
                      behind the term it points at, not just the tool it was learned on.
entity memory         An entity the agent resolved (a channel id, a page id) becomes an Entity node reachable
                      from its name, so the lookup round trip is skipped next time.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from collections import OrderedDict

from .ingest import META_ID
from .resolver import Context
from .store import Store
from .text import nid, terms


@dataclass
class Rates:
    miss: float = 1.0       # added to term->tool weight when the tool was needed and not offered
    rank: float = 0.5       # ... when it was offered but outranked by a tool that was not used
    neg: float = 0.25       # removed from term->tool weight for a wrongly offered tool
    l_max: float = 4.0      # bound on learned weight per edge
    max_fp: int = 3         # wrongly offered tools decayed per outcome
    y_up: float = 0.5
    y_down: float = 0.25
    y_max: float = 3.0
    alias_df_max: int = 80  # words more common than this are not alias candidates
    alias_target_df_max: int = 40  # a tool-name word shared by more tools than this is too generic to be an alias target


class Learner:
    def __init__(self, store: Store, rates: Rates | None = None, on_change=None) -> None:
        """@param on_change called with the term ids whose MENTIONS edges were rewritten (cache invalidation)"""
        self.store = store
        self.r = rates or Rates()
        self.on_change = on_change
        self._lock = asyncio.Lock()
        # a readable sample of recent learning, for display; the learning itself is in the graph
        self.recent: dict[str, OrderedDict] = {k: OrderedDict() for k in ("aliases", "words", "workflows", "entities")}

    def _note(self, kind: str, key, value: dict, keep: int = 40) -> None:
        log = self.recent[kind]
        log.pop(key, None)
        log[key] = value
        while len(log) > keep:
            log.popitem(last=False)

    async def feedback(self, ctx: Context, used: list[str], entities: list[dict] | None = None) -> dict:
        """Apply one task outcome.

        @param ctx      the Context that was served for the task
        @param used     tool keys the agent actually called, in call order
        @param entities entities the agent resolved: {server, kind, name, ref}
        @return a summary of what changed in the graph
        """
        async with self._lock:
            out = await self._apply(ctx, used, entities or [])
        if self.on_change and (out["reinforced"] or out["decayed"]):
            self.on_change(list(ctx.terms))
        return out

    async def _apply(self, ctx: Context, used: list[str], entities: list[dict]) -> dict:
        """Three phases: read every edge the update depends on (concurrently), compute, then write nodes and
        edges (concurrently). An outcome costs two commit latencies however many edges it touches."""
        st, r = self.store, self.r
        out = {"reinforced": [], "decayed": [], "new_terms": [], "workflows": [], "lookups": [], "entities": []}
        used_ids = [nid("tool", k) for k in dict.fromkeys(used)]
        info = await st.get("Tool", used_ids, ("key", "uses", "server", "nt"))
        used_ids = [i for i in used_ids if i in info]
        if not used_ids:
            return out
        used_set = set(used_ids)
        offered = {t.id: t for t in ctx.tools}
        pos = {t: i for i, (t, _) in enumerate(ctx.ranked)}
        seeds = [t.id for t in ctx.tools if t.role == "seed"]
        term_ids = list(ctx.terms)
        uni = [t for t in term_ids if "_" not in ctx.terms[t]]
        fresh = {t for t in term_ids if t not in ctx.known}
        known = {t: dict(ctx.known.get(t) or {"df": 0, "srv": 0, "ent": 0, "al": 0, "seen": 0}) for t in term_ids}
        cand = [t for t in uni if (known[t].get("df") or 0) <= r.alias_df_max]

        # which tools need their word edges moved: error-driven, a correctly ranked tool is left alone
        rates: dict[int, float] = {}
        worst, any_miss = -1, False
        for u in used_ids:
            if u not in offered:
                rates[u], any_miss = r.miss, True
                worst = max(worst, pos.get(u, 99))
            elif offered[u].role == "seed" and any(pos.get(f, 99) < pos.get(u, 99) for f in seeds if f not in used_set):
                rates[u] = r.rank
                worst = max(worst, pos.get(u, 99))
            else:
                worst = max(worst, pos.get(u, -1))
        wrong = [f for f in seeds if f not in used_set and (any_miss or pos.get(f, 99) < worst)][: r.max_fp]
        servers = list({nid("server", info[u]["server"]) for u in used_ids})
        name_terms = {u: (info[u].get("nt") or "").split() for u in used_ids}
        all_names = {nid("term", n) for ns in name_terms.values() for n in ns}
        chain = list(zip(used_ids, used_ids[1:]))

        # ---- read phase
        name_df, means, names, up_edges, down_edges, then = await asyncio.gather(
            st.get("Term", all_names, ("df",)),
            st.out("Term", cand, "MEANS", ("c",), ()),
            asyncio.gather(*(st.into("Term", cand, "NAMES", "Server", sid, ("c",)) for sid in servers)),
            asyncio.gather(*(st.into("Term", term_ids, "MENTIONS", "Tool", u, ("p", "l")) for u in rates)),
            asyncio.gather(*(st.into("Term", [t for t in ctx.hits if f in ctx.hits[t]], "MENTIONS", "Tool", f, ("p", "l")) for f in wrong)),
            asyncio.gather(*(st.into("Tool", [a], "THEN", "Tool", b, ("w",)) for a, b in chain)),
        )

        # ---- compute phase
        # alias learning: count which servers and which specific tool-name words each word arrives with
        specific = {i for i, v in name_df.items() if 0 < (v.get("df") or 0) <= r.alias_target_df_max}
        pairs = {(t, nid("term", n)) for u in used_ids for t in cand for n in name_terms[u] if ctx.terms[t] not in name_terms[u]}
        pairs = {(t, n) for t, n in pairs if n not in ctx.terms and n in specific}
        have_m = {(e["src"], e["dst"]): e.get("c") or 0 for e in means}
        means_rows = [{"src": t, "dst": n, "c": have_m.get((t, n), 0) + 1} for t, n in sorted(pairs)]
        tally = {"outcomes": 1, "moved": 0, "words": len(fresh), "aliases": 0, "workflows": 0,
                 "edges": sum(1 for k in pairs if k not in have_m)}
        names_by_id = {nid("term", n): n for ns in name_terms.values() for n in ns}
        names_rows = []
        for sid, rows in zip(servers, names):
            have_n = {e["src"]: e.get("c") or 0 for e in rows}
            names_rows += [{"src": t, "dst": sid, "c": have_n.get(t, 0) + 1} for t in cand]
            tally["edges"] += sum(1 for t in cand if t not in have_n)
        aliased = {t for t, _ in pairs}
        for t in uni:
            known[t]["seen"] = (known[t].get("seen") or 0) + 1
            if t in cand:
                known[t]["srv"] = 1
            if t in aliased:
                known[t]["al"] = 1
        for row in means_rows:
            seen = known[row["src"]].get("seen") or 1
            if row["c"] >= 2 and row["c"] / seen >= 0.5:
                tally["aliases"] += row["c"] == 2
                self._note("aliases", (row["src"], row["dst"]), {"word": ctx.terms[row["src"]], "means": names_by_id[row["dst"]],
                                                              "seen": row["c"], "share": round(row["c"] / seen, 2)})

        # word -> tool reweighting, and gap-driven ingestion of words the graph had never seen
        mention_rows: dict[tuple[int, int], dict] = {}
        touched: set[int] = set(uni)
        for u, rows in zip(rates, up_edges):
            have = {e["src"]: e for e in rows}
            for t in term_ids:
                e = have.get(t)
                p, l = (e.get("p") or 0.0, e.get("l") or 0.0) if e else (0.0, 0.0)
                l2 = min(r.l_max, l + rates[u])
                if l2 == l:
                    continue
                mention_rows[(t, u)] = {"src": t, "dst": u, "p": p, "l": round(l2, 4), "w": round(max(0.0, p + l2), 4)}
                if e is None:
                    known[t]["df"] = (known[t].get("df") or 0) + 1
                    touched.add(t)
                    tally["edges"] += 1
                    if "_" not in ctx.terms[t] and not p:
                        self._note("words", (t, u), {"word": ctx.terms[t], "tool": info[u]["key"]})
                    if t in fresh and ctx.terms[t] not in out["new_terms"]:
                        out["new_terms"].append(ctx.terms[t])
                out["reinforced"].append({"term": ctx.terms[t], "tool": info[u]["key"],
                                          "from": round(max(0.0, p + l), 3), "to": round(max(0.0, p + l2), 3)})
        # decay the words that pushed an unused tool above a used one
        for f, rows in zip(wrong, down_edges):
            for e in rows:
                p, l = e.get("p") or 0.0, e.get("l") or 0.0
                l2 = max(-p, l - r.neg)
                if l2 != l:
                    mention_rows[(e["src"], f)] = {"src": e["src"], "dst": f, "p": p, "l": round(l2, 4), "w": round(max(0.0, p + l2), 4)}
                    out["decayed"].append({"term": ctx.terms[e["src"]], "tool": offered[f].key,
                                           "from": round(max(0.0, p + l), 3), "to": round(max(0.0, p + l2), 3)})
        # path compression: consecutive tools get a direct THEN edge
        then_rows = []
        for (a, b), cur in zip(chain, then):
            w = (cur[0].get("w") or 0.0 if cur else 0.0) + 1.0
            then_rows.append({"src": a, "dst": b, "w": w})
            tally["workflows"] += not cur
            tally["edges"] += not cur
            self._note("workflows", (a, b), {"from": info[a]["key"], "to": info[b]["key"], "seen": w})
            out["workflows"].append({"from": info[a]["key"], "to": info[b]["key"], "seen": w})
        # lookup pruning: reward the lookups that were used, decay the ones offered and ignored
        seen_y: dict[tuple[int, int], float] = {}
        for y in ctx.yielders:
            if y["src"] not in used_set:
                continue
            w = y.get("w") or 0.0
            if y["dst"] in used_set:
                seen_y[(y["mid"], y["dst"])] = min(r.y_max, w + r.y_up)
            elif y["dst"] in offered and offered[y["dst"]].role == "prereq":
                seen_y[(y["mid"], y["dst"])] = max(0.0, w - r.y_down)
        out["lookups"] = [{"tool": ctx.keys.get(d, d), "weight": round(w, 3)} for (m, d), w in seen_y.items()]

        # ---- write phase: nodes first (edges match their endpoints), then every edge type at once
        term_rows = []
        for t in sorted(touched):
            k = known[t]
            row = {"id": t, "df": k.get("df") or 0, "seen": k.get("seen") or 0, "srv": k.get("srv") or 0, "al": k.get("al") or 0}
            if t in fresh:
                row.update({"key": ctx.terms[t], "ent": 0})
            term_rows.append(row)
            ctx.known[t] = {**k, "ent": k.get("ent") or 0}
        await asyncio.gather(
            st.put_nodes("Term", term_rows),
            st.put_nodes("Tool", [{"id": u, "uses": (info[u].get("uses") or 0) + 1} for u in used_ids]),
        )
        await asyncio.gather(
            st.put_edges("MENTIONS", "Term", "Tool", list(mention_rows.values())),
            st.put_edges("NAMES", "Term", "Server", names_rows),
            st.put_edges("MEANS", "Term", "Term", means_rows),
            st.put_edges("THEN", "Tool", "Tool", then_rows),
            st.put_edges("YIELDED_BY", "Resource", "Tool", [{"src": m, "dst": d, "w": round(w, 4)} for (m, d), w in seen_y.items()]),
        )
        tally["moved"] = len(mention_rows)
        tally["terms"] = len(fresh)
        for e in entities:
            out["entities"].append(await self.remember(e))
        await self._count(tally)
        return out

    async def _count(self, delta: dict) -> None:
        """Add to the running totals on the Meta node."""
        cur = (await self.store.get("Meta", [META_ID], tuple(delta))).get(META_ID)
        if cur is not None:
            await self.store.put_nodes("Meta", [{"id": META_ID, **{k: (cur.get(k) or 0) + int(v) for k, v in delta.items()}}])

    async def remember(self, e: dict) -> dict:
        """Store a resolved entity: {server, kind, name, ref}. `kind` is the resource it is an instance of."""
        st = self.store
        from .text import stems

        kind = "_".join(stems(e["kind"])) or e["kind"]
        res_key = f"{e['server']}:{kind}"
        rid = nid("resource", res_key)
        name_terms = list(dict.fromkeys(stems(e["name"]))) or [e["name"].lower()]
        key = f"{res_key}:{e['name'].lower()}"
        eid = nid("entity", key)
        existed = bool(await st.get("Entity", [eid], ("ref",)))
        await st.put_nodes("Resource", [{"id": rid, "key": res_key, "server": e["server"], "kind": kind}])  # name kept if set
        await st.put_nodes(
            "Entity",
            [{"id": eid, "key": key, "name": e["name"], "kind": kind, "ref": str(e["ref"]), "res": rid,
              "nterms": len(name_terms), "server": e["server"]}],
        )
        tids = {nid("term", t): t for t in name_terms}
        have = await st.get("Term", tids, ("df",))
        await st.put_nodes(
            "Term",
            [{"id": i, "ent": 1} if i in have else {"id": i, "key": t, "df": 0, "srv": 0, "ent": 1, "al": 0, "seen": 0}
             for i, t in tids.items()],
        )
        await st.put_edges("DENOTES", "Term", "Entity", [{"src": i, "dst": eid, "w": 1.0} for i in tids])
        await st.put_edges("HOLDS", "Resource", "Entity", [{"src": rid, "dst": eid}])
        if not existed:
            await self._count({"entities": 1, "edges": len(tids) + 1})
        self._note("entities", (e["server"], kind, e["name"].lower()), {"server": e["server"], "kind": kind, "name": e["name"], "ref": str(e["ref"])})
        return {"key": key, "ref": str(e["ref"])}


async def wipe(store: Store) -> int:
    """Remove every Hydrant node (and with them every edge)."""
    n = 0
    for label in ("Entity", "Term", "Tool", "Resource", "Server", "Meta"):
        ids = [r["id"] for r in await store.scan(label, ())]
        if ids:
            await store.drop(ids)
            n += len(ids)
    return n
