"""Synthetic Reddit API payloads and a scripted client. No real users or content."""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from reddit_research_mcp.reddit import Config, RedditClient

OCT_2_2024 = 1727827200  # 2024-10-02T00:00:00Z
JSON_HEADERS = {"content-type": "application/json; charset=UTF-8"}


def post(id: str = "abc123", sub: str = "testsub", title: str = "A synthetic title", **kw: Any) -> dict:
    d = {
        "id": id,
        "name": f"t3_{id}",
        "subreddit": sub,
        "title": title,
        "author": "example_author",
        "created_utc": OCT_2_2024,
        "score": 31,
        "upvote_ratio": 0.91,
        "num_comments": 54,
        "is_self": True,
        "selftext": "",
        "url": f"https://www.reddit.com/r/{sub}/comments/{id}/a_synthetic_title/",
        "permalink": f"/r/{sub}/comments/{id}/a_synthetic_title/",
        "domain": f"self.{sub}",
        "link_flair_text": None,
        "over_18": False,
        "spoiler": False,
        "stickied": False,
        "locked": False,
        "archived": False,
        "edited": False,
        "removed_by_category": None,
        "distinguished": None,
    }
    d.update(kw)
    return d


def t3(**kw: Any) -> dict:
    return {"kind": "t3", "data": post(**kw)}


def listing(children: list, after: str | None = None) -> dict:
    return {"kind": "Listing", "data": {"after": after, "children": children}}


def comment(
    id: str,
    body: str = "A synthetic comment.",
    *,
    replies: list | None = None,
    author: str = "example_commenter",
    parent: str = "t3_abc123",
    score: int = 5,
    **kw: Any,
) -> dict:
    d = {
        "id": id,
        "name": f"t1_{id}",
        "body": body,
        "author": author,
        "created_utc": OCT_2_2024 + 3600,
        "score": score,
        "score_hidden": False,
        "is_submitter": False,
        "edited": False,
        "author_flair_text": None,
        "distinguished": None,
        "stickied": False,
        "parent_id": parent,
        "link_id": "t3_abc123",
        "subreddit": "testsub",
        "replies": listing(replies) if replies else "",
    }
    d.update(kw)
    return {"kind": "t1", "data": d}


def more(ids: list[str], count: int, parent: str = "t3_abc123") -> dict:
    return {
        "kind": "more",
        "data": {
            "count": count,
            "children": ids,
            "id": ids[0] if ids else "_",
            "name": f"t1_{ids[0]}" if ids else "t1__",
            "parent_id": parent,
            "depth": 0,
        },
    }


def cont(parent: str) -> dict:
    return {
        "kind": "more",
        "data": {"count": 0, "children": [], "id": "_", "name": "t1__", "parent_id": parent, "depth": 9},
    }


def ok(obj: Any, status: int = 200, headers: dict | None = None) -> tuple[int, dict, bytes]:
    return status, {**JSON_HEADERS, **(headers or {})}, json.dumps(obj).encode()


def raw(status: int, body: bytes = b"", headers: dict | None = None) -> tuple[int, dict, bytes]:
    return status, dict(headers or {}), body


Response = Any  # tuple, exception, or callable(params) -> tuple


class FakeReddit(RedditClient):
    """RedditClient whose network seam replays scripted responses and records calls."""

    def __init__(self, routes: dict[tuple[str, str], Response | list[Response]] | None = None) -> None:
        super().__init__(Config("anonymous"))
        self.routes: dict[tuple[str, str], Any] = dict(routes or {})
        self.calls: list[tuple[str, str, dict, Any]] = []
        self.timeouts: list[float | None] = []
        self.sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        self.sleep = fake_sleep

    def paths(self) -> list[str]:
        return [p for _, p, _, _ in self.calls]

    async def _send_raw(self, verb, path, params, data, timeout=None):  # type: ignore[override]
        self.calls.append((verb, path, dict(params), data))
        self.timeouts.append(timeout)
        key = (verb, path)
        if key not in self.routes:
            raise AssertionError(f"unexpected request {key} params={dict(params)}")
        entry = self.routes[key]
        if isinstance(entry, list):
            item = entry.pop(0) if len(entry) > 1 else entry[0]
        else:
            item = entry
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            item = item(dict(params))
        return item


FOOTER = re.compile(r"\n\[\d+ Reddit requests?, [\d.]+ s\]$")


def run_raw(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)


def run(coro: Any) -> Any:
    """Run a tool coroutine and drop the request/time footer, so tests can check endings."""
    out = run_raw(coro)
    return FOOTER.sub("", out) if isinstance(out, str) else out


Factory = Callable[..., Any]


def sfw_info(params: dict) -> Any:
    """/api/info answer for the NSFW checks: every t3_ id is a safe post, t5 names safe communities."""
    if params.get("sr_name"):
        names = str(params["sr_name"]).split(",")
        return ok(listing([{"kind": "t5", "data": {"display_name": n, "over18": False}} for n in names]))
    ids = str(params.get("id", "")).split(",")
    return ok(listing([t3(id=i[3:]) for i in ids if i.startswith("t3_")]))
