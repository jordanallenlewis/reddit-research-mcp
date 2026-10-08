# reddit-research-mcp

A read-only [Model Context Protocol](https://modelcontextprotocol.io) server for researching
Reddit. It lets an MCP client such as Claude Code or Claude Desktop search posts, browse and
discover communities, read whole threads including the comments Reddit hides behind
"load more" links, read subreddit rules and wikis, and check who is posting.

It never posts, votes, edits or deletes anything. Every tool is marked read-only, and every
Reddit API request it sends is a `GET` (the only other request is the OAuth token fetch).

**Why not the client's built-in web search or fetch?** This server calls Reddit's API, so results
come back as compact text with stable ids, dates, scores and comment counts, filters by
subreddit, sort and time window, and a coverage line that says how much of a thread the model
has read and how to load the rest.

## Example prompts

- "What do data engineers on Reddit say about migrating from Hive to Iceberg? Read the most
  discussed threads, including the comments, and tell me where opinions split."
- "Find communities about Kubernetes cost optimization, check their rules, and summarise the top
  posts of the last year."
- "Has anyone discussed this article on Reddit? <url> Read the threads and list the objections."

## Highlights

- Ten tools covering search, browsing, community discovery, full threads, hidden-comment
  expansion, bulk post bodies, user history and "other discussions" of a link.
- Thread coverage is explicit: every "load more" stub that fits the response budget is listed
  with its comment ids, and each thread ends with
  `Shown X of Y comments; ~Z more in N stubs -> expand_comments(...)`. When the budget cuts
  comments, the line says how many stubs sit inside them and lists the cut comments' ids instead.
- Compact text output: one header line per item with id, subreddit, UTC date, score, upvote
  ratio, comment count, author, flair, post type and flags. Every response has a size budget
  and says how to get the rest when it is cut.
- Accepts the identifiers people paste: post ids, `t3_` fullnames, reddit.com and
  old.reddit.com permalinks, comment permalinks, redd.it links, `r/name`, `u/name`.
- Errors say what went wrong and what to do next, for example
  `r/dremio does not exist or is private; use search_subreddits to find the right name; similar names: r/dremio_lakehouse`.
- NSFW (18+) posts, comments and communities are hidden by default; every tool takes
  `include_nsfw=true` to show them, and a server setting can block them outright.
- Works without credentials. Optional Reddit app credentials give the server its own quota.
- Never stalls a tool call on rate limits: short waits (up to 15 s) are absorbed, longer ones
  fail fast with `Reddit rate limit reached; retry after N s`. Every tool call ends within 45 s.

## Requirements

- [uv](https://docs.astral.sh/uv/) (provides `uvx`). Python 3.11 or newer is fetched by uv if needed.

## Install

### Claude Code

```bash
claude mcp add --scope user reddit -- uvx --from git+https://github.com/jordanallenlewis/reddit-research-mcp reddit-research-mcp
```

With your own Reddit app credentials (see [Authentication](#authentication)):

```bash
claude mcp add --scope user reddit \
  -e REDDIT_CLIENT_ID=your_client_id -e REDDIT_CLIENT_SECRET=your_client_secret \
  -- uvx --from git+https://github.com/jordanallenlewis/reddit-research-mcp reddit-research-mcp
```

### Claude Desktop

Add this to `claude_desktop_config.json` (Settings, Developer, Edit Config) and restart the app:

```json
{
  "mcpServers": {
    "reddit": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/jordanallenlewis/reddit-research-mcp",
        "reddit-research-mcp"
      ],
      "env": {}
    }
  }
}
```

If the app cannot find `uvx`, use its full path (`which uvx`, often `~/.local/bin/uvx`).
Put `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET` in `env` to use your own app.

### Other MCP clients

The server speaks MCP over stdio. Run it with:

```bash
uvx --from git+https://github.com/jordanallenlewis/reddit-research-mcp reddit-research-mcp
```

### From a local checkout

```bash
git clone https://github.com/jordanallenlewis/reddit-research-mcp
cd reddit-research-mcp
uv sync
uv run reddit-research-mcp --version
claude mcp add --scope user reddit -- uv run --directory "$PWD" reddit-research-mcp
```

## Tools

| Tool | What it does | Reddit requests |
|---|---|---|
| `search_reddit(query, subreddit="", sort="relevance", time="all", limit=25, after=None, body_chars=400)` | Search post titles and bodies, site-wide or in `a+b` subreddits. Returns a `next: after=` cursor. | 1 |
| `browse_subreddit(subreddit, listing="hot", time="week", limit=25, after=None, body_chars=400)` | `hot`, `new`, `top`, `rising` or `controversial` posts of one or more subreddits. `time` applies to top and controversial. | 1 |
| `search_subreddits(query, limit=10)` | Find communities by description, plus names that start with the query, with subscribers, NSFW flag, creation date and description. | 1 to 3 |
| `get_subreddit_info(subreddit, include_rules=True, include_sidebar=False, sidebar_chars=3000)` | Size, age, type, description, rules, sidebar and wiki page list. For a private or Premium-only community it shows the public listing data and says what needs membership. | 1 to 3 |
| `get_subreddit_wiki(subreddit, page="index", max_chars=20000)` | Read a wiki page (FAQs, guides), or list pages with `page=""`. | 2 (1 with `include_nsfw=true`) |
| `get_post(post, comment_sort="top", comment_limit=200, comment_depth=8, comment_id=None, context=0, body_chars=6000, max_chars=40000)` | The post and its comment tree in one request, with the "more" stubs and a coverage line. The body shows its first 6,000 characters by default so long posts leave room for comments; `get_posts(..., body_chars=40000)` returns the whole text. A `comment_id` that is not in the post is an error that says where the comment lives, when Reddit knows. | 1 (2 when a `comment_id` is not found) |
| `expand_comments(post, comment_ids, sort="top", max_chars=40000)` | Load the comments behind "more" stubs, as reply trees. Up to 500 ids per call; the rest are listed for the next call. | 1 per 100 ids, plus 1 for the NSFW check unless `include_nsfw=true`, plus 1 when ids come back missing |
| `get_posts(posts, body_chars=4000, max_chars=60000)` | Full headers and bodies of many posts at once (no comments). Up to 300 posts per call; the rest are listed. | 1 per 100 posts |
| `get_user_activity(username, kind="overview", sort="new", time="all", limit=25, after=None, body_chars=400)` | Account age and karma, recent posts and comments, and which subreddits the activity is concentrated in (from the last 100 items, whatever `limit` is). `[deleted]` is explained, not looked up. | 2 |
| `find_other_discussions(post_or_url, limit=25)` | Crossposts and other submissions of a post's link, or every thread that submitted an external URL. | 1 to 2 |

Every tool also takes `include_nsfw=False` (see [NSFW content](#nsfw-content)); it is left out of
the signatures above.

Numeric arguments are clamped to the ranges the tool descriptions state (for example `limit` 1 to
100, `comment_limit` 1 to 500, `comment_depth` 1 to 10), and the output says when a value was
clamped. Text arguments have limits too: search queries up to 512 characters, `find_other_discussions`
URLs up to 2,048, and at most 50 subreddits in one `a+b` list. Anything longer is refused with a
message that says so and no request is sent. A refused argument is quoted back in the error only in
shortened form.

### Output format

A listing item looks like this:

```text
[1abc234] r/dataengineering 2024-10-02 31(91%) 54c u/example_author [Discussion] self edited
Title of the post
  First 400 characters of the body ... [+1234 chars]
```

That is: `[id] r/subreddit date score(upvote ratio) comments author [flair] type flags`.
Types are `self`, `link`, `image`, `video`, `gallery`, `poll` and `crosspost`. Flags are `nsfw`,
`spoiler`, `pinned`, `locked`, `deleted` or `removed(...)`, `edited`, `mod` (or `admin`),
`score-hidden` and, in `get_post`/`get_posts`, `archived`. `score-hidden` means the subreddit
hides the score of new posts on its site; the API still returns it, and the number shown is that
value. A score of `?` means Reddit sent none (comments with hidden scores). Link posts add a
`url:` line, galleries an item count, polls their options, crossposts the original post id and
subreddit. Listings end with `next: after=<cursor>` or `next: none (end of results)`.

Comments in `get_post` are indented by depth:

```text
[k2x9a1b] 2024-10-03 12 u/example_user (OP) (edited) [flair text]
  Comment body
  [k2x9c3d] 2024-10-03 4 u/another_user
    Reply body
  [more: 7 comments; ids: k2xa001,k2xa002]
[k2x9z9z] 2024-10-04 [removed]
[more top-level: 1,552 comments; 540 ids: ...]

Shown 180 of 2,849 comments; ~2,669 more in 45 stubs -> expand_comments(post="1abc234", comment_ids=[ids from the [more ...] lines])
[1 Reddit request, 0.6 s]
```

Stub counts carry a `~` because Reddit counts removed comments in them, so shown plus stub
comments can add up to slightly more than the thread's comment count.

When `max_chars` cuts the thread, stubs inside the comments that were not shown cannot be listed.
The coverage line then splits the count, for example
`~2,468 more in 336 stubs: 50 stubs listed with ids -> expand_comments(...); 286 stubs (~1,268 comments) inside the loaded comments not shown, ids not listed`,
and the next line (`Output budget reached: 40 loaded comments in 12 threads not shown`) lists
the top id of each cut thread, which `expand_comments` accepts too. Loaded comments take
priority over stub id lists, so a small `max_chars` shortens the id lists first. Raising
`max_chars` or lowering `body_chars` shows more in one call.

`expand_comments` reports any requested id Reddit did not return, grouped by reason, for
example `Not returned (2): removed: pc1uss9; not found: abc1234`.

Every result ends with a line such as `[2 Reddit requests, 0.4 s]`.

A comment's permalink is the post permalink plus the comment id; the thread header prints the
template once instead of repeating it on every comment.

## NSFW content

Every tool takes `include_nsfw` (default `false`). With the default:

- Posts, comments and crossposts that Reddit marks 18+ (`over_18`), and communities marked
  18+ (`over18`), are removed before formatting. Search and community search do not
  request them (`include_over_18` is sent only when NSFW is allowed).
- Each result says how many items were hidden, for example
  `25 posts (3 NSFW posts hidden; include_nsfw=true shows them)`.
- `get_post`, `expand_comments`, `get_subreddit_info`, `get_subreddit_wiki` and
  `find_other_discussions` on an 18+ post or community return a short notice instead of
  the content. `get_posts` lists the hidden ids.
- `get_user_activity` leaves 18+ items out of the listing and the subreddit summary, and hides
  the profile description of an 18+ profile.
- Community name suggestions (in errors and `search_subreddits`) are checked first, because
  Reddit's name lookup returns 18+ communities; names whose status cannot be checked are
  not shown.

Pass `include_nsfw=true` to show everything. Set `REDDIT_RESEARCH_MCP_BLOCK_NSFW=1` (`true`, `yes` and `on` also work) in the
server's environment to keep NSFW hidden whatever the tool call asks:

```bash
claude mcp add --scope user reddit -e REDDIT_RESEARCH_MCP_BLOCK_NSFW=1 \
  -- uvx --from git+https://github.com/jordanallenlewis/reddit-research-mcp reddit-research-mcp
```

The filter relies on Reddit's own 18+ flags. Posts that Reddit does not flag, in communities
that are not marked 18+, are shown.

## Search tips

Reddit's search is loose. Unquoted multi-word queries match posts containing any of the words
and rank by popularity, so `dremio reflections tips` returns unrelated posts about game
graphics and tipping. What works:

- Use one to three specific words and drop generic ones such as tips, best or help.
- Quote the rare or exact term: `"dremio" reflections`.
- Require terms with `AND`: `dremio AND iceberg`.
- Field operators: `subreddit:dataengineering`, `flair:Discussion`, `title:benchmark`,
  `selftext:kubernetes`, `author:name`, and `-subreddit:name` to exclude a community.
  Product names attract job-bot spam; `-subreddit:jobboardsearch` removes most of it.
- Search several communities in one call with `subreddit="dataengineering+dremio_lakehouse"`.
- Reddit's API cannot search comment text. To find advice inside threads, search with
  `sort="comments"` (most discussed), then read the threads with `get_post` and expand stubs.
- Not sure of a community name? `search_subreddits` matches descriptions and name prefixes.

## Authentication

| Variables set | Mode |
|---|---|
| none | Anonymous. Uses the public installed-app client id that ships with the redditwarp library. No account needed. |
| `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET` | App-only OAuth (client credentials). Gives the server its own rate-limit quota. |
| both of the above and `REDDIT_REFRESH_TOKEN` | User OAuth with a refresh token issued to that app. |

Any other combination (for example a client id without a secret) is a configuration error.
The server still starts, logs the problem to stderr, and every tool call returns the message.

To create credentials, sign in at <https://www.reddit.com/prefs/apps>, choose "create another
app", pick the "script" type, and use the string under the app name as `REDDIT_CLIENT_ID` and
the "secret" as `REDDIT_CLIENT_SECRET`.

Anonymous mode shares one public client id, and therefore one rate-limit quota, with every other
installation of this server (and of anything else using that id) worldwide. Anyone installing this
for a team should create their own app and set `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET`.
Reddit's Data API terms apply to your use of Reddit data through this server; read them before
you rely on it.

Other settings:

| Variable | Default | Purpose |
|---|---|---|
| `REDDIT_USER_AGENT` | `reddit-research-mcp/<version> (+https://github.com/jordanallenlewis/reddit-research-mcp)` | User-Agent sent to Reddit. Reddit asks for a descriptive one that names you. |
| `REDDIT_RESEARCH_MCP_LOG_LEVEL` | `WARNING` | Set `DEBUG` to log every request (path, status, latency, remaining quota) to stderr. |
| `REDDIT_RESEARCH_MCP_BLOCK_NSFW` | unset | Set `1` to hide NSFW content even when a tool call passes `include_nsfw=true` (for shared or work installs). |

## Rate limits

Reddit allows about 100 requests per minute per OAuth client, counted over a 10-minute window
(1,000 requests per 600 s). In anonymous mode the client id is shared with other installations
worldwide, so the window can already be partly used when the server starts; set your own
credentials (see [Authentication](#authentication)) for a team or for heavy use.

The server reads Reddit's `x-ratelimit-*` headers on every response. When the window is used
up it waits if the reset is at most 15 s away and otherwise fails at once with
`Reddit rate limit reached; retry after N s`. A `429` response is retried once when
`Retry-After` is at most 15 s. Network errors and `5xx` responses are retried once after 1 s.
A request times out after 15 s, and its retry after 10 s, so a dead network fails a request in
about 26 s. Tools that send several requests (`get_posts`, `expand_comments`) start no new batch
after 15 s and list what they did not fetch, and every tool call is cut off after 45 s with an
error. Comment expansion calls are sent one at a time, as Reddit requires.

## Troubleshooting

| Message | Meaning and fix |
|---|---|
| `r/NAME does not exist or is private; use search_subreddits ...` | Reddit redirected the name to its search page. Check the spelling or call `search_subreddits`. |
| `r/NAME is private` / `is quarantined` / `is banned` / `is restricted to Reddit Premium` | The community cannot be read with this server's access. |
| `r/NAME has its wiki disabled` | Use `get_subreddit_info` for the description, rules and sidebar. |
| `post ID not found` | Deleted, removed or a wrong id. Pass the `[id]` from a listing or the post URL. |
| `comment ID not found in post ID` / `comment ID belongs to post OTHER` | The `comment_id` is not in that thread: it was deleted, mistyped, or belongs to another post (the message then names the right one). |
| `Reddit rate limit reached; retry after N s` | Wait, or set your own `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET`. |
| `Network error reaching Reddit for GET <path> (...)` / `Reddit did not respond for GET <path> (waited 15 s, then 10 s on a retry)` | Connectivity problem or a slow Reddit; it was already retried once. |
| `TOOL gave up after 45 s: Reddit is slow or not answering ...` (TOOL is the tool name) | Reddit was too slow for the whole call (including rate-limit waits). Retry later or ask for less. |
| `Could not get an anonymous Reddit access token` | Reddit refused the anonymous token. Retry later or configure your own app. |
| `Configuration error: ...` | Fix the `REDDIT_*` variables as the message says. |
| HTML `403` on every call | Reddit may be blocking the network or user agent. Set credentials and a descriptive `REDDIT_USER_AGENT`. |
| `ModuleNotFoundError: No module named 'reddit_research_mcp'` from a local checkout on macOS | The checkout is in a folder synced by iCloud Drive (such as Desktop or Documents). iCloud marks files inside `.venv` as hidden, and Python 3.13 skips hidden `.pth` files, which breaks editable installs. Move the checkout out of the synced folder, or keep the environment out of sync with `rm -rf .venv && mkdir .venv.nosync && ln -s .venv.nosync .venv && uv sync`, or run with `uv run --no-editable`. |

Reddit content is untrusted user-generated text. The server returns it verbatim as data; the
client and model should treat it as material to evaluate, not as instructions.

## Development

```bash
uv sync                      # create the environment with dev dependencies
uv run pytest -q             # offline tests with synthetic fixtures
uv run ruff check .          # lint (settings in pyproject.toml)
uv run python scripts/smoke.py   # live end-to-end check over stdio, about 30 requests
```

`scripts/smoke.py` starts the server as a subprocess, runs `initialize` and `tools/list`, calls
every tool against live Reddit, and prints latency, output size and Reddit request count per
call. Pass `--server-cmd` to test another command, such as the `uvx` install.

Code layout:

- `src/reddit_research_mcp/server.py`: tool definitions and error messages.
- `src/reddit_research_mcp/reddit.py`: HTTP, OAuth (via redditwarp), rate limiting, retries.
- `src/reddit_research_mcp/format.py`: text rendering of posts, comments and listings.
- `src/reddit_research_mcp/refs.py`: parsing of ids, URLs, subreddit and user names.

redditwarp is used for OAuth token handling and the HTTP transport only. Endpoints are called
directly and their JSON parsed here, because redditwarp 1.3.0's model loaders fail on some
current API responses.

## Credits

Started from [adhikasp/mcp-reddit](https://github.com/adhikasp/mcp-reddit) (MIT), which
exposed hot threads and single posts. This project rewrites it with the tools above.

## License

MIT. See [LICENSE](LICENSE).
