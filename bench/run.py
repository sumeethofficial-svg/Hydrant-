"""Benchmark: does the context layer get better with use, and does the graph earn its place?

Protocol
--------
- 351 hand-written tasks over 435 real MCP tool definitions (27 servers). Each task has gold tool slots.
- Tasks are split at random into a training stream (60%) and a held-out test set (40%).
- Round 0 evaluates the held-out set on the freshly ingested graph. Then the training stream is fed in
  equal batches: each task is resolved, an oracle agent reports which tools it used, and the graph learns.
  After every batch the held-out set is evaluated again. Evaluation never writes to the graph.
- Baselines, same held-out set:
    preload-all   every tool definition in the window (what agents do today)
    flat top-k    the same field-weighted BM25 scores served from a flat index: no edges, no learning

Metric that matters: complete-context rate, the share of tasks whose context holds every tool the task
needs. A context missing one needed tool fails the task, however small it is. Reported with its cost
(context tokens) and the oracle's tool calls per task.

Run:  python bench/run.py --store hydradb --seeds 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import asdict, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydrant.ingest import build_rows, idf, ingest, load_catalog  # noqa: E402
from hydrant.learner import Learner, wipe  # noqa: E402
from hydrant.resolver import Config, Resolver  # noqa: E402
from hydrant.store import MemoryStore  # noqa: E402
from hydrant.text import nid, terms  # noqa: E402


def load_tasks() -> list[dict]:
    return [json.loads(l) for l in (ROOT / "bench" / "tasks.jsonl").read_text().splitlines() if l.strip()]


def needed_slots(task: dict, entities: list[dict]) -> tuple[list[list[str]], bool]:
    """The slots a context must cover. The lookup slot is waived when the context carries the entity."""
    ent = task.get("entity")
    known = bool(ent) and any(e.get("ref") == ent["ref"] for e in entities)
    return [s for i, s in enumerate(task["slots"]) if not (known and i == task["lookup"])], known


def judge(task: dict, keys: list[str], entities: list[dict]) -> dict:
    slots, known = needed_slots(task, entities)
    have = set(keys)
    covered = [any(k in have for k in s) for s in slots]
    return {"complete": all(covered), "recall": sum(covered) / len(covered), "calls": len(slots), "entity": known}


def oracle_used(task: dict, keys: list[str], entities: list[dict]) -> list[str]:
    """The tools a correct agent calls: for each needed slot, an offered tool if there is one, else the canonical one."""
    slots, _ = needed_slots(task, entities)
    return [next((k for k in keys if k in s), s[0]) for s in slots]


class Flat:
    """Flat top-k baseline: the identical BM25F term weights in a plain inverted index."""

    def __init__(self, catalog: dict) -> None:
        rows = build_rows(catalog)
        self.tools = {t["id"]: t for t in rows["nodes"]["Tool"]}
        self.df = {t["id"]: t["df"] for t in rows["nodes"]["Term"]}
        self.post: dict[int, list[tuple[int, float]]] = defaultdict(list)
        for e in rows["edges"][("MENTIONS", "Term", "Tool")]:
            self.post[e["src"]].append((e["dst"], e["p"]))
        self.n = len(self.tools)
        self.total_tokens = sum(t["tok"] for t in self.tools.values())

    def top(self, text: str, k: int) -> tuple[list[str], int]:
        score: dict[int, float] = defaultdict(float)
        for t in terms(text):
            i = nid("term", t)
            for tool, p in self.post.get(i, ()):
                score[tool] += idf(self.n, self.df[i]) * p
        best = sorted(score.items(), key=lambda x: (-x[1], x[0]))[:k]
        return [self.tools[i]["key"] for i, _ in best], sum(self.tools[i]["tok"] for i, _ in best)


def summarise(rows: list[dict]) -> dict:
    lat = sorted(r["ms"] for r in rows) if rows and "ms" in rows[0] else []
    out = {
        "n": len(rows),
        "complete": round(100 * statistics.mean(r["complete"] for r in rows), 2),
        "recall": round(100 * statistics.mean(r["recall"] for r in rows), 2),
        "tools": round(statistics.mean(r["tools"] for r in rows), 2),
        "tokens": round(statistics.mean(r["tokens"] for r in rows), 1),
        "calls": round(statistics.mean(r["calls"] for r in rows), 3),
    }
    multi = [r for r in rows if r["multi"]]
    if multi:
        out["complete_multi"] = round(100 * statistics.mean(r["complete"] for r in multi), 2)
    single = [r for r in rows if not r["multi"]]
    if single:
        out["complete_single"] = round(100 * statistics.mean(r["complete"] for r in single), 2)
    if lat:
        out["p50_ms"] = round(lat[len(lat) // 2], 2)
        out["p95_ms"] = round(lat[min(len(lat) - 1, int(len(lat) * 0.95))], 2)
        out["queries"] = round(statistics.mean(r["queries"] for r in rows), 2)
    return out


async def evaluate(resolver: Resolver, tasks: list[dict], cfg: Config | None = None) -> dict:
    rows = []
    for t in tasks:
        ctx = await resolver.resolve(t["text"], cfg)
        j = judge(t, [p.key for p in ctx.tools], ctx.entities)
        rows.append({**j, "tools": len(ctx.tools), "tokens": ctx.tokens, "ms": ctx.ms, "queries": ctx.queries,
                     "multi": len(t["slots"]) > 1})
    return summarise(rows)


def evaluate_flat(flat: Flat, tasks: list[dict], k: int) -> dict:
    rows = []
    for t in tasks:
        keys, tok = flat.top(t["text"], k)
        rows.append({**judge(t, keys, []), "tools": len(keys), "tokens": tok, "multi": len(t["slots"]) > 1})
    return summarise(rows)


async def make_store(kind: str):
    if kind == "memory":
        return MemoryStore()
    from hydrant.hydra_store import HydraStore
    import os

    return HydraStore(os.environ.get("HYDRADB_URI", "bolt://127.0.0.1:7687"),
                      os.environ.get("HYDRADB_TOKEN", "local-development-token-32-bytes"))


async def run_seed(kind: str, seed: int, rounds: int, test_frac: float, cfg: Config, verbose: bool) -> dict:
    catalog = load_catalog(ROOT / "catalog")
    tasks = load_tasks()
    rng = random.Random(seed)
    rng.shuffle(tasks)
    n_test = round(len(tasks) * test_frac)
    test, train = tasks[:n_test], tasks[n_test:]
    store = await make_store(kind)
    await wipe(store)
    await ingest(store, catalog)
    resolver = Resolver(store, cfg)
    learner = Learner(store, on_change=resolver.forget)
    flat = Flat(catalog)

    curve = [{"round": 0, "seen": 0, **(await evaluate(resolver, test))}]
    if verbose:
        print(f"  seed {seed} round 0: {curve[-1]}", flush=True)
    size = -(-len(train) // rounds)
    online = []
    for r in range(rounds):
        batch = train[r * size : (r + 1) * size]
        for t in batch:
            ctx = await resolver.resolve(t["text"])
            keys = [p.key for p in ctx.tools]
            online.append(judge(t, keys, ctx.entities)["complete"])
            used = oracle_used(t, keys, ctx.entities)
            ent = [t["entity"]] if t.get("entity") and not any(e["ref"] == t["entity"]["ref"] for e in ctx.entities) else []
            await learner.feedback(ctx, used, ent)
        resolver._n = None
        seen = min(len(train), (r + 1) * size)
        curve.append({"round": r + 1, "seen": seen, **(await evaluate(resolver, test))})
        if verbose:
            print(f"  seed {seed} round {r + 1}: {curve[-1]}", flush=True)

    final = {
        "full": curve[-1],
        "no_traversal": await evaluate(resolver, test, replace(cfg, expand=False, entities=False, aliases=False)),
        "no_requirement_workflow_hops": await evaluate(resolver, test, replace(cfg, expand=False)),
        "no_alias_hops": await evaluate(resolver, test, replace(cfg, aliases=False)),
        "no_entity_memory": await evaluate(resolver, test, replace(cfg, entities=False)),
    }
    baselines = {
        "preload_all": {"n": len(test), "complete": 100.0, "recall": 100.0, "tools": flat.n,
                        "tokens": flat.total_tokens, "calls": round(statistics.mean(len(t["slots"]) for t in test), 3)},
        **{f"flat_top{k}": evaluate_flat(flat, test, k) for k in (5, 8, 10, 20)},
    }
    stats = {
        "terms": len(await store.scan("Term", ())), "entities": len(await store.scan("Entity", ())),
        "workflows": len(await store.scan_edges("Tool", "THEN", ())),
        "mentions": len(await store.scan_edges("Term", "MENTIONS", ())),
    }
    await store.close()
    return {"seed": seed, "train": len(train), "test": len(test), "curve": curve, "final": final,
            "baselines": baselines, "graph": stats,
            "online_complete_first_half": round(100 * statistics.mean(online[: len(online) // 2]), 2),
            "online_complete_second_half": round(100 * statistics.mean(online[len(online) // 2 :]), 2)}


def aggregate(runs: list[dict]) -> dict:
    def ms(vals):
        return {"mean": round(statistics.mean(vals), 2), "sd": round(statistics.pstdev(vals), 2)}

    rounds = len(runs[0]["curve"])
    keys = [k for k in runs[0]["curve"][0] if k not in ("round", "seen", "n")]
    curve = []
    for i in range(rounds):
        curve.append({"round": i, "seen": runs[0]["curve"][i]["seen"],
                      **{k: ms([r["curve"][i][k] for r in runs if k in r["curve"][i]]) for k in keys}})
    def agg(section, name):
        ks = [k for k in runs[0][section][name] if k != "n"]
        return {k: ms([r[section][name][k] for r in runs]) for k in ks}

    return {
        "curve": curve,
        "final": {n: agg("final", n) for n in runs[0]["final"]},
        "baselines": {n: agg("baselines", n) for n in runs[0]["baselines"]},
        "graph": {k: ms([r["graph"][k] for r in runs]) for k in runs[0]["graph"]},
        "online": {"first_half": ms([r["online_complete_first_half"] for r in runs]),
                   "second_half": ms([r["online_complete_second_half"] for r in runs])},
    }


def table(a: dict) -> str:
    def c(d, k, unit=""):
        if k not in d:
            return "-"
        if k == "tokens":
            return f"{round(d[k]['mean']):,}"
        if k in ("tools", "calls"):
            return f"{d[k]['mean']:.2f}" if k == "calls" else f"{d[k]['mean']:.1f}"
        return f"{d[k]['mean']:.1f}{unit} ± {d[k]['sd']:.1f}"

    lines = ["| system | complete context | multi-tool tasks | tools | context tokens | tool calls |", "|---|---|---|---|---|---|"]
    b, f, cv = a["baselines"], a["final"], a["curve"]
    rows = [
        ("preload-all (today)", b["preload_all"]),
        ("flat top-5", b["flat_top5"]), ("flat top-8", b["flat_top8"]), ("flat top-10", b["flat_top10"]),
        ("flat top-20", b["flat_top20"]),
        ("Hydrant, round 0 (no learning)", cv[0]),
        (f"Hydrant, round {cv[-1]['round']} ({cv[-1]['seen']} tasks seen)", cv[-1]),
        ("  same learned graph, read without traversal (term weights only)", f["no_traversal"]),
        ("  ... without requirement and workflow hops", f["no_requirement_workflow_hops"]),
        ("  ... without alias hops", f["no_alias_hops"]),
        ("  ... without entity memory", f["no_entity_memory"]),
    ]
    for name, d in rows:
        lines.append(f"| {name} | {c(d, 'complete', '%')} | {c(d, 'complete_multi', '%')} | {c(d, 'tools')} | {c(d, 'tokens')} | {c(d, 'calls')} |")
    return "\n".join(lines)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="hydradb", choices=["hydradb", "memory"])
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--rounds", type=int, default=8)
    ap.add_argument("--test-frac", type=float, default=0.4)
    ap.add_argument("--out", default=str(ROOT / "bench" / "results.json"))
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()
    cfg = Config()
    t0 = time.time()
    runs = []
    for s in range(1, args.seeds + 1):
        runs.append(await run_seed(args.store, s, args.rounds, args.test_frac, cfg, not args.quiet))
    agg = aggregate(runs)
    out = {"store": args.store, "seeds": args.seeds, "rounds": args.rounds, "config": asdict(cfg),
           "tasks": len(load_tasks()), "aggregate": agg, "runs": runs, "seconds": round(time.time() - t0, 1)}
    Path(args.out).write_text(json.dumps(out, indent=1))
    print("\n" + table(agg))
    print(f"\ncurve (complete %): " + "  ".join(f"r{c['round']}={c['complete']['mean']}" for c in agg["curve"]))
    print(f"online complete, first half vs second half of the stream: {agg['online']['first_half']['mean']}% -> {agg['online']['second_half']['mean']}%")
    print(f"wrote {args.out} in {out['seconds']}s")


if __name__ == "__main__":
    asyncio.run(main())
