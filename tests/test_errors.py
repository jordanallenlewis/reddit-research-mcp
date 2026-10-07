import pytest
from fastmcp.exceptions import ToolError
from fixtures import FakeReddit, listing, ok, raw, run, t3
from redditwarp.core.exceptions import ClientCredentialsError
from redditwarp.exceptions import RedditError
from redditwarp.http.exceptions import StatusCodeException, TimeoutException
from redditwarp.http.exceptions import TransportError as RWTransportError

from reddit_research_mcp import server
from reddit_research_mcp.reddit import (
    MAX_RETRY_WAIT,
    REQUEST_TIMEOUT,
    RETRY_BACKOFF,
    RETRY_TIMEOUT,
    AuthError,
    HTTPError,
    RateLimitedError,
    RedditLabelError,
    TransportError,
    describe_exception,
)

REDIRECT = raw(302, b"", {"location": "https://oauth.reddit.com/subreddits/search?q=dremio"})
HTML_404 = raw(404, b"<html><title>reddit.com: page not found</title></html>", {"content-type": "text/html"})


# ---------------------------------------------------------------- HTTP layer


def test_redirect_and_html_404_become_http_errors():
    fake = FakeReddit({("GET", "/r/dremio/hot"): REDIRECT, ("GET", "/r/x1/hot"): HTML_404})
    with pytest.raises(HTTPError) as e:
        run(fake.get("/r/dremio/hot"))
    assert e.value.status == 302 and "subreddits/search" in e.value.location
    with pytest.raises(HTTPError) as e:
        run(fake.get("/r/x1/hot"))
    assert e.value.status == 404 and e.value.html


@pytest.mark.parametrize("label, status", [("private", 403), ("banned", 404), ("quarantined", 403), ("gold_only", 403)])
def test_json_reason_becomes_label_error(label, status):
    fake = FakeReddit({("GET", "/r/x1/about"): ok({"reason": label, "message": "Forbidden", "error": status}, status)})
    with pytest.raises(RedditLabelError) as e:
        run(fake.get("/r/x1/about"))
    assert e.value.label == label and e.value.status == status


def test_empty_transport_error_is_retried_once_and_described():
    err = RWTransportError()
    assert str(err) == ""  # redditwarp raises these with no message
    fake = FakeReddit({("GET", "/r/x1/hot"): [err, err]})
    with pytest.raises(TransportError) as e:
        run(fake.get("/r/x1/hot"))
    assert len(fake.calls) == 2 and fake.sleeps == [1.0]
    msg = str(e.value)
    assert msg and "Network error reaching Reddit" in msg and "TransportError" in msg


def test_transport_error_cause_is_named():
    try:
        try:
            raise OSError("nodename nor servname provided")
        except OSError as cause:
            raise RWTransportError() from cause
    except RWTransportError as exc:
        text = describe_exception(exc)
    assert text.startswith("TransportError") and "OSError: nodename nor servname provided" in text


def test_timeout_then_success():
    fake = FakeReddit({("GET", "/r/x1/hot"): [TimeoutException(), ok(listing([]))]})
    assert run(fake.get("/r/x1/hot"))["kind"] == "Listing"
    assert len(fake.calls) == 2


def test_timeout_twice_has_clear_message():
    fake = FakeReddit({("GET", "/r/x1/hot"): TimeoutException()})
    with pytest.raises(TransportError, match=r"did not respond .*waited 15 s, then 10 s on a retry"):
        run(fake.get("/r/x1/hot"))


def test_retry_uses_shorter_timeout_so_two_attempts_stay_under_30_s():
    fake = FakeReddit({("GET", "/r/x1/hot"): TimeoutException()})
    with pytest.raises(TransportError):
        run(fake.get("/r/x1/hot"))
    assert fake.timeouts == [REQUEST_TIMEOUT, RETRY_TIMEOUT]
    assert REQUEST_TIMEOUT + RETRY_BACKOFF + RETRY_TIMEOUT < 30


def test_429_with_short_retry_after_waits_and_retries_once():
    fake = FakeReddit({("GET", "/search"): [raw(429, b"", {"retry-after": "3"}), ok(listing([]))]})
    run(fake.get("/search", q="x"))
    assert len(fake.calls) == 2 and fake.sleeps == [3.0]


def test_429_with_long_retry_after_fails_fast():
    fake = FakeReddit({("GET", "/search"): raw(429, b"", {"retry-after": "120"})})
    with pytest.raises(RateLimitedError) as e:
        run(fake.get("/search", q="x"))
    assert len(fake.calls) == 1 and fake.sleeps == []
    assert e.value.retry_after == 120
    assert str(e.value).startswith("Reddit rate limit reached; retry after 120 s")


def test_429_twice_gives_up_after_one_retry():
    fake = FakeReddit({("GET", "/search"): raw(429, b"", {"retry-after": "2", "x-ratelimit-reset": "40"})})
    with pytest.raises(RateLimitedError, match="retry after 2 s"):
        run(fake.get("/search", q="x"))
    assert len(fake.calls) == 2


def test_429_without_retry_after_uses_reset_header():
    fake = FakeReddit({("GET", "/search"): raw(429, b"", {"x-ratelimit-reset": "300"})})
    with pytest.raises(RateLimitedError, match="retry after 300 s"):
        run(fake.get("/search", q="x"))


def test_5xx_retried_once():
    fake = FakeReddit({("GET", "/search"): [raw(503, b"busy"), ok(listing([]))]})
    run(fake.get("/search", q="x"))
    assert len(fake.calls) == 2


def test_exhausted_window_fails_fast_without_a_request():
    headers = {"x-ratelimit-remaining": "0.0", "x-ratelimit-reset": "200", "x-ratelimit-used": "1000"}
    fake = FakeReddit({("GET", "/search"): ok(listing([]), headers=headers)})
    run(fake.get("/search", q="x"))
    with pytest.raises(RateLimitedError) as e:
        run(fake.get("/search", q="x"))
    assert len(fake.calls) == 1 and fake.sleeps == []
    assert 190 <= e.value.retry_after <= 200


def test_nearly_reset_window_waits_briefly():
    headers = {"x-ratelimit-remaining": "0.0", "x-ratelimit-reset": "4", "x-ratelimit-used": "1000"}
    fake = FakeReddit({("GET", "/search"): ok(listing([]), headers=headers)})
    run(fake.get("/search", q="x"))
    run(fake.get("/search", q="x"))
    assert len(fake.calls) == 2 and len(fake.sleeps) == 1 and fake.sleeps[0] <= 4
    assert MAX_RETRY_WAIT >= 4


def test_raw_json_always_sent():
    fake = FakeReddit({("GET", "/search"): ok(listing([]))})
    run(fake.get("/search", q="x", after=None, t=""))
    assert fake.calls[0][2] == {"q": "x", "raw_json": "1"}


def test_token_endpoint_failures_become_auth_errors():
    fake = FakeReddit({("GET", "/search"): ClientCredentialsError("Check your client credentials")})
    with pytest.raises(AuthError, match="anonymous Reddit access token"):
        run(fake.get("/search", q="x"))
    fake = FakeReddit({("GET", "/search"): StatusCodeException(status_code=429)})
    with pytest.raises(RateLimitedError, match="token endpoint"):
        run(fake.get("/search", q="x"))


# ---------------------------------------------------------------- tool error mapping


def test_missing_subreddit_maps_to_actionable_error_with_suggestions():
    fake = FakeReddit({
        ("GET", "/r/dremio/top"): REDIRECT,
        ("GET", "/api/search_reddit_names"): ok({"names": ["dremio_lakehouse", "dremio_x"]}),
    })
    server.set_client(fake)
    with pytest.raises(ToolError) as e:
        run(server.browse_subreddit(subreddit="r/dremio", listing="top", time="year"))
    msg = str(e.value)
    assert msg.startswith("r/dremio does not exist or is private; use search_subreddits")
    assert "similar names: r/dremio_lakehouse, r/dremio_x" in msg


def test_html_404_subreddit_maps_to_missing():
    fake = FakeReddit({("GET", "/r/zzqq/hot"): HTML_404, ("GET", "/api/search_reddit_names"): ok({"names": []})})
    server.set_client(fake)
    with pytest.raises(ToolError, match=r"^r/zzqq does not exist or is private; use search_subreddits"):
        run(server.browse_subreddit(subreddit="zzqq"))


@pytest.mark.parametrize(
    "label, fragment",
    [
        ("private", "r/x1 is private"),
        ("banned", "r/x1 is banned by Reddit"),
        ("quarantined", "r/x1 is quarantined"),
        ("gold_only", "r/x1 is restricted to Reddit Premium"),
        ("SOMETHING_NEW", "Reddit refused the request for r/x1 (SOMETHING_NEW"),
    ],
)
def test_reddit_error_labels(label, fragment):
    rw = RedditError(label=label, explanation="", field="")
    mine = RedditLabelError(label, "", 403)
    for exc in (rw, mine):
        err = run(server.tool_error(exc, subreddit="x1"))
        assert isinstance(err, ToolError) and fragment in str(err)


def test_wiki_errors():
    err = run(server.tool_error(RedditLabelError("WIKI_DISABLED", "", 403), subreddit="x1", wiki_page="index"))
    assert "r/x1 has its wiki disabled" in str(err)
    err = run(server.tool_error(RedditLabelError("PAGE_NOT_FOUND", "", 404), subreddit="x1", wiki_page="faq"))
    assert "r/x1 has no wiki page 'faq'" in str(err) and 'page="")' in str(err)


def test_post_and_user_not_found():
    err = run(server.tool_error(HTTPError(404, "/comments/abc123"), post="abc123"))
    assert str(err).startswith("post abc123 not found")
    err = run(server.tool_error(HTTPError(404, "/user/x1/about"), user="x1"))
    assert str(err).startswith("u/x1 not found")


def test_unexpected_exception_never_empty():
    err = run(server.tool_error(RuntimeError()))
    assert str(err) == "Unexpected error: RuntimeError"
    err = run(server.tool_error(TransportError("")))
    assert str(err) == "TransportError"


def test_listing_survives_bad_item_through_tool():
    bad = {"kind": "t3", "data": {"id": "bad1", "created_utc": "not a date", "num_comments": None}}
    fake = FakeReddit({("GET", "/r/x1/new"): ok(listing([t3(id="g1"), bad, t3(id="g2")], after="t3_g2"))})
    server.set_client(fake)
    out = run(server.browse_subreddit(subreddit="x1", listing="new"))
    assert "[g1]" in out and "[g2]" in out and "[bad1]" in out
    assert out.endswith("next: after=t3_g2")
