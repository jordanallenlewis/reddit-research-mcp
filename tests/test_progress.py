from fastmcp import Client
from fixtures import FakeReddit, comment, listing, ok, run, sfw_info, t3

from reddit_research_mcp import server


def posts_info(params):
    ids = [x[3:] for x in params["id"].split(",")]
    return ok(listing([t3(id=i, selftext="Body") for i in ids]))


def more_children(params):
    return ok({"json": {"errors": [], "data": {"things": [comment("e0", "One.", parent="t3_abc123")]}}})


def call_with_progress(name, args):
    seen = []
    async def handler(progress, total, message):
        seen.append((progress, total))
    async def go():
        async with Client(server.mcp, progress_handler=handler) as c:
            await c.call_tool(name, args)
    run(go())
    return seen


def test_get_posts_reports_each_batch():
    server.set_client(FakeReddit({("GET", "/api/info"): posts_info}))
    seen = call_with_progress("get_posts", {"posts": [f"p{i}" for i in range(250)]})
    assert seen == [(1, 3), (2, 3), (3, 3)]


def test_get_posts_single_batch_reports_nothing():
    server.set_client(FakeReddit({("GET", "/api/info"): posts_info}))
    seen = call_with_progress("get_posts", {"posts": [f"p{i}" for i in range(5)]})
    assert seen == []


def test_expand_comments_reports_each_batch():
    server.set_client(FakeReddit({("GET", "/api/morechildren"): more_children, ("GET", "/api/info"): sfw_info}))
    seen = call_with_progress("expand_comments", {"post": "abc123", "comment_ids": [f"e{i}" for i in range(150)]})
    assert seen == [(1, 2), (2, 2)]


def test_direct_call_without_client_still_works():
    server.set_client(FakeReddit({("GET", "/api/info"): posts_info}))
    out = run(server.get_posts(posts=[f"p{i}" for i in range(150)]))
    assert "p149" in out
