"""Build bench/tasks.jsonl: hand-written agent tasks with gold tools, over the real harvested catalog.

Each task has `slots`: every slot must be covered by at least one of its acceptable tools for the context
to be complete (several servers expose equivalent tools, so a slot can list alternatives). A slot marked as
the `lookup` exists only to resolve a named entity (a channel id, a page id); it is waived when the context
already carries that entity.

Phrasings are written the way people ask, not the way tools are named: most avoid the tool's own words.
Run:  python bench/build_tasks.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bench"))
from hydrant.ingest import load_catalog, tool_key  # noqa: E402
from more_tasks import MORE  # noqa: E402

ENTITIES = {
    "releases": ("slack", "channel", "releases", "C04REL9K2"),
    "general": ("slack", "channel", "general", "C01GEN2A7"),
    "incidents": ("slack", "channel", "incidents", "C07INC5Q1"),
    "standup": ("slack", "channel", "standup", "C09STD3M8"),
    "Roadmap": ("notion", "page", "Roadmap", "1f0c2a9e-roadmap"),
    "Onboarding": ("notion", "page", "Onboarding", "8b71d4c0-onboarding"),
    "Retro": ("notion", "page", "Retro", "c33e90aa-retro"),
    "Groceries": ("todoist", "project", "Groceries", "2301884"),
    "Launch": ("todoist", "project", "Launch", "2301990"),
    "CRM": ("airtable", "base", "CRM", "appCRM7xQ1"),
    "Inventory": ("airtable", "base", "Inventory", "appINV3kD9"),
    "Hiring": ("airtable", "base", "Hiring", "appHIR5tZ2"),
}

WEB = ["brave-search/brave_web_search", "tavily/tavily_search", "exa/web_search_exa", "firecrawl/firecrawl_search"]
SCRAPE = ["firecrawl/firecrawl_scrape", "tavily/tavily_extract", "exa/web_fetch_exa"]
NAV = ["playwright/browser_navigate", "puppeteer/puppeteer_navigate", "chrome-devtools/navigate_page", "chrome-devtools/new_page"]
SHOT = ["playwright/browser_take_screenshot", "puppeteer/puppeteer_screenshot", "chrome-devtools/take_screenshot"]
CLICK = ["playwright/browser_click", "puppeteer/puppeteer_click", "chrome-devtools/click"]
FILL = ["playwright/browser_fill_form", "playwright/browser_type", "puppeteer/puppeteer_fill", "chrome-devtools/fill", "chrome-devtools/fill_form"]
READ = ["filesystem/read_text_file", "filesystem/read_file", "desktop-commander/read_file"]
WRITE = ["filesystem/write_file", "desktop-commander/write_file"]
LISTDIR = ["filesystem/list_directory", "desktop-commander/list_directory", "filesystem/list_directory_with_sizes", "filesystem/directory_tree"]
SEARCHF = ["filesystem/search_files", "desktop-commander/start_search"]
MOVE = ["filesystem/move_file", "desktop-commander/move_file"]
EDIT = ["filesystem/edit_file", "desktop-commander/edit_block"]
GH_FIND_ISSUE = ["github/search_issues", "github/list_issues"]

# (intent, slots, lookup slot index or None, phrasings). A phrasing is text, or (text, entity name).
INTENTS = [
    ("slack.post", [["slack/slack_list_channels"], ["slack/slack_post_message"]], 0, [
        ("Post 'deploy finished' in the releases channel on Slack", "releases"),
        ("let the general channel know the office is closed Friday", "general"),
        ("ping incidents that the database failover is done", "incidents"),
        ("drop a note in standup saying I'm out sick today", "standup"),
        ("announce v2.4 is live to the releases room", "releases"),
        ("tell everyone in general that lunch is here", "general"),
        ("shout in incidents: API latency is back to normal", "incidents"),
    ]),
    ("slack.history", [["slack/slack_list_channels"], ["slack/slack_get_channel_history"]], 0, [
        ("what did people say in the incidents channel in the last hour", "incidents"),
        ("catch me up on standup from this morning", "standup"),
        ("show the latest messages from releases", "releases"),
        ("skim general and summarise what I missed", "general"),
        ("pull recent chatter from the incidents room", "incidents"),
    ]),
    ("slack.reply", [["slack/slack_get_channel_history"], ["slack/slack_reply_to_thread"]], None, [
        "reply in the thread about the outage saying a fix is rolling out",
        "answer Priya's question in the thread under the pricing announcement",
        "respond under the last deploy message that QA signed off",
        "follow up in that Slack thread with the postmortem link",
    ]),
    ("slack.react", [["slack/slack_get_channel_history"], ["slack/slack_add_reaction"]], None, [
        "put a thumbs up on the latest message in the team channel",
        "add the eyes emoji to Ravi's last Slack message",
        "mark the newest announcement with a checkmark emoji",
    ]),
    ("slack.profile", [["slack/slack_get_users"], ["slack/slack_get_user_profile"]], None, [
        "what's Ananya's title and timezone on Slack",
        "look up the Slack profile for our new designer",
        "who is Karthik on Slack, which team is he in",
    ]),
    ("gh.create_issue", [["github/create_issue"]], None, [
        "open a GitHub issue in acme/api: login returns 500 on empty password",
        "file a bug on acme/web about the broken avatar upload",
        "log a ticket on the acme/mobile repo for the crash on startup",
        "raise an issue in acme/docs that the quickstart is out of date",
    ]),
    ("gh.comment_issue", [GH_FIND_ISSUE, ["github/add_issue_comment"]], None, [
        "comment on the GitHub issue about the login bug in acme/api that it's fixed in main",
        "leave a note on the avatar upload bug in acme/web asking for repro steps",
        "reply on the flaky test ticket in acme/api saying I can't reproduce it",
        "tell the reporter of the dark mode bug on acme/web that we shipped a patch",
    ]),
    ("gh.close_issue", [GH_FIND_ISSUE, ["github/update_issue"]], None, [
        "close the GitHub issue about the typo in the README of acme/docs",
        "mark the avatar upload bug in acme/web as closed",
        "label the login bug in acme/api as p1 and assign it to me",
        "shut the stale flaky-test ticket in acme/api",
    ]),
    ("gh.open_pr", [["github/create_pull_request"]], None, [
        "open a pull request from feature/dark-mode into main on acme/web",
        "raise a PR for my fix-login branch against develop in acme/api",
        "send the hotfix branch up for review on acme/mobile",
        "create a PR titled 'Bump deps' from chore/deps to main in acme/api",
    ]),
    ("gh.merge_pr", [["github/list_pull_requests"], ["github/merge_pull_request"]], None, [
        "merge the open pull request for dark mode on acme/web",
        "land the dependency bump PR in acme/api with a squash",
        "the hotfix PR on acme/mobile is approved, ship it",
        "merge whatever PR is open from fix-login in acme/api",
    ]),
    ("gh.review_pr", [["github/get_pull_request_files"], ["github/create_pull_request_review"]], None, [
        "review PR 212 on acme/api and approve it if the diff looks fine",
        "look over the changes in pull request 87 on acme/web and request changes on the CSS",
        "go through what PR 45 touches in acme/mobile and leave review feedback",
    ]),
    ("gh.pr_status", [["github/get_pull_request_status"]], None, [
        "are the checks green on PR 212 in acme/api",
        "did CI pass for pull request 87 on acme/web",
        "is the build still red on PR 45 in acme/mobile",
        "check whether the pipeline finished for PR 301 on acme/api",
    ]),
    ("gh.branch", [["github/create_branch"]], None, [
        "create a branch called release/2.4 off main in acme/api",
        "cut a new branch hotfix/crash from develop on acme/mobile",
        "start a feature/search branch in acme/web",
    ]),
    ("gh.search_code", [["github/search_code"]], None, [
        "find where we call stripe.charges.create across the acme org on GitHub",
        "grep GitHub for uses of the deprecated parseDate helper in acme repos",
        "which files in acme/api reference FEATURE_FLAG_BETA",
        "locate every TODO(security) across our GitHub code",
    ]),
    ("gh.read_file", [["github/get_file_contents"]], None, [
        "show me package.json from acme/web on GitHub",
        "what's in the Dockerfile on the main branch of acme/api",
        "read the CODEOWNERS file in acme/mobile",
        "pull up config/settings.yaml from acme/api develop",
    ]),
    ("gh.commits", [["github/list_commits"]], None, [
        "what landed on main in acme/api this week",
        "show the last ten commits on acme/web",
        "who committed to acme/mobile most recently",
        "history of changes on the release/2.3 branch of acme/api",
    ]),
    ("gh.fork", [["github/fork_repository"]], None, [
        "fork vercel/next.js into my account",
        "make my own copy of facebook/react on GitHub",
        "fork the tailwindlabs/tailwindcss repo to the acme org",
    ]),
    ("gh.push", [["github/push_files", "github/create_or_update_file"]], None, [
        "commit the updated README and CHANGELOG to acme/docs main in one go",
        "push these three config files to the chore/config branch of acme/api",
        "update LICENSE in acme/web with the new year",
        "add a .editorconfig file to acme/mobile",
    ]),
    ("gh.find_repo", [["github/search_repositories"]], None, [
        "find popular Rust graph database repos on GitHub",
        "which GitHub repos does the acme org have about billing",
        "look for an open source MCP gateway project on GitHub",
    ]),
    ("gl.mr", [["gitlab/create_merge_request"]], None, [
        "open a merge request on GitLab from feature/export to main in platform/core",
        "raise an MR for my bugfix branch in the platform/ui GitLab project",
        "send fix/timeouts up as a GitLab merge request against develop",
    ]),
    ("gl.issue", [["gitlab/create_issue"]], None, [
        "file a GitLab issue in platform/core: export job times out",
        "log a bug in the platform/ui project on GitLab about the misaligned header",
        "create a ticket on GitLab for upgrading Postgres in platform/infra",
    ]),
    ("lin.create", [["linear/create_issue"]], None, [
        "create a Linear issue for the checkout redesign, high priority",
        "add a ticket in Linear: migrate cron jobs to the new scheduler",
        "log this bug in Linear for the mobile team",
        "open a Linear task to audit our API rate limits",
    ]),
    ("lin.mine", [["linear/get_user_issues"]], None, [
        "what's assigned to me in Linear right now",
        "show my open Linear tickets",
        "what am I supposed to be working on this sprint according to Linear",
        "list Meera's issues in Linear",
    ]),
    ("lin.comment", [["linear/search_issues"], ["linear/add_comment"]], None, [
        "comment on the Linear issue about checkout redesign that designs are ready",
        "leave an update on the cron migration ticket in Linear",
        "note on the rate limit audit in Linear that I'm blocked on access",
    ]),
    ("lin.status", [["linear/search_issues"], ["linear/update_issue"]], None, [
        "move the checkout redesign ticket in Linear to In Progress",
        "mark the cron migration issue in Linear as done",
        "bump the rate limit audit to urgent in Linear",
        "reassign the Linear ticket about flaky payments to Arjun",
    ]),
    ("no.find", [["notion/API-post-search"]], None, [
        "find the Notion doc about our pricing tiers",
        "search Notion for the incident runbook",
        "where's the hiring plan in Notion",
        "look up meeting notes from the board offsite in Notion",
    ]),
    ("no.append", [["notion/API-post-search"], ["notion/API-patch-block-children"]], 0, [
        ("add a bullet to the Roadmap page in Notion: ship SSO in Q1", "Roadmap"),
        ("append today's action items to the Retro page", "Retro"),
        ("put a new section about laptops at the bottom of Onboarding in Notion", "Onboarding"),
        ("jot 'evaluate HydraDB' onto the Roadmap doc", "Roadmap"),
        ("tack a checklist onto the Onboarding page", "Onboarding"),
    ]),
    ("no.create_page", [["notion/API-post-page"]], None, [
        "create a new Notion page called 'Q1 Planning' under Engineering",
        "start a fresh doc in Notion for the vendor comparison",
        "make a Notion page for the launch checklist",
        "spin up a blank Notion writeup titled 'Postmortem: login outage'",
    ]),
    ("no.comment", [["notion/API-post-search"], ["notion/API-create-a-comment"]], 0, [
        ("comment on the Roadmap page in Notion asking who owns SSO", "Roadmap"),
        ("leave a comment on Retro that we skipped the action items", "Retro"),
        ("add a remark on the Onboarding page that the VPN steps are stale", "Onboarding"),
    ]),
    ("no.read", [["notion/API-post-search"], ["notion/API-retrieve-page-markdown", "notion/API-get-block-children"]], 0, [
        ("what does the Roadmap page in Notion say about Q2", "Roadmap"),
        ("read me the Onboarding doc", "Onboarding"),
        ("summarise the Retro page from Notion", "Retro"),
        ("pull the contents of Roadmap so I can paste it in an email", "Roadmap"),
    ]),
    ("no.query_db", [["notion/API-query-data-source"]], None, [
        "list rows in the Notion bugs database where status is open",
        "query the Notion content calendar for posts due this week",
        "filter the candidates table in Notion to those in final round",
    ]),
    ("td.add", [["todoist/add-tasks"]], None, [
        "add a todo to call the dentist tomorrow at 10",
        "remind me to renew the domain next Monday, put it in Todoist",
        "new Todoist task: review the pitch deck, priority 1",
        "stick 'book flights to Delhi' on my todo list",
    ]),
    ("td.add_project", [["todoist/find-projects"], ["todoist/add-tasks"]], 0, [
        ("add milk and eggs to my Groceries project in Todoist", "Groceries"),
        ("put 'record Loom walkthrough' in the Launch project", "Launch"),
        ("new item under Groceries: basmati rice", "Groceries"),
        ("add 'write tweet thread' to Launch due Sunday", "Launch"),
        ("throw coffee beans onto the Groceries list", "Groceries"),
    ]),
    ("td.done", [["todoist/find-tasks"], ["todoist/complete-tasks"]], None, [
        "mark 'call the dentist' as done in Todoist",
        "tick off the domain renewal todo",
        "I finished reviewing the pitch deck, check it off",
        "complete the 'book flights' task",
    ]),
    ("td.today", [["todoist/find-tasks-by-date"]], None, [
        "what's on my plate today in Todoist",
        "show me what's due this week",
        "anything overdue on my todo list",
        "what do I have to get done tomorrow",
    ]),
    ("td.resched", [["todoist/find-tasks"], ["todoist/reschedule-tasks", "todoist/update-tasks"]], None, [
        "push the dentist call to Thursday",
        "move 'review pitch deck' to next week in Todoist",
        "postpone the flight booking todo by two days",
        "bump the domain renewal task to the 30th",
    ]),
    ("td.reminder", [["todoist/find-tasks"], ["todoist/add-reminders"]], None, [
        "set a reminder 30 minutes before the dentist call task",
        "nudge me an hour ahead of the pitch deck review in Todoist",
        "add an alert on the flight booking todo for 9am",
    ]),
    ("at.add", [["airtable/list_bases"], ["airtable/list_tables", "airtable/describe_table"], ["airtable/create_record"]], 0, [
        ("add a lead named Infosys to the Leads table in the CRM base", "CRM"),
        ("log a new candidate, Divya Rao, in Hiring on Airtable", "Hiring"),
        ("insert 40 units of SKU-118 into the Inventory base", "Inventory"),
        ("new row in CRM: Tata Digital, stage qualified", "CRM"),
        ("put Rohan Mehta into the Hiring pipeline table", "Hiring"),
    ]),
    ("at.search", [["airtable/list_bases"], ["airtable/search_records", "airtable/list_records"]], 0, [
        ("find the Infosys lead in the CRM base on Airtable", "CRM"),
        ("which items in Inventory are below 10 units", "Inventory"),
        ("show all candidates in Hiring at the offer stage", "Hiring"),
        ("look up Tata Digital in CRM", "CRM"),
        ("list everything in the Inventory base tagged fragile", "Inventory"),
    ]),
    ("at.update", [["airtable/search_records", "airtable/list_records"], ["airtable/update_records"]], None, [
        "change the Infosys lead's stage to closed-won in Airtable",
        "set the stock count for SKU-118 to 25 in Airtable",
        "update Divya Rao's status to hired in the Airtable tracker",
    ]),
    ("k8.get", [["kubernetes/kubectl_get"]], None, [
        "list the pods in the payments namespace",
        "which deployments are running in prod on the cluster",
        "show me all services in kube-system",
        "are there any pods not ready in staging",
    ]),
    ("k8.logs", [["kubernetes/kubectl_logs"]], None, [
        "tail the logs of the api pod in prod",
        "show the last 200 log lines from checkout-7d9f in payments",
        "what is the worker container printing right now",
        "grab stdout from the ingest pod",
    ]),
    ("k8.restart", [["kubernetes/kubectl_rollout"]], None, [
        "restart the payments deployment",
        "roll back the api deployment in prod to the previous version",
        "bounce the checkout service pods in staging",
        "is the rollout of web finished yet",
    ]),
    ("k8.scale", [["kubernetes/kubectl_scale"]], None, [
        "scale the api deployment to 5 replicas",
        "bring worker down to zero pods in staging",
        "we're getting hammered, give checkout 10 instances",
        "double the replica count for ingest",
    ]),
    ("k8.why", [["kubernetes/kubectl_describe"], ["kubernetes/kubectl_logs"]], None, [
        "why is the checkout pod crash looping in payments",
        "the api pod keeps restarting in prod, figure out what's wrong",
        "debug the worker pod stuck in CrashLoopBackOff",
        "ingest pod is in Error state, what happened",
    ]),
    ("k8.helm", [["kubernetes/install_helm_chart"]], None, [
        "install the bitnami redis chart into the cache namespace",
        "deploy ingress-nginx with helm on the cluster",
        "helm install prometheus into monitoring",
    ]),
    ("k8.exec", [["kubernetes/exec_in_pod"]], None, [
        "run `env` inside the api pod in prod",
        "open the worker container and check /etc/resolv.conf",
        "execute a curl to localhost:8080/health from within the checkout pod",
    ]),
    ("k8.forward", [["kubernetes/port_forward"]], None, [
        "forward local port 5432 to the postgres pod",
        "expose the grafana service on my laptop at 3000",
        "tunnel 8080 from the api pod to localhost",
    ]),
    ("k8.apply", [["kubernetes/kubectl_apply"]], None, [
        "apply this deployment manifest to the staging namespace",
        "create the configmap from the yaml I pasted",
        "push the updated ingress yaml to the cluster",
    ]),
    ("mg.find", [["mongodb/find"]], None, [
        "show me the users in MongoDB who signed up from India",
        "get the five most recent orders from the shop database in Mongo",
        "fetch documents in products where stock is zero",
        "pull the customer record with email asha@example.com from Mongo",
    ]),
    ("mg.count", [["mongodb/count"]], None, [
        "how many orders are in the shop database in Mongo",
        "count users with verified = false in MongoDB",
        "what's the number of documents in the events collection",
    ]),
    ("mg.agg", [["mongodb/aggregate"]], None, [
        "total revenue per month from the orders collection in Mongo",
        "group signups by country and sort descending in MongoDB",
        "average order value by customer tier, from Mongo",
        "top ten products by units sold in the shop database",
    ]),
    ("mg.index", [["mongodb/create-index"]], None, [
        "add an index on email in the users collection in Mongo",
        "queries on orders.createdAt are slow, index that field",
        "create a compound index on tenant and status for events in MongoDB",
    ]),
    ("mg.schema", [["mongodb/collection-schema"]], None, [
        "what fields does the orders collection have in Mongo",
        "describe the shape of documents in users",
        "what does a product document look like in the shop database",
    ]),
    ("mg.insert", [["mongodb/insert-many"]], None, [
        "insert these three test users into the users collection in Mongo",
        "seed the products collection with the sample catalogue",
        "load this JSON array of events into MongoDB",
    ]),
    ("mg.list", [["mongodb/list-collections", "mongodb/list-databases"]], None, [
        "what collections exist in the shop database in Mongo",
        "which databases are on this Mongo cluster",
        "show me what's in the analytics db in MongoDB",
    ]),
    ("pg.query", [["postgres/query"]], None, [
        "run a SQL query on Postgres: top 10 customers by lifetime spend",
        "how many rows are in the invoices table in Postgres",
        "select the latest 20 signups from the Postgres users table",
        "check Postgres for orders stuck in pending for over a day",
    ]),
    ("se.stack", [["sentry/get_event_stacktrace", "sentry/get_issue_details"]], None, [
        "show the stack trace for Sentry issue API-482",
        "what's the traceback on WEB-1093 in Sentry",
        "where exactly does MOBILE-77 blow up, according to Sentry",
        "open up Sentry error API-519 and tell me the failing line",
    ]),
    ("se.search", [["sentry/search_issues", "sentry/search_errors"]], None, [
        "what new errors showed up in Sentry since yesterday's deploy",
        "find unresolved Sentry issues in the checkout project",
        "any TypeErrors hitting production this week in Sentry",
        "list the noisiest Sentry problems for the mobile app",
    ]),
    ("se.resolve", [["sentry/update_issue"]], None, [
        "resolve Sentry issue API-482",
        "mark WEB-1093 as ignored in Sentry",
        "assign MOBILE-77 in Sentry to the payments team",
        "close out API-519 in Sentry, it's fixed in the next release",
    ]),
    ("se.seer", [["sentry/analyze_issue_with_seer"]], None, [
        "get Seer to root-cause Sentry issue API-482",
        "ask Sentry's AI what's causing WEB-1093 and how to fix it",
        "have Seer suggest a fix for MOBILE-77",
    ]),
    ("se.alert", [["sentry/create_alert_rule"]], None, [
        "alert me in Sentry when checkout errors exceed 50 per minute",
        "set up a Sentry notification rule for any new issue in the api project",
        "page the on-call when the mobile crash rate spikes, via Sentry",
    ]),
    ("se.release", [["sentry/find_releases"]], None, [
        "which releases has Sentry seen for the api project",
        "what's the latest version deployed according to Sentry",
        "list recent Sentry releases for web",
    ]),
    ("se.logs", [["sentry/search_logs"]], None, [
        "search Sentry logs for 'connection reset' in the last hour",
        "find log lines mentioning tenant 4411 in Sentry",
        "show warning-level logs from the worker in Sentry",
    ]),
    ("se.uptime", [["sentry/create_uptime_monitor"]], None, [
        "monitor https://api.acme.dev/health every minute with Sentry",
        "set up a Sentry uptime check on our status page",
        "have Sentry watch the login endpoint and tell us when it's down",
    ]),
    ("mp.directions", [["google-maps/maps_directions"]], None, [
        "how do I drive from Bangalore to Mysore",
        "get directions from Indiranagar to the airport",
        "best route by car from Hassan to Mangalore",
        "walking directions from MG Road metro to Cubbon Park",
    ]),
    ("mp.places", [["google-maps/maps_search_places", "brave-search/brave_local_search"]], None, [
        "find coffee shops near Koramangala",
        "any good dosa places around Jayanagar",
        "pharmacies open now close to HSR Layout",
        "coworking spaces near Whitefield",
    ]),
    ("mp.geocode", [["google-maps/maps_geocode"]], None, [
        "what are the coordinates of Malnad College of Engineering, Hassan",
        "latitude and longitude for 1 Infinite Loop, Cupertino",
        "convert 'UB City, Bangalore' into lat/long",
    ]),
    ("mp.distance", [["google-maps/maps_distance_matrix"]], None, [
        "how long does it take to get from Hassan to Bangalore versus Mysore to Bangalore",
        "travel time between each of these three offices",
        "how far is Electronic City from Hebbal by road",
    ]),
    ("mp.reverse", [["google-maps/maps_reverse_geocode"]], None, [
        "what address is at 12.9716, 77.5946",
        "which place is at lat 13.0068 long 76.0996",
        "turn these GPS coordinates into a street address: 28.6139, 77.2090",
    ]),
    ("web.search", [WEB], None, [
        "search the web for the latest HydraDB release notes",
        "google what changed in Next.js 16",
        "look online for reviews of the Framework 13 laptop",
        "find recent news about RBI's UPI limits",
        "what are people saying about the new Claude model, check the internet",
    ]),
    ("web.scrape", [SCRAPE], None, [
        "grab the text of https://hydradb.com/blog/launch as markdown",
        "pull the pricing table off https://vercel.com/pricing",
        "extract the article body from this URL: https://example.com/post/42",
        "read https://docs.stripe.com/api/charges and give me the content",
    ]),
    ("web.crawl", [["firecrawl/firecrawl_crawl", "tavily/tavily_crawl"]], None, [
        "crawl the whole docs site at docs.acme.dev and collect every page",
        "spider https://blog.acme.dev and fetch all posts",
        "walk every link under https://hydradb.com/docs and save the pages",
    ]),
    ("web.map", [["firecrawl/firecrawl_map", "tavily/tavily_map"]], None, [
        "list all the URLs on acme.dev",
        "give me a sitemap of https://hydradb.com",
        "discover every page under docs.acme.dev without downloading them",
    ]),
    ("web.research", [["tavily/tavily_research", "firecrawl/firecrawl_agent", "exa/agent_run"]], None, [
        "do a deep research report on graph databases for agent memory",
        "investigate the MCP gateway market and write up the main players",
        "compile a thorough briefing on object-store-native databases",
    ]),
    ("web.papers", [["firecrawl/firecrawl_research_search_papers"]], None, [
        "find academic papers on GraphRAG evaluation",
        "any recent arXiv work on tool selection for LLM agents",
        "look for publications about personalized PageRank retrieval",
    ]),
    ("br.shot", [NAV, SHOT], None, [
        "open https://acme.dev and take a screenshot of the homepage",
        "capture what https://status.acme.dev looks like right now",
        "go to our pricing page and snap a picture of it",
        "load localhost:3000 and grab an image of the landing page",
    ]),
    ("br.form", [NAV, FILL], None, [
        "go to https://acme.dev/signup and fill in the form with test data",
        "open the contact page and type my email into the newsletter box",
        "visit localhost:3000/login and enter the demo credentials",
    ]),
    ("br.click", [CLICK], None, [
        "click the 'Get started' button on the page",
        "press the accept cookies banner",
        "hit the submit button in the browser",
    ]),
    ("br.console", [["playwright/browser_console_messages", "chrome-devtools/list_console_messages"]], None, [
        "are there any JavaScript errors in the browser console",
        "show me what the page is logging to the console",
        "check devtools for warnings on this page",
    ]),
    ("br.network", [["playwright/browser_network_requests", "chrome-devtools/list_network_requests"]], None, [
        "which API calls does the page make when it loads",
        "list the network requests fired by the dashboard",
        "is anything returning 404 in the browser's network tab",
    ]),
    ("br.perf", [["chrome-devtools/performance_start_trace", "chrome-devtools/lighthouse_audit"]], None, [
        "run a Lighthouse audit on https://acme.dev",
        "profile how fast the homepage loads",
        "why is the landing page slow, measure its performance",
    ]),
    ("fs.read", [READ], None, [
        "read the file /tmp/notes/todo.md",
        "show me what's in config.yaml",
        "open src/index.ts and print it",
        "cat the .env.example in this project",
    ]),
    ("fs.write", [WRITE], None, [
        "write 'hello' to /tmp/out.txt",
        "save this JSON as data/settings.json",
        "create a file called NOTES.md with these bullet points",
        "dump the summary into report.txt",
    ]),
    ("fs.list", [LISTDIR], None, [
        "list the files in /tmp/project",
        "what's inside the src folder",
        "show me the folder structure of this repo",
        "which files are in my Downloads directory",
    ]),
    ("fs.search", [SEARCHF], None, [
        "find all the .log files under /var/tmp",
        "search this project for files named docker-compose",
        "where is the file that mentions STRIPE_SECRET on disk",
        "locate every test file in the repo",
    ]),
    ("fs.move", [MOVE], None, [
        "rename draft.md to final.md",
        "move report.pdf into the archive folder",
        "relocate the screenshots to /tmp/shots",
    ]),
    ("fs.edit", [EDIT], None, [
        "in config.yaml change the port from 8080 to 9090",
        "replace the word 'staging' with 'prod' in deploy.sh",
        "fix the typo on line 3 of README.md",
        "swap the API base URL in src/client.ts for the new one",
    ]),
    ("fs.mkdir", [["filesystem/create_directory", "desktop-commander/create_directory"]], None, [
        "make a new folder called exports under /tmp/project",
        "create the directory build/artifacts",
        "set up an empty logs folder next to src",
    ]),
    ("dc.run", [["desktop-commander/start_process"]], None, [
        "run `npm test` in the terminal",
        "start the dev server with `pnpm dev`",
        "execute the python script scripts/migrate.py from the shell",
        "launch a bash session and run `df -h`",
    ]),
    ("dc.kill", [["desktop-commander/kill_process", "desktop-commander/force_terminate"]], None, [
        "kill whatever is running on PID 4412",
        "stop the hung node process",
        "terminate the dev server that's still going in the background",
    ]),
    ("dk.run", [["docker/run_command"]], None, [
        "run `docker ps` and show me the containers",
        "docker compose up the stack in detached mode",
        "check which images are on this machine with docker",
    ]),
    ("mem.remember", [["memory/create_entities", "memory/add_observations"]], None, [
        "remember that Sumeeth prefers TypeScript over Python",
        "save to memory: the staging database password rotates monthly",
        "make a note for later that the client's fiscal year starts in April",
        "store the fact that Priya is the owner of the billing service",
    ]),
    ("mem.recall", [["memory/search_nodes", "memory/read_graph", "memory/open_nodes"]], None, [
        "what do you remember about Priya",
        "recall anything stored about the billing service",
        "what did I tell you before about the client's fiscal year",
    ]),
    ("c7.docs", [["context7/resolve-library-id"], ["context7/query-docs"]], None, [
        "how do I use server actions in Next.js, check the current docs",
        "look up the latest Tailwind docs for container queries",
        "what's the up-to-date API for FastAPI lifespan events",
        "pull the official Prisma reference for transactions",
    ]),
    ("fg.get", [["figma/get_figma_data"]], None, [
        "read the layout of this Figma file: figma.com/design/abc123/Landing",
        "what components are in the checkout frame in Figma",
        "inspect the design spec for the navbar from our Figma link",
    ]),
    ("fg.img", [["figma/download_figma_images"]], None, [
        "export the icons from the Figma file as SVG",
        "download the hero illustration out of Figma as a PNG",
        "save the logo assets from our Figma design",
    ]),
]


def build() -> list[dict]:
    catalog = load_catalog(ROOT / "catalog")
    valid = {tool_key(s, t["name"]) for s, defs in catalog.items() for t in defs}
    tasks = []
    for intent, slots, lookup, phrasings in INTENTS:
        for slot in slots:
            for k in slot:
                assert k in valid, f"{intent}: unknown tool {k}"
        for i, ph in enumerate(list(phrasings) + list(MORE.get(intent, []))):
            text, ent = (ph, None) if isinstance(ph, str) else ph
            entity = None
            if ent:
                server, kind, name, ref = ENTITIES[ent]
                entity = {"server": server, "kind": kind, "name": name, "ref": ref}
            assert (lookup is None) == (entity is None), f"{intent}: lookup and entity must come together"
            tasks.append({"id": f"{intent}.{i}", "intent": intent, "text": text, "slots": slots, "lookup": lookup, "entity": entity})
    return tasks


if __name__ == "__main__":
    assert not set(MORE) - {i[0] for i in INTENTS}, set(MORE) - {i[0] for i in INTENTS}
    tasks = build()
    assert len({t["text"] for t in tasks}) == len(tasks), "duplicate phrasing"
    out = ROOT / "bench" / "tasks.jsonl"
    out.write_text("".join(json.dumps(t) + "\n" for t in tasks))
    multi = sum(1 for t in tasks if len(t["slots"]) > 1)
    print(f"{len(tasks)} tasks, {len(INTENTS)} intents, {multi} multi-tool, {sum(1 for t in tasks if t['entity'])} with a named entity -> {out}")
