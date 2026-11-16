"""Harvest real tool definitions from public MCP servers into catalog/<server>.json.

Each server is started over stdio with npx and asked for `tools/list`. Servers that refuse to start without
credentials get placeholder values: listing tools does not call any API. Servers that still fail are skipped.
Run from the repo root:  python scripts/harvest_catalog.py
"""
import json, subprocess, sys, os, select, time
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "catalog")
SERVERS = {
 "filesystem": ["@modelcontextprotocol/server-filesystem","/tmp"],
 "memory": ["@modelcontextprotocol/server-memory"],
 "everything": ["@modelcontextprotocol/server-everything"],
 "sequential-thinking": ["@modelcontextprotocol/server-sequential-thinking"],
 "github": ["@modelcontextprotocol/server-github"],
 "slack": ["@modelcontextprotocol/server-slack"],
 "gitlab": ["@modelcontextprotocol/server-gitlab"],
 "postgres": ["@modelcontextprotocol/server-postgres","postgresql://x@localhost/x"],
 "puppeteer": ["@modelcontextprotocol/server-puppeteer"],
 "brave-search": ["@modelcontextprotocol/server-brave-search"],
 "google-maps": ["@modelcontextprotocol/server-google-maps"],
 "redis": ["@modelcontextprotocol/server-redis","redis://localhost"],
 "playwright": ["@playwright/mcp"],
 "context7": ["@upstash/context7-mcp"],
 "firecrawl": ["firecrawl-mcp"],
 "notion": ["@notionhq/notion-mcp-server"],
 "supabase": ["@supabase/mcp-server-supabase"],
 "stripe": ["@stripe/mcp","--tools=all"],
 "linear": ["mcp-linear"],
 "figma": ["figma-developer-mcp","--stdio"],
 "sentry": ["@sentry/mcp-server"],
 "tavily": ["tavily-mcp"],
 "exa": ["exa-mcp-server"],
 "mongodb": ["mongodb-mcp-server"],
 "docker": ["mcp-server-docker"],
 "kubernetes": ["mcp-server-kubernetes"],
 "airtable": ["airtable-mcp-server"],
 "todoist": ["@doist/todoist-ai"],
 "chrome-devtools": ["chrome-devtools-mcp"],
 "desktop-commander": ["@wonderwhy-er/desktop-commander"],
}
def run(name, cmd, to=100):
    env = dict(os.environ, GITHUB_PERSONAL_ACCESS_TOKEN="x", SLACK_BOT_TOKEN="xoxb-x", SLACK_TEAM_ID="T1", GITLAB_PERSONAL_ACCESS_TOKEN="x",
      BRAVE_API_KEY="x", GOOGLE_MAPS_API_KEY="x", FIRECRAWL_API_KEY="fc-x", NOTION_TOKEN="x", OPENAPI_MCP_HEADERS='{"Authorization":"Bearer x"}', STRIPE_SECRET_KEY="sk_test_x",
      LINEAR_API_KEY="x", FIGMA_API_KEY="x", SENTRY_ACCESS_TOKEN="x", TAVILY_API_KEY="tvly-x", EXA_API_KEY="x", MDB_MCP_CONNECTION_STRING="mongodb://localhost", AIRTABLE_API_KEY="x", TODOIST_API_KEY="x", SUPABASE_ACCESS_TOKEN="sbp_x")
    p = subprocess.Popen(["npx","-y",*cmd], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)
    def send(m): p.stdin.write(json.dumps(m)+"\n"); p.stdin.flush()
    def read(deadline):
        while time.time()<deadline:
            r,_,_ = select.select([p.stdout],[],[],1)
            if r:
                line = p.stdout.readline()
                if not line: return None
                try: return json.loads(line)
                except: continue
        return None
    dl = time.time()+to
    try:
        send({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"h","version":"0"}}})
        while True:
            m = read(dl)
            if m is None: return None
            if m.get("id")==1: break
        send({"jsonrpc":"2.0","method":"notifications/initialized"})
        send({"jsonrpc":"2.0","id":2,"method":"tools/list"})
        while True:
            m = read(dl)
            if m is None: return None
            if m.get("id")==2: return m.get("result",{}).get("tools")
    except Exception as e:
        return None
    finally:
        p.kill()
os.makedirs(OUT, exist_ok=True)
for name, cmd in SERVERS.items():
    if os.path.exists(f"{OUT}/{name}.json"): continue
    t=run(name, cmd)
    if t: json.dump(t, open(f"{OUT}/{name}.json","w"), indent=1)
    print(name, "FAIL" if not t else f"{len(t)} tools", flush=True)
