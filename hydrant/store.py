"""The graph primitives Hydrant needs.

`HydraStore` (hydra_store.py) runs them on HydraDB and is what production and the benchmark use.
`MemoryStore` here exists for unit tests only; it mirrors the same semantics in plain dicts.
The resolver and the learner call these primitives and never see a query language.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Protocol

Row = dict[str, Any]


class Store(Protocol):
    name: str
    queries: int

    async def put_nodes(self, label: str, rows: list[Row]) -> None:
        """Upsert nodes by integer `id`: set the label and the scalar properties given."""

    async def put_edges(self, etype: str, src_label: str, dst_label: str, rows: list[Row]) -> None:
        """Upsert edges on (src, type, dst) and set the properties given. Rows carry `src`, `dst`, props."""

    async def out(
        self, label: str, srcs: Iterable[int], etype: str, eprops: tuple[str, ...] = (),
        nprops: tuple[str, ...] = (),
    ) -> list[Row]:
        """One hop out of many sources at once: rows of `src`, `dst`, edge props, target props."""

    async def out2(
        self, label: str, srcs: Iterable[int], e1: str, e2: str, eprops2: tuple[str, ...] = (),
        nprops: tuple[str, ...] = (),
    ) -> list[Row]:
        """Two hops in one traversal: rows of `src`, `mid`, `dst`, the second edge's props, target props."""

    async def reach(self, srcs: Iterable[int], etype: str) -> list[Row]:
        """Topology only, many sources in one request: rows of `src`, `dst`."""

    async def into(
        self, label: str, srcs: Iterable[int], etype: str, dst_label: str, dst: int, eprops: tuple[str, ...] = (),
    ) -> list[Row]:
        """The edges of one type from many sources into one target: rows of `src` and the edge props."""

    async def get(self, label: str, ids: Iterable[int], props: tuple[str, ...]) -> dict[int, Row]:
        """Point reads by id."""

    async def scan(self, label: str, props: tuple[str, ...]) -> list[Row]:
        """Every node of a label."""

    async def scan_edges(self, label: str, etype: str, eprops: tuple[str, ...] = ()) -> list[Row]:
        """Every edge of one type leaving nodes of a label."""

    async def drop_edges(self, etype: str, src_label: str, dst_label: str, pairs: list[tuple[int, int]]) -> None:
        """Delete edges by (src, dst)."""

    async def drop(self, ids: list[int]) -> None:
        """Detach-delete nodes by id."""

    async def close(self) -> None: ...


class MemoryStore:
    name = "memory"

    def __init__(self) -> None:
        self.nodes: dict[int, Row] = {}
        self.labels: dict[str, set[int]] = defaultdict(set)
        self.edges: dict[tuple[int, str], dict[int, Row]] = defaultdict(dict)
        self.queries = 0

    async def put_nodes(self, label, rows):
        self.queries += 1
        for r in rows:
            self.nodes.setdefault(r["id"], {}).update({k: v for k, v in r.items() if k != "id" and v is not None})
            self.labels[label].add(r["id"])

    async def put_edges(self, etype, src_label, dst_label, rows):
        self.queries += 1
        for r in rows:
            if r["src"] not in self.nodes or r["dst"] not in self.nodes:
                continue
            e = self.edges[(r["src"], etype)].setdefault(r["dst"], {})
            e.update({k: v for k, v in r.items() if k not in ("src", "dst") and v is not None})

    def _n(self, i, props):
        return {p: self.nodes.get(i, {}).get(p) for p in props}

    async def out(self, label, srcs, etype, eprops=(), nprops=()):
        self.queries += 1
        return [
            {"src": s, "dst": d, **{p: e.get(p) for p in eprops}, **self._n(d, nprops)}
            for s in srcs
            if s in self.labels[label]
            for d, e in self.edges.get((s, etype), {}).items()
        ]

    async def out2(self, label, srcs, e1, e2, eprops2=(), nprops=()):
        self.queries += 1
        return [
            {"src": s, "mid": m, "dst": d, **{p: e.get(p) for p in eprops2}, **self._n(d, nprops)}
            for s in srcs
            if s in self.labels[label]
            for m in self.edges.get((s, e1), {})
            for d, e in self.edges.get((m, e2), {}).items()
        ]

    async def reach(self, srcs, etype):
        self.queries += 1
        return [{"src": s, "dst": d} for s in srcs for d in self.edges.get((s, etype), {})]

    async def into(self, label, srcs, etype, dst_label, dst, eprops=()):
        self.queries += 1
        out = []
        for s in srcs:
            e = self.edges.get((s, etype), {}).get(dst)
            if e is not None and s in self.labels[label]:
                out.append({"src": s, **{p: e.get(p) for p in eprops}})
        return out

    async def get(self, label, ids, props):
        self.queries += 1
        return {i: self._n(i, props) for i in ids if i in self.nodes and i in self.labels[label]}

    async def scan(self, label, props):
        self.queries += 1
        return [{"id": i, **self._n(i, props)} for i in sorted(self.labels[label])]

    async def scan_edges(self, label, etype, eprops=()):
        self.queries += 1
        return [
            {"src": s, "dst": d, **{p: e.get(p) for p in eprops}}
            for s in sorted(self.labels[label])
            for d, e in self.edges.get((s, etype), {}).items()
        ]

    async def drop_edges(self, etype, src_label, dst_label, pairs):
        self.queries += 1
        for s, d in pairs:
            self.edges.get((s, etype), {}).pop(d, None)

    async def drop(self, ids):
        self.queries += 1
        gone = set(ids)
        for i in gone:
            self.nodes.pop(i, None)
        for s in self.labels.values():
            s -= gone
        for key in [k for k in self.edges if k[0] in gone]:
            del self.edges[key]
        for m in self.edges.values():
            for d in [d for d in m if d in gone]:
                del m[d]

    async def close(self) -> None:
        return None
