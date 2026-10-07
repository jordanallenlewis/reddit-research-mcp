from fixtures import listing, post, t3

from reddit_research_mcp import format as fmt


def test_date_and_truncate():
    assert fmt.date_str(1727827200) == "2024-10-02"
    assert fmt.date_str(None) == "????-??-??"
    assert fmt.truncate("short", 10) == "short"
    cut = fmt.truncate("word " * 100, 50)
    assert cut.endswith("chars]") and "[+" in cut
    kept = cut.split(" [+")[0]
    assert len(kept) <= 50
    assert int(cut.split("[+")[1].split()[0]) == 500 - len(kept)  # marker counts the rest
    assert fmt.truncate("abcdef", 0) == "[6 chars]"


def test_truncate_marker_counts_remaining_characters():
    text = "a" * 120
    out = fmt.truncate(text, 100)
    assert out == "a" * 100 + " [+20 chars]"


def test_listing_self_post():
    d = post(selftext="Body text here.\n\nSecond paragraph.", link_flair_text="Discussion", edited=1727830000.0)
    out = fmt.listing_post(d, 400)
    lines = out.split("\n")
    assert lines[0] == "[abc123] r/testsub 2024-10-02 31(91%) 54c u/example_author [Discussion] self edited"
    assert lines[1] == "A synthetic title"
    assert lines[2] == "  Body text here. Second paragraph."


def test_listing_link_post_with_body_shows_url_and_body():
    d = post(
        is_self=False,
        url="https://example.com/article",
        url_overridden_by_dest="https://example.com/article",
        domain="example.com",
        post_hint="link",
        selftext="Context written by the poster.",
    )
    out = fmt.listing_post(d, 400)
    assert " link" in out.split("\n")[0]
    assert "url: https://example.com/article" in out
    assert "Context written by the poster." in out


def test_image_video_gallery_poll_kinds():
    image = post(is_self=False, url_overridden_by_dest="https://i.redd.it/x1.jpeg", post_hint="image")
    assert fmt.post_kind(image) == "image"
    assert "url: https://i.redd.it/x1.jpeg" in fmt.listing_post(image, 100)

    video = post(
        is_self=False,
        is_video=True,
        url_overridden_by_dest="https://v.redd.it/v1",
        media={"reddit_video": {"duration": 34}},
    )
    assert fmt.post_kind(video) == "video"
    assert "video: https://v.redd.it/v1 (34 s)" in fmt.post_detail(video, 1000)

    gallery = post(
        is_self=False,
        is_gallery=True,
        url="https://www.reddit.com/gallery/abc123",
        gallery_data={"items": [{"media_id": "m1", "caption": "first"}, {"media_id": "m2"}, {"media_id": "m3"}]},
        selftext="Gallery post with a written body.",
    )
    assert fmt.post_kind(gallery) == "gallery"
    detail = fmt.post_detail(gallery, 1000)
    assert "gallery: 3 items" in detail
    assert "captions: first" in detail
    assert "Gallery post with a written body." in detail

    poll = post(
        poll_data={
            "options": [{"text": "Option A", "id": "1"}, {"text": "Option B", "id": "2"}],
            "total_vote_count": 1234,
            "voting_end_timestamp": 1728432000000,
        },
        selftext="Which one?",
    )
    assert fmt.post_kind(poll) == "poll"
    detail = fmt.post_detail(poll, 1000)
    assert "poll: Option A | Option B; 1,234 votes; voting ends 2024-10-09" in detail
    assert "Which one?" in detail


def test_crosspost_shows_origin_and_original_body():
    original = post(id="orig99", sub="othersub", selftext="Original body text.", author="origin_author")
    xp = post(
        id="xp1",
        is_self=False,
        crosspost_parent="t3_orig99",
        crosspost_parent_list=[original],
        url="/r/othersub/comments/orig99/a_synthetic_title/",
    )
    assert fmt.post_kind(xp) == "crosspost"
    listing_out = fmt.listing_post(xp, 400)
    assert "crosspost of [orig99] r/othersub u/origin_author 2024-10-02" in listing_out
    assert "(original) Original body text." in listing_out
    detail = fmt.post_detail(xp, 1000)
    assert "body (from the original post):" in detail
    assert "Original body text." in detail


def test_deleted_and_removed_posts():
    deleted = post(author="[deleted]", selftext="[deleted]", removed_by_category="deleted")
    head = fmt.post_head(deleted)
    assert "[deleted]" in head and head.endswith("self deleted")
    removed = post(selftext="[removed]", removed_by_category="moderator")
    assert "removed(moderator)" in fmt.post_head(removed)
    assert "[removed]" in fmt.post_detail(removed, 1000)


def test_post_detail_header_fields():
    d = post(
        is_self=False,
        url_overridden_by_dest="https://example.com/a",
        domain="example.com",
        selftext="Long body " * 50,
        locked=True,
        over_18=True,
        stickied=True,
        archived=True,
        link_flair_text="News",
    )
    out = fmt.post_detail(d, 100)
    first = out.split("\n")[0]
    for flag in ("nsfw", "pinned", "locked", "archived", "[News]", "31(91%)", "54c"):
        assert flag in first
    assert "url: https://example.com/a (example.com)" in out
    assert "permalink: https://www.reddit.com/r/testsub/comments/abc123/a_synthetic_title/" in out
    assert "[+" in out  # body truncated with a marker


def test_unknown_items_never_break_a_listing():
    bad = {"kind": "t3", "data": {"id": "bad1", "num_comments": object(), "title": None}}
    weird = {"kind": "zz9", "data": {"id": "w1"}}
    res = fmt.render_listing([t3(id="a1"), bad, weird, t3(id="a2")], body_chars=100, max_chars=10_000)
    assert res.shown == 4 and not res.truncated
    assert "[a1]" in res.text and "[a2]" in res.text
    assert "[w1] (zz9 item; not rendered)" in res.text
    assert "[bad1]" in res.text


def test_listing_budget_and_cursor():
    items = [t3(id=f"p{i}", selftext="x" * 300) for i in range(10)]
    res = fmt.render_listing(items, body_chars=300, max_chars=1500)
    assert res.truncated and 0 < res.shown < 10
    assert len(res.text) <= 1500
    line = fmt.next_line("t3_p9", res)
    assert line.startswith(f"next: after=t3_p{res.shown - 1} (output budget reached")
    full = fmt.render_listing(items[:2], body_chars=10, max_chars=10_000)
    assert fmt.next_line("t3_zz", full) == "next: after=t3_zz"
    assert fmt.next_line(None, full) == "next: none (end of results)"


def test_subreddit_and_comment_items():
    sub = {"kind": "t5", "data": {"display_name": "testsub", "subscribers": 12345, "created_utc": 1727827200,
                                  "over18": True, "subreddit_type": "restricted",
                                  "public_description": "A test community."}}
    out = fmt.render_thing(sub, 0)
    assert out.startswith("r/testsub 12,345 subscribers created 2024-10-02 nsfw restricted")
    assert "A test community." in out
    c = {"kind": "t1", "data": {"id": "c1", "subreddit": "testsub", "created_utc": 1727827200, "score": 3,
                                "author": "example_user", "link_id": "t3_abc123", "link_title": "Thread title",
                                "body": "Reply text", "is_submitter": True}}
    out = fmt.render_thing(c, 100)
    assert out.split("\n")[0] == "[c1] r/testsub 2024-10-02 3 u/example_user comment on [abc123] (OP)"
    assert "re: Thread title" in out and "Reply text" in out


def test_activity_summary():
    kids = [t3(id="p1", sub="alpha"), t3(id="p2", sub="alpha"),
            {"kind": "t1", "data": {"subreddit": "beta", "created_utc": 1727000000}}]
    out = fmt.activity_summary(kids)
    assert out.startswith("Activity by subreddit (3 items: 2 posts, 1 comments")
    assert "r/alpha 2 (67%)" in out and "r/beta 1 (33%)" in out
    assert fmt.activity_summary([]).startswith("Activity: none visible")


def test_listing_wrapper_shape():
    assert listing([t3()])["data"]["children"][0]["kind"] == "t3"


def test_hide_score_posts_show_the_score_and_a_flag():
    # Reddit hides scores of new posts on the site for some subreddits, but the API still sends them.
    d = post(score=13, upvote_ratio=1.0, hide_score=True, num_comments=10)
    head = fmt.post_head(d)
    assert head == "[abc123] r/testsub 2024-10-02 13(100%) 10c u/example_author self score-hidden"
    assert fmt.post_detail(d, 100).split("\n")[0].endswith("self score-hidden")
    assert "score-hidden" not in fmt.post_head(post())


def test_hidden_comment_scores_still_print_a_question_mark():
    c = {"kind": "t1", "data": {"id": "c1", "subreddit": "testsub", "created_utc": 1727827200, "score": 1,
                                "score_hidden": True, "author": "example_user", "link_id": "t3_abc123"}}
    assert fmt.render_thing(c, 100).split("\n")[0] == "[c1] r/testsub 2024-10-02 ? u/example_user comment on [abc123]"
    assert fmt.comment_head(c["data"]) == "[c1] 2024-10-02 ? u/example_user"
    assert fmt.score_str(post(score=None)) == "?(91%)"


def test_non_finite_and_junk_numbers_never_raise():
    inf, nan = float("inf"), float("nan")
    assert fmt.num(inf) == "?" and fmt.num(nan) == "?" and fmt.num("x") == "?" and fmt.num(1234) == "1,234"
    assert fmt.plural(inf, "comment") == "? comments" and fmt.plural(1, "stub") == "1 stub"
    assert fmt.score_str({"score": inf, "upvote_ratio": nan}) == "?"
    assert fmt.score_str({"score": 7.0, "upvote_ratio": 0.5}) == "7(50%)"
    assert fmt.score_str({"score": True}) == "?"
    d = post(num_comments=inf, upvote_ratio=nan, score=nan, created_utc=inf)
    assert fmt.post_head(d) == "[abc123] r/testsub ????-??-?? ? ?c u/example_author self"
    fmt.post_detail(d, 100)
    fmt.listing_post(d, 100)
