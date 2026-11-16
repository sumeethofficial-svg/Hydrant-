"""Optional proxy to the real MCP servers, so an agent connected only to Hydrant can call what it found.

upstreams.json maps a server name (as in the catalog) to how to start it over stdio:

    {"filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"]},
     "memory":     {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"]}}

A server is started on first use and kept open. Servers not listed are not callable through Hydrant;
`find_tools` still returns their definitions so the agent can call them on its own connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path


class Upstreams:
    def __init__(self, path: str | None) -> None:
        self.config: dict[str, dict] = {}
        if path and Path(path).exists():
            self.config = json.loads(Path(path).read_text())
        self._sessions: dict[str, object] = {}
        self._stack = contextlib.AsyncExitStack()
        self._lock = asyncio.Lock()

    def available(self) -> list[str]:
        return sorted(self.config)

    async def _session(self, server: str):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async with self._lock:
            if server not in self._sessions:
                c = self.config[server]
                params = StdioServerParameters(command=c["command"], args=c.get("args", []), env={**os.environ, **c.get("env", {})})
                read, write = await self._stack.enter_async_context(stdio_client(params))
                session = await self._stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self._sessions[server] = session
            return self._sessions[server]

    async def call(self, server: str, name: str, arguments: dict) -> dict:
        if server not in self.config:
            return {"error": f"server '{server}' is not connected to this Hydrant. Connected: {self.available() or 'none'}. "
                             "Call the tool on your own connection, then report it with report_outcome."}
        session = await self._session(server)
        res = await session.call_tool(name, arguments or {})
        return {"is_error": bool(res.isError), "content": [c.model_dump(exclude_none=True) for c in res.content]}

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._stack.aclose()
