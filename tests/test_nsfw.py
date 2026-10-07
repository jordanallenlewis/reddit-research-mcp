import pytest
from fastmcp.exceptions import ToolError
from fixtures import FakeReddit, comment, listing, ok, run, t3

from reddit_research_mcp import format as fmt
from reddit_research_mcp import server

LOCK = server.NSFW_LOCK_ENV


@pytest.fixture(autouse=True)
def _no_lock(monkeypatch):
    monkeypatch.delenv(LOCK, raising=False)


def nsfw_t3(id, **kw):
    return t3(id=id, over_18=True, **kw)


def test_is_nsfw_covers_posts_communities_comments_and_crossposts():
    assert fmt.is_nsfw({"kind": "t3", "data": {"over_18": True}})
    assert fmt.is_nsfw({"kind": "t5", "data": {"over18": True}})
    assert fmt.is_nsfw({"over_18": True})
    assert fmt.is_nsfw({"kind": "t3", "data": {"over_18": False, "crosspost_parent_list": [{"over_18": True}]}})
    assert not fmt.is_nsfw({"kind": "t3", "data": {"over_18": False}})
    assert not fmt.is_nsfw({"kind": "t1", "data": {}})


def test_search_hides_nsfw_by_default_and_shows_on_request():
    fake = FakeReddit({("GET", "/search"): ok(listing([t3(id="s1"), nsfw_t3("n1"), nsfw_t3("n2")], after="t3_n2"))})
    server.set_client(fake)
    out = run(server.search_reddit(query="iceberg"))
    assert "include_over_18" not in fake.calls[0][2]
    assert "[s1]" in out and "[n1]" not in out and "[n2]" not in out
    assert "1 posts (2 NSFW posts hidden; include_nsfw=true shows them)" in out
    assert out.endswith("next: after=t3_n2")

    out = run(server.search_reddit(query="iceberg", include_nsfw=True))
    assert fake.calls[1][2]["include_over_18"] == "on"
    assert "[n1]" in out and "nsfw" in out and "hidden" not in out


def test_browse_all_nsfw_listing_says_why_and_keeps_cursor():
    fake = FakeReddit({("GET", "/r/adult1/hot"): ok(listing([nsfw_t3("a1"), nsfw_t3("a2")], after="t3_a2"))})
    server.set_client(fake)
    out = run(server.browse_subreddit(subreddit="adult1"))
    assert "[a1]" not in out
    assert "No posts to show: all 2 were NSFW (r/adult1 is likely an 18+ community). next: after=t3_a2" in out


def test_server_lock_blocks_nsfw_even_when_requested(monkeypatch):
    monkeypatch.setenv(LOCK, "1")
    fake = FakeReddit({("GET", "/search"): ok(listing([t3(id="s1"), nsfw_t3("n1")]))})
    server.set_client(fake)
    out = run(server.search_reddit(query="iceberg", include_nsfw=True))
    assert "include_over_18" not in fake.calls[0][2]
    assert "[n1]" not in out and f"blocked on this server by {LOCK}" in out


def test_search_subreddits_hides_nsfw_communities_and_unchecked_names():
    desc = listing([
        {"kind": "t5", "data": {"display_name": "safe1", "subscribers": 5, "over18": False}},
        {"kind": "t5", "data": {"display_name": "adult1", "subscribers": 9, "over18": True}},
    ])
    info = ok(listing([
        {"kind": "t5", "data": {"display_name": "safe2", "over18": False}},
        {"kind": "t5", "data": {"display_name": "adult2", "over18": True}},
    ]))
    fake = FakeReddit({
        ("GET", "/subreddits/search"): ok(desc),
        ("GET", "/api/search_reddit_names"): ok({"names": ["safe2", "adult2"]}),
        ("GET", "/api/info"): info,
    })
    server.set_client(fake)
    out = run(server.search_subreddits(query="topic"))
    assert "r/safe1" in out and "r/safe2" in out
    assert "adult1" not in out and "adult2" not in out
    assert "2 NSFW communities hidden" in out

    fake = FakeReddit({
        ("GET", "/subreddits/search"): ok(listing([])),
        ("GET", "/api/search_reddit_names"): ok({"names": ["maybe1"]}),
        ("GET", "/api/info"): RuntimeError("down"),
    })
    server.set_client(fake)
    out = run(server.search_subreddits(query="topic"))
    assert "r/maybe1" not in out and "NSFW status is unknown" in out


def test_missing_subreddit_suggestions_exclude_nsfw_names():
    from fixtures import raw

    info = ok(listing([
        {"kind": "t5", "data": {"display_name": "topic_ok", "over18": False}},
        {"kind": "t5", "data": {"display_name": "topic_nsfw", "over18": True}},
    ]))
    fake = FakeReddit({
        ("GET", "/r/topic/hot"): raw(302, b"", {"location": "https://www.reddit.com/subreddits/search?q=topic"}),
        ("GET", "/api/search_reddit_names"): ok({"names": ["topic_nsfw", "topic_ok"]}),
        ("GET", "/api/info"): info,
    })
    server.set_client(fake)
    with pytest.raises(ToolError) as e:
        run(server.browse_subreddit(subreddit="topic"))
    assert "similar names: r/topic_ok" in str(e.value) and "topic_nsfw" not in str(e.value)


def test_subreddit_info_and_wiki_blocked_for_nsfw_community():
    about = ok({"kind": "t5", "data": {"display_name": "adult1", "over18": True, "public_description": "x"}})
    fake = FakeReddit({("GET", "/r/adult1/about"): about})
    server.set_client(fake)
    out = run(server.get_subreddit_info(subreddit="adult1"))
    assert out.startswith("r/adult1 is marked NSFW (18+) and is hidden by default")
    assert fake.paths() == ["/r/adult1/about"]  # no rules or wiki requests
    out = run(server.get_subreddit_wiki(subreddit="adult1"))
    assert "Its wiki is not shown." in out and fake.paths()[-1] == "/r/adult1/about"


def test_get_post_and_expand_blocked_for_nsfw_post():
    payload = [listing([nsfw_t3("abc123", title="Adult title")]), listing([comment("c1", "hi")])]
    fake = FakeReddit({("GET", "/comments/abc123"): ok(payload)})
    server.set_client(fake)
    out = run(server.get_post(post="abc123"))
    assert out.startswith("post abc123 is marked NSFW") and "Adult title" not in out
    out = run(server.get_post(post="abc123", include_nsfw=True))
    assert "Adult title" in out

    fake = FakeReddit({("GET", "/api/info"): ok(listing([nsfw_t3("abc123")]))})
    server.set_client(fake)
    out = run(server.expand_comments(post="abc123", comment_ids=["c9"]))
    assert "Its comments are not shown." in out and fake.paths() == ["/api/info"]


def test_get_posts_lists_hidden_nsfw_ids():
    fake = FakeReddit({("GET", "/api/info"): ok(listing([t3(id="p1"), nsfw_t3("p2")]))})
    server.set_client(fake)
    out = run(server.get_posts(posts=["p1", "p2", "p3"]))
    assert out.startswith("1 of 3 posts") and "[p2]" not in out
    assert "NSFW, hidden (1; include_nsfw=true shows them): p2" in out
    assert "Not found (deleted, private or wrong id): p3" in out


def test_user_activity_hides_nsfw_items_and_profile():
    about = {"kind": "t2", "data": {"name": "example_user", "created_utc": 1600000000, "subreddit": {
        "over_18": True, "public_description": "explicit profile text"}}}
    items = [t3(id="u1", sub="alpha"), nsfw_t3("u2", sub="adult1"),
             comment("u3", "Adult reply.", subreddit="adult1", over_18=True)]
    fake = FakeReddit({
        ("GET", "/user/example_user/about"): ok(about),
        ("GET", "/user/example_user/overview"): ok(listing(items)),
    })
    server.set_client(fake)
    out = run(server.get_user_activity(username="example_user"))
    assert "explicit profile text" not in out and "profile: hidden (NSFW profile" in out
    assert "adult1" not in out and "[u2]" not in out and "[u3]" not in out
    assert "Activity by subreddit (1 items" in out and "2 NSFW items hidden" in out


def test_other_discussions_filters_nsfw_threads():
    fake = FakeReddit({("GET", "/api/info"): ok(listing([t3(id="d1"), nsfw_t3("d2")]))})
    server.set_client(fake)
    out = run(server.find_other_discussions(post_or_url="https://example.com/article"))
    assert "[d1]" in out and "[d2]" not in out and "1 NSFW thread hidden" in out


def test_tools_default_include_nsfw_false():
    import inspect

    for name in ("search_reddit", "browse_subreddit", "search_subreddits", "get_subreddit_info",
                 "get_subreddit_wiki", "get_post", "expand_comments", "get_posts", "get_user_activity",
                 "find_other_discussions"):
        fn = getattr(server, name)
        fn = getattr(fn, "fn", fn)
        sig = inspect.signature(inspect.unwrap(fn))
        assert sig.parameters["include_nsfw"].default is False, name
