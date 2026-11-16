# Hydrant: go-to-market

## One line
One MCP server in front of all your MCP servers. The agent sees three tools instead of 435, gets handed
only what each task needs, and the selection gets better every time it is used.

## The problem, in other people's numbers
Tool definitions are billed on every turn and crowd the window the model reasons in.
- Three MCP servers were measured at 143,000 tokens of definitions, 72% of a 200K window, before the first user
  message ([Unblocked, May 2026](https://getunblocked.com/blog/mcp-tool-overload/)).
- Seven servers measured 67,300 tokens ([mcp.directory](https://mcp.directory/blog/mcp-context-bloat-fix-2026-tool-search-code-mode-progressive-disclosure)).
- Past roughly thirty tools the cost is accuracy, not only money ([WorkOS, Sept 2026](https://workos.com/blog/mcp-server-token-cost)).
- Our own catalog: 435 real tools from 27 servers is 166,025 tokens.

## What exists, and what it does not do
Clients now defer tool definitions and let the model search for them (Claude Code's tool search is on by
default). That fixes the bill. It does not fix selection:
- Search is static. The thousandth "ship the hotfix PR" is matched exactly as badly as the first.
- Search returns the tool that matches the words, not the lookup that tool cannot run without.
- Nothing remembers that "#releases" is `C04REL9K2`, so every task pays the lookup round trip again.

Hydrant is the layer that learns. Measured on HydraDB, 5 random splits, held-out tasks:

| | complete context | context tokens |
|---|---|---|
| Preload every tool | 100% | 166,025 |
| Flat index, top 10 (what static search gives you) | 55.1% | 4,211 |
| Hydrant, day one | 55.0% | 3,488 |
| Hydrant, after 445 tasks of usage | **84.1%** | **2,888** |

## Who wants it
1. **Teams running internal agents on many MCP servers** (platform and developer-experience teams). Pain: token
   bills, wrong tool picks, the 128-tool caps some providers enforce. Buyer: head of platform or AI infra.
2. **Agent product companies** (support, coding, ops agents) whose customers each connect a different tool
   set. Pain: tool selection quality differs per customer and nobody tunes it. Hydrant tunes itself per tenant.
3. **MCP gateway and registry vendors** who already sit in the path and need a selection layer. Hydrant is a
   component they can embed.

## Why a graph, and why HydraDB
The product is edges: word to tool, tool to the resource it needs, resource to the tool that looks it up, tool
to the tool that follows it, name to the entity it resolved to. Every answer is a short traversal, and every
outcome is a handful of edge updates. In the benchmark, reading the same learned graph without traversal loses
8.9 points overall and 27.3 points on multi-tool tasks. HydraDB keeps the graph on object
storage, so each tenant's learned graph is cheap to keep and cold tenants cost storage only.

## How it makes money
- **Open source core** (this repo): self-host with your own HydraDB.
- **Hydrant Cloud**: hosted, per-tenant learned graphs, dashboard of what the graph learned and where agents
  still miss. Priced per resolved task: free to 10k tasks a month, then $2 per 10k. At 163,137 tokens saved per
  task, a team at 1M tasks a month saves far more in input tokens than the $200 it pays.
- **Enterprise**: in-VPC deployment on the customer's HydraDB, SSO, audit log of every tool handed to every
  agent (which is also a security story: an agent cannot call what it was never handed).

## First 90 days
- Launch the open-source repo and the live demo; post the benchmark and the curve.
- Ship the upstream proxy for the 10 most-used MCP servers so Hydrant is a drop-in single endpoint.
- Design partners: 5 teams with 100+ tools connected. Success metric: their own complete-context curve.

## Posts

**X / Twitter**
> Your agent reads 166,025 tokens of tool definitions before it reads the task.
>
> I built Hydrant for the @hydradb hackathon: every MCP tool lives in a HydraDB graph, the agent gets only what
> the task needs (2,888 tokens), and it learns from every task.
>
> Held-out accuracy: 55.0% → 84.1% after 445 tasks. Static top-10 search: 55.1%.
>
> Live demo + code 👇

**LinkedIn**
> Agents with many MCP servers have two problems: the tool definitions eat the context window, and static tool
> search picks by keywords, so it never improves.
>
> For the HydraDB hackathon I built Hydrant, a context layer that stores 435 real tool definitions from
> 27 MCP servers as a graph in HydraDB. A task walks the graph: words → tools → the lookups those tools need →
> entities already resolved. Every finished task rewrites edges: new vocabulary, aliases ("PR" means pull
> request), learned workflows, remembered ids.
>
> Measured on held-out tasks over 5 random splits, complete-context rate goes from 55.0% to 84.1%
> with use, at 2,888 tokens instead of 166,025. A flat top-10 index over the same text gets 55.1%.
>
> Try it: <live link>. Code: <repo>. Built on the open-source HydraDB engine.
