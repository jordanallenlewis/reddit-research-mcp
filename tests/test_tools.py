import asyncio
import time

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fixtures import FakeReddit, comment, listing, ok, post, raw, run, t3
from redditwarp.http.exceptions import TransportError as RWTransportError

from reddit_research_mcp import server

EXPECTED_TOOLS = {
    "search_reddit",
    "browse_subreddit",
    "search_subreddits",
    "get_subreddit_info",
    "get_subreddit_wiki",
    "get_post",
    "expand_comments",
    "get_posts",
    "get_user_activity",
    "find_other_discussions",
}


def test_tools_are_read_only_and_text_only():
    async def go():
        async with Client(server.mcp) as c:
            return await c.list_tools()

    tools = run(go())
    assert {t.name for t in tools} == EXPECTED_TOOLS
    for t in tools:
        a = t.annotations
        assert a.read_only_hint is True and a.idempotent_hint is True
        assert a.open_world_hint is True and a.destructive_hint is False
        assert t.output_schema is None  # no duplicate structured copy of the text
        assert t.description and len(t.description) < 2000
        props = t.input_schema["properties"]  # the deadline wrapper keeps the real signature
        assert props and all(p.get("description") for p in props.values()), t.name
    get_post = next(t for t in tools if t.name == "get_post")
    assert get_post.input_schema["properties"]["body_chars"]["default"] == 6000


def test_mcp_error_result_is_flagged_and_non_empty():
    server.set_client(FakeReddit({("GET", "/r/x1/hot"): raw(503, b"")}))

    async def go():
        async with Client(server.mcp) as c:
            return await c.call_tool("browse_subreddit", {"subreddit": "x1"}, raise_on_error=False)

    res = run(go())
    assert res.is_error
    assert "HTTP 503 twice" in res.content[0].text


def test_search_params_cursor_and_clamps():
    fake = FakeReddit({("GET", "/r/a1+b2/search"): ok(listing([t3(id="p1")], after="t3_p1"))})
    server.set_client(fake)
    out = run(server.search_reddit(query='"dremio" AND iceberg', subreddit="r/a1 + b2", sort="top",
                                   time="year", limit=500, after="t3_zz9", body_chars=-3))
    params = fake.calls[0][2]
    assert params == {"q": '"dremio" AND iceberg', "sort": "top", "t": "year", "limit": "100",
                      "after": "t3_zz9", "type": "link", "restrict_sr": "1", "raw_json": "1"}
    assert out.startswith('search: "dremio" AND iceberg (in r/a1+b2, sort=top, time=year): 1 posts')
    assert "limit clamped to 100" in out and "body_chars clamped to 0" in out
    assert out.endswith("next: after=t3_p1")


def test_site_wide_search_has_no_restrict_sr():
    fake = FakeReddit({("GET", "/search"): ok(listing([]))})
    server.set_client(fake)
    out = run(server.search_reddit(query="dremio"))
    assert "restrict_sr" not in fake.calls[0][2]
    assert "No posts matched" in out


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        ({"query": "  "}, "query is empty"),
        ({"query": "x" * 600}, "at most 512"),
        ({"query": "x", "after": "page2"}, "after 'page2' is not a cursor"),
        ({"query": "x", "limit": "lots"}, "limit must be an integer"),
        ({"query": "x", "subreddit": "not valid!"}, "not a valid subreddit name"),
    ],
)
def test_search_input_errors_cost_no_request(kwargs, fragment):
    fake = FakeReddit({})
    server.set_client(fake)
    with pytest.raises(ToolError, match=fragment):
        run(server.search_reddit(**kwargs))
    assert fake.calls == []


def test_browse_time_only_for_top_and_controversial():
    fake = FakeReddit({
        ("GET", "/r/x1/top"): ok(listing([t3()])),
        ("GET", "/r/x1/rising"): ok(listing([t3()])),
    })
    server.set_client(fake)
    out = run(server.browse_subreddit(subreddit="x1", listing="top", time="all", limit=0))
    assert fake.calls[0][2]["t"] == "all" and fake.calls[0][2]["limit"] == "1"
    assert out.startswith("r/x1 top time=all: 1 posts (limit clamped to 1)")
    run(server.browse_subreddit(subreddit="x1", listing="rising", time="all"))
    assert "t" not in fake.calls[1][2]


def test_search_subreddits_merges_description_and_name_matches():
    def sub(name, subs):
        return {"kind": "t5", "data": {"display_name": name, "subscribers": subs,
                                       "created_utc": 1727827200, "public_description": f"About {name}."}}

    fake = FakeReddit({
        ("GET", "/subreddits/search"): ok(listing([sub("bigtopic", 50000)])),
        ("GET", "/api/search_reddit_names"): ok({"names": ["bigtopic", "topic_lake"]}),
        ("GET", "/api/info"): ok(listing([sub("topic_lake", 27)])),
    })
    server.set_client(fake)
    out = run(server.search_subreddits(query="r/topic"))
    names = next(c for c in fake.calls if c[1] == "/api/search_reddit_names")
    assert names[2]["query"] == "topic"
    info = next(c for c in fake.calls if c[1] == "/api/info")
    assert info[2]["sr_name"] == "topic_lake"
    assert "By description (1):\nr/bigtopic 50,000 subscribers" in out
    assert 'Names starting with "topic" (1):\nr/topic_lake 27 subscribers' in out


def test_subreddit_info_rules_wiki_sidebar():
    about = {"kind": "t5", "data": {"display_name": "TestSub", "subscribers": 1000, "created_utc": 1727827200,
                                    "title": "Test Sub", "public_description": "Public text.",
                                    "description": "Sidebar " * 100, "subreddit_type": "public",
                                    "submission_type": "self"}}
    rules = {"rules": [{"short_name": "Be kind", "description": "No insults."}, {"short_name": "On topic"}]}
    fake = FakeReddit({
        ("GET", "/r/testsub/about"): ok(about),
        ("GET", "/r/TestSub/about/rules"): ok(rules),
        ("GET", "/r/TestSub/wiki/pages"): ok({"kind": "wikipagelisting", "data": ["config/sidebar", "index", "faq"]}),
    })
    server.set_client(fake)
    out = run(server.get_subreddit_info(subreddit="testsub", include_sidebar=True, sidebar_chars=50))
    assert out.startswith("r/TestSub 1,000 subscribers created 2024-10-02")
    assert "rules (2):\n1. Be kind: No insults.\n2. On topic" in out
    assert "wiki pages (2): index, faq -> get_subreddit_wiki(subreddit, page)" in out
    assert "sidebar (799 chars):" in out and "[+" in out


def test_subreddit_info_wiki_disabled_and_missing_sub():
    about = {"kind": "t5", "data": {"display_name": "x1", "subscribers": 5, "created_utc": 1727827200}}
    fake = FakeReddit({
        ("GET", "/r/x1/about"): ok(about),
        ("GET", "/r/x1/about/rules"): ok({"rules": []}),
        ("GET", "/r/x1/wiki/pages"): ok({"reason": "WIKI_DISABLED", "message": "Forbidden", "error": 403}, 403),
    })
    server.set_client(fake)
    out = run(server.get_subreddit_info(subreddit="x1"))
    assert "wiki: disabled" in out


def test_wiki_page_truncation_and_listing():
    page = {"kind": "wikipage", "data": {"content_md": "# FAQ\n\n" + "line\n" * 300, "revision_date": 1727827200,
                                          "revision_by": {"data": {"name": "example_mod"}}}}
    fake = FakeReddit({
        ("GET", "/r/x1/wiki/faq"): ok(page),
        ("GET", "/r/x1/wiki/pages"): ok({"kind": "wikipagelisting", "data": ["index", "faq"]}),
    })
    server.set_client(fake)
    out = run(server.get_subreddit_wiki(subreddit="x1", page="FAQ", max_chars=500))
    assert out.startswith("r/x1 wiki/faq: ")
    assert "revised 2024-10-02 by u/example_mod" in out
    assert '[truncated: showing' in out and 'page="faq", max_chars=' in out
    out = run(server.get_subreddit_wiki(subreddit="x1", page=""))
    assert out == "r/x1 wiki pages (2):\nindex\nfaq"


def test_get_posts_chunks_orders_and_reports_missing():
    def info(params):
        ids = [x[3:] for x in params["id"].split(",")]
        return ok(listing([t3(id=i, selftext=f"Body {i}") for i in ids if i != "gone1"]))

    fake = FakeReddit({("GET", "/api/info"): info})
    server.set_client(fake)
    refs = [f"p{i}" for i in range(120)] + ["gone1", "https://redd.it/p5"]
    out = run(server.get_posts(posts=refs, body_chars=50))
    assert [len(c[2]["id"].split(",")) for c in fake.calls] == [100, 21]
    assert out.startswith("120 of 121 posts")
    assert out.index("[p0]") < out.index("[p1]") < out.index("[p119]")
    assert "Not found (deleted, private or wrong id): gone1" in out


def test_get_posts_budget_lists_unshown_ids():
    fake = FakeReddit({("GET", "/api/info"): ok(listing([t3(id=f"q{i}", selftext="z" * 3000) for i in range(5)]))})
    server.set_client(fake)
    out = run(server.get_posts(posts=[f"q{i}" for i in range(5)], max_chars=7000))
    assert len(out) <= 7000
    assert "Output budget reached; not shown" in out and "get_posts(posts=[" in out


def test_user_activity_summary_and_suspended():
    about = {"kind": "t2", "data": {"name": "example_user", "created_utc": 1600000000, "link_karma": 10,
                                    "comment_karma": 90, "total_karma": 100, "has_verified_email": True}}
    items = [t3(id="u1", sub="alpha"), comment("u2", "Reply body.", subreddit="beta", link_title="Some thread")]
    fake = FakeReddit({
        ("GET", "/user/example_user/about"): ok(about),
        ("GET", "/user/example_user/comments"): ok(listing(items, after="t1_u2")),
    })
    server.set_client(fake)
    out = run(server.get_user_activity(username="u/example_user", kind="comments", sort="top", time="year"))
    assert fake.calls[1][2]["t"] == "year"
    assert out.startswith("u/example_user  created 2020-09-13")
    assert "karma 100 total (10 post, 90 comment)" in out and "verified email" in out
    assert "Activity by subreddit (2 items: 1 posts, 1 comments" in out
    assert "r/alpha 1 (50%)" in out and "r/beta 1 (50%)" in out
    assert out.endswith("next: after=t1_u2")

    suspended = {"kind": "t2", "data": {"name": "gone_user", "is_suspended": True}}
    fake = FakeReddit({("GET", "/user/gone_user/about"): ok(suspended)})
    server.set_client(fake)
    out = run(server.get_user_activity(username="gone_user"))
    assert out == "u/gone_user is suspended; Reddit hides the profile and its history."
    assert len(fake.calls) == 1


def test_user_not_found():
    fake = FakeReddit({("GET", "/user/nobody_here/about"): ok({"message": "Not Found", "error": 404}, 404)})
    server.set_client(fake)
    with pytest.raises(ToolError, match="u/nobody_here not found"):
        run(server.get_user_activity(username="nobody_here"))


def test_find_other_discussions_for_post_and_url():
    dup = [listing([t3(id="abc123")]), listing([t3(id="d1", sub="othersub")])]
    fake = FakeReddit({("GET", "/duplicates/abc123"): ok(dup)})
    server.set_client(fake)
    out = run(server.find_other_discussions(post_or_url="https://redd.it/abc123"))
    assert out.startswith("other discussions of [abc123]: 1 found")
    assert "[d1] r/othersub" in out

    calls = []

    def info(params):
        calls.append(params["url"])
        return ok(listing([t3(id="h1")] if params["url"].endswith("/") else []))

    fake = FakeReddit({("GET", "/api/info"): info})
    server.set_client(fake)
    out = run(server.find_other_discussions(post_or_url="https://example.com/article"))
    assert calls == ["https://example.com/article", "https://example.com/article/"]
    assert out.startswith("threads that submitted https://example.com/article/: 1 found")


def test_find_other_discussions_none():
    fake = FakeReddit({("GET", "/api/info"): ok(listing([]))})
    server.set_client(fake)
    out = run(server.find_other_discussions(post_or_url="https://example.com/a/"))
    assert "None. Tried: https://example.com/a/, https://example.com/a." in out


def test_link_post_body_in_get_posts():
    d = post(id="lk1", is_self=False, url_overridden_by_dest="https://example.com/x", selftext="Poster's notes.")
    fake = FakeReddit({("GET", "/api/info"): ok(listing([{"kind": "t3", "data": d}]))})
    server.set_client(fake)
    out = run(server.get_posts(posts="lk1"))
    assert "url: https://example.com/x" in out and "body:\nPoster's notes." in out


# ---------------------------------------------------------------- partial failures and deadlines


def test_search_subreddits_rate_limit_on_one_leg_is_an_error():
    fake = FakeReddit({
        ("GET", "/subreddits/search"): raw(429, b"", {"retry-after": "120"}),
        ("GET", "/api/search_reddit_names"): ok({"names": ["x1abc"]}),
        ("GET", "/api/info"): ok(listing([])),
    })
    server.set_client(fake)
    with pytest.raises(ToolError, match="Reddit rate limit reached; retry after 120 s"):
        run(server.search_subreddits(query="x1"))

    fake = FakeReddit({
        ("GET", "/subreddits/search"): ok(listing([])),
        ("GET", "/api/search_reddit_names"): raw(429, b"", {"retry-after": "90"}),
    })
    server.set_client(fake)
    with pytest.raises(ToolError, match="retry after 90 s"):
        run(server.search_subreddits(query="x1"))


def test_search_subreddits_short_query_with_failed_search_is_an_error():
    # A 1-character query skips the name lookup, so a failed description search leaves nothing.
    fake = FakeReddit({("GET", "/subreddits/search"): raw(500, b"oops")})
    server.set_client(fake)
    with pytest.raises(ToolError, match="HTTP 500"):
        run(server.search_subreddits(query="x"))
    assert fake.paths() == ["/subreddits/search", "/subreddits/search"]  # retried once, no name lookup

    fake = FakeReddit({("GET", "/subreddits/search"): RWTransportError()})
    server.set_client(fake)
    with pytest.raises(ToolError, match="Network error reaching Reddit"):
        run(server.search_subreddits(query="x"))


def test_search_subreddits_keeps_partial_results_for_a_plain_http_failure():
    fake = FakeReddit({
        ("GET", "/subreddits/search"): raw(500, b"oops"),
        ("GET", "/api/search_reddit_names"): ok({"names": ["x1abc"]}),
        ("GET", "/api/info"): ok(listing([])),
    })
    server.set_client(fake)
    out = run(server.search_subreddits(query="x1"))
    assert 'Names starting with "x1" (1):\nr/x1abc' in out
    assert "note: description search failed: HTTPError" in out


class HangingReddit(FakeReddit):
    """Every request hangs (real asyncio sleep), as on a dead network."""

    async def _send_raw(self, verb, path, params, data, timeout=None):  # type: ignore[override]
        self.calls.append((verb, path, dict(params), data))
        await asyncio.sleep(30)


def test_tool_deadline_turns_a_hang_into_a_tool_error(monkeypatch):
    monkeypatch.setattr(server, "TOOL_DEADLINE", 0.2)
    server.set_client(HangingReddit())
    t0 = time.perf_counter()
    with pytest.raises(ToolError) as e:
        run(server.browse_subreddit(subreddit="x1"))
    assert time.perf_counter() - t0 < 2
    assert str(e.value).startswith("browse_subreddit gave up after 0.2 s: Reddit is slow or not answering "
                                   "(1 request sent in this call")

    async def go():
        async with Client(server.mcp) as c:
            return await c.call_tool("get_post", {"post": "abc123"}, raise_on_error=False)

    res = run(go())
    assert res.is_error and "get_post gave up after 0.2 s" in res.content[0].text


def test_worst_case_request_fits_inside_the_tool_deadline():
    from reddit_research_mcp import reddit

    worst_request = reddit.REQUEST_TIMEOUT + reddit.RETRY_BACKOFF + reddit.RETRY_TIMEOUT
    assert worst_request < 30
    # a batch may start just before the multi-request cutoff and still end before the deadline
    assert server.MULTI_REQUEST_SECONDS + worst_request < server.TOOL_DEADLINE


def test_get_posts_stops_starting_batches_after_the_cutoff(monkeypatch):
    monkeypatch.setattr(server, "MULTI_REQUEST_SECONDS", -1.0)
    fake = FakeReddit({("GET", "/api/info"): lambda p: ok(listing([t3(id=x[3:]) for x in p["id"].split(",")]))})
    server.set_client(fake)
    out = run(server.get_posts(posts=[f"p{i}" for i in range(150)], body_chars=0))
    assert len(fake.calls) == 1  # the first batch always runs
    assert out.startswith("100 of 150 posts")
    assert "Stopped after -1 s; not fetched (50): get_posts(posts=[p100, p101," in out
