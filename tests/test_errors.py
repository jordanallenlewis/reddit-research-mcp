import asyncio
import gzip
import json
import logging
import re
import uuid

import httpx
import pytest
from fastmcp.exceptions import ToolError
from fixtures import FakeReddit, listing, ok, raw, run, sfw_info, t3
from redditwarp.core.exceptions import ClientCredentialsError
from redditwarp.exceptions import RedditError
from redditwarp.http.exceptions import StatusCodeException, TimeoutException
from redditwarp.http.exceptions import TransportError as RWTransportError

from reddit_research_mcp import reddit, server
from reddit_research_mcp.reddit import (
    DEFAULT_USER_AGENT,
    MAX_RETRY_WAIT,
    REQUEST_TIMEOUT,
    RETRY_BACKOFF,
    RETRY_TIMEOUT,
    AuthError,
    Config,
    HTTPError,
    RateLimitedError,
    RedditAPIError,
    RedditClient,
    RedditLabelError,
    ResponseTooLargeError,
    TransportError,
    describe_exception,
    load_config,
    redact,
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
        ("GET", "/api/info"): sfw_info,
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


# ---------------------------------------------------------------- response and header hardening


@pytest.mark.parametrize(
    "response",
    [
        raw(200, b"<html><body>captcha</body></html>", {"content-type": "text/html"}),
        raw(200, b"", {"content-type": "application/json"}),
        raw(200, b"null", {"content-type": "application/json"}),
        raw(200, b"5", {"content-type": "application/json"}),
        raw(200, b'"just a string"', {"content-type": "application/json"}),
        raw(200, b"[" * 200_000, {"content-type": "application/json"}),  # RecursionError in json
        raw(200, b"\xff\xfe not utf-8", {"content-type": "application/json"}),
    ],
)
def test_unusable_2xx_bodies_become_a_clear_error(response):
    fake = FakeReddit({("GET", "/r/x1/hot"): response})
    with pytest.raises(RedditAPIError, match="non-JSON or empty response"):
        run(fake.get("/r/x1/hot"))
    assert len(fake.calls) == 1  # not retried


@pytest.mark.parametrize(
    "response, html",
    [
        (raw(403, b"<html>blocked, solve this captcha</html>", {"content-type": "text/html; charset=utf-8"}), True),
        (raw(403, b""), False),
        (raw(500 + 99, b"x"), False),
    ],
)
def test_error_status_with_unusable_body_is_http_error(response, html):
    fake = FakeReddit({("GET", "/r/x1/hot"): response})
    with pytest.raises(HTTPError) as e:
        run(fake.get("/r/x1/hot"))
    assert e.value.html is html and "captcha" not in str(e.value)


def _retry_after(value):
    fake = FakeReddit({("GET", "/search"): raw(429, b"", {"retry-after": value})})
    with pytest.raises(RateLimitedError) as e:
        run(fake.get("/search", q="x"))
    return fake, e.value


@pytest.mark.parametrize(
    "value, calls, retry_after",
    [
        ("inf", 1, 60),  # unusable header: fall back to the 60 s default and fail fast
        ("-inf", 1, 60),
        ("nan", 1, 60),
        ("abc", 1, 60),
        ("Wed, 99 Foo 9999 99:99:99 GMT", 1, 60),
        ("1e9", 1, 3600),  # absurd: clamped
        ("99999999999999999999", 1, 3600),
        ("-5", 2, 1),  # negative: treated as 0, one short wait, one retry
    ],
)
def test_hostile_retry_after_never_stalls_or_crashes(value, calls, retry_after):
    fake, err = _retry_after(value)
    assert len(fake.calls) == calls and all(s <= MAX_RETRY_WAIT for s in fake.sleeps)
    assert err.retry_after == retry_after


def test_retry_after_http_date_in_the_past_retries_once():
    fake, _ = _retry_after("Wed, 21 Oct 2015 07:28:00 GMT")
    assert len(fake.calls) == 2 and fake.sleeps == [0.5]


@pytest.mark.parametrize(
    "headers",
    [
        {"x-ratelimit-remaining": "nan", "x-ratelimit-reset": "10", "x-ratelimit-used": "inf"},
        {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "inf"},
        {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "garbage"},
        {"x-ratelimit-remaining": "garbage", "x-ratelimit-reset": "100"},
        {"x-ratelimit-remaining": "5"},
        {"x-ratelimit-used": "99999999999999999999999"},
        {},
    ],
)
def test_garbled_ratelimit_headers_are_ignored_as_a_set(headers):
    fake = FakeReddit({("GET", "/search"): ok(listing([]), headers=headers)})
    for _ in range(30):
        run(fake.get("/search", q="x"))
    assert len(fake.calls) == 30 and fake.sleeps == []  # fails open locally, one request per call
    assert fake.rate_limit_status()["remaining"] is None


def test_absurd_reset_closes_the_window_for_a_bounded_time_only():
    headers = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1e12"}
    fake = FakeReddit({("GET", "/search"): ok(listing([]), headers=headers)})
    run(fake.get("/search", q="x"))
    with pytest.raises(RateLimitedError) as e:
        run(fake.get("/search", q="x"))
    assert e.value.retry_after <= 3600 and len(fake.calls) == 1 and fake.sleeps == []


def test_missing_ratelimit_headers_do_not_loop():
    fake = FakeReddit({("GET", "/search"): raw(429, b"", {"retry-after": "1"})})
    for _ in range(5):
        with pytest.raises(RateLimitedError):
            run(fake.get("/search", q="x"))
    assert len(fake.calls) == 10  # exactly one retry per request, never more
    assert fake.sleeps == [1.0] * 5


def test_gate_wait_and_429_wait_share_one_budget():
    first = ok(listing([]), headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "10"})
    fake = FakeReddit({("GET", "/search"): [first, raw(429, b"", {"retry-after": "10"})]})
    run(fake.get("/search", q="x"))
    with pytest.raises(RateLimitedError):
        run(fake.get("/search", q="x"))
    assert len(fake.sleeps) == 1 and sum(fake.sleeps) <= MAX_RETRY_WAIT


@pytest.mark.parametrize(
    "path",
    ["//other.example/x", "https://other.example/x", "r/x1/hot", "/r/x1/wiki/../../api/v1/me",
     "/search?q=private", "/r/x1/hot#frag", "/r/x1/hot\nX-Evil: 1", "/r\\x1"],
)
def test_paths_that_could_leave_the_api_are_refused_unsent(path):
    fake = FakeReddit({})
    with pytest.raises(HTTPError) as e:
        run(fake.get(path))
    assert fake.calls == [] and e.value.status == 404
    assert "private" not in str(e.value) and "other.example" not in str(e.value)


def test_defensive_wiki_traversal_reads_as_missing_page():
    fake = FakeReddit({})
    server.set_client(fake)
    err = run(server.tool_error(HTTPError(404, "<invalid path>"), subreddit="x1", wiki_page="../a"))
    assert "no wiki page" in str(err) and fake.calls == []


# ---------------------------------------------------------------- real stack over a mock httpx transport

SECRET = "SENTINEL_SECRET_VALUE"
CLIENT_ID = "SENTINEL_CLIENT_ID_VALUE"
REFRESH = "SENTINEL_REFRESH_TOKEN"
ACCESS = "SENTINEL_ACCESS_TOKEN"
TOKEN_BODY = "SENTINEL_TOKEN_BODY_TEXT"
QUERY = "SENTINEL_QUERY_TEXT"
CURSOR = "t3_sentinel9"
CREDENTIAL_SENTINELS = (SECRET, CLIENT_ID, REFRESH, ACCESS, TOKEN_BODY)
USER_CFG = Config("user", CLIENT_ID, SECRET, REFRESH, DEFAULT_USER_AGENT)
APP_CFG = Config("app", CLIENT_ID, SECRET, "", DEFAULT_USER_AGENT)
BEARER_401 = {"www-authenticate": 'Bearer realm="reddit", error="invalid_token"'}


def token_ok(request, expires_in=86400):
    return httpx.Response(
        200, json={"access_token": ACCESS, "token_type": "bearer", "expires_in": expires_in, "scope": "*"}
    )


def api_ok(request):
    return httpx.Response(200, json=listing([t3(id="g1")]))


_REAL_NEW_CLIENT = reddit._new_async_client


class Stack:
    def __init__(self, monkeypatch, api, token=token_ok, cfg=USER_CFG):
        self.token_calls = 0
        self.api_calls = 0
        self.requests = []
        self.sleeps = []

        def handler(request):
            self.requests.append(request)
            if request.url.path == "/api/v1/access_token":
                self.token_calls += 1
                return token(request)
            self.api_calls += 1
            return api(request)

        monkeypatch.setattr(
            reddit, "_new_async_client", lambda: _REAL_NEW_CLIENT(httpx.MockTransport(handler))
        )
        self.client = RedditClient(cfg)

        async def fake_sleep(seconds):
            self.sleeps.append(seconds)

        self.client.sleep = fake_sleep
        server.set_client(self.client)

    @property
    def auth_headers(self):
        return [r.headers.get("authorization") for r in self.requests if r.url.path != "/api/v1/access_token"]


def test_stack_success_caches_the_token(monkeypatch):
    st = Stack(monkeypatch, api_ok)
    run(st.client.get("/search", q="x"))
    run(st.client.get("/search", q="y"))
    assert st.token_calls == 1 and st.api_calls == 2
    assert st.auth_headers == [f"bearer {ACCESS}"] * 2


def test_token_is_renewed_after_expiry(monkeypatch):
    st = Stack(monkeypatch, api_ok, token=lambda r: token_ok(r, expires_in=5))  # inside the 30 s skew
    run(st.client.get("/search", q="x"))
    run(st.client.get("/search", q="y"))
    assert st.token_calls == 2 and st.api_calls == 2


def test_concurrent_calls_share_one_token_fetch(monkeypatch):
    st = Stack(monkeypatch, api_ok)

    async def many():
        return await asyncio.gather(*(st.client.get("/search", q=str(i)) for i in range(8)))

    assert len(asyncio.run(many())) == 8
    assert st.token_calls == 1 and st.api_calls == 8


def test_concurrent_calls_cannot_overspend_a_local_window(monkeypatch):
    headers = {"x-ratelimit-remaining": "3", "x-ratelimit-reset": "100"}
    st = Stack(monkeypatch, lambda r: httpx.Response(200, json=listing([]), headers=headers))
    run(st.client.get("/search", q="x"))  # window now: 3 left

    async def burst():
        return await asyncio.gather(*(st.client.get("/search", q=str(i)) for i in range(6)), return_exceptions=True)

    results = asyncio.run(burst())
    # every response re-states "3 remaining", so the counter is refreshed from the server each time;
    # the point is that nothing raised an unexpected error and no call corrupted the window
    assert all(not isinstance(r, BaseException) or isinstance(r, RateLimitedError) for r in results)
    assert st.client._remaining is None or st.client._remaining >= 0


def test_401_invalid_token_is_renewed_once_and_retried(monkeypatch):
    answers = [httpx.Response(401, headers=BEARER_401), httpx.Response(200, json=listing([]))]
    st = Stack(monkeypatch, lambda r: answers.pop(0))
    assert run(st.client.get("/search", q="x"))["kind"] == "Listing"
    assert st.token_calls == 2 and st.api_calls == 2


def test_401_invalid_token_forever_stops_after_one_retry(monkeypatch):
    st = Stack(monkeypatch, lambda r: httpx.Response(401, headers=BEARER_401))
    with pytest.raises(AuthError) as e:
        run(st.client.get("/search", q="x"))
    assert st.api_calls == 2 and st.token_calls == 2
    assert not any(v in str(e.value) for v in CREDENTIAL_SENTINELS)


def test_401_without_header_drops_the_token_and_retries_once(monkeypatch):
    answers = [
        httpx.Response(401, json={"message": "Unauthorized", "error": 401}),
        httpx.Response(200, json=listing([])),
    ]
    st = Stack(monkeypatch, lambda r: answers.pop(0))
    assert run(st.client.get("/search", q="x"))["kind"] == "Listing"
    assert st.token_calls == 2 and st.api_calls == 2


def test_401_without_header_forever_is_bounded(monkeypatch):
    st = Stack(monkeypatch, lambda r: httpx.Response(401, json={"message": "Unauthorized", "error": 401}))
    with pytest.raises(HTTPError) as e:
        run(st.client.get("/search", q="x"))
    assert e.value.status == 401 and st.api_calls == 2 and st.token_calls == 2


class _Stream(httpx.AsyncByteStream):
    async def __aiter__(self):
        for _ in range(30):  # 3000 bytes, no content-length
            yield b"x" * 100


@pytest.mark.parametrize(
    "make",
    [
        lambda: httpx.Response(200, content=b"x" * 2000, headers={"content-type": "application/json"}),
        lambda: httpx.Response(200, stream=_Stream(), headers={"content-type": "application/json"}),
        lambda: httpx.Response(
            200,
            content=gzip.compress(b"a" * 5000),
            headers={"content-type": "application/json", "content-encoding": "gzip"},
        ),
    ],
    ids=["content-length", "chunked-no-length", "decompression-bomb"],
)
def test_oversized_bodies_are_dropped_before_parsing_and_not_retried(monkeypatch, make):
    monkeypatch.setattr(reddit, "MAX_BODY_BYTES", 1000)
    st = Stack(monkeypatch, lambda r: make())
    with pytest.raises(ResponseTooLargeError, match="discarded"):
        run(st.client.get("/search", q="x"))
    assert st.api_calls == 1


def test_body_under_the_cap_is_parsed(monkeypatch):
    big = json.dumps(listing([t3(id="g1", selftext="y" * 5000)]))
    st = Stack(monkeypatch, lambda r: httpx.Response(200, content=big, headers={"content-type": "application/json"}))
    assert run(st.client.get("/search", q="x"))["kind"] == "Listing"


def test_default_user_agent_is_name_version_and_repo_url_only():
    assert re.fullmatch(
        r"reddit-research-mcp/\d+\.\d+\.\d+ \(\+https://github\.com/[\w.-]+/reddit-research-mcp\)",
        DEFAULT_USER_AGENT,
    )


def test_user_agent_reaches_token_and_api_requests(monkeypatch):
    st = Stack(monkeypatch, api_ok, cfg=load_config({"REDDIT_CLIENT_ID": "abcd1234", "REDDIT_CLIENT_SECRET": "efgh5678",
                                                     "REDDIT_USER_AGENT": "my-research/2.0 (contact: ops team)"}))
    run(st.client.get("/search", q="x"))
    assert {r.headers["user-agent"] for r in st.requests} == {"my-research/2.0 (contact: ops team)"}
    st = Stack(monkeypatch, api_ok, cfg=Config("anonymous"))
    run(st.client.get("/search", q="x"))
    assert {r.headers["user-agent"] for r in st.requests} == {DEFAULT_USER_AGENT}


def test_anonymous_device_id_is_random_not_a_mac_derived_uuid(monkeypatch):
    st = Stack(monkeypatch, api_ok, cfg=Config("anonymous"))
    run(st.client.get("/search", q="x"))
    form = dict(x.split("=", 1) for x in st.requests[0].content.decode().split("&"))
    device = form["device_id"]
    assert 20 <= len(device) <= 30 and f"{uuid.getnode():012x}" not in device.replace("-", "")
    assert st.requests[0].headers["authorization"].startswith("Basic ")


# ---------------------------------------------------------------- nothing credential-shaped reaches logs or tool errors


def token_http_400(request):
    return httpx.Response(400, json={"error": "invalid_grant", "error_description": TOKEN_BODY})


def token_401(request):
    return httpx.Response(401, json={"message": "Unauthorized", "error": 401, "detail": TOKEN_BODY})


def token_500(request):
    return httpx.Response(500, text=TOKEN_BODY)


def token_429(request):
    return httpx.Response(429, text=TOKEN_BODY)


def token_not_json(request):
    return httpx.Response(200, text=f"<html>{TOKEN_BODY} {ACCESS}</html>")


def token_wrong_type(request):
    return httpx.Response(200, json={"access_token": ACCESS, "token_type": TOKEN_BODY, "expires_in": 100})


def token_no_access_token(request):
    return httpx.Response(200, json={"error": "invalid_client", "error_description": f"{TOKEN_BODY} {SECRET}"})


def token_missing_fields(request):
    return httpx.Response(200, json={"token_type": "bearer", "note": TOKEN_BODY})


def token_ratelimited_json(request):
    return httpx.Response(200, json={"error": "access_denied", "error_description": TOKEN_BODY})


def token_timeout(request):
    raise httpx.ConnectTimeout(f"timed out {TOKEN_BODY}", request=request)


def api_timeout(request):
    raise httpx.ReadTimeout(f"read timed out for {request.url}", request=request)


def api_connect_error(request):
    raise httpx.ConnectError(f"[Errno 8] failed {request.url} Authorization: Bearer {ACCESS}", request=request)


def api_proxy_style_error(request):
    raise httpx.ProxyError(f"proxy http://user:{SECRET}@proxy.example:3128/ refused {request.url}", request=request)


def api_403_html(request):
    return httpx.Response(403, html=f"<html>blocked {TOKEN_BODY} {request.headers['authorization']}</html>")


def api_403_json(request):
    return httpx.Response(403, json={"reason": "private", "message": f"Forbidden {TOKEN_BODY}", "error": 403})


def api_429(request):
    return httpx.Response(429, text=TOKEN_BODY, headers={"retry-after": "2", "x-ratelimit-reset": "30"})


def api_429_long(request):
    return httpx.Response(429, text=TOKEN_BODY, headers={"retry-after": "500"})


def api_503(request):
    return httpx.Response(503, text=f"{TOKEN_BODY} {request.url}")


def api_invalid_token_forever(request):
    return httpx.Response(
        401,
        headers={"www-authenticate": f'Bearer realm="reddit", error="invalid_token", error_description="{TOKEN_BODY}"'},
    )


def api_garbage_json(request):
    return httpx.Response(200, text=f'{{"truncated": "{TOKEN_BODY}')


FAILURES = {
    "token-400-invalid-grant": (api_ok, token_http_400),
    "token-401": (api_ok, token_401),
    "token-500": (api_ok, token_500),
    "token-429": (api_ok, token_429),
    "token-not-json": (api_ok, token_not_json),
    "token-wrong-type": (api_ok, token_wrong_type),
    "token-error-200": (api_ok, token_no_access_token),
    "token-missing-fields": (api_ok, token_missing_fields),
    "token-access-denied": (api_ok, token_ratelimited_json),
    "token-timeout": (api_ok, token_timeout),
    "api-timeout": (api_timeout, token_ok),
    "api-connect-error": (api_connect_error, token_ok),
    "api-proxy-error": (api_proxy_style_error, token_ok),
    "api-403-html": (api_403_html, token_ok),
    "api-403-json": (api_403_json, token_ok),
    "api-429": (api_429, token_ok),
    "api-429-long": (api_429_long, token_ok),
    "api-503": (api_503, token_ok),
    "api-401-invalid-token": (api_invalid_token_forever, token_ok),
    "api-garbage-json": (api_garbage_json, token_ok),
}


def _all_log_text(caplog):
    return "\n".join(r.getMessage() for r in caplog.records) + "\n" + caplog.text


def _debug_everything(caplog):
    caplog.set_level(logging.DEBUG)  # root, so httpx/httpcore would show if they were allowed to
    caplog.set_level(logging.DEBUG, logger="reddit_research_mcp")


@pytest.mark.parametrize("cfg", [USER_CFG, APP_CFG], ids=["user", "app"])
@pytest.mark.parametrize("name", sorted(FAILURES))
def test_failure_paths_leak_no_credentials_in_logs_or_tool_errors(monkeypatch, caplog, name, cfg):
    _debug_everything(caplog)
    api, token = FAILURES[name]
    Stack(monkeypatch, api, token=token, cfg=cfg)
    with pytest.raises(ToolError) as e:
        run(server.search_reddit(query=f"{QUERY} dremio", after=CURSOR))
    message = str(e.value)
    assert message  # always actionable, never empty
    blob = _all_log_text(caplog) + "\n" + message
    for sentinel in (*CREDENTIAL_SENTINELS, QUERY, CURSOR):
        assert sentinel.lower() not in blob.lower(), f"{sentinel!r} leaked on path {name}"
    assert "client_secret" not in blob and "refresh_token" not in blob


@pytest.mark.parametrize("name", ["api-403-html", "api-403-json", "api-429", "api-503", "token-400-invalid-grant"])
def test_failure_paths_still_log_the_debug_summary(monkeypatch, caplog, name):
    _debug_everything(caplog)
    api, token = FAILURES[name]
    Stack(monkeypatch, api, token=token)
    with pytest.raises(ToolError):
        run(server.search_reddit(query="dremio"))
    if name.startswith("api"):
        assert re.search(r"GET /search -> \d{3} \d+\.\d\d?s \d+B remaining=", caplog.text)


def test_success_path_logs_only_path_status_latency_and_quota(monkeypatch, caplog):
    _debug_everything(caplog)
    headers = {"x-ratelimit-remaining": "97.0", "x-ratelimit-reset": "300", "x-ratelimit-used": "3"}
    st = Stack(monkeypatch, lambda r: httpx.Response(200, json=listing([t3(id="g1")]), headers=headers))
    out = run(server.search_reddit(query=f"{QUERY} dremio", after=CURSOR))
    assert "[g1]" in out
    assert st.auth_headers == [f"bearer {ACCESS}"]  # the sentinel token really was in play
    lines = [r.getMessage() for r in caplog.records if r.name == "reddit_research_mcp"]
    assert len(lines) == 1 and re.fullmatch(r"GET /search -> 200 \d+\.\d\ds \d+B remaining=97\.0", lines[0])
    blob = _all_log_text(caplog)
    for sentinel in (*CREDENTIAL_SENTINELS, QUERY, CURSOR, "dremio"):
        assert sentinel not in blob
    assert not [r for r in caplog.records if r.name.startswith(("httpx", "httpcore"))]


def test_timeout_log_line_has_path_and_latency_only(monkeypatch, caplog):
    _debug_everything(caplog)
    Stack(monkeypatch, api_timeout)
    with pytest.raises(ToolError, match="did not respond"):
        run(server.search_reddit(query=QUERY))
    lines = [r.getMessage() for r in caplog.records if r.name == "reddit_research_mcp"]
    assert len(lines) == 2 and all(re.fullmatch(r"GET /search timeout after \d+\.\ds", ln) for ln in lines)


def test_config_error_during_a_tool_call_names_variables_not_values(monkeypatch, caplog):
    _debug_everything(caplog)
    monkeypatch.setenv("REDDIT_CLIENT_ID", CLIENT_ID)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("REDDIT_REFRESH_TOKEN", REFRESH)
    server.set_client(RedditClient())
    with pytest.raises(ToolError) as e:
        run(server.search_reddit(query=QUERY))
    assert "REDDIT_CLIENT_SECRET" in str(e.value)
    assert CLIENT_ID not in str(e.value) and REFRESH not in str(e.value) and CLIENT_ID not in _all_log_text(caplog)


# ---------------------------------------------------------------- redaction helpers


def test_redact_scrubs_configured_secrets_headers_urls_and_query_strings():
    Config("user", CLIENT_ID, SECRET, REFRESH)  # registers the values
    text = (
        f"failed https://user:pw@oauth.reddit.com/search?q={QUERY}&after={CURSOR}#x with {SECRET} "
        f"and {REFRESH}; Authorization: Bearer {ACCESS}; Basic dXNlcjpwdw=="
    )
    out = redact(text)
    for bad in (QUERY, CURSOR, SECRET, REFRESH, ACCESS, "pw@", "dXNlcjpwdw"):
        assert bad not in out
    assert "https://oauth.reddit.com/search" in out


def test_describe_exception_redacts_the_whole_cause_chain():
    try:
        try:
            raise OSError(f"cannot reach https://oauth.reddit.com/search?q={QUERY} Bearer {ACCESS}")
        except OSError as cause:
            raise RWTransportError() from cause
    except RWTransportError as exc:
        text = describe_exception(exc)
    assert "OSError" in text and QUERY not in text and ACCESS not in text


def test_token_error_text_from_reddit_is_not_echoed():
    for exc in (
        __import__("redditwarp.auth.exceptions", fromlist=["x"]).OAuth2ResponseError(
            error_name="invalid_grant", description=f"{TOKEN_BODY}\nforged line"
        ),
    ):
        err = RedditClient(Config("app", CLIENT_ID, SECRET))._auth_failure(exc)
        assert TOKEN_BODY not in str(err) and "forged" not in str(err) and "invalid_grant" in str(err)
