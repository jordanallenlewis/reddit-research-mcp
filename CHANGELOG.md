# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/).

## [Unreleased]

### Added

- `reddit_research(question)` MCP prompt: a step-by-step, cited Reddit research workflow that uses the server's tools.
- `--transport http` serves streamable HTTP (with `--host`, default 127.0.0.1, and `--port`, default 8000). stdio stays the default.
- Progress updates from `get_posts` and `expand_comments` when a call needs more than one Reddit request.

### Changed

- README "Authentication" gives the dates Reddit announced for closing its public Data API (no new
  access requests after 2026-10-31, access removed from 2027-01-12 for apps that have not
  registered, closed in March 2027) and says new app credentials may no longer be available.
- The package and command are now `reddit-readonly-mcp`, so the name no longer clashes with an unrelated PyPI package. `reddit-research-mcp` still works as a command alias, and the User-Agent now starts with `reddit-readonly-mcp/`.

### Fixed

- `get_subreddit_wiki(page="")` now keeps the page list within `max_chars`; a longer list ends with the count left out and the `max_chars` that returns all of it.
- `search_subreddits` no longer lists a name-prefix match that Reddit gave no details for while NSFW is off, and the "Names starting with" count now matches the names listed.
- `get_post` and `expand_comments` accept path-only permalinks (`/r/<sub>/comments/<id>/<slug>/`), the form Reddit's JSON `permalink` field returns.
- After Reddit answered 429 with a long `Retry-After` (and no `x-ratelimit-*` headers), the next
  tool calls still sent requests to Reddit, and a parallel call waking from a short wait could
  clear a longer wait. The server now holds API requests until Reddit's wait is over and fails
  those calls locally with the time left; a short wait is slept out before the next request.
- When Reddit's token endpoint answered 429, the next tool call asked it for a token again at
  once. The server now holds requests for the 60 s the error reports, and a parallel call waking
  from a short 429 wait no longer clears a cool-down another call started meanwhile.
- A call waiting a few seconds for a used-up request window to reset sent its request as soon as
  it woke, even if a parallel call had started a 429 cool-down or seen a new used-up window in the
  meantime. It now checks again: it waits on only if its total wait stays within 15 s, and
  otherwise fails locally with the time left, without sending.
- When Reddit answered 429 with a short `Retry-After` (up to 15 s), only the call that got it
  waited before retrying; parallel calls kept sending to Reddit during that wait. Calls started
  during the wait now wait until it is over, or fail locally with the time left when they can't
  wait that long. A call waking from its own short wait also no longer clears another call's
  longer 429 cool-down that was already in force when its 429 arrived: it waits that out too, or
  fails locally with the time left, instead of retrying into it.
- A response from a call that was already in flight could lift or shorten a 429 cool-down
  another call had started: its `x-ratelimit-*` headers replaced the held window, so later
  calls went out while Reddit had asked this client to wait. A cool-down now holds whatever
  window headers arrive meanwhile, and a call held by one is told that Reddit asked this
  client to wait, not that the request window is used up.
- A redd.it link with a reddit.com-style path (`redd.it/r/<sub>/comments/<id>/...`,
  `redd.it/comments/<id>`, `redd.it/r/<sub>/s/<code>`) was read as a post id made of its
  first word (`r`, `comments`), so tools fetched an unrelated post. Links longer than
  `redd.it/<id>` are now read like reddit.com links: the post id, the share-link error, or
  "not a post" (an article lookup in `find_other_discussions`).
- After waiting out its own `429`, a call's retry reopened a request window that was used up
  (Reddit's `x-ratelimit-remaining: 0`, or the window's last request spent), so other calls went
  out inside that window, some while the retry was still in flight. Only the retry Reddit asked for now passes the used-up
  window, once; every other call still waits for the window to reset, or fails locally with
  the time left.
- A call that had already waited most of its 15 s rate-limit budget could still retry a 429:
  the 0.5 s minimum wait before a retry was added after the budget check, so a retry could go
  out at 15.3 s. The minimum wait now counts against the budget, and a retry that no longer
  fits fails locally, like one with a long `Retry-After`. A 429 that is given up holds the next
  calls for at least that minimum wait, even when Reddit's `Retry-After` is 0.
- A call whose 429 asked for its whole 15 s budget could fail with "the request window is used
  up" when another call's shorter 429 arrived during its wait and its timer woke slightly early
  (event-loop timers may wake up to a few milliseconds early). A call that has waited out a cool-down now also passes a used-up window that ends by
  then; the window stays used up for other calls.
- A share link with a doubled slash in its path (`redd.it//r/<sub>/s/<code>`,
  `reddit.com/r/<sub>//s/<code>`) got "not a post", or in `find_other_discussions` was looked up
  as an article. Doubled slashes in a share-link path are now read as one, so these links get
  the share-link error too.
- Share links in any letter case, including profile ones (`/u/<name>/s/<code>`, `/user/<name>/s/<code>`), now get the "share link" error that says to pass the `/comments/` URL, instead of "not a post" (in post and comment_id arguments). `find_other_discussions` gives the same error instead of looking the share link up as an article URL, which could not find the discussion behind it.

## [0.2.2] - 2026-10-08

### Fixed

- The `[N Reddit requests, ...]` footer counted requests from other tool calls running at the same
  time (a client that sends several calls in parallel saw inflated counts). Each call now counts
  only its own.
- `get_posts` rejected the whole call when one entry was not a post id or link. It now skips
  such entries, lists them (shortened, at most 10) and reads the rest. A call with no valid
  entry is still an error.

## [0.2.1] - 2026-10-08

### Added

- Input length limits that fail fast with a message and no Reddit request: `search_subreddits`
  queries over 512 characters and `find_other_discussions` URLs over 2,048 characters.
- `get_posts` lists the ids beyond its 300-id limit instead of only counting them.
- README: example prompts, a comparison with built-in web search, a note that anonymous mode
  shares one client id's quota worldwide, and the `include_nsfw` parameter note under the tools table.
- Tests for annotations, GET-only requests, oversized and hostile input, and hostile Reddit values.

### Changed

- Error messages shorten long echoed input (keeping its start and the advice at the end), so a
  100,000 character argument no longer produces a 100,000 character error.
- Tool descriptions: clearer `listing`, `sort` and `context` parameters, "when to use which"
  guidance for `browse_subreddit`, `search_subreddits`, `get_subreddit_info`, `get_user_activity`
  and `expand_comments`.

### Fixed

- `expand_comments` with thousands of ids spent seconds rebuilding a set per id, and dropped the
  "Not fetched yet" id list when Reddit returned no comments at all.
- Wiki page names, the wiki revision author, a community's accepted post types and Reddit's
  `after` cursor are now flattened or validated before they are printed, so a hostile value cannot
  add lines (such as a fake `next:` line) to the output.
- Names that Reddit returns (a community's `display_name`, a user's `name`, a comment's `link_id`,
  names from the community-name lookup) no longer choose a request path or a suggested call.
  `get_subreddit_info` and `get_user_activity` keep the validated input for later requests and
  use the returned name only for display, and only when it matches the requested one.
- README request counts for `get_subreddit_wiki`, `expand_comments`, `search_subreddits` and
  `get_subreddit_info`, and the Troubleshooting text for network and timeout errors.

## [0.2.0] - 2026-10-07

### Added

- `include_nsfw` on every tool, default `false`. Posts, comments, crossposts and communities
  that Reddit marks 18+ are hidden, and each result counts what was hidden. Single-item tools
  (`get_post`, `expand_comments`, `get_subreddit_info`, `get_subreddit_wiki`,
  `find_other_discussions`) return a notice for 18+ items.
- `REDDIT_RESEARCH_MCP_BLOCK_NSFW=1` keeps NSFW hidden even when a call asks for it.

### Changed

- NSFW content is no longer shown by default. Browsing an 18+ community previously returned
  its posts.
- Community name suggestions are checked with one `/api/info` request, because Reddit's name
  lookup returns 18+ communities; unchecked names are not shown.
- `expand_comments` and `get_subreddit_wiki` send one extra request to check the post or
  community when NSFW is hidden. If that check cannot run because of a network error, the
  tool fails instead of showing unchecked content.

## [0.1.1] - 2026-10-07

Fixes from a full live smoke test of every tool.

### Fixed

- `get_post` with a small `max_chars` printed long stub id lists and dropped comments it had
  already loaded. Loaded comments now come first and the stub id lists are shortened instead.
- `expand_comments` silently skipped ids Reddit did not return. It now lists them, grouped by
  reason (removed, deleted, in another post, not found), using one lookup request.
- The budget line said "N loaded comments not shown" next to fewer ids. It now reads
  "N loaded comments in M threads not shown" and counts unlisted stub ids separately.
- Stub comment counts are marked approximate (`~`), since Reddit includes removed comments.
- `get_user_activity("[deleted]")` now explains the placeholder instead of rejecting the name.
- `get_posts` now says how to read a cut body in full.
- `get_subreddit_info` on a private or Premium-only community returns its public listing
  data and what needs membership, instead of only an error.

### Changed

- `get_user_activity` builds its subreddit summary from the last 100 items in one request,
  whatever `limit` is; the cursor continues after the last item shown.
- Every result ends with the number of Reddit requests and the elapsed time.
- The `search_reddit` description suggests `-subreddit:jobboardsearch` against job-bot spam.

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
