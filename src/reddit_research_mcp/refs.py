"""Normalise the identifiers agents pass in: subreddits, posts, comments, users, URLs.

Every parser here is offline. Bad input raises InputError with a message that
names the argument and says what to pass instead, so no request is wasted on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit


class InputError(ValueError):
    """An argument that cannot be turned into a valid Reddit request."""


_SUB_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9_]{1,20}")
_ID36 = re.compile(r"[0-9a-z]{1,13}")
_USER = re.compile(r"[A-Za-z0-9_-]{1,20}")
_WIKI_PAGE = re.compile(r"[A-Za-z0-9_\-/.:]{1,256}")
_MEDIA_HOSTS = ("i.redd.it", "v.redd.it", "preview.redd.it", "external-preview.redd.it")


@dataclass(frozen=True)
class PostRef:
    """A post id (base36, lowercase) and, for comment permalinks, a comment id."""

    post_id: str
    comment_id: str | None = None


def _looks_like_url(s: str) -> bool:
    low = s.lower()
    if "://" in low:
        return True
    return bool(re.match(r"^(?:[a-z0-9-]+\.)*(?:reddit\.com|redd\.it)(?:/|$)", low))


def _split_url(s: str):
    if "://" not in s:
        s = "https://" + s
    parts = urlsplit(s)
    return (parts.hostname or "").lower(), parts.path or "/"


def _is_reddit_host(host: str) -> bool:
    return host == "reddit.com" or host.endswith(".reddit.com") or host == "redd.it"


# ---------------------------------------------------------------- subreddits


def normalize_subreddit(
    raw: str | None,
    *,
    arg: str = "subreddit",
    allow_empty: bool = False,
    allow_multi: bool = True,
) -> str:
    """Return a subreddit path segment: "name" or "a+b" for several.

    Accepts "r/name", "/r/name/", "R/Name", reddit.com URLs, surrounding
    whitespace, and lists separated by "+" or ",". Case is kept
    because Reddit matches subreddit names case-insensitively.
    """
    s = (raw or "").strip()
    if s and _looks_like_url(s):
        host, path = _split_url(s)
        m = re.search(r"/r/([^/?#]+)", path, re.I)
        if not _is_reddit_host(host) or not m:
            raise InputError(
                f"{arg} {raw!r} is not a subreddit; pass a name such as dataengineering "
                "(use search_subreddits to find one)"
            )
        s = m.group(1)
    s = re.sub(r"^/?r/", "", s.strip(), flags=re.I).strip("/")
    pieces = [p for p in re.split(r"\s*[+,]\s*", s) if p]
    names: list[str] = []
    for p in pieces:
        p = p.strip("/")
        p = re.sub(r"^/?r/", "", p, flags=re.I)
        if not p:
            continue
        if not _SUB_PART.fullmatch(p):
            raise InputError(
                f"{arg} {raw!r} is not a valid subreddit name (letters, digits and underscores, "
                "2 to 21 characters). Use search_subreddits to find the right name"
            )
        if p.lower() not in {n.lower() for n in names}:
            names.append(p)
    if not names:
        if allow_empty:
            return ""
        raise InputError(
            f"{arg} is empty; pass a subreddit name such as dataengineering, "
            "or use search_subreddits to find one"
        )
    if len(names) > 1 and not allow_multi:
        raise InputError(f"{arg} takes one subreddit here; got {'+'.join(names)!r}")
    return "+".join(names)


# ---------------------------------------------------------------- posts and comments


def _post_from_url(s: str, raw: str, arg: str) -> PostRef:
    host, path = _split_url(s)
    if host in _MEDIA_HOSTS:
        raise InputError(
            f"{arg} {raw!r} is a media file URL, not a post; use find_other_discussions "
            "with that URL to find the threads that link it"
        )
    if not _is_reddit_host(host):
        raise InputError(
            f"{arg} {raw!r} is not a Reddit post URL. Pass a post id (1abc234), a t3_ fullname "
            "or a reddit.com / redd.it post link. For an external article use find_other_discussions"
        )
    if host == "redd.it":
        seg = path.strip("/").split("/")[0]
        return _post_ref(seg, None, raw, arg)
    m = re.search(r"/comments/([0-9a-z]+)(?:/[^/]*)?(?:/([0-9a-z]+))?", path, re.I)
    if m:
        return _post_ref(m.group(1), m.group(2), raw, arg)
    m = re.search(r"/gallery/([0-9a-z]+)", path, re.I)
    if m:
        return _post_ref(m.group(1), None, raw, arg)
    if re.search(r"/r/[^/]+/s/[A-Za-z0-9]+", path):
        raise InputError(
            f"{arg} {raw!r} is a share link (/s/...), which the API cannot resolve. Open it in a "
            "browser and pass the /comments/ URL it redirects to"
        )
    raise InputError(
        f"{arg} {raw!r} is a Reddit URL but not a post; post URLs contain /comments/<id>/"
    )


def _post_ref(pid: str, cid: str | None, raw: str, arg: str) -> PostRef:
    pid = pid.lower()
    if not _ID36.fullmatch(pid):
        raise InputError(
            f"{arg} {raw!r} does not contain a valid post id; pass the [id] shown in a listing "
            "(e.g. 1abc234), a t3_ fullname or a post URL"
        )
    if cid is not None:
        cid = cid.lower()
        if not _ID36.fullmatch(cid):
            cid = None
    return PostRef(pid, cid)


def parse_post_ref(raw: str | None, *, arg: str = "post") -> PostRef:
    """Parse a post id36, t3_ fullname, permalink, comment permalink or redd.it link."""
    s = (raw or "").strip().strip("<>").strip()
    if not s:
        raise InputError(f"{arg} is empty; pass a post id such as 1abc234 or a post URL")
    if _looks_like_url(s):
        return _post_from_url(s, raw or "", arg)
    low = s.lower()
    if low.startswith("t1_"):
        raise InputError(
            f"{arg} {raw!r} is a comment id. Pass the post id with comment_id={s[3:]!r}, "
            "or the comment permalink"
        )
    if low.startswith("t3_"):
        s = s[3:]
    return _post_ref(s, None, raw or "", arg)


def parse_comment_id(raw: str | None, *, arg: str = "comment_id") -> str:
    """Parse a comment id36, t1_ fullname or comment permalink into a lowercase id36."""
    s = (raw or "").strip()
    if not s:
        raise InputError(f"{arg} is empty; pass a comment id such as abc1234")
    if _looks_like_url(s):
        ref = _post_from_url(s, raw or "", arg)
        if ref.comment_id is None:
            raise InputError(f"{arg} {raw!r} is a post URL, not a comment permalink")
        return ref.comment_id
    if s.lower().startswith("t1_"):
        s = s[3:]
    s = s.lower()
    if not _ID36.fullmatch(s):
        raise InputError(f"{arg} {raw!r} is not a comment id; pass the [id] shown before a comment")
    return s


def parse_comment_ids(raw: list[str] | str | None, *, arg: str = "comment_ids") -> list[str]:
    """Parse a list (or a comma/space separated string) of comment ids, deduplicated in order."""
    items: list[str] = []
    if raw is None:
        raw = []
    if isinstance(raw, str):
        raw = [raw]
    for entry in raw:
        items.extend(p for p in re.split(r"[\s,]+", str(entry)) if p)
    ids: list[str] = []
    seen: set[str] = set()
    for item in items:
        cid = parse_comment_id(item, arg=arg)
        if cid not in seen:
            seen.add(cid)
            ids.append(cid)
    if not ids:
        raise InputError(
            f"{arg} is empty; pass the ids listed on a [more ...] line of get_post output"
        )
    return ids


def parse_post_refs(raw: list[str] | str | None, *, arg: str = "posts") -> list[str]:
    """Parse several post references into unique post ids, keeping order."""
    items: list[str] = []
    if raw is None:
        raw = []
    if isinstance(raw, str):
        raw = [raw]
    for entry in raw:
        items.extend(p for p in re.split(r"[\s,]+", str(entry)) if p)
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        pid = parse_post_ref(item, arg=arg).post_id
        if pid not in seen:
            seen.add(pid)
            out.append(pid)
    if not out:
        raise InputError(f"{arg} is empty; pass post ids such as ['1abc234', '1def567']")
    return out


# ---------------------------------------------------------------- users, wiki, urls


def normalize_username(raw: str | None, *, arg: str = "username") -> str:
    """Accept "name", "u/name", "/u/name", "@name" or a reddit.com/user/name URL."""
    s = (raw or "").strip()
    if s.lower() in ("[deleted]", "[removed]", "u/[deleted]"):
        raise InputError(
            f"{s} is Reddit's placeholder for a deleted or removed author, not an account; "
            "there is no profile or history to read"
        )
    if s and _looks_like_url(s):
        host, path = _split_url(s)
        m = re.search(r"/(?:u|user)/([^/?#]+)", path, re.I)
        if not _is_reddit_host(host) or not m:
            raise InputError(f"{arg} {raw!r} is not a Reddit user; pass a username without the u/ prefix")
        s = m.group(1)
    s = re.sub(r"^/?(?:u|user)/", "", s.strip(), flags=re.I).strip("/").lstrip("@")
    if not s:
        raise InputError(f"{arg} is empty; pass a Reddit username without the u/ prefix")
    if not _USER.fullmatch(s):
        raise InputError(
            f"{arg} {raw!r} is not a valid Reddit username (letters, digits, _ and -, up to 20)"
        )
    return s


def normalize_wiki_page(raw: str | None, *, arg: str = "page") -> str:
    """Return a wiki page path, or "" to mean "list the pages"."""
    s = (raw or "").strip()
    if s and _looks_like_url(s):
        _, path = _split_url(s)
        m = re.search(r"/wiki/(.*)$", path, re.I)
        s = m.group(1) if m else ""
    s = s.strip().strip("/")
    s = re.sub(r"^wiki/", "", s, flags=re.I)
    if s.lower() in ("", "pages"):
        return ""
    if not _WIKI_PAGE.fullmatch(s):
        raise InputError(f"{arg} {raw!r} is not a wiki page name such as index or faq")
    return s.lower()


@dataclass(frozen=True)
class DiscussionTarget:
    """Either a Reddit post (find its crossposts) or an external URL (find threads linking it)."""

    post: PostRef | None = None
    url: str | None = None


def parse_discussion_target(raw: str | None, *, arg: str = "post_or_url") -> DiscussionTarget:
    s = (raw or "").strip().strip("<>").strip()
    if not s:
        raise InputError(f"{arg} is empty; pass a post id, a Reddit post URL or an article URL")
    if _looks_like_url(s):
        host, _ = _split_url(s)
        if _is_reddit_host(host):
            try:
                return DiscussionTarget(post=parse_post_ref(s, arg=arg))
            except InputError:
                pass
        url = s if "://" in s else "https://" + s
        return DiscussionTarget(url=url)
    if " " not in s and "." in s and "/" in s:
        return DiscussionTarget(url="https://" + s)
    return DiscussionTarget(post=parse_post_ref(s, arg=arg))


def url_variants(url: str) -> list[str]:
    """Close variants of a URL for Reddit's exact-match URL lookup, most likely first."""
    out: list[str] = []
    base = url.split("#", 1)[0]
    no_query = base.split("?", 1)[0]
    for cand in (
        no_query[:-1] if no_query.endswith("/") else no_query + "/",
        no_query,
    ):
        if cand and cand != url and cand not in out:
            out.append(cand)
    return out
