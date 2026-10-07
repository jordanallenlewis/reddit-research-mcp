# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/).

## [0.1.0] - 2026-10-07

First release as a package, rewritten from a single-file server based on
adhikasp/mcp-reddit.

### Added

- Tools: `search_reddit`, `browse_subreddit`, `search_subreddits`, `get_subreddit_info`,
  `get_subreddit_wiki`, `get_post`, `expand_comments`, `get_posts`, `get_user_activity`,
  `find_other_discussions`. All are annotated read-only, idempotent and open-world.
- Pagination with `after` cursors on search, browse and user listings; up to 100 results per page.
- Full comment coverage reporting: every "load more" and "continue this thread" stub that fits
  the response budget is listed with its ids, and each thread ends with a `Shown X of Y comments`
  line that also counts the stubs inside comments the budget cut.
- Comment sorting (`confidence`, `top`, `new`, `controversial`, `old`, `qa`) and opening a
  thread at a specific comment with parent context. A comment id that is not in the post is an
  error that names the post it belongs to when Reddit knows it.
- `get_post(body_chars=6000)` caps the post body so long self posts leave room for comments.
- Post headers with id, date, score, upvote ratio, comment count, flair, flags (edited, locked,
  NSFW, pinned, removed, score-hidden), link URL and domain, poll options, gallery size and
  crosspost origin. Posts in subreddits that hide new scores still show the score the API returns.
  Bodies are shown for every post type, including link, image and gallery posts.
- Input normalisation for post ids, `t3_` fullnames, permalinks, comment permalinks, redd.it
  links, `r/` and `u/` prefixes, multi-subreddit `a+b`, and wiki page URLs.
- App-only and refresh-token OAuth through `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` and
  `REDDIT_REFRESH_TOKEN`, with validation of partial combinations; `REDDIT_USER_AGENT` override.
- Offline test suite with synthetic fixtures and a live stdio smoke test (`scripts/smoke.py`).
  Lint settings (ruff) are in `pyproject.toml`.

### Changed

- Thread reads take one request instead of two.
- Listing output is about half the size of the previous labelled format, with consistent
  truncation (`[+N chars]`) and a response budget on every tool.
- Errors are returned as MCP errors (`isError`) with a specific message and next step instead of
  strings such as `302 Found` or an empty `An error occurred: `.
- Rate limiting no longer blocks: waits longer than 15 s fail fast with the retry time, a 429 is
  retried once when its `Retry-After` is short, and transport errors and 5xx responses are
  retried once. Requests time out after 15 s (10 s on the retry), multi-request tools start no
  new batch after 15 s, and every tool call ends within 45 s.
- Dependencies are pinned (`redditwarp==1.3.0`, `fastmcp>=4.0.11,<5`) with a committed `uv.lock`.

### Removed

- `fetch_reddit_hot_threads`, `fetch_reddit_top_threads` and `fetch_reddit_new_threads`
  (replaced by `browse_subreddit`), and `fetch_reddit_post_content` (replaced by `get_post`).
