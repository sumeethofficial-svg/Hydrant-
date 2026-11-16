"""Load test against HydraDB: concurrent agents resolving tasks while outcomes are written back.

Each simulated agent loops: resolve a task; one in five also reports an outcome (a write that reweights
edges). Levels of concurrency run for a fixed time each. A second part scales the catalog by cloning every
server (435, 2175, 4350 tools) and measures resolve latency at each size.

Run:  python bench/load.py            (needs a HydraDB node; wipes and re-ingests the graph)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bench"))

from hydrant.hydra_store import HydraStore  # noqa: E402
from hydrant.ingest import ingest, load_catalog  # noqa: E402
from hydrant.learner import Learner, wipe  # noqa: E402
from hydrant.resolver import Resolver  # noqa: E402
from run import load_tasks, oracle_used  # noqa: E402


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(len(xs) * p))], 1) if xs else 0.0


async def level(resolver, learner, tasks, concurrency: int, seconds: float, write_share: float) -> dict:
    lat, errors, writes = [], 0, 0
    stop = time.perf_counter() + seconds

    async def agent(i: int) -> None:
        nonlocal errors, writes
        rng = random.Random(i)
        while time.perf_counter() < stop:
            t = rng.choice(tasks)
            t0 = time.perf_counter()
            try:
                ctx = await resolver.resolve(t["text"])
                lat.append((time.perf_counter() - t0) * 1000)
                if rng.random() < write_share:
                    keys = [p.key for p in ctx.tools]
                    await learner.feedback(ctx, oracle_used(t, keys, ctx.entities))
                    writes += 1
            except Exception:
                errors += 1

    t0 = time.perf_counter()
    await asyncio.gather(*(agent(i) for i in range(concurrency)))
    took = time.perf_counter() - t0
    return {"concurrency": concurrency, "resolves": len(lat), "rps": round(len(lat) / took, 1), "writes": writes,
            "p50_ms": pct(lat, 0.5), "p95_ms": pct(lat, 0.95), "p99_ms": pct(lat, 0.99), "errors": errors}


def scaled(catalog: dict, k: int) -> dict:
    out = dict(catalog)
    for i in range(2, k + 1):
        for server, defs in catalog.items():
            out[f"{server}-r{i}"] = defs
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--levels", default="1,8,32,64")
    ap.add_argument("--scales", default="1,5,10")
    ap.add_argument("--out", default=str(ROOT / "bench" / "results" / "load.json"))
    args = ap.parse_args()
    uri = os.environ.get("HYDRADB_URI", "bolt://127.0.0.1:7687")
    token = os.environ.get("HYDRADB_TOKEN", "local-development-token-32-bytes")
    catalog, tasks = load_catalog(ROOT / "catalog"), load_tasks()
    levels = [int(x) for x in args.levels.split(",")]
    store = HydraStore(uri, token, pool=max(levels) + 8)
    out = {"machine": f"{os.cpu_count()} cores, client and HydraDB node on the same host", "seconds_per_level": args.seconds,
           "write_share": 0.2, "levels": [], "scale": []}

    await wipe(store)
    await ingest(store, catalog)
    resolver = Resolver(store)
    learner = Learner(store, on_change=resolver.forget)
    await level(resolver, learner, tasks, 4, 3, 0.0)
    for c in levels:
        r = await level(resolver, learner, tasks, c, args.seconds, 0.2)
        out["levels"].append(r)
        print(r, flush=True)
    # the same mix with the word-edge cache off: every hop of every resolve is read from HydraDB
    from hydrant.resolver import Config
    cold = Resolver(store, Config(cache_ttl=0))
    out["uncached"] = await level(cold, Learner(store), tasks, 8, args.seconds, 0.2)
    print("uncached", out["uncached"], flush=True)

    for k in [int(x) for x in args.scales.split(",")]:
        await wipe(store)
        t0 = time.perf_counter()
        counts = await ingest(store, scaled(catalog, k))
        ingest_s = round(time.perf_counter() - t0, 1)
        resolver = Resolver(store)
        learner = Learner(store, on_change=resolver.forget)
        await level(resolver, learner, tasks, 4, 3, 0.0)
        r = await level(resolver, learner, tasks, 8, args.seconds / 2, 0.0)
        row = {"tools": counts["Tool"], "edges": sum(v for n, v in counts.items() if n.isupper()), "ingest_s": ingest_s,
               "rps": r["rps"], "p50_ms": r["p50_ms"], "p95_ms": r["p95_ms"], "errors": r["errors"]}
        out["scale"].append(row)
        print(row, flush=True)

    await wipe(store)
    await store.close()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=1))
    print("wrote", args.out)


if __name__ == "__main__":
    asyncio.run(main())
