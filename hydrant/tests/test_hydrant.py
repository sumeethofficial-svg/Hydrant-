"""Unit tests on the in-memory store, plus a parity test against a real HydraDB node.

The parity test wipes the graph it connects to, so it only runs when HYDRANT_TEST_HYDRADB_URI points at a
disposable node:  HYDRANT_TEST_HYDRADB_URI=bolt://127.0.0.1:17687 pytest
"""

import asyncio
import os
from pathlib import Path

import pytest

from hydrant.ingest import build_rows, ingest, load_catalog, needed_resources
from hydrant.learner import Learner, wipe
from hydrant.resolver import Resolver
from hydrant.store import MemoryStore
from hydrant.text import nid, terms

CATALOG = load_catalog(Path(__file__).resolve().parent.parent / "catalog")


def run(coro):
    return asyncio.run(coro)


async def fresh(store=None):
    store = store or MemoryStore()
    await ingest(store, CATALOG)
    r = Resolver(store)
    return store, r, Learner(store, on_change=r.forget)


def keys(ctx):
    return [t.key for t in ctx.tools]


def test_terms_and_ids():
    assert terms("Merge the pull requests") == ["merg", "pull", "request", "merg_pull", "pull_request"]
    assert nid("tool", "a/b") == nid("tool", "a/b") != nid("term", "a/b")
    assert 0 <= nid("tool", "a/b") < 2**52


def test_catalog_shape():
    rows = build_rows(CATALOG)
    assert len(rows["nodes"]["Tool"]) == 435 and len(rows["nodes"]["Server"]) == 27
    slack = {t["name"]: t for t in CATALOG["slack"]}
    assert "slack:channel" in needed_resources("slack", slack["slack_post_message"])
    res = {r["id"]: r["key"] for r in rows["nodes"]["Resource"]}
    tools = {t["id"]: t["key"] for t in rows["nodes"]["Tool"]}
    yielders = {(res[e["src"]], tools[e["dst"]]) for e in rows["edges"][("YIELDED_BY", "Resource", "Tool")]}
    assert ("slack:channel", "slack/slack_list_channels") in yielders


def test_resolve_walks_to_the_lookup_tool():
    async def go():
        _, r, _ = await fresh()
        ctx = await r.resolve("post a message to a Slack channel")
        assert keys(ctx)[0] == "slack/slack_post_message"
        assert "slack/slack_list_channels" in keys(ctx)
        assert ctx.tokens < 5000 and ctx.queries <= 16
    run(go())


def test_unknown_words_return_nothing_then_are_learned():
    async def go():
        _, r, l = await fresh()
        task = "ping the crew that lunch is here"
        ctx = await r.resolve(task)
        assert "slack/slack_post_message" not in keys(ctx)
        diff = await l.feedback(ctx, ["slack/slack_post_message"])
        assert "crew" in diff["new_terms"]
        assert "slack/slack_post_message" in keys(await r.resolve("ping the crew about the outage"))
    run(go())


def test_correct_answers_do_not_move_weights():
    async def go():
        _, r, l = await fresh()
        ctx = await r.resolve("post a message to a Slack channel")
        diff = await l.feedback(ctx, ["slack/slack_post_message"])
        assert diff["reinforced"] == [] and diff["decayed"] == []
    run(go())


def test_workflow_edge_brings_the_next_tool():
    async def go():
        _, r, l = await fresh()
        task = "tick off the dentist todo"
        ctx = await r.resolve(task)
        await l.feedback(ctx, ["todoist/find-tasks", "todoist/complete-tasks"])
        ctx = await r.resolve("find tasks in todoist")
        assert "todoist/complete-tasks" in keys(ctx)
        assert any(t.role == "workflow" or t.key == "todoist/complete-tasks" for t in ctx.tools)
    run(go())


def test_entity_memory_replaces_the_lookup():
    async def go():
        _, r, l = await fresh()
        task = "post 'deploy finished' in the releases channel on Slack"
        before = await r.resolve(task)
        assert before.entities == []
        await l.feedback(before, ["slack/slack_list_channels", "slack/slack_post_message"],
                         [{"server": "slack", "kind": "channel", "name": "releases", "ref": "C04REL9K2"}])
        after = await r.resolve("tell the releases channel on Slack that v2 is out")
        assert [e["ref"] for e in after.entities] == ["C04REL9K2"]
        assert not any(t.role == "prereq" and t.key == "slack/slack_list_channels" for t in after.tools)
    run(go())


def test_repeating_one_report_does_not_invent_generic_aliases():
    async def go():
        st, r, l = await fresh()
        for _ in range(3):
            ctx = await r.resolve("merge the open pull request for dark mode on acme/web")
            await l.feedback(ctx, ["github/list_pull_requests", "github/merge_pull_request"])
        targets = {e["dst"] for e in await st.scan_edges("Term", "MEANS", ("c",))}
        assert nid("term", "list") not in targets
    run(go())


def test_trace_is_a_connected_picture():
    async def go():
        _, r, _ = await fresh()
        tr = (await r.resolve("comment on the github issue about the login bug")).trace()
        ids = {n["id"] for n in tr["nodes"]}
        assert tr["edges"] and all(e["from"] in ids and e["to"] in ids for e in tr["edges"])
    run(go())


@pytest.mark.skipif(not os.environ.get("HYDRANT_TEST_HYDRADB_URI"), reason="needs a disposable HydraDB node")
def test_hydradb_matches_the_reference_store():
    from hydrant.hydra_store import HydraStore

    tasks = [
        "post 'deploy finished' in the releases channel on Slack",
        "comment on the github issue about the login bug in acme/api",
        "tick off the dentist todo",
        "why is the checkout pod crash looping",
        "tell the releases channel on Slack that v2 is out",
    ]
    used = [["slack/slack_list_channels", "slack/slack_post_message"], ["github/search_issues", "github/add_issue_comment"],
            ["todoist/find-tasks", "todoist/complete-tasks"], ["kubernetes/kubectl_describe", "kubernetes/kubectl_logs"],
            ["slack/slack_post_message"]]
    ent = {"server": "slack", "kind": "channel", "name": "releases", "ref": "C04REL9K2"}

    async def play(store):
        _, r, l = await fresh(store)
        seen = []
        for _round in range(2):
            for i, (t, u) in enumerate(zip(tasks, used)):
                ctx = await r.resolve(t)
                seen.append((sorted(keys(ctx)), [e["ref"] for e in ctx.entities]))
                await l.feedback(ctx, u, [ent] if i == 0 else [])
        return seen

    async def go():
        hydra = HydraStore(os.environ["HYDRANT_TEST_HYDRADB_URI"], os.environ.get("HYDRADB_TOKEN", "local-development-token-32-bytes"))
        await wipe(hydra)
        try:
            a = await play(hydra)
        finally:
            await wipe(hydra)
            await hydra.close()
        assert a == await play(MemoryStore())
    run(go())
