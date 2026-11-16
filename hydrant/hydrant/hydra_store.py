"""HydraDB implementation of the graph primitives, over Bolt.

Every statement here was run against a HydraDB 0.4.0 graph-node. The dialect points that shaped it:

- Node identity is an integer `id`; nodes are upserted with `UNWIND ... MERGE (n {id: row.id}) SET n:Label, ...`.
- A batched edge upsert needs an edge id: `MERGE (a)-[r:TYPE {id: row.eid}]->(b) SET r.x = row.x`.
- A relationship pattern is directed and names exactly one type, so each hop is a typed edge.
- `RETURN` takes `binding.property` and aggregates only; scoring happens in the resolver.
- Writes auto-commit; there are no transactions, so every write is an idempotent upsert.
- Batches are capped at 1024 items.
- An id-anchored pattern `(a:L {id: $id})-[r:T]->(b)` is a point lookup; `WHERE a.id IN $ids` on a pattern scans.
  So hops run as concurrent anchored matches, and hops that need no properties use the batched adjacency read.
"""

from __future__ import annotations

import asyncio
import re
from typing import Iterable

from neo4j import AsyncGraphDatabase

from .store import Row
from .text import nid

BATCH = 1000
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe(*names: str) -> None:
    for n in names:
        if not _NAME.match(n):
            raise ValueError(f"unsafe identifier {n!r}")


def edge_id(src: int, etype: str, dst: int) -> int:
    return nid("edge", f"{src}|{etype}|{dst}")


class HydraStore:
    name = "hydradb"

    def __init__(
        self, uri: str = "bolt://127.0.0.1:7687", token: str = "local-development-token-32-bytes",
        graph: str = "default", pool: int = 32,
    ) -> None:
        self.driver = AsyncGraphDatabase.driver(uri, auth=("neo4j", token), max_connection_pool_size=pool)
        self.graph = graph
        self.queries = 0

    async def run(self, cypher: str, **params) -> list[Row]:
        """Run one auto-commit statement and return its rows."""
        self.queries += 1
        last = None
        for attempt in range(4):
            try:
                async with self.driver.session(database=self.graph) as s:
                    res = await s.run(cypher, **params)
                    return [r.data() async for r in res]
            except Exception as e:  # transient snapshot/epoch conflicts are retried, everything else re-raised
                last = e
                msg = str(e).lower()
                if not any(k in msg for k in ("snapshot", "epoch", "transient", "conflict", "lock", "defunct")):
                    raise
                await asyncio.sleep(0.05 * (attempt + 1))
        raise last

    async def ping(self) -> bool:
        await self.driver.verify_connectivity()
        return True

    async def put_nodes(self, label: str, rows: list[Row]) -> None:
        _safe(label)
        groups: dict[tuple[str, ...], list[Row]] = {}
        for r in rows:
            clean = {k: v for k, v in r.items() if v is not None}
            groups.setdefault(tuple(sorted(k for k in clean if k != "id")), []).append(clean)
        for props, grp in groups.items():
            _safe(*props)
            sets = "".join(f", n.{p} = row.{p}" for p in props)
            for i in range(0, len(grp), BATCH):
                await self.run(
                    f"UNWIND $rows AS row MERGE (n {{id: row.id}}) SET n:{label}{sets}", rows=grp[i : i + BATCH]
                )

    async def put_edges(self, etype: str, src_label: str, dst_label: str, rows: list[Row]) -> None:
        _safe(etype, src_label, dst_label)
        groups: dict[tuple[str, ...], list[Row]] = {}
        for r in rows:
            clean = {k: v for k, v in r.items() if v is not None}
            clean["eid"] = edge_id(r["src"], etype, r["dst"])
            groups.setdefault(tuple(sorted(k for k in clean if k not in ("src", "dst", "eid"))), []).append(clean)
        for props, grp in groups.items():
            _safe(*props)
            sets = (" SET " + ", ".join(f"r.{p} = row.{p}" for p in props)) if props else ""
            q = (
                f"UNWIND $rows AS row MATCH (a:{src_label} {{id: row.src}}), (b:{dst_label} {{id: row.dst}}) "
                f"MERGE (a)-[r:{etype} {{id: row.eid}}]->(b){sets}"
            )
            for i in range(0, len(grp), BATCH):
                await self.run(q, rows=grp[i : i + BATCH])

    @staticmethod
    def _cols(eprops, nprops, edge="r", node="b") -> str:
        return "".join(f", {edge}.{p} AS {p}" for p in eprops) + "".join(f", {node}.{p} AS {p}" for p in nprops)

    async def out(self, label, srcs: Iterable[int], etype, eprops=(), nprops=()) -> list[Row]:
        """One hop out of each source. Each source is an id-anchored match, run concurrently: on HydraDB an
        anchored pattern is a point lookup, where `WHERE a.id IN [...]` scans the label."""
        _safe(label, etype, *eprops, *nprops)
        ids = list(dict.fromkeys(srcs))
        q = f"MATCH (a:{label} {{id: $id}})-[r:{etype}]->(b) RETURN b.id AS dst{self._cols(eprops, nprops)}"
        parts = await asyncio.gather(*(self.run(q, id=i) for i in ids))
        return [{"src": i, **r} for i, rows in zip(ids, parts) for r in rows]

    async def reach(self, srcs: Iterable[int], etype) -> list[Row]:
        """Topology only, many sources in one request: HydraDB's batched adjacency read, served from the
        traversal index. Rows of `src`, `dst`; no properties."""
        _safe(etype)
        ids = list(dict.fromkeys(srcs))
        out: list[Row] = []
        for i in range(0, len(ids), BATCH):
            out += await self.run(
                f"UNWIND $rows AS row MATCH (a {{id: row.id}})-[:{etype}]->(b) RETURN row.id AS src, b.id AS dst",
                rows=[{"id": x} for x in ids[i : i + BATCH]],
            )
        return out

    async def out2(self, label, srcs: Iterable[int], e1, e2, eprops2=(), nprops=()) -> list[Row]:
        """Two hops out of each source in one MATCH, id-anchored and concurrent."""
        _safe(label, e1, e2, *eprops2, *nprops)
        ids = list(dict.fromkeys(srcs))
        q = (f"MATCH (a:{label} {{id: $id}})-[:{e1}]->(m)-[r:{e2}]->(b) "
             f"RETURN m.id AS mid, b.id AS dst{self._cols(eprops2, nprops)}")
        parts = await asyncio.gather(*(self.run(q, id=i) for i in ids))
        return [{"src": i, **r} for i, rows in zip(ids, parts) for r in rows]

    async def into(self, label, srcs: Iterable[int], etype, dst_label, dst: int, eprops=()) -> list[Row]:
        _safe(label, etype, dst_label, *eprops)
        ids = list(srcs)
        if not ids:
            return []
        return await self.run(
            f"MATCH (a:{label})-[r:{etype}]->(b:{dst_label} {{id: $d}}) WHERE a.id IN $ids "
            f"RETURN a.id AS src{self._cols(eprops, ())}",
            ids=ids, d=dst,
        )

    async def get(self, label, ids: Iterable[int], props) -> dict[int, Row]:
        _safe(label, *props)
        ids = list(ids)
        if not ids:
            return {}
        cols = "".join(f", n.{p} AS {p}" for p in props)
        rows = await self.run(f"MATCH (n:{label}) WHERE n.id IN $ids RETURN n.id AS id{cols}", ids=ids)
        return {r.pop("id"): r for r in rows}

    async def scan(self, label, props) -> list[Row]:
        _safe(label, *props)
        cols = "".join(f", n.{p} AS {p}" for p in props)
        return await self.run(f"MATCH (n:{label}) RETURN n.id AS id{cols}")

    async def scan_edges(self, label, etype, eprops=()) -> list[Row]:
        _safe(label, etype, *eprops)
        return await self.run(
            f"MATCH (a:{label})-[r:{etype}]->(b) RETURN a.id AS src, b.id AS dst{self._cols(eprops, ())}"
        )

    async def drop_edges(self, etype, src_label, dst_label, pairs) -> None:
        _safe(etype, src_label, dst_label)
        for s, d in pairs:
            await self.run(f"MATCH (a:{src_label} {{id: $s}})-[r:{etype}]->(b:{dst_label} {{id: $d}}) DELETE r", s=s, d=d)

    async def paths(self, src: int, etypes: list[str], max_len: int = 2, count: int = 50) -> list[list[int]]:
        """Native multi-type traversal (algo.SSpaths): node-id paths out of `src` across several edge types."""
        _safe(*etypes)
        types = ", ".join(f"'{t}'" for t in etypes)
        rows = await self.run(
            f"CALL algo.SSpaths({{sourceNode: $s, relTypes: [{types}], relDirection: 'outgoing', "
            f"maxLen: $m, pathCount: $c}}) YIELD path RETURN path",
            s=src, m=max_len, c=count,
        )
        return rows

    async def drop(self, ids: list[int]) -> None:
        for i in range(0, len(ids), BATCH):
            await self.run("MATCH (n) WHERE n.id IN $ids DETACH DELETE n", ids=ids[i : i + BATCH])

    async def close(self) -> None:
        await self.driver.close()
