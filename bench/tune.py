"""Settings were chosen here, on the usage stream only. The held-out set is never read by this script.

For each candidate setting the usage stream of a split is cut in half: the graph learns from the first half
and is scored on the second. The defaults in hydrant/resolver.py and hydrant/learner.py are the winners.
Run:  python bench/tune.py
"""

from __future__ import annotations

import asyncio
import random
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bench"))

from hydrant.ingest import ingest, load_catalog  # noqa: E402
from hydrant.learner import Learner, Rates  # noqa: E402
from hydrant.resolver import Config, Resolver  # noqa: E402
from hydrant.store import MemoryStore  # noqa: E402
from run import evaluate, load_tasks, oracle_used  # noqa: E402

CATALOG = load_catalog(ROOT / "catalog")


async def score(cfg: Config, rates: Rates, seed: int) -> dict:
    tasks = load_tasks()
    random.Random(seed).shuffle(tasks)
    stream = tasks[round(len(tasks) * 0.4):]
    fit, val = stream[: len(stream) // 2], stream[len(stream) // 2:]
    store = MemoryStore()
    await ingest(store, CATALOG)
    resolver = Resolver(store, cfg)
    learner = Learner(store, rates, on_change=resolver.forget)
    for t in fit:
        ctx = await resolver.resolve(t["text"])
        keys = [p.key for p in ctx.tools]
        ent = [t["entity"]] if t.get("entity") and not any(e["ref"] == t["entity"]["ref"] for e in ctx.entities) else []
        await learner.feedback(ctx, oracle_used(t, keys, ctx.entities), ent)
    return await evaluate(resolver, val)


async def main() -> None:
    grid = [
        ("defaults", {}, {}),
        ("k_seed 5, tau 0.5", dict(k_seed=5, tau=0.5, k_max=9), {}),
        ("k_seed 10, tau 0.3", dict(k_seed=10, tau=0.3, k_max=14), {}),
        ("workflow needs 2 sightings", dict(then_min=2.0, expand_top=3), {}),
        ("alias needs 3 sightings", dict(alias_min=3.0), {}),
        ("no aliases", dict(aliases=False), {}),
        ("decay 0.1", {}, dict(neg=0.1)),
        ("miss rate 2.0", {}, dict(miss=2.0, rank=1.0, l_max=6.0)),
    ]
    for name, c, r in grid:
        res = [await score(Config(**c), Rates(**r), s) for s in (11, 12, 13)]
        print(f"{name:28s} complete {statistics.mean(x['complete'] for x in res):5.1f}%  "
              f"multi-tool {statistics.mean(x['complete_multi'] for x in res):5.1f}%  "
              f"tokens {statistics.mean(x['tokens'] for x in res):6.0f}")


if __name__ == "__main__":
    asyncio.run(main())
