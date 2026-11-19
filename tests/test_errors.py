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


def _manual_clock(fake: FakeReddit, start: float = 1000.0) -> list[float]:
    """Give the client a clock that moves only when it sleeps or the test moves it."""
    now = [start]
    fake.clock = lambda: now[0]

    async def sleep(seconds: float) -> None:
        fake.sleeps.append(seconds)
        now[0] += seconds

    fake.sleep = sleep
    return now


def test_long_429_cools_down_later_calls_without_sending():
    # Reddit said "wait 120 s" with no x-ratelimit headers: later calls must not hit it again.
    fake = FakeReddit({("GET", "/search"): raw(429, b"", {"retry-after": "120"})})
    now = _manual_clock(fake)
    with pytest.raises(RateLimitedError):
        run(fake.get("/search", q="x"))
    now[0] += 30
    for _ in range(2):
        with pytest.raises(RateLimitedError) as e:
            run(fake.get("/search", q="x"))
        assert e.value.retry_after == 90
    assert len(fake.calls) == 1 and fake.sleeps == []
    assert fake.rate_limit_status()["remaining"] == 0


def test_requests_flow_again_once_the_429_cool_down_is_over():
    fake = FakeReddit({("GET", "/search"): [raw(429, b"", {"retry-after": "120"}), ok(listing([]))]})
    now = _manual_clock(fake)
    with pytest.raises(RateLimitedError):
        run(fake.get("/search", q="x"))
    now[0] += 120
    assert run(fake.get("/search", q="x"))["kind"] == "Listing"
    assert len(fake.calls) == 2 and fake.sleeps == []


def test_failed_429_retry_makes_the_next_call_wait_before_sending():
    short = raw(429, b"", {"retry-after": "2"})
    fake = FakeReddit({("GET", "/search"): [short, short, ok(listing([]))]})
    _manual_clock(fake)
    with pytest.raises(RateLimitedError, match="retry after 2 s"):
        run(fake.get("/search", q="x"))
    run(fake.get("/search", q="x"))  # waits out the 2 s at the gate, then sends
    assert len(fake.calls) == 3 and fake.sleeps == [2.0, 2.0]


def test_short_429_wait_in_one_call_does_not_erase_a_longer_cool_down_from_another():
    # Call A sleeps 2 s before its retry; meanwhile call B gets "retry after 120" and gives up.
    # When A wakes, B's cool-down must still hold: A's retry and later calls stay unsent.
    fake = FakeReddit({
        ("GET", "/search"): raw(429, b"", {"retry-after": "2"}),
        ("GET", "/r/x1/hot"): raw(429, b"", {"retry-after": "120"}),
    })
    now = _manual_clock(fake)
    other_ran = []

    async def sleep(seconds: float) -> None:
        fake.sleeps.append(seconds)
        if not other_ran:
            other_ran.append(True)
            with pytest.raises(RateLimitedError):
                await fake.get("/r/x1/hot")
        now[0] += seconds

    fake.sleep = sleep
    with pytest.raises(RateLimitedError):
        run(fake.get("/search", q="x"))
    with pytest.raises(RateLimitedError):
        run(fake.get("/search", q="x"))
    assert fake.paths() == ["/search", "/r/x1/hot"]


def test_token_endpoint_429_cools_down_later_calls_without_sending():
    # The token fetch got a 429: later calls must wait it out, not ask the token endpoint again.
    fake = FakeReddit({("GET", "/search"): StatusCodeException(status_code=429)})
    now = _manual_clock(fake)
    with pytest.raises(RateLimitedError, match="token endpoint") as first:
        run(fake.get("/search", q="x"))
    assert first.value.retry_after == 60
    now[0] += 20
    for _ in range(2):
        with pytest.raises(RateLimitedError) as e:
            run(fake.get("/search", q="x"))
        assert e.value.retry_after == 40
    assert len(fake.calls) == 1 and fake.sleeps == []
    assert fake.rate_limit_status()["remaining"] == 0


def test_requests_flow_again_once_the_token_endpoint_cool_down_is_over():
    fake = FakeReddit({("GET", "/search"): [StatusCodeException(status_code=429), ok(listing([]))]})
    now = _manual_clock(fake)
    with pytest.raises(RateLimitedError):
        run(fake.get("/search", q="x"))
    now[0] += 60
    assert run(fake.get("/search", q="x"))["kind"] == "Listing"
    assert len(fake.calls) == 2 and fake.sleeps == []


def test_token_endpoint_429_never_shortens_a_longer_known_cool_down():
    # The last request of a window that resets in 300 s gets a token 429: the empty window still holds.
    window = {"x-ratelimit-remaining": "1", "x-ratelimit-reset": "300"}
    fake = FakeReddit({
        ("GET", "/r/x1/hot"): ok(listing([]), headers=window),
        ("GET", "/search"): StatusCodeException(status_code=429),
    })
    now = _manual_clock(fake)
    run(fake.get("/r/x1/hot"))
    with pytest.raises(RateLimitedError, match="token endpoint"):
        run(fake.get("/search", q="x"))
    now[0] += 100
    with pytest.raises(RateLimitedError) as e:
        run(fake.get("/r/x1/hot"))
    assert e.value.retry_after == 200 and fake.paths() == ["/r/x1/hot", "/search"]


def test_token_endpoint_429_holds_60_s_not_to_the_end_of_a_window_with_requests_left():
    # A window with 50 requests left that resets in 500 s is not a cool-down: hold only 60 s.
    window = {"x-ratelimit-remaining": "50", "x-ratelimit-reset": "500"}
    fake = FakeReddit({
        ("GET", "/r/x1/hot"): ok(listing([]), headers=window),
        ("GET", "/search"): [StatusCodeException(status_code=429), ok(listing([]))],
    })
    now = _manual_clock(fake)
    run(fake.get("/r/x1/hot"))
    with pytest.raises(RateLimitedError, match="retry after 60 s"):
        run(fake.get("/search", q="x"))
    now[0] += 30
    with pytest.raises(RateLimitedError) as e:
        run(fake.get("/search", q="x"))
    assert e.value.retry_after == 30
    now[0] += 30
    assert run(fake.get("/search", q="x"))["kind"] == "Listing"
    assert fake.paths() == ["/r/x1/hot", "/search", "/search"] and fake.sleeps == []


def test_waking_429_retry_does_not_clear_a_token_endpoint_cool_down_from_another_call():
    # Call A sleeps 2 s before retrying a short 429; meanwhile call B's token fetch gets a 429.
    # A known window (50 left, resets in 500 s) is in force. When A wakes, B's cool-down must hold.
    window = {"x-ratelimit-remaining": "50", "x-ratelimit-reset": "500"}
    fake = FakeReddit({
        ("GET", "/r/x0/hot"): ok(listing([]), headers=window),
        ("GET", "/search"): raw(429, b"", {"retry-after": "2", **window}),
        ("GET", "/r/x1/hot"): StatusCodeException(status_code=429),
    })
    now = _manual_clock(fake)
    run(fake.get("/r/x0/hot"))
    other_ran = []

    async def sleep(seconds: float) -> None:
        fake.sleeps.append(seconds)
        if not other_ran:
            other_ran.append(True)
            with pytest.raises(RateLimitedError, match="token endpoint"):
                await fake.get("/r/x1/hot")
        now[0] += seconds

    fake.sleep = sleep
    with pytest.raises(RateLimitedError):
        run(fake.get("/search", q="x"))
    with pytest.raises(RateLimitedError):
        run(fake.get("/r/x1/hot"))
    assert fake.paths() == ["/r/x0/hot", "/search", "/r/x1/hot"]


def test_other_token_endpoint_failures_do_not_start_a_cool_down():
    fake = FakeReddit({
        ("GET", "/search"): [ClientCredentialsError("Check your client credentials"), ok(listing([]))]
    })
    _manual_clock(fake)
    with pytest.raises(AuthError):
        run(fake.get("/search", q="x"))
    assert run(fake.get("/search", q="x"))["kind"] == "Listing"
    assert fake.rate_limit_status()["remaining"] is None


def test_waking_429_retry_keeps_a_token_cool_down_that_left_the_window_end_unchanged():
    # As above, but B's request is the window's last one (resets in 500 s), so B's cool-down keeps
    # the window's end. A must still see that a cool-down started while it slept.
    fake = FakeReddit({
        ("GET", "/r/x0/hot"): ok(listing([]), headers={"x-ratelimit-remaining": "2", "x-ratelimit-reset": "500"}),
        ("GET", "/search"): [raw(429, b"", {"retry-after": "2"}), ok(listing([]))],
        ("GET", "/r/x1/hot"): StatusCodeException(status_code=429),
    })
    now = _manual_clock(fake)
    run(fake.get("/r/x0/hot"))
    other_ran = []

    async def sleep(seconds: float) -> None:
        fake.sleeps.append(seconds)
        if not other_ran:
            other_ran.append(True)
            with pytest.raises(RateLimitedError, match="token endpoint"):
                await fake.get("/r/x1/hot")
        now[0] += seconds

    fake.sleep = sleep
    with pytest.raises(RateLimitedError):
        run(fake.get("/search", q="x"))
    assert fake.paths() == ["/r/x0/hot", "/search", "/r/x1/hot"]


def test_call_waiting_at_the_gate_does_not_send_into_a_cool_down_started_while_it_slept():
    # The window is used up and resets in 4 s, so call A waits at the gate. While A sleeps, call B
    # gets "retry after 120" and gives up. When A wakes, B's cool-down holds: A fails fast, unsent.
    empty = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "4"}
    fake = FakeReddit({
        ("GET", "/r/x0/hot"): ok(listing([]), headers=empty),
        ("GET", "/r/x1/hot"): raw(429, b"", {"retry-after": "120"}),
        ("GET", "/search"): ok(listing([])),
    })
    now = _manual_clock(fake)
    run(fake.get("/r/x0/hot"))
    other_ran = []

    async def sleep(seconds: float) -> None:
        fake.sleeps.append(seconds)
        now[0] += seconds
        if not other_ran:
            other_ran.append(True)
            with pytest.raises(RateLimitedError):
                await fake.get("/r/x1/hot")

    fake.sleep = sleep
    with pytest.raises(RateLimitedError) as e:
        run(fake.get("/search", q="x"))
    assert fake.paths() == ["/r/x0/hot", "/r/x1/hot"]
    assert e.value.retry_after == 120 and fake.sleeps == [4.0]


def test_call_waiting_at_the_gate_waits_out_a_short_cool_down_started_while_it_slept():
    # As above, but B's cool-down is 2 s, which still fits A's wait budget: A waits again, then
    # sends once, after B's cool-down is over.
    empty = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "4"}
    sent_at = []

    def search(params):
        sent_at.append(now[0])
        return ok(listing([]))

    fake = FakeReddit({
        ("GET", "/r/x0/hot"): ok(listing([]), headers=empty),
        ("GET", "/r/x1/hot"): raw(429, b"", {"retry-after": "2"}),
        ("GET", "/search"): search,
    })
    now = _manual_clock(fake)
    run(fake.get("/r/x0/hot"))
    other_ran = []

    async def sleep(seconds: float) -> None:
        fake.sleeps.append(seconds)
        now[0] += seconds
        if not other_ran:
            other_ran.append(True)
            with pytest.raises(RateLimitedError):
                await fake.get("/r/x1/hot")  # 429, waits 2 s, 429 again: a 2 s cool-down from 1006

    fake.sleep = sleep
    assert run(fake.get("/search", q="x"))["kind"] == "Listing"
    assert fake.paths() == ["/r/x0/hot", "/r/x1/hot", "/r/x1/hot", "/search"]
    assert fake.sleeps == [4.0, 2.0, 2.0] and sent_at == [1008.0]


def test_both_gate_waits_count_against_the_429_retry_budget():
    # As above, A waits 4 s and then 2 s at the gate (6 s). Its request then gets "retry after 10":
    # 6 + 10 is over MAX_RETRY_WAIT (15 s), so A gives up instead of retrying.
    empty = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "4"}
    fake = FakeReddit({
        ("GET", "/r/x0/hot"): ok(listing([]), headers=empty),
        ("GET", "/r/x1/hot"): raw(429, b"", {"retry-after": "2"}),
        ("GET", "/search"): [raw(429, b"", {"retry-after": "10"}), ok(listing([]))],
    })
    now = _manual_clock(fake)
    run(fake.get("/r/x0/hot"))
    other_ran = []

    async def sleep(seconds: float) -> None:
        fake.sleeps.append(seconds)
        now[0] += seconds
        if not other_ran:
            other_ran.append(True)
            with pytest.raises(RateLimitedError):
                await fake.get("/r/x1/hot")

    fake.sleep = sleep
    with pytest.raises(RateLimitedError, match="retry after 10 s"):
        run(fake.get("/search", q="x"))
    assert fake.paths() == ["/r/x0/hot", "/r/x1/hot", "/r/x1/hot", "/search"]
    assert fake.sleeps == [4.0, 2.0, 2.0]


def _429_after_gate_wait(gate_wait: str) -> tuple[FakeReddit, list[float]]:
    # The call first waits gate_wait s at the gate for a used-up window, then its request gets
    # a 429 with "retry-after: 0", which the client rounds up to its 0.5 s minimum wait.
    sent_at = []

    def search(params):
        sent_at.append(now[0])
        return raw(429, b"", {"retry-after": "0"}) if len(sent_at) == 1 else ok(listing([]))

    fake = FakeReddit({
        ("GET", "/r/x0/hot"): ok(listing([]), headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": gate_wait}),
        ("GET", "/search"): search,
    })
    now = _manual_clock(fake)
    run(fake.get("/r/x0/hot"))
    return fake, sent_at


def test_429_minimum_wait_that_no_longer_fits_the_budget_fails_without_a_retry():
    # 14.8 s at the gate + the 0.5 s minimum wait is over MAX_RETRY_WAIT (15 s): give up locally
    # instead of sleeping past the budget and sending the retry at 15.3 s.
    fake, sent_at = _429_after_gate_wait("14.8")
    with pytest.raises(RateLimitedError):
        run(fake.get("/search", q="x"))
    assert sent_at == [pytest.approx(1014.8)]
    assert fake.sleeps == [pytest.approx(14.8)]
    assert sum(fake.sleeps) <= MAX_RETRY_WAIT


def test_429_given_up_holds_the_next_call_for_the_minimum_wait():
    # Even with "retry-after: 0", the call after a 429 that was given up is not sent at once.
    fake, sent_at = _429_after_gate_wait("14.8")
    with pytest.raises(RateLimitedError):
        run(fake.get("/search", q="x"))
    run(fake.get("/search", q="x"))
    assert sent_at == [pytest.approx(1014.8), pytest.approx(1015.3)]


def test_429_minimum_wait_that_exactly_fills_the_budget_still_retries():
    # 14.5 s at the gate + the 0.5 s minimum wait is exactly MAX_RETRY_WAIT: the retry goes out.
    fake, sent_at = _429_after_gate_wait("14.5")
    assert run(fake.get("/search", q="x"))["kind"] == "Listing"
    assert sent_at == [1014.5, 1015.0]
    assert fake.sleeps == [14.5, 0.5]


@pytest.mark.parametrize("new_reset, sent", [(4, [1014.0]), (10, [])])
def test_gate_waits_for_a_new_used_up_window_only_within_the_budget_left(new_reset, sent):
    # A waits 10 s at the gate. Meanwhile B's request (sent as the old window ends) shows a new,
    # used-up window. A waits again only if both waits together stay within MAX_RETRY_WAIT (15 s);
    # otherwise it fails fast, unsent.
    sent_at = []

    def search(params):
        sent_at.append(now[0])
        return ok(listing([]))

    fake = FakeReddit({
        ("GET", "/r/x0/hot"): ok(listing([]), headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "10"}),
        ("GET", "/r/x1/hot"): ok(
            listing([]), headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(new_reset)}
        ),
        ("GET", "/search"): search,
    })
    now = _manual_clock(fake)
    run(fake.get("/r/x0/hot"))
    other_ran = []

    async def sleep(seconds: float) -> None:
        fake.sleeps.append(seconds)
        now[0] += seconds
        if not other_ran:
            other_ran.append(True)
            await fake.get("/r/x1/hot")

    fake.sleep = sleep
    if sent:
        assert run(fake.get("/search", q="x"))["kind"] == "Listing"
        assert fake.sleeps == [10.0, 4.0]
    else:
        with pytest.raises(RateLimitedError) as e:
            run(fake.get("/search", q="x"))
        assert e.value.retry_after == 10 and fake.sleeps == [10.0]
    assert sent_at == sent


def test_parallel_call_waits_out_another_calls_retried_429_before_sending():
    # Call A gets "retry after 10" (short enough to retry) and sleeps. Four seconds in, call B
    # starts. Reddit asked this client to wait 10 s, so B must not be sent before 1010.
    sent_at: dict[str, list[float]] = {"/search": [], "/r/x1/hot": []}

    def sender(path, *responses):
        queue = list(responses)

        def send(params):
            sent_at[path].append(now[0])
            return queue.pop(0) if len(queue) > 1 else queue[0]

        return send

    fake = FakeReddit({
        ("GET", "/search"): sender("/search", raw(429, b"", {"retry-after": "10"}), ok(listing([]))),
        ("GET", "/r/x1/hot"): sender("/r/x1/hot", ok(listing([]))),
    })
    now = _manual_clock(fake)
    other_ran = []

    async def sleep(seconds: float) -> None:
        fake.sleeps.append(seconds)
        if not other_ran:
            other_ran.append(True)
            now[0] += 4
            assert (await fake.get("/r/x1/hot"))["kind"] == "Listing"  # waits 6 s at the gate
            now[0] += seconds - 4 - 6  # A's own 10 s are over too; the clock never runs back
            return
        now[0] += seconds

    fake.sleep = sleep
    assert run(fake.get("/search", q="x"))["kind"] == "Listing"
    assert sent_at == {"/search": [1000.0, 1010.0], "/r/x1/hot": [1010.0]}
    assert fake.sleeps == [10.0, 6.0]


def test_parallel_call_during_a_retried_429_still_honours_a_longer_known_window():
    # A's 429 says "retry after 2" but also that the window is used up for 40 s. A retries once
    # after its 2 s (as before); B, started during that wait, fails fast with the window's 40 s,
    # unsent: the short wait never shortens a longer window Reddit already announced.
    headers = {"retry-after": "2", "x-ratelimit-remaining": "0", "x-ratelimit-reset": "40"}
    fake = FakeReddit({
        ("GET", "/search"): [raw(429, b"", headers), ok(listing([]))],
        ("GET", "/r/x1/hot"): ok(listing([])),
    })
    now = _manual_clock(fake)
    errors = []

    async def sleep(seconds: float) -> None:
        fake.sleeps.append(seconds)
        if not errors:
            with pytest.raises(RateLimitedError) as e:
                await fake.get("/r/x1/hot")
            errors.append(e.value)
        now[0] += seconds

    fake.sleep = sleep
    assert run(fake.get("/search", q="x"))["kind"] == "Listing"
    assert fake.paths() == ["/search", "/search"]
    assert errors[0].retry_after == 40 and fake.sleeps == [2.0]


@pytest.mark.parametrize(
    "retry_headers",
    [{}, {"x-ratelimit-remaining": "50", "x-ratelimit-reset": "500"}],
    ids=["retry-answer-without-window", "retry-answer-with-new-window"],
)
def test_retried_429_keeps_a_used_up_window_for_other_calls_while_its_retry_is_in_flight(retry_headers):
    # A's 429 at 1000 says "retry after 2" and that the window is used up until 1040. A retries
    # once at 1002, as Reddit asked; the retry is in flight for 3 s. B starts at 1003: the window
    # is still used up, so B fails locally with the 37 s left and is never sent. Only A's retry
    # may pass the used-up window, once.
    vt = None
    sent: list[tuple[str, float]] = []
    used_up = {"retry-after": "2", "x-ratelimit-remaining": "0", "x-ratelimit-reset": "40"}
    fake = FakeReddit({
        ("GET", "/search"): [raw(429, b"", used_up), ok(listing([]), headers=retry_headers)],
        ("GET", "/r/x1/hot"): ok(listing([])),
    })
    send = fake._send_raw

    async def in_flight(verb, path, params, data, timeout=None):
        sent.append((path, vt.now))
        if path == "/search" and len(sent) == 2:
            await vt.wait(3)
        return await send(verb, path, params, data, timeout)

    fake._send_raw = in_flight
    vt = _VirtualTime(fake)

    async def b_later():
        await vt.wait(3)
        with pytest.raises(RateLimitedError) as e:
            await fake.get("/r/x1/hot")
        return e.value

    a, b = vt.run(fake.get("/search", q="x"), b_later())
    assert a["kind"] == "Listing"
    assert sent == [("/search", 1000.0), ("/search", 1002.0)]
    assert b.retry_after == 37 and "window is used up" in str(b)


def test_retried_429_answered_without_window_headers_leaves_the_used_up_window_in_force():
    # A's 429 says "retry after 2" and that the window is used up for 40 s; its retry succeeds
    # with no x-ratelimit headers. Nothing has said the window was renewed, so the next call
    # (at 1002) fails locally with the 38 s left instead of being sent.
    used_up = {"retry-after": "2", "x-ratelimit-remaining": "0", "x-ratelimit-reset": "40"}
    fake = FakeReddit({
        ("GET", "/search"): [raw(429, b"", used_up), ok(listing([]))],
        ("GET", "/r/x1/hot"): ok(listing([])),
    })
    _manual_clock(fake)
    assert run(fake.get("/search", q="x"))["kind"] == "Listing"
    with pytest.raises(RateLimitedError) as e:
        run(fake.get("/r/x1/hot"))
    assert e.value.retry_after == 38 and fake.paths() == ["/search", "/search"]
    assert fake.rate_limit_status()["remaining"] == 0


def test_retry_after_the_windows_last_request_does_not_reopen_the_window():
    # The window has 1 request left until 1050. A spends it, gets "retry after 3" and retries once
    # at 1003 (as before). The window is still used up afterwards: a call at 1003 is not sent.
    window = {"x-ratelimit-remaining": "1", "x-ratelimit-reset": "50"}
    fake = FakeReddit({
        ("GET", "/r/x1/hot"): ok(listing([]), headers=window),
        ("GET", "/search"): [raw(429, b"", {"retry-after": "3"}), ok(listing([]))],
        ("GET", "/r/x2/hot"): ok(listing([])),
    })
    _manual_clock(fake)
    run(fake.get("/r/x1/hot"))
    assert run(fake.get("/search", q="x"))["kind"] == "Listing"
    with pytest.raises(RateLimitedError) as e:
        run(fake.get("/r/x2/hot"))
    assert e.value.retry_after == 47 and fake.paths() == ["/r/x1/hot", "/search", "/search"]


def test_window_waiver_does_not_survive_a_sleep_at_the_gate():
    # The waiver is for the retry Reddit asked for, at the moment it arrives. If the call has to
    # sleep at the gate first (another cool-down) and a response meanwhile says the window is used
    # up until 1030, the call must honour that window, not pass it with the stale waiver.
    fake = FakeReddit({})
    now = _manual_clock(fake)
    fake._hold_until, fake._holds = 1005.0, 1

    async def sleep(seconds: float) -> None:
        fake.sleeps.append(seconds)
        fake._note_limits({"x-ratelimit-remaining": "0", "x-ratelimit-reset": "30"})
        now[0] += seconds

    fake.sleep = sleep
    with pytest.raises(RateLimitedError) as e:
        run(fake._gate(15.0, 0.0, waive_window=True))
    assert fake.sleeps == [5.0] and e.value.retry_after == 25 and "window is used up" in str(e.value)


def test_window_replaced_during_the_429_wait_is_honoured_by_the_retry():
    # A: 429 at 1000, retry-after 2, window used up to 1040. At 1001 an in-flight answer says the
    # window is used up until 1061. A's retry must not be sent at 1002.
    used_up = {"retry-after": "2", "x-ratelimit-remaining": "0", "x-ratelimit-reset": "40"}
    fake = FakeReddit({("GET", "/search"): [raw(429, b"", used_up), ok(listing([]))]})
    now = _manual_clock(fake)

    async def sleep(seconds):
        fake.sleeps.append(seconds)
        now[0] += 1
        fake._note_limits({"x-ratelimit-remaining": "0", "x-ratelimit-reset": "60"})
        now[0] += seconds - 1

    fake.sleep = sleep
    with pytest.raises(RateLimitedError) as e:
        run(fake.get("/search", q="x"))
    assert fake.paths() == ["/search"] and e.value.retry_after == 59


def test_other_calls_short_429_during_the_wait_cancels_the_waiver():
    # B, in flight 1000-1001, gets "retry after 2" (no window headers); A got "retry after 5,
    # window used up for 40 s" at 1000. B's 429 during A's wait cancels A's waiver: neither
    # retry may go out inside the window.
    vt = None
    sent = []
    fake = FakeReddit({
        ("GET", "/r/x1/hot"): [raw(429, b"", {"retry-after": "2"}), ok(listing([]))],
        ("GET", "/search"): [raw(429, b"", {"retry-after": "5", "x-ratelimit-remaining": "0",
                                             "x-ratelimit-reset": "40"}), ok(listing([]))],
    })
    send = fake._send_raw

    async def in_flight(verb, path, params, data, timeout=None):
        sent.append((path, vt.now))
        if path == "/r/x1/hot" and len(sent) == 1:
            await vt.wait(1)
        return await send(verb, path, params, data, timeout)

    fake._send_raw = in_flight
    vt = _VirtualTime(fake)

    async def call(path, **kw):
        try:
            return await fake.get(path, **kw)
        except RateLimitedError as e:
            return e

    b, a = vt.run(call("/r/x1/hot"), call("/search", q="x"))
    assert isinstance(a, RateLimitedError) and isinstance(b, RateLimitedError)
    assert sent == [("/r/x1/hot", 1000.0), ("/search", 1000.0)]


def test_early_wake_waived_retry_then_other_call_held():
    # A's sleep ends a microsecond early: its retry is still sent once, and the window stays
    # used up for the next call.
    used_up = {"retry-after": "2", "x-ratelimit-remaining": "0", "x-ratelimit-reset": "40"}
    fake = FakeReddit({("GET", "/search"): [raw(429, b"", used_up), ok(listing([]))],
                       ("GET", "/r/x1/hot"): ok(listing([]))})
    _early_waking_clock(fake)
    assert run(fake.get("/search", q="x"))["kind"] == "Listing"
    with pytest.raises(RateLimitedError):
        run(fake.get("/r/x1/hot"))
    st = fake.rate_limit_status()
    assert st["remaining"] == 0 and fake.paths() == ["/search", "/search"]


def test_retried_429_holds_parallel_calls_only_for_its_wait_when_the_window_has_requests_left():
    # A's 429 says "retry after 2" while the window still has 50 requests and resets in 500 s.
    # B, started during A's wait, waits the 2 s and is sent: a window with requests left is not
    # a cool-down, so B never fails with the window's 500 s.
    window = {"x-ratelimit-remaining": "50", "x-ratelimit-reset": "500"}
    sent_at = []

    def other(params):
        sent_at.append(now[0])
        return ok(listing([]))

    fake = FakeReddit({
        ("GET", "/search"): [raw(429, b"", {"retry-after": "2", **window}), ok(listing([]))],
        ("GET", "/r/x1/hot"): other,
    })
    now = _manual_clock(fake)
    other_ran = []

    async def sleep(seconds: float) -> None:
        fake.sleeps.append(seconds)
        if not other_ran:
            other_ran.append(True)
            assert (await fake.get("/r/x1/hot"))["kind"] == "Listing"
            return  # B's gate wait already moved the clock past A's 2 s
        now[0] += seconds

    fake.sleep = sleep
    assert run(fake.get("/search", q="x"))["kind"] == "Listing"
    assert sent_at == [1002.0] and fake.sleeps == [2.0, 2.0]
    assert fake.paths() == ["/search", "/r/x1/hot", "/search"]


class _VirtualTime:
    """Real asyncio concurrency on a clock that jumps to the next wake-up when every task is idle."""

    def __init__(self, fake: FakeReddit, start: float = 1000.0) -> None:
        self.now = start
        self.fake = fake
        self._timers: list[tuple[float, int, asyncio.Future]] = []
        fake.clock = lambda: self.now

        async def sleep(seconds: float) -> None:
            fake.sleeps.append(seconds)
            await self.wait(seconds)

        fake.sleep = sleep

    async def wait(self, seconds: float) -> None:
        fut = asyncio.get_running_loop().create_future()
        self._timers.append((self.now + seconds, len(self._timers), fut))
        await fut

    def run(self, *coros):
        async def main():
            tasks = [asyncio.create_task(c) for c in coros]
            while not all(t.done() for t in tasks):
                for _ in range(50):
                    await asyncio.sleep(0)
                live = [t for t in self._timers if not t[2].done()]
                if not live:
                    assert all(t.done() for t in tasks), "tasks blocked with no timer pending"
                    break
                when, _, fut = min(live, key=lambda t: (t[0], t[1]))
                self.now = max(self.now, when)
                fut.set_result(None)
            return [t.result() for t in tasks]

        return asyncio.run(main())


def test_second_short_429_does_not_clear_a_longer_hold_from_an_earlier_retried_429():
    # D's request is in flight for 3 s. Meanwhile A gets "retry after 10" at 1000, so calls are
    # held to 1010. D's answer (1003) is "retry after 2". D must not clear A's hold when it wakes
    # at 1005: D's retry and call E (started at 1006) are sent at 1010, not inside A's wait.
    vt = None
    sent: list[tuple[str, float]] = []
    fake = FakeReddit({
        ("GET", "/search"): [raw(429, b"", {"retry-after": "10"}), ok(listing([]))],
        ("GET", "/r/x1/hot"): [raw(429, b"", {"retry-after": "2"}), ok(listing([]))],
        ("GET", "/r/x2/hot"): ok(listing([])),
    })
    send = fake._send_raw
    first_d = []

    async def in_flight(verb, path, params, data, timeout=None):
        sent.append((path, vt.now))
        if path == "/r/x1/hot" and not first_d:
            first_d.append(True)
            await vt.wait(3)
        return await send(verb, path, params, data, timeout)

    fake._send_raw = in_flight
    vt = _VirtualTime(fake)

    async def e_later():
        await vt.wait(6)
        return await fake.get("/r/x2/hot")

    results = vt.run(fake.get("/r/x1/hot"), fake.get("/search", q="x"), e_later())
    assert all(r["kind"] == "Listing" for r in results)
    assert sent[:2] == [("/r/x1/hot", 1000.0), ("/search", 1000.0)]
    assert sorted(sent[2:]) == [("/r/x1/hot", 1010.0), ("/r/x2/hot", 1010.0), ("/search", 1010.0)]


@pytest.mark.parametrize(
    "hold, retry_after",
    [(raw(429, b"", {"retry-after": "120"}), 120), (StatusCodeException(status_code=429), 60)],
    ids=["given-up-429", "token-endpoint-429"],
)
@pytest.mark.parametrize(
    "d_headers",
    [{}, {"x-ratelimit-remaining": "50", "x-ratelimit-reset": "500"},
     {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "5"}],
    ids=["no-window", "window-with-requests-left", "short-used-up-window"],
)
def test_short_429_does_not_clear_another_calls_cool_down_in_force_when_it_arrived(hold, retry_after, d_headers):
    # D's request is in flight for 3 s; meanwhile B's 429 starts a long cool-down at 1000. D's own
    # answer (1003) is "retry after 2". Waking at 1005, D must leave B's cool-down alone: its retry
    # fails locally with the time left, and a call started at 1006 is not sent either. Window
    # headers on D's own 429 must not replace B's cool-down.
    vt = None
    sent: list[tuple[str, float]] = []
    fake = FakeReddit({
        ("GET", "/r/x1/hot"): raw(429, b"", {"retry-after": "2", **d_headers}),
        ("GET", "/search"): hold,
        ("GET", "/r/x2/hot"): ok(listing([])),
    })
    send = fake._send_raw

    async def in_flight(verb, path, params, data, timeout=None):
        sent.append((path, vt.now))
        if path == "/r/x1/hot":
            await vt.wait(3)
        return await send(verb, path, params, data, timeout)

    fake._send_raw = in_flight
    vt = _VirtualTime(fake)

    async def expect_held(coro):
        with pytest.raises(RateLimitedError) as e:
            await coro
        return e.value.retry_after

    async def e_later():
        await vt.wait(6)
        return await expect_held(fake.get("/r/x2/hot"))

    waits = vt.run(expect_held(fake.get("/r/x1/hot")), expect_held(fake.get("/search", q="x")), e_later())
    assert sent == [("/r/x1/hot", 1000.0), ("/search", 1000.0)]
    assert waits == [retry_after - 5, retry_after, retry_after - 6]


@pytest.mark.parametrize(
    "hold, retry_after",
    [(raw(429, b"", {"retry-after": "120"}), 120), (StatusCodeException(status_code=429), 60)],
    ids=["given-up-429", "token-endpoint-429"],
)
@pytest.mark.parametrize(
    "d_headers",
    [{"x-ratelimit-remaining": "50", "x-ratelimit-reset": "500"},
     {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1"},
     {"x-ratelimit-remaining": "3", "x-ratelimit-reset": "2"}],
    ids=["window-with-requests-left", "used-up-window-ending-sooner", "short-window-with-requests-left"],
)
def test_window_headers_from_a_call_in_flight_do_not_lift_another_calls_cool_down(hold, retry_after, d_headers):
    # D's request is in flight for 3 s; meanwhile B's 429 starts a long cool-down at 1000. D's own
    # answer (1003) is a 200 whose window headers say requests are left, or that the window ends
    # sooner. Reddit's request to wait still stands: a call started at 1006 is not sent.
    vt = None
    sent: list[tuple[str, float]] = []
    fake = FakeReddit({
        ("GET", "/r/x1/hot"): ok(listing([]), headers=d_headers),
        ("GET", "/search"): hold,
        ("GET", "/r/x2/hot"): ok(listing([])),
    })
    send = fake._send_raw

    async def in_flight(verb, path, params, data, timeout=None):
        sent.append((path, vt.now))
        if path == "/r/x1/hot":
            await vt.wait(3)
        return await send(verb, path, params, data, timeout)

    fake._send_raw = in_flight
    vt = _VirtualTime(fake)

    async def expect_held(coro):
        with pytest.raises(RateLimitedError) as e:
            await coro
        return e.value

    async def e_later():
        await vt.wait(6)
        return await expect_held(fake.get("/r/x2/hot"))

    d, b, e = vt.run(fake.get("/r/x1/hot"), expect_held(fake.get("/search", q="x")), e_later())
    assert d["kind"] == "Listing"
    assert sent == [("/r/x1/hot", 1000.0), ("/search", 1000.0)]
    assert b.retry_after == retry_after and e.retry_after == retry_after - 6
    assert "asked this client to wait" in str(e) and "window is used up" not in str(e)


def test_window_headers_from_a_call_in_flight_do_not_cut_a_retried_429s_wait():
    # A gets "retry after 10" at 1000 and sleeps before its retry, so calls are held to 1010. D's
    # request, in flight since 1000, answers at 1003 with a 200 saying 50 requests are left. E
    # starts at 1004: it waits at the gate and is sent at 1010 with A's retry, not at 1004.
    vt = None
    sent: list[tuple[str, float]] = []
    fake = FakeReddit({
        ("GET", "/search"): [raw(429, b"", {"retry-after": "10"}), ok(listing([]))],
        ("GET", "/r/x1/hot"): ok(listing([]), headers={"x-ratelimit-remaining": "50", "x-ratelimit-reset": "500"}),
        ("GET", "/r/x2/hot"): ok(listing([])),
    })
    send = fake._send_raw

    async def in_flight(verb, path, params, data, timeout=None):
        sent.append((path, vt.now))
        if path == "/r/x1/hot":
            await vt.wait(3)
        return await send(verb, path, params, data, timeout)

    fake._send_raw = in_flight
    vt = _VirtualTime(fake)

    async def e_later():
        await vt.wait(4)
        return await fake.get("/r/x2/hot")

    results = vt.run(fake.get("/r/x1/hot"), fake.get("/search", q="x"), e_later())
    assert all(r["kind"] == "Listing" for r in results)
    assert sent[:2] == [("/r/x1/hot", 1000.0), ("/search", 1000.0)]
    assert sorted(sent[2:]) == [("/r/x2/hot", 1010.0), ("/search", 1010.0)]


def test_call_waiting_out_a_cool_down_fails_locally_when_the_wait_is_over_its_budget():
    # A token-endpoint 429 holds calls for 60 s. A 200 already in flight says the window has
    # requests left. A call made 50 s later would wait 10 s: within the 15 s budget, so it waits
    # and is sent at the end of the hold. A call made at once would wait 60 s: it fails locally.
    vt = None
    sent: list[tuple[str, float]] = []
    fake = FakeReddit({
        ("GET", "/r/x1/hot"): ok(listing([]), headers={"x-ratelimit-remaining": "50", "x-ratelimit-reset": "500"}),
        ("GET", "/search"): [StatusCodeException(status_code=429), ok(listing([]))],
    })
    send = fake._send_raw

    async def in_flight(verb, path, params, data, timeout=None):
        sent.append((path, vt.now))
        if path == "/r/x1/hot":
            await vt.wait(1)
        return await send(verb, path, params, data, timeout)

    fake._send_raw = in_flight
    vt = _VirtualTime(fake)

    async def held_now():
        with pytest.raises(RateLimitedError) as e:
            await fake.get("/search", q="x")
        await vt.wait(2)
        with pytest.raises(RateLimitedError) as again:
            await fake.get("/search", q="x")
        return e.value.retry_after, again.value.retry_after

    async def later():
        await vt.wait(50)
        return await fake.get("/search", q="x")

    d, waits, last = vt.run(fake.get("/r/x1/hot"), held_now(), later())
    assert d["kind"] == "Listing" and last["kind"] == "Listing"
    assert waits == (60, 58)
    assert sent == [("/r/x1/hot", 1000.0), ("/search", 1000.0), ("/search", 1060.0)]


def _early_waking_clock(fake: FakeReddit, start: float = 1000.0, early: float = 1e-6) -> list[float]:
    """Like _manual_clock, but every sleep ends a hair early, as event-loop timers may."""
    now = _manual_clock(fake, start)

    async def sleep(seconds: float) -> None:
        fake.sleeps.append(seconds)
        now[0] += seconds - early

    fake.sleep = sleep
    return now


def test_429_retry_that_spends_the_whole_wait_budget_is_sent_after_an_early_wake():
    # "retry after 15" uses the whole 15 s budget. The sleep ends a microsecond early: the call
    # has served its own wait, so its retry is sent rather than failing over that microsecond.
    fake = FakeReddit({("GET", "/search"): [raw(429, b"", {"retry-after": "15"}), ok(listing([]))]})
    _early_waking_clock(fake)
    assert run(fake.get("/search", q="x"))["kind"] == "Listing"
    assert len(fake.calls) == 2 and fake.sleeps == [15.0]


def test_call_that_waits_out_a_cool_down_with_its_whole_budget_is_sent_after_an_early_wake():
    # A token-endpoint 429 holds calls for 60 s; then a response that was already in flight says
    # the window has requests left (so only the cool-down holds). A call made 45 s later waits the
    # last 15 s (its whole budget); the sleep ends a microsecond early, and the call is still sent.
    fake = FakeReddit({("GET", "/search"): [StatusCodeException(status_code=429), ok(listing([]))]})
    now = _early_waking_clock(fake)
    with pytest.raises(RateLimitedError):
        run(fake.get("/search", q="x"))
    fake._note_limits({"x-ratelimit-remaining": "50", "x-ratelimit-reset": "500"})  # the in-flight answer
    now[0] += 45
    assert run(fake.get("/search", q="x"))["kind"] == "Listing"
    assert len(fake.calls) == 2 and fake.sleeps == [15.0]


def test_short_429_on_the_windows_last_request_still_retries_once():
    # The call spends the window's last request (1 left, resets in 50 s) and gets "retry after 3".
    # Its own spend is not another call's cool-down: it waits 3 s and retries, as before.
    window = {"x-ratelimit-remaining": "1", "x-ratelimit-reset": "50"}
    fake = FakeReddit({
        ("GET", "/r/x1/hot"): ok(listing([]), headers=window),
        ("GET", "/search"): [raw(429, b"", {"retry-after": "3"}), ok(listing([]))],
    })
    _manual_clock(fake)
    run(fake.get("/r/x1/hot"))
    assert run(fake.get("/search", q="x"))["kind"] == "Listing"
    assert fake.paths() == ["/r/x1/hot", "/search", "/search"] and fake.sleeps == [3.0]


def test_retried_429_leaves_no_hold_once_its_retry_succeeds():
    fake = FakeReddit({("GET", "/search"): [raw(429, b"", {"retry-after": "3"}), ok(listing([])), ok(listing([]))]})
    _manual_clock(fake)
    run(fake.get("/search", q="x"))
    assert fake.rate_limit_status()["remaining"] is None
    run(fake.get("/search", q="x"))
    assert len(fake.calls) == 3 and fake.sleeps == [3.0]


def test_429_cool_down_never_shortens_a_longer_reset_window():
    headers = {"retry-after": "2", "x-ratelimit-remaining": "0", "x-ratelimit-reset": "40"}
    fake = FakeReddit({("GET", "/search"): raw(429, b"", headers)})
    _manual_clock(fake)
    with pytest.raises(RateLimitedError, match="retry after 2 s"):
        run(fake.get("/search", q="x"))
    with pytest.raises(RateLimitedError) as e:
        run(fake.get("/search", q="x"))
    assert e.value.retry_after == 40 and len(fake.calls) == 2  # the window, not the 2 s


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
    assert "window is used up" in str(e.value) and "asked this client to wait" not in str(e.value)


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
    _manual_clock(fake)
    for _ in range(5):
        with pytest.raises(RateLimitedError):
            run(fake.get("/search", q="x"))
    assert len(fake.calls) == 10  # exactly one retry per request, never more
    # The first call waits once before its retry; each later call first waits out the
    # previous call's final "retry after 1 s" at the gate, then once more before its retry.
    assert fake.sleeps == [1.0] * 9


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
        r"reddit-readonly-mcp/\d+\.\d+\.\d+ \(\+https://github\.com/[\w.-]+/reddit-research-mcp\)",
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
