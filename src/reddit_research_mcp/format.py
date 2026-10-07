"""Render Reddit API JSON as compact text.

Conventions shared by every tool:

* One header line per item:
  ``[id] r/sub YYYY-MM-DD score(ratio) Nc u/author [flair] type flags``
* Dates are UTC and written YYYY-MM-DD.
* Cut text ends with ``[+N chars]``.
* A listing ends with ``next: after=<cursor>`` when more results exist.
* One malformed item never breaks a listing; it renders as a placeholder.

Reddit text is untrusted user content. It is rendered verbatim as data.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

REDDIT = "https://www.reddit.com"
DELETED_BODIES = ("[deleted]", "[removed]")
MAX_STUB_IDS = 500  # ids printed per [more ...] line; ~8 chars each


# ---------------------------------------------------------------- primitives


def date_str(utc: Any) -> str:
    try:
        return datetime.fromtimestamp(float(utc), tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError, OSError):
        return "????-??-??"


def clean_text(text: Any, *, keep_blank_lines: bool = True) -> str:
    """Normalise newlines and trim; optionally drop blank lines between paragraphs."""
    s = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s) if keep_blank_lines else re.sub(r"\n{2,}", "\n", s)
    return s.strip()


def flatten(text: Any) -> str:
    """Collapse all whitespace (including newlines) to single spaces."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def truncate(text: str, limit: int) -> str:
    """Cut text to at most ``limit`` characters (plus marker), at whitespace when close."""
    if limit < 0:
        limit = 0
    if len(text) <= limit:
        return text
    if limit == 0:
        return f"[{len(text)} chars]"
    cut = text.rfind(" ", int(limit * 0.8), limit)
    nl = text.rfind("\n", int(limit * 0.8), limit)
    cut = max(cut, nl)
    if cut <= 0:
        cut = limit
    return text[:cut].rstrip() + f" [+{len(text) - cut} chars]"


def indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line if line else prefix.rstrip() for line in text.split("\n"))


def num(n: Any) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError, OverflowError):  # None, junk, inf, nan
        return "?"


def plural(n: Any, word: str) -> str:
    try:
        one = int(n) == 1
    except (TypeError, ValueError, OverflowError):
        one = False
    return f"{num(n)} {word}" + ("" if one else "s")


def permalink(d: Mapping[str, Any]) -> str:
    p = str(d.get("permalink") or "")
    if not p:
        return ""
    return p if p.startswith("http") else REDDIT + p


def fullname(thing: Mapping[str, Any]) -> str:
    d = thing.get("data") or {}
    if d.get("name"):
        return str(d["name"])
    kind = thing.get("kind") or ""
    return f"{kind}_{d.get('id', '')}" if kind and d.get("id") else ""


def author_str(d: Mapping[str, Any]) -> str:
    a = d.get("author") or "[deleted]"
    return "[deleted]" if a == "[deleted]" else f"u/{a}"


def children_of(d: Mapping[str, Any]) -> list:
    if not isinstance(d, Mapping):
        return []
    replies = d.get("replies")
    if isinstance(replies, Mapping):
        return list((replies.get("data") or {}).get("children") or [])
    return []


# ---------------------------------------------------------------- posts


def post_kind(d: Mapping[str, Any]) -> str:
    if d.get("crosspost_parent") or d.get("crosspost_parent_list"):
        return "crosspost"
    if d.get("poll_data"):
        return "poll"
    if d.get("is_gallery") or d.get("gallery_data"):
        return "gallery"
    if d.get("is_self"):
        return "self"
    hint = str(d.get("post_hint") or "")
    url = str(d.get("url_overridden_by_dest") or d.get("url") or "")
    if d.get("is_video") or hint in ("hosted:video", "rich:video") or "v.redd.it" in url:
        return "video"
    if hint == "image" or "i.redd.it" in url or re.search(r"\.(jpe?g|png|gif|webp)(\?|$)", url, re.I):
        return "image"
    if url:
        return "link"
    return "post"


def post_flags(d: Mapping[str, Any], *, full: bool = False) -> list[str]:
    out: list[str] = []
    if d.get("over_18"):
        out.append("nsfw")
    if d.get("spoiler"):
        out.append("spoiler")
    if d.get("stickied"):
        out.append("pinned")
    if d.get("locked"):
        out.append("locked")
    cat = d.get("removed_by_category")
    if cat:
        out.append("deleted" if cat == "deleted" else f"removed({cat})")
    elif d.get("selftext") == "[removed]":
        out.append("removed")
    if d.get("edited"):
        out.append("edited")
    dist = d.get("distinguished")
    if dist:
        out.append("mod" if dist == "moderator" else str(dist))
    if d.get("hide_score"):
        # The subreddit hides scores of new posts on its site; the API still sends the number.
        out.append("score-hidden")
    if full and d.get("archived"):
        out.append("archived")
    return out


def score_str(d: Mapping[str, Any], *, with_ratio: bool = True) -> str:
    """Score, plus upvote ratio for posts. '?' only when Reddit sends no usable score.

    Posts with hide_score=true still carry their score in the API (post_flags adds
    "score-hidden"); comments with score_hidden=true carry a placeholder, so they print '?'.
    """
    score = d.get("score")
    if d.get("score_hidden") or not isinstance(score, (int, float)) or isinstance(score, bool):
        s = "?"
    elif isinstance(score, float):
        s = str(int(score)) if math.isfinite(score) else "?"
    else:
        s = str(score)
    ratio = d.get("upvote_ratio")
    if with_ratio and isinstance(ratio, (int, float)) and math.isfinite(ratio):
        s += f"({round(ratio * 100)}%)"
    return s


def post_head(d: Mapping[str, Any], *, full: bool = False, omit_author: bool = False) -> str:
    parts = [
        f"[{d.get('id', '?')}]",
        f"r/{d.get('subreddit', '?')}",
        date_str(d.get("created_utc")),
        score_str(d),
        f"{num(d.get('num_comments'))}c",
    ]
    if not omit_author:
        parts.append(author_str(d))
    flair = flatten(d.get("link_flair_text"))
    if flair:
        parts.append(f"[{flair}]")
    parts.append(post_kind(d))
    parts.extend(post_flags(d, full=full))
    return " ".join(parts)


def _origin(d: Mapping[str, Any]) -> Mapping[str, Any]:
    lst = d.get("crosspost_parent_list")
    if isinstance(lst, list) and lst and isinstance(lst[0], Mapping):
        return lst[0]
    return {}


def _origin_id(d: Mapping[str, Any]) -> str:
    o = _origin(d)
    if o.get("id"):
        return str(o["id"])
    parent = str(d.get("crosspost_parent") or "")
    return parent.removeprefix("t3_") or "?"


def _gallery_count(d: Mapping[str, Any]) -> int:
    items = (d.get("gallery_data") or {}).get("items")
    if isinstance(items, list):
        return len(items)
    meta = d.get("media_metadata")
    return len(meta) if isinstance(meta, Mapping) else 0


def _poll_line(d: Mapping[str, Any]) -> str:
    pd = d.get("poll_data") or {}
    opts = []
    for o in pd.get("options") or []:
        if not isinstance(o, Mapping):
            continue
        text = flatten(o.get("text"))
        if o.get("vote_count") is not None:
            text += f" ({num(o.get('vote_count'))})"
        opts.append(text)
    line = "poll: " + (" | ".join(opts) if opts else "(options not visible)")
    if pd.get("total_vote_count") is not None:
        line += f"; {num(pd.get('total_vote_count'))} votes"
    end = pd.get("voting_end_timestamp")
    if end:
        try:
            line += f"; voting ends {date_str(float(end) / 1000)}"
        except (TypeError, ValueError):
            pass
    return line


def _video_line(d: Mapping[str, Any]) -> str:
    url = str(d.get("url_overridden_by_dest") or d.get("url") or "")
    rv = ((d.get("media") or {}) if isinstance(d.get("media"), Mapping) else {}).get("reddit_video")
    if isinstance(rv, Mapping) and rv.get("duration"):
        return f"video: {url} ({rv.get('duration')} s)"
    return f"video: {url}"


def _link_line(d: Mapping[str, Any], kind: str) -> str:
    url = str(d.get("url_overridden_by_dest") or d.get("url") or "")
    if kind == "video":
        return _video_line(d)
    if kind == "gallery":
        return f"gallery: {_gallery_count(d)} items"
    if kind == "poll":
        return _poll_line(d)
    if kind == "crosspost":
        o = _origin(d)
        line = f"crosspost of [{_origin_id(d)}] r/{o.get('subreddit', '?')}"
        if o:
            line += f" {author_str(o)} {date_str(o.get('created_utc'))}"
            okind = post_kind(o)
            if okind not in ("self", "poll", "post"):
                line += f" {okind}: {o.get('url_overridden_by_dest') or o.get('url') or ''}".rstrip()
        return line
    if kind in ("link", "image"):
        return f"url: {url}"
    return ""


def listing_post(d: Mapping[str, Any], body_chars: int, *, omit_author: bool = False) -> str:
    kind = post_kind(d)
    lines = [post_head(d, omit_author=omit_author), flatten(d.get("title")) or "(no title)"]
    extra = _link_line(d, kind)
    if extra:
        lines.append(extra)
    body = d.get("selftext") or ""
    prefix = ""
    if not body and kind == "crosspost":
        body = _origin(d).get("selftext") or ""
        prefix = "(original) "
    body = flatten(body)
    if body:
        lines.append("  " + prefix + truncate(body, body_chars))
    return "\n".join(lines)


def post_detail(d: Mapping[str, Any], body_chars: int) -> str:
    """Full header for get_post / get_posts: every field, full (or capped) body."""
    kind = post_kind(d)
    lines = [post_head(d, full=True), "title: " + (flatten(d.get("title")) or "(no title)")]
    extra = _link_line(d, kind)
    if extra:
        lines.append(extra)
    if kind in ("link", "image", "video") and d.get("domain"):
        lines[-1] += f" ({d.get('domain')})"
    if kind == "gallery":
        caps = [
            flatten(i.get("caption"))
            for i in ((d.get("gallery_data") or {}).get("items") or [])
            if isinstance(i, Mapping) and i.get("caption")
        ]
        if caps:
            lines.append("captions: " + truncate(" | ".join(caps), 600))
    if kind == "crosspost":
        o = _origin(d)
        if o.get("title"):
            lines.append("original title: " + flatten(o.get("title")))
    lines.append("permalink: " + (permalink(d) or f"{REDDIT}/comments/{d.get('id', '')}/"))
    body = clean_text(d.get("selftext"))
    label = "body"
    if not body and kind == "crosspost":
        body = clean_text(_origin(d).get("selftext"))
        label = "body (from the original post)"
    if body:
        lines.append(f"{label}:")
        lines.append(truncate(body, body_chars))
    return "\n".join(lines)


# ---------------------------------------------------------------- comments, subreddits, users


def listing_comment(d: Mapping[str, Any], body_chars: int, *, omit_author: bool = False) -> str:
    link = str(d.get("link_id") or "").removeprefix("t3_")
    parts = [
        f"[{d.get('id', '?')}]",
        f"r/{d.get('subreddit', '?')}",
        date_str(d.get("created_utc")),
        score_str(d, with_ratio=False),
    ]
    if not omit_author:
        parts.append(author_str(d))
    parts.append(f"comment on [{link or '?'}]")
    if d.get("is_submitter"):
        parts.append("(OP)")
    if d.get("edited"):
        parts.append("(edited)")
    lines = [" ".join(parts)]
    if d.get("link_title"):
        lines.append("  re: " + flatten(d.get("link_title")))
    body = flatten(d.get("body"))
    if body:
        lines.append("  " + truncate(body, body_chars))
    return "\n".join(lines)


def listing_subreddit(d: Mapping[str, Any], desc_chars: int = 300) -> str:
    parts = [
        f"r/{d.get('display_name', '?')}",
        f"{num(d.get('subscribers'))} subscribers",
        f"created {date_str(d.get('created_utc'))}",
    ]
    if d.get("over18"):
        parts.append("nsfw")
    stype = d.get("subreddit_type")
    if stype and stype != "public":
        parts.append(str(stype))
    if d.get("quarantine"):
        parts.append("quarantined")
    lines = [" ".join(parts)]
    desc = flatten(d.get("public_description") or d.get("title") or "")
    if desc:
        lines.append("  " + truncate(desc, desc_chars))
    return "\n".join(lines)


def is_nsfw(thing: Mapping[str, Any]) -> bool:
    """True when Reddit marks a post, comment or community 18+ (also via a crossposted original).

    Accepts a listing child ({"kind", "data"}) or a bare data mapping. Posts in NSFW
    communities carry over_18 too, so this also covers whole communities' content.
    """
    d = thing.get("data") if isinstance(thing.get("data"), Mapping) else thing
    if not isinstance(d, Mapping):
        return False
    if d.get("over_18") or d.get("over18"):
        return True
    parents = d.get("crosspost_parent_list")
    if isinstance(parents, list):
        return any(isinstance(p, Mapping) and (p.get("over_18") or p.get("over18")) for p in parents)
    return False


def render_thing(thing: Mapping[str, Any], body_chars: int, *, omit_author: bool = False) -> str:
    """Render one listing child of any kind; never raises."""
    try:
        kind = thing.get("kind")
        d = thing.get("data") or {}
        if kind == "t3":
            return listing_post(d, body_chars, omit_author=omit_author)
        if kind == "t1":
            return listing_comment(d, body_chars, omit_author=omit_author)
        if kind == "t5":
            return listing_subreddit(d)
        if kind == "t2":
            return f"u/{d.get('name', '?')} created {date_str(d.get('created_utc'))}"
        return f"[{d.get('id', '?')}] ({kind or 'unknown'} item; not rendered)"
    except Exception as exc:  # one bad item must not kill the listing
        ident = "?"
        try:
            ident = str((thing.get("data") or {}).get("id") or "?")
        except Exception:
            pass
        return f"[{ident}] (could not render this item: {type(exc).__name__})"


@dataclass
class ListingRender:
    text: str
    shown: int
    total: int
    truncated: bool
    last_fullname: str | None


def render_listing(
    children: Iterable[Mapping[str, Any]],
    *,
    body_chars: int,
    max_chars: int,
    omit_author: bool = False,
) -> ListingRender:
    items = list(children)
    blocks: list[str] = []
    size = 0
    last: str | None = None
    for i, thing in enumerate(items):
        block = render_thing(thing, body_chars, omit_author=omit_author)
        if blocks and size + len(block) + 2 > max_chars:
            return ListingRender("\n\n".join(blocks), i, len(items), True, last)
        if not blocks and len(block) > max_chars:
            block = truncate(block, max_chars)
        blocks.append(block)
        size += len(block) + 2
        last = fullname(thing) or last
    return ListingRender("\n\n".join(blocks), len(items), len(items), False, last)


def next_line(after: str | None, res: ListingRender) -> str:
    if res.truncated and res.last_fullname:
        return (
            f"next: after={res.last_fullname} (output budget reached: {res.shown} of {res.total} "
            "items shown; pass this cursor to continue, or lower body_chars to fit more per call)"
        )
    if after:
        return f"next: after={after}"
    return "next: none (end of results)"


# ---------------------------------------------------------------- comment trees


@dataclass
class CommentStats:
    shown: int = 0
    stubs: int = 0  # every "more" stub seen, listed or not
    stub_comments: int = 0
    continues: list[str] = field(default_factory=list)  # every "continue this thread" parent
    unshown_ids: list[str] = field(default_factory=list)
    unshown_comments: int = 0
    pending_ids: list[str] = field(default_factory=list)
    root_stubs: list[Mapping[str, Any]] = field(default_factory=list)
    # Stubs inside comments cut by the budget: counted above, but their ids are not printed.
    hidden_stubs: int = 0
    hidden_stub_comments: int = 0
    hidden_continues: int = 0


def _iter_tree(children: Iterable[Mapping[str, Any]]) -> Iterable[Mapping[str, Any]]:
    """Every node of a comment forest, depth first, without recursion (any depth is safe)."""
    end = object()
    stack = [iter(children)]
    while stack:
        thing = next(stack[-1], end)
        if thing is end:
            stack.pop()
            continue
        if not isinstance(thing, Mapping):
            continue
        yield thing
        if thing.get("kind") == "t1":
            stack.append(iter(children_of(thing.get("data") or {})))


def count_loaded(children: Iterable[Mapping[str, Any]]) -> int:
    return sum(1 for t in _iter_tree(children) if t.get("kind") == "t1")


def tree_ids(children: Iterable[Mapping[str, Any]]) -> set[str]:
    """Ids of every loaded comment plus every id listed in a "more" stub."""
    ids: set[str] = set()
    for t in _iter_tree(children):
        d = t.get("data")
        if not isinstance(d, Mapping):
            continue
        if t.get("kind") == "t1" and d.get("id"):
            ids.add(str(d["id"]))
        elif t.get("kind") == "more":
            ids.update(str(i) for i in (d.get("children") or []))
    return ids


def stub_ids(ids: list[str], cap: int = MAX_STUB_IDS) -> str:
    shown = ids[:cap]
    s = ",".join(shown)
    if len(ids) > len(shown):
        s += f" (+{len(ids) - len(shown)} more ids not listed)"
    return s


def comment_head(d: Mapping[str, Any], *, focus: str | None = None) -> str:
    body = str(d.get("body") or "").strip()
    cid = d.get("id", "?")
    if body in DELETED_BODIES and (d.get("author") in (None, "[deleted]")):
        head = f"[{cid}] {date_str(d.get('created_utc'))} {body}"
        return f"{head} <- requested comment" if focus and cid == focus else head
    parts = [f"[{cid}]", date_str(d.get("created_utc")), score_str(d, with_ratio=False), author_str(d)]
    if d.get("is_submitter"):
        parts.append("(OP)")
    if d.get("edited"):
        parts.append("(edited)")
    flair = flatten(d.get("author_flair_text"))
    if flair:
        parts.append(f"[{flair}]")
    dist = d.get("distinguished")
    if dist:
        parts.append("(mod)" if dist == "moderator" else f"({dist})")
    if d.get("stickied"):
        parts.append("(pinned)")
    if focus and cid == focus:
        parts.append("<- requested comment")
    return " ".join(parts)


def comment_block(d: Mapping[str, Any], depth: int, *, focus: str | None = None) -> str:
    pad = "  " * depth
    head = pad + comment_head(d, focus=focus)
    body = str(d.get("body") or "").strip()
    if body in DELETED_BODIES and d.get("author") in (None, "[deleted]"):
        return head
    text = clean_text(body, keep_blank_lines=False)
    if not text:
        return head
    return head + "\n" + indent(text, pad + "  ")


def stub_line(d: Mapping[str, Any], depth: int) -> str:
    pad = "  " * depth
    ids = [str(i) for i in (d.get("children") or [])]
    if d.get("id") == "_" or (not ids and not d.get("count")):
        parent = str(d.get("parent_id") or "").split("_", 1)[-1]
        return f'{pad}[continue: deeper replies under [{parent}] -> get_post(post, comment_id="{parent}")]'
    return f"{pad}[more: {plural(d.get('count'), 'comment')}; ids: {stub_ids(ids)}]"


class CommentRenderer:
    """Depth-first renderer with a character budget.

    Comments that do not fit are not dropped silently: their ids are collected
    in ``stats.unshown_ids`` (top-most only) so the caller can point the agent
    at expand_comments.
    """

    def __init__(self, *, max_chars: int, focus: str | None = None, root_stubs_inline: bool = False) -> None:
        self.max_chars = max(0, max_chars)
        self.focus = focus
        self.root_stubs_inline = root_stubs_inline
        self.parts: list[str] = []
        self.size = 0
        self.stopped = False
        self.stats = CommentStats()

    @property
    def text(self) -> str:
        return "\n".join(self.parts)

    def add(self, block: str) -> bool:
        if self.stopped or self.size + len(block) + 1 > self.max_chars:
            self.stopped = True
            return False
        self.parts.append(block)
        self.size += len(block) + 1
        return True

    def _count_hidden(self, children: Iterable[Mapping[str, Any]]) -> None:
        for t in _iter_tree(children):
            if t.get("kind") == "more":
                self._note_stub(t.get("data") or {}, hidden=True)

    def _note_stub(self, d: Mapping[str, Any], *, hidden: bool = False) -> None:
        s = self.stats
        if d.get("id") == "_" or (not d.get("children") and not d.get("count")):
            s.continues.append(str(d.get("parent_id") or "").split("_", 1)[-1])
            if hidden:
                s.hidden_continues += 1
            return
        try:
            n = int(d.get("count") or 0)
        except (TypeError, ValueError, OverflowError):
            n = 0
        s.stubs += 1
        s.stub_comments += n
        if hidden:
            s.hidden_stubs += 1
            s.hidden_stub_comments += n

    def walk(self, children: Iterable[Mapping[str, Any]], depth: int = 0) -> None:
        for thing in children:
            try:
                self._visit(thing, depth)
            except Exception as exc:  # malformed node: say so and keep going
                self.add("  " * depth + f"(could not render a comment: {type(exc).__name__})")

    def _visit(self, thing: Mapping[str, Any], depth: int) -> None:
        kind = thing.get("kind")
        d = thing.get("data") or {}
        if kind == "more":
            if depth == 0 and not self.root_stubs_inline:
                self.stats.root_stubs.append(d)
                self._note_stub(d)
                return
            self._note_stub(d)
            if not self.add(stub_line(d, depth)):
                self.stats.pending_ids.extend(str(i) for i in (d.get("children") or []))
            return
        if kind != "t1":
            self.add("  " * depth + f"({kind or 'unknown'} item not rendered)")
            return
        replies = children_of(d)
        if self.stopped or not self.add(comment_block(d, depth, focus=self.focus)):
            self.stats.unshown_ids.append(str(d.get("id")))
            self.stats.unshown_comments += 1 + count_loaded(replies)
            self._count_hidden(replies)
            return
        self.stats.shown += 1
        self.walk(replies, depth + 1)


def build_morechildren_forest(things: Iterable[Mapping[str, Any]]) -> list[tuple[str, list[dict]]]:
    """Turn /api/morechildren's flat list into (parent fullname, subtree) groups."""
    items = [t for t in things if isinstance(t, Mapping)]
    nodes: dict[str, dict] = {}
    for t in items:
        d = t.get("data") or {}
        if t.get("kind") == "t1":
            name = str(d.get("name") or f"t1_{d.get('id', '')}")
            nodes[name] = {"kind": "t1", "data": {**d, "replies": {"kind": "Listing", "data": {"children": []}}}}
    groups: dict[str, list[dict]] = {}
    for t in items:
        d = t.get("data") or {}
        if t.get("kind") == "t1":
            node = nodes[str(d.get("name") or f"t1_{d.get('id', '')}")]
        else:
            node = {"kind": t.get("kind"), "data": d}
        parent = str(d.get("parent_id") or "")
        if parent in nodes and nodes[parent] is not node:
            nodes[parent]["data"]["replies"]["data"]["children"].append(node)
        else:
            groups.setdefault(parent, []).append(node)
    return list(groups.items())


# ---------------------------------------------------------------- users


def activity_summary(children: Iterable[Mapping[str, Any]], top: int = 8) -> str:
    subs: Counter[str] = Counter()
    n_posts = n_comments = 0
    dates: list[float] = []
    for t in children:
        d = t.get("data") or {}
        subs[str(d.get("subreddit") or "?")] += 1
        if t.get("kind") == "t3":
            n_posts += 1
        elif t.get("kind") == "t1":
            n_comments += 1
        try:
            dates.append(float(d.get("created_utc")))
        except (TypeError, ValueError):
            pass
    n = sum(subs.values())
    if not n:
        return "Activity: none visible (no posts or comments returned)."
    parts = [f"r/{s} {k} ({round(100 * k / n)}%)" for s, k in subs.most_common(top)]
    line = f"Activity by subreddit ({n} items: {n_posts} posts, {n_comments} comments"
    if dates:
        line += f", {date_str(min(dates))} to {date_str(max(dates))}"
    line += "): " + ", ".join(parts)
    if len(subs) > top:
        line += f", plus {len(subs) - top} other subreddits"
    return line


def user_header(d: Mapping[str, Any], now: float, *, show_nsfw_profile: bool = True) -> str:
    created = d.get("created_utc")
    parts = [f"u/{d.get('name', '?')}", f"created {date_str(created)}"]
    try:
        parts[-1] += f" ({(now - float(created)) / 31_557_600:.1f} years)"
    except (TypeError, ValueError):
        pass
    karma = (
        f"karma {num(d.get('total_karma'))} total"
        f" ({num(d.get('link_karma'))} post, {num(d.get('comment_karma'))} comment)"
    )
    parts.append(karma)
    flags = []
    if d.get("is_employee"):
        flags.append("Reddit employee")
    if d.get("is_mod"):
        flags.append("moderator somewhere")
    if d.get("verified") or d.get("has_verified_email"):
        flags.append("verified email")
    if d.get("is_gold"):
        flags.append("premium")
    sub = d.get("subreddit") if isinstance(d.get("subreddit"), Mapping) else {}
    if sub and sub.get("over_18"):
        flags.append("nsfw profile")
    if flags:
        parts.append("flags: " + ", ".join(flags))
    line = "  ".join(parts)
    desc = flatten(sub.get("public_description") if sub else "")
    if desc and sub.get("over_18") and not show_nsfw_profile:
        line += "\nprofile: hidden (NSFW profile; include_nsfw=true shows it)"
    elif desc:
        line += "\nprofile: " + truncate(desc, 300)
    return line
