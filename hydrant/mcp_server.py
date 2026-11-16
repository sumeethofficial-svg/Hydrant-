"""Hydrant as an MCP server: three tools in the agent's window instead of every tool it might need.

Served over streamable HTTP at /mcp by hydrant.api. For a stdio client run `python -m hydrant.mcp_server`
with HYDRANT_URL pointing at a running Hydrant (default http://127.0.0.1:8000).
"""

from __future__ import annotations

import os
import time

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

mcp = FastMCP(
    "hydrant",
    instructions=(
        "Hydrant holds the tool definitions of every connected MCP server in a graph. Before a task, call "
        "find_tools with the task in plain words; it returns only the tools (with full input schemas) and "
        "already-resolved entities that task needs. Call them with call_tool. When the task is done, call "
        "report_outcome with the tools you actually used so the graph gets sharper."
    ),
    stateless_http=True,
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
async def find_tools(task: str) -> dict:
    """Find the tools a task needs. Describe the task in plain words, including any names (channel, repo, page).

    Returns `tools` (key, role, and the full MCP definition with inputSchema), `entities` the graph has already
    resolved for this task (use their `ref` directly instead of looking them up), and a `context_id`.
    """
    from . import api

    cid, ctx = await api.do_resolve(task)
    return {
        "context_id": cid,
        "tools": [{"key": t.key, "role": t.role, "definition": t.definition} for t in ctx.tools],
        "entities": ctx.entities,
        "tokens": ctx.tokens,
        "note": None if ctx.tools else "No tool matched these words. Rephrase with the action and the system, e.g. 'post a message to a Slack channel'.",
    }


@mcp.tool()
async def call_tool(context_id: str, tool: str, arguments: dict | None = None) -> dict:
    """Call a tool returned by find_tools. `tool` is its key, e.g. "slack/slack_post_message".

    Calls made here are observed, so the graph learns from them without a separate report.
    """
    from . import api

    entry = api.S.contexts.get(context_id)
    if entry is None:
        return {"error": f"context {context_id} is unknown or expired: call find_tools again"}
    server, _, name = tool.partition("/")
    res = await api.S.upstreams.call(server, name, arguments or {})
    if "error" not in res and not res.get("is_error"):
        entry["calls"].append(tool)
        entry["at"] = time.time()
    return res


@mcp.tool()
async def report_outcome(context_id: str, used_tools: list[str], resolved_entities: list[dict] | None = None) -> dict:
    """Tell Hydrant which tools the task actually used, in call order, so it learns.

    `resolved_entities` is optional: things you looked up that will be asked for again, each as
    {"server": "slack", "kind": "channel", "name": "releases", "ref": "C04REL9K2"}.
    """
    from . import api

    diff = await api.do_feedback(context_id, used_tools, resolved_entities)
    return {"learned": {k: len(v) for k, v in diff.items()}, "new_words": diff["new_terms"][:10]}


def main() -> None:
    """stdio bridge: forwards the three tools to a running Hydrant over HTTP."""
    import httpx

    url = os.environ.get("HYDRANT_URL", "http://127.0.0.1:8000").rstrip("/")
    bridge = FastMCP("hydrant", instructions=mcp.instructions)

    @bridge.tool(name="find_tools", description=find_tools.__doc__)
    async def _find(task: str) -> dict:
        async with httpx.AsyncClient(timeout=30) as c:
            r = (await c.post(f"{url}/api/resolve", json={"task": task})).json()
        return {"context_id": r.get("context_id"), "entities": r.get("entities"), "tokens": r.get("tokens"),
                "tools": [{"key": t["key"], "role": t["role"], "definition": t["definition"]} for t in r.get("tools", [])]}

    @bridge.tool(name="report_outcome", description=report_outcome.__doc__)
    async def _report(context_id: str, used_tools: list[str], resolved_entities: list[dict] | None = None) -> dict:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{url}/api/feedback", json={"context_id": context_id, "used": used_tools, "entities": resolved_entities or []})
        return r.json()

    bridge.run()


if __name__ == "__main__":
    main()
