# Loom walkthrough (about 4 minutes)

Have the live demo open in one tab and the repo README in another. Reset the graph before recording.

**0:00 The problem (20s).** Show the headline. "An agent connected to these 27 MCP servers reads 166,025
tokens of tool definitions before it reads the task. Hydrant keeps them in a HydraDB graph instead."

**0:20 One resolve (50s).** Type: `comment on the github issue about the login bug in acme/api`.
- Point at the red sliver: tokens handed over against preloading everything.
- Point at the walk: words → `add_issue_comment`; that tool needs an issue number, so the graph walks
  NEEDS → YIELDED_BY to `search_issues`. "A flat index cannot find that tool: none of its words are in my task."

**1:10 It learns (60s).** Type: `the hotfix PR on acme/mobile is approved, ship it`. Show that the merge tool is
missing or ranked low. Search "merge", add `github/merge_pull_request`, report. Read the diff aloud: new words,
strengthened edges, a workflow edge. Run the same task again: it is now first.

**2:10 Entity memory (30s).** Run `post 'deploy finished' in the releases channel on Slack`; report with the
entity form (slack, channel, releases, C04REL9K2). Run `tell the releases channel v2 is out`: the lookup tool
is gone and the channel id is in the context. "One fewer round trip, forever."

**2:40 The curve (50s).** Click "Replay 50 tasks of usage" two or three times. The red points climb toward the
blue benchmark line. "Held-out tasks, never trained on. 55.0% before any usage, 84.1% after 445 tasks,
mean of 5 random splits on HydraDB. A flat top-10 index is the dashed line at 55.1%."

**3:30 Is the graph doing real work? (20s).** Scroll to the table. "Same learned weights, read without
traversal: 75.3%. With traversal: 84.1%. On multi-tool tasks the gap is 27.3 points."

**3:50 How to use it (10s).** Show the MCP config. "One MCP server, three tools. Code is open source."
