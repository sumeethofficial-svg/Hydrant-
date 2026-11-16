"""Turn MCP tool definitions into graph rows.

Graph model
-----------
(Server)-[:PROVIDES]->(Tool)             a tool belongs to an MCP server
(Term)-[:MENTIONS {p, l, w}]->(Tool)     a word indicates a tool. p = prior from the definition,
                                         l = learned from outcomes, w = max(0, p + l)
(Term)-[:NAMES {p, c}]->(Server)         a word names a server. p = 1 when it is the server's own name,
                                         c = learned: times the word came with a tool of that server
(Term)-[:MEANS {c}]->(Term)              learned alias: "pr" means "pull_request", "ticket" means "issu"
(Tool)-[:NEEDS]->(Resource)              a required argument is the id of a resource ("channel_id")
(Resource)-[:YIELDED_BY {w}]->(Tool)     a tool that can look that resource up ("slack_list_channels")
(Tool)-[:THEN {w}]->(Tool)               learned: these tools are used together, in this order
(Term)-[:DENOTES {w}]->(Entity)          learned: a word refers to an entity the agent already resolved
(Resource)-[:HOLDS]->(Entity)            the entity is an instance of that resource
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

from .store import Store
from .text import count_tokens, nid, stems, words

K1, B = 1.2, 0.5
W_NAME, W_DESC, W_PARAM = 3.0, 1.0, 0.5
MAX_TERMS = 48
DESC_CHARS = 600
LOOKUP_VERBS = {"list", "search", "find", "query", "resolve", "get"}
ID_SUFFIX = {"id", "ids", "number", "ts", "slug"}
BARE_RESOURCES = {"repo": "repositori"}
CHUNK = 1000
META_ID = nid("meta", "stats")


def tool_key(server: str, name: str) -> str:
    return f"{server}/{name}"


def load_catalog(path: str | Path) -> dict[str, list[dict]]:
    """Read `<server>.json` files, each a list of MCP tool definitions as returned by `tools/list`."""
    out = {}
    for f in sorted(Path(path).glob("*.json")):
        out[f.stem] = json.loads(f.read_text())
    return out


def compact_def(tool: dict) -> str:
    """The definition an agent would be handed: name, description, input schema."""
    return json.dumps(
        {"name": tool["name"], "description": tool.get("description", ""), "inputSchema": tool.get("inputSchema", {})},
        separators=(",", ":"),
    )


def _bigrams(s: list[str]) -> list[str]:
    return [f"{a}_{b}" for a, b in zip(s, s[1:]) if a != b]


def tool_terms(server: str, tool: dict) -> dict[str, float]:
    """Field-weighted term frequencies for one tool."""
    tf: Counter[str] = Counter()
    name = stems(tool["name"])
    for t in name + _bigrams(name):
        tf[t] += W_NAME
    desc = (tool.get("description") or "")[:DESC_CHARS]
    first = stems(re.split(r"(?<=[.!?])\s|\n", desc, maxsplit=1)[0])
    for t in stems(desc) + _bigrams(first):
        tf[t] += W_DESC
    for p in (tool.get("inputSchema") or {}).get("properties", {}):
        for t in stems(p):
            tf[t] += W_PARAM
    return dict(tf)


def needed_resources(server: str, tool: dict) -> dict[str, str]:
    """Resources a tool cannot run without, keyed `server:resource`, mapped to a readable name ("channel")."""
    schema = tool.get("inputSchema") or {}
    out: dict[str, str] = {}
    for param in schema.get("required") or []:
        ws = words(param)
        if not ws:
            continue
        if param in BARE_RESOURCES:
            out.setdefault(f"{server}:{BARE_RESOURCES[param]}", "repository")
        elif len(ws) > 1 and ws[-1] in ID_SUFFIX:
            res = "_".join(stems(" ".join(ws[:-1])))
            if res:
                out.setdefault(f"{server}:{res}", " ".join(ws[:-1]))
    return out


def _yields(tool: dict, res: str) -> bool:
    raw = words(tool["name"])
    name = stems(tool["name"])
    return any(v in raw for v in LOOKUP_VERBS) and all(p in name for p in res.split("_"))


def build_rows(catalog: dict[str, list[dict]]) -> dict:
    """Compute every node and edge row for a catalog. Pure: no store access."""
    servers, tools, terms_df = [], [], Counter()
    mentions, provides, names, needs, yielded = [], [], [], [], []
    resources: dict[str, dict] = {}
    per_tool: dict[str, dict[str, float]] = {}
    lengths: dict[str, float] = {}

    for server, defs in catalog.items():
        for tool in defs:
            key = tool_key(server, tool["name"])
            tf = tool_terms(server, tool)
            per_tool[key] = tf
            lengths[key] = sum(tf.values())
    avg = sum(lengths.values()) / max(1, len(lengths))

    for server, defs in catalog.items():
        sid = nid("server", server)
        servers.append({"id": sid, "key": server, "ntools": len(defs)})
        for t in dict.fromkeys(stems(server)):
            names.append({"src": nid("term", t), "dst": sid, "term": t, "p": 1, "c": 0})
        need_by_tool = {t["name"]: needed_resources(server, t) for t in defs}
        for tool in defs:
            key = tool_key(server, tool["name"])
            tid = nid("tool", key)
            d = compact_def(tool)
            tools.append(
                {"id": tid, "key": key, "name": tool["name"], "server": server,
                 "summary": " ".join((tool.get("description") or "").split())[:160],
                 "defn": d, "tok": count_tokens(d), "uses": 0,
                 "nt": " ".join(dict.fromkeys(stems(tool["name"])))}
            )
            provides.append({"src": sid, "dst": tid})
            norm = K1 * (1 - B + B * lengths[key] / avg)
            scored = sorted(
                ((t, f * (K1 + 1) / (f + norm)) for t, f in per_tool[key].items()), key=lambda x: (-x[1], x[0])
            )[:MAX_TERMS]
            for t, p in scored:
                p = round(p, 4)
                mentions.append({"src": nid("term", t), "dst": tid, "p": p, "l": 0.0, "w": p, "term": t})
                terms_df[t] += 1
            for res, name in need_by_tool[tool["name"]].items():
                rid = nid("resource", res)
                resources.setdefault(res, {"id": rid, "key": res, "server": server, "kind": res.split(":", 1)[1], "name": name})
                needs.append({"src": tid, "dst": rid})
        for res in [r for r in resources if r.startswith(server + ":")]:
            kind = res.split(":", 1)[1]
            for tool in defs:
                if res in need_by_tool[tool["name"]] or not _yields(tool, kind):
                    continue
                verbs = words(tool["name"])
                w = 1.0 if any(v in verbs for v in ("list", "search", "find")) else 0.6
                yielded.append({"src": resources[res]["id"], "dst": nid("tool", tool_key(server, tool["name"])), "w": w})

    server_terms = {r["term"] for r in names}
    for t in server_terms:
        terms_df.setdefault(t, 0)
    term_rows = [
        {"id": nid("term", t), "key": t, "df": df, "srv": int(t in server_terms), "ent": 0, "al": 0, "seen": 0}
        for t, df in sorted(terms_df.items())
    ]
    return {
        "nodes": {"Server": servers, "Tool": tools, "Term": term_rows, "Resource": list(resources.values())},
        "edges": {
            ("PROVIDES", "Server", "Tool"): provides,
            ("MENTIONS", "Term", "Tool"): [{k: v for k, v in m.items() if k != "term"} for m in mentions],
            ("NAMES", "Term", "Server"): [{k: v for k, v in m.items() if k != "term"} for m in names],
            ("NEEDS", "Tool", "Resource"): needs,
            ("YIELDED_BY", "Resource", "Tool"): yielded,
        },
    }


async def ingest(store: Store, catalog: dict[str, list[dict]]) -> dict[str, int]:
    """Write a catalog into the graph. Idempotent: every write is an upsert on a stable id.

    Learned weights survive a re-ingest only if the store keeps them; call this on an empty graph or
    use `Learner.reset` first.
    """
    rows = build_rows(catalog)
    counts = {}
    for label, ns in rows["nodes"].items():
        for i in range(0, len(ns), CHUNK):
            await store.put_nodes(label, ns[i : i + CHUNK])
        counts[label] = len(ns)
    for (etype, sl, dl), es in rows["edges"].items():
        for i in range(0, len(es), CHUNK):
            await store.put_edges(etype, sl, dl, es[i : i + CHUNK])
        counts[etype] = len(es)
    # running totals live on one node, so the size of the graph is a point read, not a scan
    await store.put_nodes("Meta", [{
        "id": META_ID, "tools": counts["Tool"], "servers": counts["Server"], "terms": counts["Term"],
        "resources": counts["Resource"], "edges": sum(v for k, v in counts.items() if k.isupper()),
        "entities": 0, "workflows": 0, "outcomes": 0, "moved": 0, "words": 0, "aliases": 0,
    }])
    return counts


def idf(n_tools: int, df: float) -> float:
    return math.log(1 + (n_tools - df + 0.5) / (df + 0.5))
