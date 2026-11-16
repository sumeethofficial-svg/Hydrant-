"""HTTP API, MCP endpoint and demo UI for Hydrant.

Run:  uvicorn hydrant.api:app --port 8000
Env:  HYDRADB_URI (bolt://127.0.0.1:7687), HYDRADB_TOKEN, HYDRADB_GRAPH (default),
      HYDRANT_CATALOG (./catalog), HYDRANT_ADMIN_TOKEN (optional, guards reset),
      HYDRANT_UPSTREAMS (optional path to upstreams.json for call_tool proxying)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import statistics
import time
import uuid
from collections import OrderedDict
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .hydra_store import HydraStore
from .ingest import META_ID, ingest, load_catalog
from .learner import Learner, wipe
from .resolver import Context, Resolver
from .upstream import Upstreams

ROOT = Path(__file__).resolve().parent.parent
CATALOG = Path(os.environ.get("HYDRANT_CATALOG", ROOT / "catalog"))


class State:
    store: HydraStore
    resolver: Resolver
    learner: Learner
    upstreams: Upstreams
    contexts: "OrderedDict[str, dict]"
    tools: list[dict]
    preload_tokens: int
    train: list[dict]
    test: list[dict]
    seen: int
    live: list[dict]
    busy: asyncio.Lock


S = State()


def _load_tasks() -> tuple[list[dict], list[dict]]:
    """The benchmark's seed-1 split: a training stream to replay as usage and a held-out set to evaluate on."""
    path = ROOT / "bench" / "tasks.jsonl"
    if not path.exists():
        return [], []
    tasks = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    random.Random(1).shuffle(tasks)
    n = round(len(tasks) * 0.4)
    return tasks[n:], tasks[:n]


async def _refresh_tools() -> None:
    S.tools = sorted(await S.store.scan("Tool", ("key", "server", "name", "summary", "tok", "uses")), key=lambda t: t["key"])
    S.preload_tokens = sum(t["tok"] or 0 for t in S.tools)
    S.resolver._n = None
    S.resolver._res = None
    S.resolver._post.clear()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    S.store = HydraStore(
        os.environ.get("HYDRADB_URI", "bolt://127.0.0.1:7687"),
        os.environ.get("HYDRADB_TOKEN", "local-development-token-32-bytes"),
        os.environ.get("HYDRADB_GRAPH", "default"),
    )
    S.resolver = Resolver(S.store)
    S.learner = Learner(S.store, on_change=S.resolver.forget)
    S.contexts, S.live, S.seen, S.busy = OrderedDict(), [], 0, asyncio.Lock()
    S.train, S.test = _load_tasks()
    S.upstreams = Upstreams(os.environ.get("HYDRANT_UPSTREAMS"))
    for attempt in range(60):
        try:
            await S.store.ping()
            break
        except Exception:
            await asyncio.sleep(1)
    await _refresh_tools()
    if not S.tools:
        await ingest(S.store, load_catalog(CATALOG))
        await _refresh_tools()
    flusher = asyncio.create_task(_flush_idle())
    from .mcp_server import mcp

    async with mcp.session_manager.run():
        yield
    flusher.cancel()
    await S.upstreams.close()
    await S.store.close()


app = FastAPI(title="Hydrant", lifespan=lifespan)


# ---------------------------------------------------------------- core operations (shared with MCP)

def _remember(ctx: Context) -> str:
    cid = uuid.uuid4().hex[:12]
    S.contexts[cid] = {"ctx": ctx, "at": time.time(), "calls": [], "reported": False}
    while len(S.contexts) > 2000:
        S.contexts.popitem(last=False)
    return cid


async def do_resolve(task: str) -> tuple[str, Context]:
    task = (task or "").strip()
    if not task:
        raise HTTPException(400, "task is empty: describe what the agent needs to do")
    if len(task) > 2000:
        raise HTTPException(400, "task is longer than 2000 characters")
    ctx = await S.resolver.resolve(task)
    return _remember(ctx), ctx


async def do_feedback(cid: str, used: list[str], entities: list[dict] | None = None) -> dict:
    entry = S.contexts.get(cid)
    if entry is None:
        raise HTTPException(404, f"context {cid} is unknown or expired: resolve the task again")
    for e in entities or []:
        if not all(e.get(k) for k in ("server", "kind", "name", "ref")):
            raise HTTPException(400, "each entity needs server, kind, name and ref")
    diff = await S.learner.feedback(entry["ctx"], used, entities)
    entry["reported"] = True
    return diff


async def _flush_idle() -> None:
    """Contexts whose tools were called through the proxy report themselves once they go quiet."""
    while True:
        await asyncio.sleep(15)
        now = time.time()
        for cid, e in list(S.contexts.items()):
            if e["calls"] and not e["reported"] and now - e["at"] > 45:
                with contextlib.suppress(Exception):
                    await do_feedback(cid, e["calls"])


# ---------------------------------------------------------------- HTTP

class ResolveIn(BaseModel):
    task: str


class FeedbackIn(BaseModel):
    context_id: str
    used: list[str]
    entities: list[dict] = []


class ReplayIn(BaseModel):
    n: int = 50


@app.get("/api/health")
async def health():
    return {"ok": True, "store": S.store.name, "tools": len(S.tools)}


@app.post("/api/resolve")
async def resolve(body: ResolveIn):
    cid, ctx = await do_resolve(body.task)
    out = ctx.to_dict()
    out["context_id"] = cid
    out["trace"] = ctx.trace()
    out["compare"] = {"preload_tokens": S.preload_tokens, "preload_tools": len(S.tools)}
    return out


@app.post("/api/feedback")
async def feedback(body: FeedbackIn):
    return await do_feedback(body.context_id, body.used, body.entities)


@app.get("/api/tools")
async def tools(q: str = "", limit: int = 20):
    ql = q.lower().strip()
    hits = [t for t in S.tools if ql in t["key"].lower() or ql in (t["summary"] or "").lower()] if ql else S.tools
    return {"total": len(S.tools), "tools": [{k: t[k] for k in ("key", "server", "name", "summary", "tok")} for t in hits[:limit]]}


@app.get("/api/stats")
async def stats():
    """Graph size and learning totals: one point read of the Meta node the ingest and the learner maintain."""
    m = (await S.store.get("Meta", [META_ID], ("tools", "servers", "terms", "resources", "edges", "entities",
                                                "workflows", "outcomes", "moved", "words", "aliases"))).get(META_ID) or {}
    g = lambda k: m.get(k) or 0
    return {
        "nodes": {"Tool": g("tools") or len(S.tools), "Server": g("servers"), "Term": g("terms"), "Resource": g("resources"), "Entity": g("entities")},
        "edges": g("edges"),
        "learned": {"outcomes": g("outcomes"), "reweighted_edges": g("moved"), "new_words": g("words"), "aliases": g("aliases"),
                    "workflows": g("workflows"), "entities": g("entities")},
        "preload_tokens": S.preload_tokens, "usage_replayed": S.seen, "usage_total": len(S.train),
    }


@app.get("/api/learned")
async def learned(limit: int = 8):
    """A readable sample of what this process has watched the graph learn, newest first."""
    r = S.learner.recent
    newest = lambda k: list(reversed(r[k].values()))[:limit]
    return {"aliases": sorted(r["aliases"].values(), key=lambda a: (-a["seen"], -a["share"]))[:limit], "words": newest("words"),
            "workflows": sorted(r["workflows"].values(), key=lambda w: -w["seen"])[:limit], "entities": newest("entities")}


@app.get("/api/bench")
async def bench():
    out = {}
    for name in ("learning", "load"):
        f = ROOT / "bench" / "results" / f"{name}.json"
        if f.exists():
            data = json.loads(f.read_text())
            data.pop("runs", None)
            out[name] = data
    return out


@app.get("/api/examples")
async def examples():
    rng = random.Random(7)
    picks = rng.sample(S.test, min(8, len(S.test))) if S.test else []
    return {"examples": [t["text"] for t in picks]}


# ---------------------------------------------------------------- live learning demo

def _judge(task: dict, ctx: Context) -> tuple[bool, list[str], list[dict]]:
    ent = task.get("entity")
    known = bool(ent) and any(e.get("ref") == ent["ref"] for e in ctx.entities)
    slots = [s for i, s in enumerate(task["slots"]) if not (known and i == task["lookup"])]
    keys = [t.key for t in ctx.tools]
    complete = all(any(k in keys for k in s) for s in slots)
    used = [next((k for k in keys if k in s), s[0]) for s in slots]
    return complete, used, ([ent] if ent and not known else [])


async def _evaluate() -> dict:
    rows = []
    for t in S.test:
        ctx = await S.resolver.resolve(t["text"])
        rows.append((_judge(t, ctx)[0], ctx.tokens, ctx.ms))
    lat = sorted(r[2] for r in rows)
    return {
        "seen": S.seen, "n": len(rows), "complete": round(100 * statistics.mean(r[0] for r in rows), 1),
        "tokens": round(statistics.mean(r[1] for r in rows)), "p50_ms": round(lat[len(lat) // 2], 1),
        "p95_ms": round(lat[int(len(lat) * 0.95)], 1),
    }


@app.get("/api/live")
async def live():
    return {"seen": S.seen, "total": len(S.train), "held_out": len(S.test), "curve": S.live, "busy": S.busy.locked()}


@app.post("/api/live/evaluate")
async def live_evaluate():
    if not S.test:
        raise HTTPException(404, "bench/tasks.jsonl is missing: run bench/build_tasks.py")
    if S.busy.locked():
        raise HTTPException(409, "a replay or evaluation is already running: wait for it to finish")
    async with S.busy:
        point = await _evaluate()
        if S.live and S.live[-1]["seen"] == point["seen"]:
            S.live[-1] = point
        else:
            S.live.append(point)
    return await live()


@app.post("/api/live/replay")
async def live_replay(body: ReplayIn):
    """Replay the next n tasks of recorded usage through the learning loop, then evaluate the held-out set."""
    if S.busy.locked():
        raise HTTPException(409, "a replay or evaluation is already running: wait for it to finish")
    async with S.busy:
        if not S.live:
            S.live.append(await _evaluate())
        batch = S.train[S.seen : S.seen + max(1, min(body.n, 200))]
        for t in batch:
            ctx = await S.resolver.resolve(t["text"])
            _, used, ents = _judge(t, ctx)
            await S.learner.feedback(ctx, used, ents)
        S.seen += len(batch)
        if batch:
            S.live.append(await _evaluate())
    return await live()


@app.post("/api/admin/reset")
async def reset(x_admin_token: str | None = Header(default=None)):
    want = os.environ.get("HYDRANT_ADMIN_TOKEN")
    if want and x_admin_token != want:
        raise HTTPException(401, "reset needs the X-Admin-Token header")
    if S.busy.locked():
        raise HTTPException(409, "a replay or evaluation is already running: wait for it to finish")
    async with S.busy:
        await wipe(S.store)
        counts = await ingest(S.store, load_catalog(CATALOG))
        S.contexts.clear()
        S.live, S.seen = [], 0
        for log in S.learner.recent.values():
            log.clear()
        await _refresh_tools()
    return {"reset": True, "ingested": counts}


@app.exception_handler(Exception)
async def unhandled(request, exc):
    return JSONResponse(status_code=502, content={"detail": f"HydraDB request failed: {type(exc).__name__}: {str(exc)[:300]}"})


from .mcp_server import mcp  # noqa: E402

app.mount("/mcp", mcp.streamable_http_app())
if (ROOT / "web").exists():
    @app.get("/")
    async def index():
        return FileResponse(ROOT / "web" / "index.html")

    app.mount("/", StaticFiles(directory=ROOT / "web"), name="web")
