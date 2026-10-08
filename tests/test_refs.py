import time

import pytest

from reddit_research_mcp import refs
from reddit_research_mcp.refs import (
    InputError,
    PostRef,
    normalize_subreddit,
    normalize_username,
    normalize_wiki_page,
    parse_comment_id,
    parse_comment_ids,
    parse_discussion_target,
    parse_post_ref,
    parse_post_refs,
    url_variants,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("abc123", PostRef("abc123")),
        ("ABC123", PostRef("abc123")),
        ("  t3_abc123 ", PostRef("abc123")),
        ("T3_ABC123", PostRef("abc123")),
        ("https://www.reddit.com/r/testsub/comments/abc123/some_slug/", PostRef("abc123")),
        ("https://www.reddit.com/r/testsub/comments/abc123/", PostRef("abc123")),
        ("https://old.reddit.com/r/testsub/comments/abc123/some_slug/", PostRef("abc123")),
        ("https://np.reddit.com/r/testsub/comments/abc123/some_slug/?utm_source=share", PostRef("abc123")),
        ("www.reddit.com/r/testsub/comments/abc123/some_slug", PostRef("abc123")),
        ("reddit.com/comments/abc123", PostRef("abc123")),
        ("https://www.reddit.com/r/testsub/comments/abc123/some_slug/def456/", PostRef("abc123", "def456")),
        ("https://www.reddit.com/r/testsub/comments/abc123/some_slug/def456/?context=3", PostRef("abc123", "def456")),
        ("https://www.reddit.com/r/TestSub/comments/ABC123/Slug/DEF456", PostRef("abc123", "def456")),
        ("https://redd.it/abc123", PostRef("abc123")),
        ("redd.it/abc123", PostRef("abc123")),
        ("https://www.reddit.com/gallery/abc123", PostRef("abc123")),
        ("<https://redd.it/abc123>", PostRef("abc123")),
    ],
)
def test_parse_post_ref(raw, expected):
    assert parse_post_ref(raw) == expected


@pytest.mark.parametrize(
    "raw, fragment",
    [
        ("", "empty"),
        ("   ", "empty"),
        ("t1_def456", "comment id"),
        ("https://example.com/article", "not a Reddit post URL"),
        ("https://i.redd.it/picture.jpg", "media file URL"),
        ("https://www.reddit.com/r/testsub/s/AbCdEf123", "share link"),
        ("https://www.reddit.com/r/testsub/", "not a post"),
        ("not an id!", "valid post id"),
    ],
)
def test_parse_post_ref_errors(raw, fragment):
    with pytest.raises(InputError) as e:
        parse_post_ref(raw)
    assert fragment in str(e.value)
    assert "post" in str(e.value)  # names the argument


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("def456", "def456"),
        ("t1_DEF456", "def456"),
        ("https://www.reddit.com/r/testsub/comments/abc123/slug/def456/", "def456"),
    ],
)
def test_parse_comment_id(raw, expected):
    assert parse_comment_id(raw) == expected


def test_parse_comment_id_rejects_post_url():
    with pytest.raises(InputError, match="not a comment permalink"):
        parse_comment_id("https://www.reddit.com/r/testsub/comments/abc123/slug/")


def test_parse_comment_ids_accepts_list_or_string_and_dedupes():
    assert parse_comment_ids(["a1", "t1_b2", "a1"]) == ["a1", "b2"]
    assert parse_comment_ids("a1, b2 c3") == ["a1", "b2", "c3"]
    assert parse_comment_ids(["a1,b2"]) == ["a1", "b2"]
    with pytest.raises(InputError, match="comment_ids is empty"):
        parse_comment_ids([])


def test_parse_post_refs():
    assert parse_post_refs(["abc123", "t3_def456", "https://redd.it/abc123"]) == ["abc123", "def456"]
    assert parse_post_refs("abc123,def456") == ["abc123", "def456"]
    with pytest.raises(InputError, match="posts"):
        parse_post_refs(["t1_zzz"])


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("dataengineering", "dataengineering"),
        ("  r/dataengineering  ", "dataengineering"),
        ("/r/dataengineering/", "dataengineering"),
        ("R/DataEngineering", "DataEngineering"),
        ("https://www.reddit.com/r/dataengineering/top/?t=year", "dataengineering"),
        ("dataengineering+dremio_lakehouse", "dataengineering+dremio_lakehouse"),
        ("r/dataengineering + r/dremio_lakehouse", "dataengineering+dremio_lakehouse"),
        ("dataengineering, dremio_lakehouse", "dataengineering+dremio_lakehouse"),
        ("a1+A1+b2", "a1+b2"),
        ("all", "all"),
    ],
)
def test_normalize_subreddit(raw, expected):
    assert normalize_subreddit(raw) == expected


def test_normalize_subreddit_errors():
    with pytest.raises(InputError, match="subreddit is empty"):
        normalize_subreddit("  ")
    assert normalize_subreddit("", allow_empty=True) == ""
    assert normalize_subreddit("r/", allow_empty=True) == ""
    with pytest.raises(InputError, match="not a valid subreddit name"):
        normalize_subreddit("data engineering")
    with pytest.raises(InputError, match="not a valid subreddit name"):
        normalize_subreddit("bad-name!")
    with pytest.raises(InputError, match="one subreddit"):
        normalize_subreddit("a1+b2", allow_multi=False)
    with pytest.raises(InputError, match="not a subreddit"):
        normalize_subreddit("https://example.com/r/x")


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("example_user", "example_user"),
        ("u/example_user", "example_user"),
        ("/u/example_user/", "example_user"),
        ("@example_user", "example_user"),
        ("https://www.reddit.com/user/example_user/comments/", "example_user"),
    ],
)
def test_normalize_username(raw, expected):
    assert normalize_username(raw) == expected


def test_normalize_username_errors():
    with pytest.raises(InputError, match="username is empty"):
        normalize_username("u/")
    with pytest.raises(InputError, match="not a valid Reddit username"):
        normalize_username("has space")


def test_normalize_wiki_page():
    assert normalize_wiki_page("index") == "index"
    assert normalize_wiki_page("/wiki/FAQ/") == "faq"
    assert normalize_wiki_page("") == ""
    assert normalize_wiki_page("pages") == ""
    assert normalize_wiki_page("https://www.reddit.com/r/testsub/wiki/tools/databases") == "tools/databases"
    with pytest.raises(InputError):
        normalize_wiki_page("bad page?")


def test_discussion_target():
    assert parse_discussion_target("abc123").post == PostRef("abc123")
    assert parse_discussion_target("https://www.reddit.com/r/x1/comments/abc123/s/").post == PostRef("abc123")
    assert parse_discussion_target("https://example.com/a?b=1").url == "https://example.com/a?b=1"
    assert parse_discussion_target("example.com/article/1").url == "https://example.com/article/1"
    # a reddit URL that is not a post is treated as a link to look up
    assert parse_discussion_target("https://www.reddit.com/r/x1/wiki/").url == "https://www.reddit.com/r/x1/wiki/"
    with pytest.raises(InputError, match="post_or_url is empty"):
        parse_discussion_target(" ")


def test_url_variants():
    assert url_variants("https://example.com/a/") == ["https://example.com/a"]
    assert url_variants("https://example.com/a") == ["https://example.com/a/"]
    assert url_variants("https://example.com/a?x=1") == ["https://example.com/a/", "https://example.com/a"]


# ---------------------------------------------------------------- hostile input
#
# Everything below feeds identifiers that users paste (and that Reddit content or a
# prompt-injected page could make an agent paste) into the parsers that build Reddit API
# paths. A parser may reject or treat the value as an external link, but it must never
# accept a look-alike host as Reddit, and it must never let a character through that
# changes the request path or query.

POST_PATH = "r/x1/comments/abc123/slug/"

# Hosts that are not Reddit, in every disguise the URL grammar offers.
NOT_REDDIT_URLS = [
    f"https://reddit.com.evil.com/{POST_PATH}",
    f"https://www.reddit.com.evil.com/{POST_PATH}",
    "evil.com/reddit.com/comments/abc123",
    "https://evil.com/reddit.com/comments/abc123",
    "https://evil.com/?x=reddit.com/comments/abc123",
    "https://evil.com?x=reddit.com/comments/abc123",
    f"https://reddit.com@evil.com/{POST_PATH}",
    f"https://user:secret@reddit.com/{POST_PATH}",
    f"https://evil.com#@reddit.com/{POST_PATH}",
    f"https://evil.com\\@reddit.com/{POST_PATH}",
    f"https://evil.com\\.reddit.com/{POST_PATH}",
    f"https://evil.com/\\reddit.com/{POST_PATH}",
    f"https://evilreddit.com/{POST_PATH}",
    f"https://notreddit.com/{POST_PATH}",
    f"https://reddit.com.au/{POST_PATH}",
    f"https://reddit.company/{POST_PATH}",
    f"https://reddit.com:evil/{POST_PATH}",
    f"https://reddit.com:8080/{POST_PATH}",
    f"https://reddit.com..evil.com/{POST_PATH}",
    f"https://reddit.com%2eevil.com/{POST_PATH}",
    f"https://reddit.com%00.evil.com/{POST_PATH}",
    f"https://.reddit.com/{POST_PATH}",
    "https://redd.it.evil.com/abc123",
    "https://xredd.it/abc123",
    "https://redd.it:8080/abc123",
    # look-alike characters: Cyrillic e, fullwidth letter, ideographic stop, fullwidth @ and .
    "https://rеddit.com/" + POST_PATH,
    "https://ｒeddit.com/" + POST_PATH,
    "https://reddit。com/" + POST_PATH,
    "https://reddit．com/" + POST_PATH,
    "https://evil.com＠reddit.com/" + POST_PATH,
    "https://reddit.com∕evil.com/" + POST_PATH,
    "https://xn--reddit-.com/" + POST_PATH,
    # malformed hosts
    f"https://reddit.com]/{POST_PATH}",
    f"https://[::1]/{POST_PATH}",
    f"https:///reddit.com/{POST_PATH}",
    f"https://[reddit.com]/{POST_PATH}",
    # unsupported schemes, even when the host is Reddit
    f"file://reddit.com/{POST_PATH}",
    f"ftp://reddit.com/{POST_PATH}",
    f"javascript://reddit.com/{POST_PATH}",
    "file:///reddit.com/comments/abc123",
    "javascript:alert(1)//reddit.com/comments/abc123",
    "data:text/html,reddit.com/comments/abc123",
    "javascript:alert(1)",
    # control characters and whitespace that URL parsers silently delete
    f"https://reddit.com\x00.evil.com/{POST_PATH}",
    f"https://evil.com/\x00reddit.com/{POST_PATH}",
    f"https://red\ndit.com/{POST_PATH}",
    f"https://red\rdit.com/{POST_PATH}",
    f"https://reddit.com\t.evil.com/{POST_PATH}",
    f"https://reddit.com .evil.com/{POST_PATH}",
    f"https://reddit.com .evil.com/{POST_PATH}",
    f"https://reddit.com/{POST_PATH}\nhttps://evil.com",
    f"https://reddit.com/{POST_PATH} https://evil.com",
    f"https://reddit.com/{POST_PATH}\x00",
]


@pytest.mark.parametrize("url", NOT_REDDIT_URLS)
def test_look_alike_hosts_are_never_reddit_posts(url):
    with pytest.raises(InputError):
        parse_post_ref(url)
    with pytest.raises(InputError):
        parse_comment_id(url)
    with pytest.raises(InputError):
        parse_post_refs([url])
    # the discussion tool may treat it as an external link, never as a Reddit post
    try:
        target = parse_discussion_target(url)
    except InputError:
        return
    assert target.post is None
    assert target.url


@pytest.mark.parametrize("url", [u for u in NOT_REDDIT_URLS if u.startswith("https://")])
def test_look_alike_hosts_are_not_subreddit_or_user_or_wiki_links(url):
    with pytest.raises(InputError):
        normalize_subreddit(url.replace("comments/abc123/slug/", ""))
    with pytest.raises(InputError):
        normalize_username(url.replace("r/x1/comments/abc123/slug/", "user/example_user/"))
    with pytest.raises(InputError):
        normalize_wiki_page(url.replace("comments/abc123/slug/", "wiki/faq"))


@pytest.mark.parametrize(
    "url",
    [
        f"https://REDDIT.COM/{POST_PATH}",
        f"https://Www.Reddit.Com/{POST_PATH}",
        f"https://reddit.com./{POST_PATH}",  # trailing dot is the same host
        f"https://www.reddit.com./{POST_PATH}",
        f"https://reddit.com:443/{POST_PATH}",
        f"http://reddit.com:80/{POST_PATH}",
        f"//reddit.com/{POST_PATH}",
        f"reddit.com/{POST_PATH}",
        f"reddit.com:443/{POST_PATH}",
        f"www.reddit.com/{POST_PATH}",
        f"http://old.reddit.com/{POST_PATH}",
        f"https://sh.reddit.com/{POST_PATH}",
        f"  https://reddit.com/{POST_PATH}  \n",
        f"<https://reddit.com/{POST_PATH}>",
        f"https://reddit.com/{POST_PATH}?next=http://evil.com/x",
        f"reddit.com/{POST_PATH}?next=https://evil.com/x",
        f"https://reddit.com/{POST_PATH}#frag@evil.com",
    ],
)
def test_real_reddit_urls_still_parse(url):
    assert parse_post_ref(url) == PostRef("abc123")
    assert parse_discussion_target(url).post == PostRef("abc123")


@pytest.mark.parametrize(
    "url", ["https://redd.it/abc123", "http://REDD.IT/abc123", "redd.it/abc123", "//redd.it/abc123"]
)
def test_short_links_parse(url):
    assert parse_post_ref(url) == PostRef("abc123")


def test_comment_permalink_ids_come_only_from_the_path():
    url = f"https://reddit.com/{POST_PATH}def456/?x=/comments/zzz/q/yyy"
    assert parse_post_ref(url) == PostRef("abc123", "def456")
    assert parse_comment_id(url) == "def456"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("//example.com/a", "https://example.com/a"),
        ("example.com/a", "https://example.com/a"),
        ("example.com:8443/a", "https://example.com:8443/a"),
        ("http://example.com/a?x=1&y=2#f", "http://example.com/a?x=1&y=2#f"),
        ("https://münchen.example/a", "https://münchen.example/a"),
        (
            "https://example.com/?u=https://reddit.com/comments/abc123",
            "https://example.com/?u=https://reddit.com/comments/abc123",
        ),
        ("https://reddit.com/r/x1/wiki/", "https://reddit.com/r/x1/wiki/"),
    ],
)
def test_external_links_pass_through_unchanged(raw, expected):
    target = parse_discussion_target(raw)
    assert target.post is None
    assert target.url == expected


@pytest.mark.parametrize(
    "raw",
    [
        "https://user:pw@example.com/a",
        "https://token@example.com/a",
        "ftp://example.com/a",
        "javascript:alert(1)//example.com/a",
        "https://example.com/a b",
        "https://example.com/a\nb",
        "https://example.com/a\x00",
        "https://example.com\\a",
        "https://example.com:notaport/a",
        "https://exa mple.com/a",
        "https://" + "a" * 5000 + ".com/",
        "https://example.com/" + "a" * 5000,
    ],
)
def test_unsafe_external_links_are_refused(raw):
    with pytest.raises(InputError):
        parse_discussion_target(raw)


# Characters that would change the API path or query if they reached an f-string route.
PATH_BREAKERS = [
    "a/b",
    "a\\b",
    "a?b",
    "a#b",
    "a&b",
    "a=b",
    "a%2fb",
    "a%2Fb",
    "a%252fb",
    "a%00b",
    "a%0ab",
    "..",
    "a..b",
    "a.b",
    "../ab",
    "ab/../cd",
    "ab/..",
    "a b",
    "a\tb",
    "ab\ncd",
    "ab\x00",
    "ab\x00cd",
    "a;b",
    "a:b",
    "a@b",
    "a*b",
    "a'b",
    "a\"b",
    "a<b",
    "аbc",  # Cyrillic a
    "abé",
    "ａｂ",  # fullwidth
    "ab​",  # zero-width space
    "a∕b",
]


@pytest.mark.parametrize("name", PATH_BREAKERS + ["x", "x" * 22, "a-b", "-ab", "_ab"])
def test_subreddit_names_outside_the_allowlist_are_refused(name):
    for multi in (True, False):
        with pytest.raises(InputError):
            normalize_subreddit(name, allow_multi=multi)
        with pytest.raises(InputError):
            normalize_subreddit(f"r/{name}", allow_multi=multi)
    with pytest.raises(InputError):
        normalize_subreddit(f"good_sub+{name}")


@pytest.mark.parametrize("name", ["ab", "a1", "x" * 21, "Ab_9", "a_", "9a", "u_example_user"])
def test_subreddit_names_inside_the_allowlist_pass(name):
    assert normalize_subreddit(name) == name
    assert normalize_subreddit(name, allow_multi=False) == name


@pytest.mark.parametrize("raw", ["a1+b2", "a1,b2", "a1 + b2", "a1+b2+", "+a1+b2", "r/a1+r/b2"])
def test_single_subreddit_slots_refuse_lists(raw):
    assert normalize_subreddit(raw) == "a1+b2"
    with pytest.raises(InputError, match="one subreddit"):
        normalize_subreddit(raw, allow_multi=False)


def test_subreddit_trailing_newline_is_not_part_of_the_name():
    assert normalize_subreddit("ab\n") == "ab"
    with pytest.raises(InputError):
        normalize_subreddit("ab\ncd")


def test_subreddit_url_takes_only_the_path_segment():
    assert normalize_subreddit("https://reddit.com/r/ab?x=/r/cd") == "ab"
    assert normalize_subreddit("https://reddit.com/r/ab#/r/cd") == "ab"
    with pytest.raises(InputError):
        normalize_subreddit("https://reddit.com/r/a%2fb/")
    with pytest.raises(InputError):
        normalize_subreddit("https://reddit.com/r/a..b/")
    with pytest.raises(InputError):
        normalize_subreddit("https://reddit.com/r/ab\\cd/")


def test_subreddit_list_is_capped():
    names = [f"sub{i}" for i in range(50)]
    assert normalize_subreddit("+".join(names)) == "+".join(names)
    with pytest.raises(InputError, match="more than 50"):
        normalize_subreddit("+".join(names + ["sub50"]))


@pytest.mark.parametrize("name", [*PATH_BREAKERS, "x" * 21, "a+b", "a,b", "[deleted]"])
def test_usernames_outside_the_allowlist_are_refused(name):
    with pytest.raises(InputError):
        normalize_username(name)
    with pytest.raises(InputError):
        normalize_username(f"u/{name}")


@pytest.mark.parametrize("name", ["abc", "a-b", "a_b", "A1_-", "x" * 20, "ab"])
def test_usernames_inside_the_allowlist_pass(name):
    assert normalize_username(name) == name
    assert normalize_username(f"u/{name}") == name
    assert normalize_username(f"https://reddit.com/user/{name}/") == name


@pytest.mark.parametrize(
    "page",
    [
        "..",
        "../..",
        "../../../message/inbox",
        "a/../../api/v1/me",
        "a/../b",
        "a/..",
        "./a",
        "a/./b",
        "a//b",
        ".hidden",
        "a/.hidden",
        "a%2f..%2f..",
        "..%2fapi",
        "a?x=1",
        "a#b",
        "a&b",
        "a\\b",
        "a b",
        "a\nb",
        "a\x00",
        "a" * 257,
        "аbc",
        "https://reddit.com/r/x1/wiki/../../../message/inbox",
        "https://evil.com/wiki/faq",
    ],
)
def test_wiki_pages_cannot_climb_out_of_the_wiki_path(page):
    with pytest.raises(InputError):
        normalize_wiki_page(page)


@pytest.mark.parametrize(
    "page, expected",
    [
        ("index", "index"),
        ("Tools/Databases", "tools/databases"),
        ("faq-2", "faq-2"),
        ("v1.2", "v1.2"),
        ("a/b/c", "a/b/c"),
        ("/wiki/faq/", "faq"),
        ("https://old.reddit.com/r/x1/wiki/faq?v=1#top", "faq"),
        ("https://reddit.com/r/x1/wiki/", ""),
    ],
)
def test_wiki_pages_inside_the_allowlist_pass(page, expected):
    assert normalize_wiki_page(page) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "a" * 14, "abc-123", "abc/123", "abc?x", "abc 123", "abc12K", "аbc123", "abc12ſ", "t3_", "../x"],
)
def test_ids_outside_the_base36_allowlist_are_refused(raw):
    with pytest.raises(InputError):
        parse_post_ref(raw)
    if raw.strip():
        with pytest.raises(InputError):
            parse_comment_id(raw)


@pytest.mark.parametrize("raw", ["a", "ABCDEF", "a" * 13, "0z9"])
def test_ids_inside_the_allowlist_pass(raw):
    assert parse_post_ref(raw).post_id == raw.lower()
    assert parse_comment_id(raw) == raw.lower()


def test_kelvin_sign_is_not_folded_into_an_id():
    # "K".lower() == "k": without an ASCII check this would parse as "abc12k"
    for fn in (parse_post_ref, parse_comment_id):
        with pytest.raises(InputError):
            fn("abc12K")
    with pytest.raises(InputError):
        parse_post_ref("https://reddit.com/comments/abc12K/")


@pytest.mark.parametrize("raw", ["a1,b2\n../x", "a1 b2/c3", "a1;b2", "a1,t3_b2?x", "a1 https://evil.com/c3"])
def test_id_lists_validate_every_entry(raw):
    with pytest.raises(InputError):
        parse_comment_ids(raw)
    with pytest.raises(InputError):
        parse_post_refs(raw)


def test_id_lists_accept_exactly_the_ids():
    assert parse_comment_ids(["a1\n", " B2 ", "t1_c3,d4"]) == ["a1", "b2", "c3", "d4"]


# ---------------------------------------------------------------- size and speed

HUGE = 100_000


@pytest.mark.parametrize(
    "fn",
    [
        parse_post_ref,
        parse_comment_id,
        parse_discussion_target,
        normalize_subreddit,
        normalize_username,
        normalize_wiki_page,
    ],
)
@pytest.mark.parametrize(
    "make",
    [
        lambda: "a" + " " * HUGE + "b",
        lambda: "a" * HUGE,
        lambda: "a." * (HUGE // 2),
        lambda: "a." * (HUGE // 2) + "reddit.com/",
        lambda: "https://reddit.com/" + "r/" * (HUGE // 2),
        lambda: "https://reddit.com/" + "comments/a/" * (HUGE // 11),
        lambda: "/" * HUGE + "a",
        lambda: "@" * HUGE + "a",
        lambda: "+".join(f"ab{i}" for i in range(HUGE // 8)),
        lambda: "https://reddit.com/" + "?" * HUGE,
        lambda: "\x00" * HUGE,
        lambda: "-" * HUGE,
    ],
    ids=[
        "spaces", "letters", "dots", "dotsreddit", "slashr", "comments",
        "slashes", "ats", "names", "queries", "nulls", "hyphens",
    ],
)
def test_huge_inputs_are_refused_quickly(fn, make):
    raw = make()
    start = time.perf_counter()
    with pytest.raises(InputError) as e:
        fn(raw)
    assert time.perf_counter() - start < 1.0
    assert len(str(e.value)) < 400  # the message does not echo the input back


def test_huge_list_entries_do_not_hang():
    start = time.perf_counter()
    with pytest.raises(InputError):
        parse_comment_ids(["a" * HUGE + "," + "b" * HUGE])
    with pytest.raises(InputError):
        parse_post_refs("a" * (HUGE + 1))
    assert len(parse_comment_ids(" ".join(f"a{i:x}" for i in range(500)))) == 500
    assert time.perf_counter() - start < 1.0


@pytest.mark.parametrize("n", [4096, 100_000])
def test_url_detection_regex_is_linear(n):
    # the module regexes themselves, bypassing the length guard
    for raw in ("a." * (n // 2), "a" * n, "a-" * (n // 2) + "reddit.com", "reddit.com" * (n // 10)):
        start = time.perf_counter()
        refs._looks_like_url(raw)
        assert time.perf_counter() - start < 1.0


def test_error_messages_clip_the_echoed_input():
    with pytest.raises(InputError) as e:
        parse_post_ref("https://evil.com/" + "a" * 500)
    assert len(str(e.value)) < 400
    with pytest.raises(InputError) as e:
        normalize_subreddit("x y" * 100)
    assert len(str(e.value)) < 400
