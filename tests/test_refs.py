import pytest

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
