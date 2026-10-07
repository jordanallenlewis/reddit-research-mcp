"""Live end-to-end check through a real stdio MCP session.

Starts the server as a subprocess, initialises an MCP session, lists the tools and
calls every tool once (plus four error paths) against live Reddit, printing latency,
output size and Reddit requests per call. Uses about 30 API requests, well under
Reddit's per-minute limit. Ids, names and threads are picked from live results at
run time; nothing about real users is stored.

    uv run python scripts/smoke.py
    uv run python scripts/smoke.py --server-cmd \
        "uvx --from git+https://github.com/jordanallenlewis/reddit-research-mcp reddit-research-mcp"
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shlex
import sys
import tempfile
import time
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

ROOT = Path(__file__).resolve().parent.parent
REQUEST_LINE = re.compile(r"DEBUG (GET|POST) \S+ (->|timeout|transport)")
MAX_REQUESTS = 40


class Smoke:
    def __init__(self, client: Client, log_path: Path, dump: Path | None = None) -> None:
        self.client = client
        self.log_path = log_path
        self.dump = dump
        self.rows: list[tuple[str, float, int, int, str]] = []
        self.failures: list[str] = []

    def requests_so_far(self) -> int:
        try:
            text = self.log_path.read_text(errors="replace")
        except FileNotFoundError:
            return 0
        return len(REQUEST_LINE.findall(text))

    async def call(
        self, label: str, tool: str, args: dict, *, expect_error: bool = False, expect_text: str = ""
    ) -> str:
        before = self.requests_so_far()
        if before >= MAX_REQUESTS:
            raise SystemExit(f"request budget of {MAX_REQUESTS} reached; stopping")
        t0 = time.perf_counter()
        res = await self.client.call_tool(tool, args, raise_on_error=False)
        dt = time.perf_counter() - t0
        await asyncio.sleep(0.05)  # let the server flush its log line
        used = self.requests_so_far() - before
        text = "\n".join(getattr(c, "text", "") for c in res.content)
        status = "error" if res.is_error else "ok"
        if res.is_error != expect_error:
            want = "error" if expect_error else "success"
            self.failures.append(f"{label}: expected {want}, got {status}: {text[:200]}")
        if not text.strip():
            self.failures.append(f"{label}: empty output")
        if expect_text and expect_text not in text:
            self.failures.append(f"{label}: expected {expect_text!r} in: {text[:200]}")
        self.rows.append((label, dt, len(text), used, status))
        if self.dump:
            name = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
            (self.dump / f"{len(self.rows):02d}_{name}.txt").write_text(text)
        first = text.strip().split("\n", 1)[0][:110]
        print(f"{label:<34} {dt:6.2f}s {len(text):7,d} chars {used:2d} req  {status:<5} | {first}")
        return text


def first_ids(text: str, n: int) -> list[str]:
    return re.findall(r"^\[([0-9a-z]+)\] r/", text, re.M)[:n]


def busiest_post(text: str) -> str | None:
    best, best_n = None, -1
    for m in re.finditer(r"^\[([0-9a-z]+)\] r/\S+ \S+ \S+ ([\d,]+)c ", text, re.M):
        n = int(m.group(2).replace(",", ""))
        if n > best_n:
            best, best_n = m.group(1), n
    return best


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--server-cmd",
        default=f"uv run --quiet --directory {shlex.quote(str(ROOT))} reddit-research-mcp",
        help="Command that starts the server on stdio.",
    )
    ap.add_argument("--subreddit", default="dataengineering", help="Busy subreddit to exercise.")
    ap.add_argument("--wiki-subreddit", default="datascience", help="Subreddit with a public wiki index.")
    ap.add_argument("--query", default='"dremio"', help="Search query.")
    ap.add_argument("--dump", type=Path, help="Directory to write each tool output to.")
    opts = ap.parse_args()

    cmd = shlex.split(opts.server_cmd)
    log_path = Path(tempfile.mkstemp(prefix="reddit-research-mcp-smoke-", suffix=".log")[1])
    env = dict(os.environ, REDDIT_RESEARCH_MCP_LOG_LEVEL="DEBUG")
    transport = StdioTransport(command=cmd[0], args=cmd[1:], env=env, log_file=log_path, keep_alive=False)
    print(f"server: {' '.join(cmd)}\nlog: {log_path}\n")

    t0 = time.perf_counter()
    async with Client(transport) as client:
        t_init = time.perf_counter() - t0
        si = client.server_info
        print(f"initialize: {t_init:.2f}s server={getattr(si, 'name', '?')} {getattr(si, 'version', '')}")
        t1 = time.perf_counter()
        tools = await client.list_tools()
        print(f"tools/list: {time.perf_counter() - t1:.2f}s {len(tools)} tools: {', '.join(t.name for t in tools)}\n")

        if opts.dump:
            opts.dump.mkdir(parents=True, exist_ok=True)
        s = Smoke(client, log_path, opts.dump)
        sub = opts.subreddit
        await s.call("search_subreddits", "search_subreddits", {"query": "dremio", "limit": 5})
        found = await s.call("search_reddit", "search_reddit",
                             {"query": opts.query, "subreddit": sub, "limit": 10, "body_chars": 200})
        top = await s.call("browse_subreddit top/month", "browse_subreddit",
                           {"subreddit": sub, "listing": "top", "time": "month", "limit": 10, "body_chars": 0})
        await s.call("browse_subreddit missing sub", "browse_subreddit", {"subreddit": "dremio"}, expect_error=True)
        await s.call("get_subreddit_info", "get_subreddit_info",
                     {"subreddit": sub, "include_sidebar": True, "sidebar_chars": 400})
        await s.call("get_subreddit_wiki", "get_subreddit_wiki",
                     {"subreddit": opts.wiki_subreddit, "page": "index", "max_chars": 2000})

        pid = busiest_post(top) or (first_ids(found, 1) or [None])[0]
        if not pid:
            print("no post ids found; stopping")
            return 1
        thread = await s.call("get_post", "get_post", {"post": pid, "comment_limit": 60, "comment_depth": 4})
        m = re.search(r"\[more top-level: [\d,]+ comments?; \d+ ids: ([0-9a-z,]+)", thread) or re.search(
            r"\[more: [\d,]+ comments?; ids: ([0-9a-z,]+)", thread
        )
        if m:
            ids = m.group(1).split(",")[:20]
            await s.call("expand_comments", "expand_comments", {"post": pid, "comment_ids": ids, "max_chars": 8000})
        else:
            print("(no [more] stubs in this thread; expand_comments skipped)")
        cm = re.search(r"^\s*\[([0-9a-z]+)\] \d{4}-\d{2}-\d{2} ", thread.split("comments (", 1)[-1], re.M)
        if cm:
            await s.call("get_post comment permalink", "get_post",
                         {"post": f"https://www.reddit.com/comments/{pid}/_/{cm.group(1)}/", "context": 2,
                          "comment_depth": 2, "max_chars": 6000})
        # Reddit ignores an unknown comment id and returns the normal thread; the tool must not.
        await s.call("get_post unknown comment id", "get_post",
                     {"post": pid, "comment_id": "zzzzzzz", "comment_limit": 5, "comment_depth": 1},
                     expect_error=True, expect_text=f"comment zzzzzzz not found in post {pid}")
        other = next((x for x in first_ids(top, 10) + first_ids(found, 10) if x != pid), None)
        if cm and other:
            # Reddit answers 404 for a comment of another post; the tool names the right post.
            await s.call("get_post comment of other post", "get_post",
                         {"post": other, "comment_id": cm.group(1), "comment_limit": 5},
                         expect_error=True, expect_text=f"belongs to post {pid}")
        ids = first_ids(found, 5) or [pid]
        await s.call("get_posts", "get_posts", {"posts": ids, "body_chars": 1500})
        au = re.search(r"^\[[0-9a-z]+\] r/\S+ \S+ \S+ \S+ u/([A-Za-z0-9_-]+)", thread, re.M)
        if au:
            await s.call("get_user_activity", "get_user_activity",
                         {"username": au.group(1), "limit": 10, "body_chars": 100})
        else:
            print("(thread author deleted; get_user_activity skipped)")
        await s.call("find_other_discussions post", "find_other_discussions", {"post_or_url": pid, "limit": 10})
        await s.call("find_other_discussions url", "find_other_discussions",
                     {"post_or_url": "https://github.com/apache/iceberg", "limit": 5})
        await s.call("get_post bad id", "get_post", {"post": "not a post!"}, expect_error=True)

    total_req = s.requests_so_far()
    print(f"\ncalls: {len(s.rows)}, Reddit API requests: {total_req} (plus one token request), "
          f"total output: {sum(r[2] for r in s.rows):,} chars, slowest: {max(r[1] for r in s.rows):.2f}s")
    if total_req > MAX_REQUESTS:
        s.failures.append(f"used {total_req} requests (> {MAX_REQUESTS})")
    if s.failures:
        print("\nFAILURES:\n" + "\n".join(s.failures))
        return 1
    print("smoke: OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
