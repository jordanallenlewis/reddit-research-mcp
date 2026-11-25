import re

import pytest
from fastmcp.exceptions import ToolError
from fixtures import FakeReddit, comment, cont, listing, more, ok, post, run, run_raw, sfw_info

from reddit_research_mcp import format as fmt
from reddit_research_mcp import server


def thread_payload(children, **post_kw):
    return [listing([{"kind": "t3", "data": post(**post_kw)}]), listing(children)]


def sample_tree():
    return [
        comment(
            "c1",
            "Top answer.\n\nWith a second paragraph.",
            score=40,
            author="example_author",
            is_submitter=True,
            author_flair_text="Senior Engineer",
            edited=1727830000.0,
            replies=[
                comment("c2", "A reply.", parent="t1_c1"),
                comment("c3", "[removed]", author="[deleted]", parent="t1_c1",
                        replies=[comment("c4", "Reply under a removed comment.", parent="t1_c3")]),
                more(["m1", "m2"], 7, parent="t1_c1"),
            ],
        ),
        comment("c5", "[deleted]", author="[deleted]"),
        comment("c6", "Deep thread.", replies=[cont("t1_c6")]),
        more(["r1", "r2", "r3"], 25),
    ]


def test_comment_renderer_layout_and_stats():
    r = fmt.CommentRenderer(max_chars=100_000)
    r.walk(sample_tree())
    text = r.text
    lines = text.split("\n")
    assert lines[0] == "[c1] 2024-10-02 40 u/example_author (OP) (edited) [Senior Engineer]"
    assert lines[1] == "  Top answer."
    assert lines[2] == "  With a second paragraph."
    assert "  [c2] 2024-10-02 5 u/example_commenter" in lines
    assert "  [c3] 2024-10-02 [removed]" in lines  # collapsed to one line
    assert "    [c4] 2024-10-02 5 u/example_commenter" in lines  # replies still shown
    assert "  [more: 7 comments; ids: m1,m2]" in lines
    assert "[c5] 2024-10-02 [deleted]" in lines
    assert '  [continue: deeper replies under [c6] -> get_post(post, comment_id="c6")]' in lines
    # root stub is held back for the footer, not printed inline
    assert "r1" not in text
    s = r.stats
    assert s.shown == 6
    assert s.stubs == 2 and s.stub_comments == 32
    assert s.continues == ["c6"]
    assert len(s.root_stubs) == 1


def test_comment_renderer_budget_collects_unshown_ids():
    kids = [comment(f"k{i}", "x" * 200) for i in range(10)] + [more(["z1"], 3)]
    r = fmt.CommentRenderer(max_chars=700)
    r.walk(kids)
    assert 0 < r.stats.shown < 10
    assert r.size <= 700
    assert r.stats.unshown_ids == [f"k{i}" for i in range(r.stats.shown, 10)]
    assert r.stats.unshown_comments == 10 - r.stats.shown


def test_get_post_single_request_header_stubs_and_coverage():
    fake = FakeReddit({("GET", "/comments/abc123"): ok(thread_payload(
        sample_tree(),
        selftext="Post body.",
        is_self=False,
        url_overridden_by_dest="https://example.com/x",
        domain="example.com",
        link_flair_text="Question",
    ))})
    server.set_client(fake)
    out = run(server.get_post(post="https://www.reddit.com/r/testsub/comments/ABC123/slug/"))
    assert fake.paths() == ["/comments/abc123"]  # one request, no /api/info
    _, _, params, _ = fake.calls[0]
    assert params == {"sort": "top", "limit": "200", "depth": "8", "raw_json": "1"}
    assert out.startswith("[abc123] r/testsub 2024-10-02 31(91%) 54c u/example_author [Question] link")
    assert "title: A synthetic title" in out
    assert "url: https://example.com/x (example.com)" in out
    assert "body:\nPost body." in out
    assert "comment link = https://www.reddit.com/r/testsub/comments/abc123/a_synthetic_title/<id>/" in out
    assert "[more top-level: 25 comments; 3 ids: r1,r2,r3]" in out
    assert out.rstrip().endswith(
        'Shown 6 of 54 comments; ~32 more in 2 stubs -> expand_comments(post="abc123", comment_ids=[ids '
        'from the [more ...] lines]); 1 deeper threads -> get_post(post="abc123", comment_id=<id in the '
        "[continue ...] line>)"
    )


def test_get_post_comment_permalink_sets_comment_and_context():
    payload = thread_payload([comment("p1", "Parent.", replies=[comment("t9", "Target.", parent="t1_p1")])])
    fake = FakeReddit({("GET", "/comments/abc123"): ok(payload)})
    server.set_client(fake)
    out = run(server.get_post(post="https://www.reddit.com/r/testsub/comments/abc123/slug/t9/", context=2))
    params = fake.calls[0][2]
    assert params["comment"] == "t9" and params["context"] == "2"
    assert "[t9] 2024-10-02 5 u/example_commenter <- requested comment" in out
    assert "comments around [t9] (context=2" in out


NOT_FOUND_404 = ok({"message": "Not Found", "error": 404}, 404)


def info_listing(*things):
    return ok(listing(list(things)))


def test_get_post_unknown_comment_id_is_an_error_not_the_normal_thread():
    # Live Reddit ignores an unknown comment= and returns the ordinary top comments.
    fake = FakeReddit({
        ("GET", "/comments/abc123"): ok(thread_payload(sample_tree())),
        ("GET", "/api/info"): info_listing({"kind": "t3", "data": post()}),
    })
    server.set_client(fake)
    with pytest.raises(ToolError) as e:
        run(server.get_post(post="abc123", comment_id="zzzzzzz", comment_limit=5, comment_depth=1))
    msg = str(e.value)
    assert msg.startswith("comment zzzzzzz not found in post abc123: it was deleted, or the id is wrong")
    assert 'get_post(post="abc123") without comment_id' in msg
    assert fake.paths() == ["/comments/abc123", "/api/info"]
    assert fake.calls[0][2]["comment"] == "zzzzzzz"
    assert fake.calls[1][2]["id"] == "t3_abc123,t1_zzzzzzz"


def test_get_post_unknown_comment_id_when_lookup_fails():
    fake = FakeReddit({("GET", "/comments/abc123"): ok(thread_payload(sample_tree()))})  # no /api/info route
    server.set_client(fake)
    with pytest.raises(ToolError, match="comment zz1 not found in post abc123: it was deleted, or it belongs"):
        run(server.get_post(post="abc123", comment_id="t1_zz1"))


def test_get_post_empty_listing_for_comment_is_still_an_error():
    fake = FakeReddit({
        ("GET", "/comments/abc123"): ok(thread_payload([])),
        ("GET", "/api/info"): info_listing({"kind": "t3", "data": post()}),
    })
    server.set_client(fake)
    with pytest.raises(ToolError, match="comment zz1 not found in post abc123"):
        run(server.get_post(post="abc123", comment_id="t1_zz1"))


def test_get_post_deleted_comment_says_so():
    gone = comment("dd1", "[deleted]", author="[deleted]")
    fake = FakeReddit({
        ("GET", "/comments/abc123"): ok(thread_payload(sample_tree())),
        ("GET", "/api/info"): info_listing({"kind": "t3", "data": post()}, gone),
    })
    server.set_client(fake)
    with pytest.raises(ToolError, match="comment dd1 in post abc123 was deleted and Reddit no longer shows it"):
        run(server.get_post(post="abc123", comment_id="dd1"))


def comments_404_when_focused(params):
    """Reddit answers 404 for a comment id that belongs to another post."""
    return NOT_FOUND_404 if "comment" in params else ok(thread_payload(sample_tree()))


def test_get_post_comment_of_another_post_points_at_the_right_post():
    elsewhere = comment("c9", "Somewhere else.", link_id="t3_other1", parent="t3_other1")
    fake = FakeReddit({
        ("GET", "/comments/abc123"): comments_404_when_focused,
        ("GET", "/api/info"): info_listing({"kind": "t3", "data": post()}, elsewhere),
    })
    server.set_client(fake)
    with pytest.raises(ToolError) as e:
        run(server.get_post(post="abc123", comment_id="c9", context=1))
    assert str(e.value) == (
        'comment c9 belongs to post other1, not abc123; call get_post(post="other1", comment_id="c9")'
    )
    assert "not found (deleted, removed" not in str(e.value)  # the post exists


def test_get_post_404_with_comment_and_missing_post():
    fake = FakeReddit({("GET", "/comments/abc123"): NOT_FOUND_404, ("GET", "/api/info"): info_listing()})
    server.set_client(fake)
    with pytest.raises(ToolError, match="post abc123 not found"):
        run(server.get_post(post="abc123", comment_id="c9"))


def test_get_post_404_with_comment_when_lookup_fails_names_both():
    fake = FakeReddit({("GET", "/comments/abc123"): NOT_FOUND_404})
    server.set_client(fake)
    with pytest.raises(ToolError, match="post abc123 or comment c9 not found: the comment may belong to another"):
        run(server.get_post(post="abc123", comment_id="c9"))


def test_get_post_404_without_comment_is_post_not_found_and_costs_one_request():
    fake = FakeReddit({("GET", "/comments/abc123"): NOT_FOUND_404})
    server.set_client(fake)
    with pytest.raises(ToolError, match="post abc123 not found"):
        run(server.get_post(post="abc123"))
    assert fake.paths() == ["/comments/abc123"]


def test_get_post_marks_a_nested_requested_comment():
    payload = thread_payload([
        comment("p1", "Parent.", replies=[
            comment("p2", "Middle.", parent="t1_p1", replies=[comment("t9", "Target.", parent="t1_p2")]),
        ]),
    ])
    fake = FakeReddit({("GET", "/comments/abc123"): ok(payload)})
    server.set_client(fake)
    out = run(server.get_post(post="abc123", comment_id="t9", context=2))
    assert "    [t9] 2024-10-02 5 u/example_commenter <- requested comment" in out
    assert out.count("<- requested comment") == 1
    assert fake.paths() == ["/comments/abc123"]  # no lookup when the comment is present


def test_get_post_requested_comment_inside_a_stub_is_not_an_error():
    payload = thread_payload([comment("p1", "Parent.", replies=[more(["t9", "t8"], 2, parent="t1_p1")])])
    fake = FakeReddit({("GET", "/comments/abc123"): ok(payload)})
    server.set_client(fake)
    out = run(server.get_post(post="abc123", comment_id="t9", comment_limit=1))
    assert "comments around [t9]" in out and "[more: 2 comments; ids: t9,t8]" in out


def test_get_post_clamps_and_reports():
    fake = FakeReddit({("GET", "/comments/abc123"): ok(thread_payload([]))})
    server.set_client(fake)
    out = run(server.get_post(post="abc123", comment_limit=100000, comment_depth=-5, context=99, max_chars=10))
    params = fake.calls[0][2]
    assert params["limit"] == "500" and params["depth"] == "1"
    assert "comment_limit clamped to 500" in out and "comment_depth clamped to 1" in out
    assert "max_chars clamped to 2000" in out
    assert "(no comments)" in out


def test_get_post_enforces_max_chars_with_truncation_marker():
    kids = [comment(f"k{i}", "word " * 150) for i in range(40)] + [more([f"x{i}" for i in range(50)], 300)]
    fake = FakeReddit({("GET", "/comments/abc123"): ok(thread_payload(kids, selftext="Body " * 200))})
    server.set_client(fake)
    out = run(server.get_post(post="abc123", max_chars=6000))
    assert len(out) <= 6000
    assert "Output budget reached:" in out
    assert 'expand_comments(post="abc123", comment_ids=[k' in out
    assert "[more top-level: 300 comments; 50 ids: x0," in out
    assert "Shown " in out


def test_get_post_long_body_is_cut_with_pointer():
    fake = FakeReddit({("GET", "/comments/abc123"): ok(thread_payload([], selftext="y" * 9000))})
    server.set_client(fake)
    out = run(server.get_post(post="abc123", max_chars=5000))
    assert "[+" in out and 'get_posts(posts=["abc123"], body_chars=40000)' in out
    assert len(out) <= 5000


def test_morechildren_forest_and_expand_comments_batches_serially():
    def things(params):
        ids = params["children"].split(",")
        out = []
        for i in ids:
            out.append(comment(i, f"Expanded {i}.", parent="t1_c1")["data"])
        # one nested reply and one nested stub under the first id
        out.append(comment("n1", "Nested.", parent=f"t1_{ids[0]}")["data"])
        out.append(more(["n2"], 4, parent=f"t1_{ids[0]}")["data"])
        kinds = ["t1"] * (len(ids) + 1) + ["more"]
        return ok({"json": {"errors": [], "data": {"things": [
            {"kind": k, "data": d} for k, d in zip(kinds, out, strict=True)
        ]}}})

    fake = FakeReddit({("GET", "/api/morechildren"): things, ("GET", "/api/info"): sfw_info})
    server.set_client(fake)
    ids = [f"e{i}" for i in range(150)]
    out = run(server.expand_comments(post="abc123", comment_ids=ids))
    assert fake.paths() == ["/api/info", "/api/morechildren", "/api/morechildren"]  # NSFW check first
    first, second = fake.calls[1][2], fake.calls[2][2]
    assert len(first["children"].split(",")) == 100 and len(second["children"].split(",")) == 50
    assert first["link_id"] == "t3_abc123" and first["sort"] == "top"
    assert "-- replies to [c1] --" in out
    assert "[e0] 2024-10-02 5 u/example_commenter\n  Expanded e0." in out
    assert "  [n1] 2024-10-02 5 u/example_commenter" in out
    assert "  [more: 4 comments; ids: n2]" in out
    assert "Shown 152 comments; ~8 more in 2 stubs" in out


def test_expand_comments_reports_unfetched_ids_beyond_request_cap():
    fake = FakeReddit({("GET", "/api/morechildren"): ok({"json": {"errors": [], "data": {"things": [
        comment("e0", "One.", parent="t3_abc123")
    ]}}}), ("GET", "/api/info"): sfw_info})
    server.set_client(fake)
    ids = [f"e{i}" for i in range(620)]
    out = run(server.expand_comments(post="abc123", comment_ids=",".join(ids)))
    assert fake.paths().count("/api/morechildren") == 5
    assert "Not fetched yet (120 ids)" in out
    assert "-- top-level comments --" in out


def test_expand_comments_empty_result():
    fake = FakeReddit({("GET", "/api/morechildren"): ok({"json": {"errors": [], "data": {"things": []}}}),
                       ("GET", "/api/info"): sfw_info})
    server.set_client(fake)
    out = run(server.expand_comments(post="abc123", comment_ids=["gone1"]))
    assert "returned no comments" in out


def test_forest_groups_by_parent():
    things = [
        comment("a1", parent="t1_p1"),
        comment("a2", parent="t1_a1"),
        comment("b1", parent="t1_p2"),
        more(["b2"], 2, parent="t1_p2"),
    ]
    forest = fmt.build_morechildren_forest(things)
    assert [p for p, _ in forest] == ["t1_p1", "t1_p2"]
    assert fmt.children_of(forest[0][1][0]["data"])[0]["data"]["id"] == "a2"
    assert [t["kind"] for t in forest[1][1]] == ["t1", "more"]


def test_tiny_budget_is_still_respected():
    kids = [comment(f"k{i}", "word " * 80) for i in range(30)] + [more([f"x{i:04d}" for i in range(540)], 1552)]
    fake = FakeReddit({("GET", "/comments/abc123"): ok(thread_payload(kids, selftext="Body " * 2000))})
    server.set_client(fake)
    out = run_raw(server.get_post(post="abc123", max_chars=2000))
    assert len(out) <= 2000
    assert "Shown " in out and "more ids not listed" in out

    fake = FakeReddit({("GET", "/api/morechildren"): ok({"json": {"errors": [], "data": {"things": [
        comment(f"e{i}", "text " * 60, parent="t3_abc123") for i in range(40)
    ]}}}), ("GET", "/api/info"): sfw_info})
    server.set_client(fake)
    out = run_raw(server.expand_comments(post="abc123", comment_ids=[f"e{i}" for i in range(700)], max_chars=2000))
    assert len(out) <= 2000
    assert "Not fetched yet (600 ids)" in out and "Output budget reached" in out
    # stops fetching once the budget is spent; the second call explains the ids not returned
    assert fake.paths() == ["/api/info", "/api/morechildren", "/api/info"]


def test_fit_cuts_body_not_tail():
    out = server._fit("HEAD", "\n".join(f"line {i}" for i in range(100)), "TAIL", 120)
    assert len(out) <= 120 and out.endswith("TAIL") and "[... comments cut to fit max_chars]" in out


def test_get_post_body_chars_default_leaves_room_for_comments():
    kids = [comment(f"k{i}", "word " * 60) for i in range(60)]
    fake = FakeReddit({("GET", "/comments/abc123"): ok(thread_payload(kids, selftext="y " * 9000))})
    server.set_client(fake)
    out = run(server.get_post(post="abc123"))
    assert '(body cut at 6,000 of 17,999 chars; get_posts(posts=["abc123"], body_chars=40000)' in out
    assert "Shown 60 of 54 comments" in out  # every loaded comment fits next to a 6,000-char body
    assert "Output budget reached" not in out

    out = run(server.get_post(post="abc123", body_chars=0, max_chars=5000))
    assert "body:\n[17999 chars]" in out and "body cut at 0 of 17,999 chars" in out

    out = run(server.get_post(post="abc123", body_chars=40000, max_chars=200000))
    assert "body cut" not in out and ("y " * 8999 + "y") in out

    out = run(server.get_post(post="abc123", body_chars=-5))
    assert "body_chars clamped to 0" in out


def _hidden_stub_thread():
    # 40 long comments, each holding one "more" stub of 5 replies.
    return [
        comment(f"k{i}", "word " * 150, replies=[more([f"m{i}"], 5, parent=f"t1_k{i}")])
        for i in range(40)
    ]


def test_coverage_splits_listed_stubs_from_stubs_inside_unshown_comments():
    fake = FakeReddit({("GET", "/comments/abc123"): ok(thread_payload(_hidden_stub_thread()))})
    server.set_client(fake)
    out = run(server.get_post(post="abc123", max_chars=8000))
    assert len(out) <= 8000
    import re

    line = next(x for x in out.split("\n") if x.startswith("Shown "))
    m = re.search(r"~200 more in 40 stubs: (\d+) stubs? listed with ids -> expand_comments\(post=\"abc123\", "
                  r"comment_ids=\[ids from the \[more \.\.\.\] lines\]\); (\d+) stubs? \(~([\d,]+) comments\) "
                  r"inside the loaded comments not shown, ids not listed", line)
    assert m, line
    listed, hidden, hidden_comments = int(m.group(1)), int(m.group(2)), int(m.group(3).replace(",", ""))
    assert listed + hidden == 40 and hidden_comments == 5 * hidden and hidden > 0
    inline = len(re.findall(r"^\s+\[more: 5 comments; ids: m\d+\]$", out, re.M))
    assert 0 < inline <= listed <= inline + 1  # at most one stub line was cut (its id is in the budget line)
    assert "Output budget reached:" in out


def test_coverage_line_unchanged_when_nothing_is_hidden():
    s = fmt.CommentStats(shown=3, stubs=2, stub_comments=9, continues=["c6"])
    assert server._stub_summary("abc123", s) == (
        '~9 more in 2 stubs -> expand_comments(post="abc123", comment_ids=[ids from the [more ...] lines]); '
        '1 deeper threads -> get_post(post="abc123", comment_id=<id in the [continue ...] line>)'
    )
    s = fmt.CommentStats(shown=0, stubs=3, stub_comments=12, continues=["a", "b"],
                         hidden_stubs=3, hidden_stub_comments=12, hidden_continues=2)
    text = server._stub_summary("abc123", s)
    assert "listed with ids" not in text and "deeper threads -> get_post" not in text
    assert "3 stubs (~12 comments) inside the loaded comments not shown" in text
    assert text.endswith("; 2 more deeper threads inside the comments not shown")


def test_expand_comments_coverage_counts_hidden_stubs():
    things = []  # /api/morechildren answers with a flat list; stubs point at their parent
    for i in range(30):
        things += [comment(f"e{i}", "text " * 80, parent="t3_abc123"), more([f"h{i}"], 3, parent=f"t1_e{i}")]
    fake = FakeReddit({("GET", "/api/morechildren"): ok({"json": {"errors": [], "data": {"things": things}}}),
                       ("GET", "/api/info"): sfw_info})
    server.set_client(fake)
    out = run(server.expand_comments(post="abc123", comment_ids=[f"e{i}" for i in range(30)], max_chars=4000))
    assert "inside the loaded comments not shown, ids not listed" in out
    assert "Output budget reached" in out


def test_get_post_survives_non_finite_numbers():
    payload = thread_payload([comment("c1", "Fine.")], num_comments=float("inf"), upvote_ratio=float("nan"),
                             score=float("inf"))
    fake = FakeReddit({("GET", "/comments/abc123"): ok(payload)})
    server.set_client(fake)
    out = run(server.get_post(post="abc123"))
    assert out.startswith("[abc123] r/testsub 2024-10-02 ? ?c u/example_author self")
    assert "Shown 1 of ? comments" in out


def test_get_post_header_render_failure_keeps_the_comments(monkeypatch):
    def boom(d, body_chars):
        raise ValueError("bad post data")

    monkeypatch.setattr(fmt, "post_detail", boom)
    fake = FakeReddit({("GET", "/comments/abc123"): ok(thread_payload([comment("c1", "Still here.")]))})
    server.set_client(fake)
    out = run(server.get_post(post="abc123"))
    assert out.startswith("[abc123] (could not render the post header: ValueError)")
    assert "permalink: https://www.reddit.com/r/testsub/comments/abc123/a_synthetic_title/" in out
    assert "[c1] 2024-10-02 5 u/example_commenter\n  Still here." in out


def _deep_chain(depth):
    node = comment(f"d{depth - 1}", "Deepest.")
    for i in range(depth - 2, -1, -1):
        node = comment(f"d{i}", "Reply.", replies=[node])
    return node


def test_deep_reply_chains_do_not_hit_the_recursion_limit():
    chain = _deep_chain(3000)
    assert fmt.count_loaded([chain]) == 3000
    assert "d2999" in fmt.tree_ids([chain])

    r = fmt.CommentRenderer(max_chars=10)  # nothing fits: the whole chain is counted, not rendered
    r.walk([chain, more(["z1"], 2, parent="t1_d2999")])
    assert r.stats.unshown_comments == 3000 and r.stats.unshown_ids == ["d0"]

    r = fmt.CommentRenderer(max_chars=10_000_000)
    r.walk([chain])  # rendering stops at Python's depth limit with a placeholder, never raises
    assert r.stats.shown > 100
    assert "(could not render a comment: RecursionError)" in r.text


@pytest.mark.parametrize("body", ["[removed]", "[deleted]"])
def test_deleted_focus_comment_keeps_marker(body):
    d = comment("t9", body, author="[deleted]")["data"]
    head = fmt.comment_head(d, focus="t9")
    assert head.endswith(f"{body} <- requested comment")
    assert "<- requested comment" not in fmt.comment_head(d, focus="other")


def test_small_budget_shows_loaded_comments_before_stub_ids():
    kids = [comment(f"k{i}", "short reply") for i in range(3)] + [more([f"x{i:04d}" for i in range(400)], 900)]
    fake = FakeReddit({("GET", "/comments/abc123"): ok(thread_payload(kids))})
    server.set_client(fake)
    out = run_raw(server.get_post(post="abc123", comment_limit=3, max_chars=2500))
    assert len(out) <= 2500
    assert all(f"[k{i}]" in out for i in range(3))  # every loaded comment is printed
    assert "Shown 3 of " in out and "loaded comments" not in out
    assert "more ids not listed" in out  # the stub id list is what gets shortened


def test_expand_comments_reports_ids_not_returned():
    returned = ok({"json": {"errors": [], "data": {"things": [comment("ok1", "Here.", parent="t3_abc123")]}}})
    info = ok(listing([
        comment("rm1", "[removed]", author="[deleted]", parent="t3_abc123", link_id="t3_abc123"),
        comment("oth1", "Elsewhere.", parent="t3_zzz999", link_id="t3_zzz999"),
    ]))
    fake = FakeReddit({("GET", "/api/morechildren"): returned, ("GET", "/api/info"): info})
    server.set_client(fake)
    out = run(server.expand_comments(post="abc123", comment_ids="ok1,rm1,oth1,nope1"))
    assert out.startswith("expanded 4 ids in post abc123: 1 returned")
    assert "Not returned (3): removed: rm1; in post zzz999: oth1; not found: nope1" in out


def test_expand_comments_lists_ids_when_lookup_fails():
    def info(params):
        if params["id"].startswith("t3_"):
            return sfw_info(params)  # the NSFW check succeeds, the missing-id lookup fails
        raise RuntimeError("lookup down")

    fake = FakeReddit({("GET", "/api/morechildren"): ok({"json": {"errors": [], "data": {"things": []}}}),
                       ("GET", "/api/info"): info})
    server.set_client(fake)
    out = run(server.expand_comments(post="abc123", comment_ids=["gone1", "gone2"]))
    assert "returned no comments" in out
    assert "Not returned (2; removed, deleted or not in this post): gone1,gone2" in out


def test_budget_line_counts_threads_and_comments():
    s = fmt.CommentStats(unshown_ids=["a1", "b1"], unshown_comments=5, pending_ids=["p1"])
    line = server._budget_line("abc123", s, 10)
    assert line.startswith("Output budget reached: 5 loaded comments in 2 threads not shown and 1 stub id not listed.")
    assert "comment_ids=[a1,b1,p1]" in line
    assert server._budget_line("abc123", fmt.CommentStats(), 10) is None


def test_no_tilde_when_there_are_no_stubs():
    assert server._stub_summary("abc123", fmt.CommentStats(shown=2)) == "0 more in 0 stubs"


def test_expand_comments_cuts_an_oversize_first_comment_instead_of_showing_none():
    big = comment("e1", "word " * 1800)["data"]  # about 9,000 characters

    def things(params):
        return ok({"json": {"errors": [], "data": {"things": [{"kind": "t1", "data": big}]}}})

    fake = FakeReddit({("GET", "/api/morechildren"): things, ("GET", "/api/info"): sfw_info})
    server.set_client(fake)
    out = run(server.expand_comments(post="abc123", comment_ids=["e1"], max_chars=5000))
    assert "-- top-level comments --" in out
    assert "[e1]" in out and "chars]" in out
    assert "Shown 1 comment" in out
    assert len(out) <= 5000


def _big_header_posts():
    title = "T" * 300
    caps = {"items": [{"caption": f"caption number {i} " + "c" * 60, "media_id": f"m{i}"} for i in range(10)]}
    return {
        "gallery": ({"title": title, "gallery_data": caps, "is_gallery": True, "is_self": False}, {}),
        "clamped": ({"title": title}, {"comment_limit": 9999, "comment_depth": 99, "context": 99}),
        "long_url": ({"title": "A link", "is_self": False, "url": "https://example.com/" + "u" * 1480}, {}),
    }


@pytest.mark.parametrize("shape", ["gallery", "clamped", "long_url"])
@pytest.mark.parametrize("budget", [2000, 2500])
def test_get_post_large_header_still_respects_max_chars(shape, budget):
    post_kw, call_kw = _big_header_posts()[shape]
    kids = [comment(f"k{i}", "word " * 80) for i in range(30)] + [more([f"x{i:04d}" for i in range(540)], 1552)]
    fake = FakeReddit({("GET", "/comments/abc123"): ok(thread_payload(kids, selftext="Body " * 2000, **post_kw))})
    server.set_client(fake)
    out = run_raw(server.get_post(post="abc123", max_chars=budget, **call_kw))
    assert len(out) <= budget


def test_get_post_large_header_without_comments_respects_max_chars():
    post_kw = {"title": "A link", "is_self": False, "url": "https://example.com/" + "u" * 1480}
    fake = FakeReddit({("GET", "/comments/abc123"): ok(thread_payload([], **post_kw))})
    server.set_client(fake)
    assert len(run_raw(server.get_post(post="abc123", max_chars=2000))) <= 2000


# A top-level [more] line cut to fit must name a call that lists every id it cut (#66).


def _root_ids(out: str) -> list[str]:
    line = next(x for x in out.split("\n") if x.startswith("[more top-level:"))
    ids = line.split(" ids: ", 1)[1]
    return ids.split(" (+", 1)[0].rstrip("]").split(",")


def test_cut_root_stub_names_a_max_chars_that_lists_every_id():
    ids = [f"x{i:05d}" for i in range(485)]
    kids = [comment(f"k{i}", "word " * 60) for i in range(150)] + [more(ids, 635)]
    fake = FakeReddit({("GET", "/comments/abc123"): ok(thread_payload(kids, selftext="Body " * 300))})
    server.set_client(fake)
    out = run_raw(server.get_post(post="abc123"))
    assert "more ids not listed" in out
    m = re.search(r'more ids not listed\) -> get_post\(post="abc123", max_chars=(\d+)\) lists them all\]', out)
    assert m, out[-1500:]
    again = run_raw(server.get_post(post="abc123", max_chars=int(m.group(1))))
    assert _root_ids(again) == ids
    assert "more ids not listed" not in again


def test_cut_root_stub_hint_keeps_the_call_s_other_arguments():
    ids = [f"x{i:05d}" for i in range(900)]
    kids = [comment(f"k{i}", "word " * 60) for i in range(20)] + [more(ids, 1500)]
    fake = FakeReddit({("GET", "/comments/abc123"): ok(thread_payload(kids))})
    server.set_client(fake)
    out = run_raw(server.get_post(post="abc123", comment_sort="new", comment_limit=20, comment_depth=2, max_chars=8000))
    m = re.search(
        r'-> get_post\(post="abc123", comment_sort="new", comment_limit=20, comment_depth=2, '
        r"max_chars=(\d+)\) lists them all\]",
        out,
    )
    assert m, out[-1500:]
    again = run_raw(server.get_post(post="abc123", comment_sort="new", comment_limit=20, comment_depth=2,
                                    max_chars=int(m.group(1))))
    assert _root_ids(again) == ids  # over 500 ids: one larger call still lists them all


def test_root_stub_too_big_for_any_call_says_so_instead_of_naming_max_chars():
    ids = [f"x{i:05d}" for i in range(6000)]
    fake = FakeReddit({("GET", "/comments/abc123"): ok(thread_payload([comment("k1", "hi"), more(ids, 9000)]))})
    server.set_client(fake)
    out = run_raw(server.get_post(post="abc123", max_chars=200_000))
    assert len(out) <= 200_000
    line = next(x for x in out.split("\n") if x.startswith("[more top-level:"))
    assert "max_chars=" not in line
    assert "no single get_post call lists them all" in line and "comment_limit=500" in line
    out = run_raw(server.get_post(post="abc123", comment_limit=500, max_chars=200_000))
    line = next(x for x in out.split("\n") if x.startswith("[more top-level:"))
    assert "no single get_post call lists them all" in line and "comment_limit" not in line


def test_every_unlisted_id_count_has_a_route_in_the_same_output():
    ids = [f"x{i:05d}" for i in range(485)]
    kids = [comment(f"k{i}", "word " * 60) for i in range(150)] + [more(ids, 635)]
    fake = FakeReddit({("GET", "/comments/abc123"): ok(thread_payload(kids))})
    server.set_client(fake)
    for budget in (2000, 6000, 40_000):
        out = run_raw(server.get_post(post="abc123", max_chars=budget))
        assert len(out) <= budget
        for line in out.split("\n"):
            if "more ids not listed" in line:
                assert "-> get_post(" in line or "larger max_chars" in line, line


def test_long_root_ids_do_not_push_loaded_comments_out():
    ids = [f"19t4{i:09d}" for i in range(3000)]  # 13-character ids, as Reddit sends today
    kids = [comment(f"k{i}", "word " * 60) for i in range(150)] + [more(ids, 4000)]
    fake = FakeReddit({("GET", "/comments/abc123"): ok(thread_payload(kids))})
    server.set_client(fake)
    out = run_raw(server.get_post(post="abc123", max_chars=200_000))
    assert len(out) <= 200_000
    assert "comments cut to fit max_chars" not in out and all(f"[k{i}]" in out for i in range(150))
    assert _root_ids(out) == ids
    # Comments take most of a 60000 budget: the id list gets the rest, not the comments' room.
    out = run_raw(server.get_post(post="abc123", max_chars=60_000))
    assert len(out) <= 60_000
    assert "comments cut to fit max_chars" not in out and all(f"[k{i}]" in out for i in range(150))
    out = run_raw(server.get_post(post="abc123"))
    m = re.search(r'max_chars=(\d+)\) lists them all\]', out)
    assert m and "comments cut to fit max_chars" not in out
    again = run_raw(server.get_post(post="abc123", max_chars=int(m.group(1))))
    assert _root_ids(again) == ids and "comments cut to fit max_chars" not in again


def test_several_top_level_stubs_stay_within_budget_and_each_cut_line_has_a_route():
    stubs = [more([f"a{i:05d}" for i in range(4999)], 6000), more([f"b{i:05d}" for i in range(3000)], 4000),
             more([f"c{i:05d}" for i in range(900)], 1000)]
    kids = [comment(f"k{i}", "word " * 60) for i in range(80)] + stubs
    fake = FakeReddit({("GET", "/comments/abc123"): ok(thread_payload(kids))})
    server.set_client(fake)
    for budget in (6000, 40_000):
        out = run_raw(server.get_post(post="abc123", max_chars=budget))
        assert len(out) <= budget
        for line in out.split("\n"):
            if line.startswith("[more top-level:") and "more ids not listed" in line:
                assert "lists them all]" in line or "no single get_post call" in line, line


def test_comments_filling_the_budget_are_not_blamed_on_the_id_list():
    ids = [f"19t4{i:09d}" for i in range(60)]
    kids = [comment(f"k{i}", "word " * 1000) for i in range(60)] + [more(ids, 80)]
    fake = FakeReddit({("GET", "/comments/abc123"): ok(thread_payload(kids))})
    server.set_client(fake)
    out = run_raw(server.get_post(post="abc123", max_chars=2000))
    assert len(out) <= 2000
    line = next(x for x in out.split("\n") if x.startswith("[more top-level:"))
    if "more ids not listed" in line:
        assert "no single get_post call" not in line
        assert "lists them all]" in line or "a smaller comment_limit" in line, line


# Several top-level stubs at a small budget: the stub lines must shrink, not overflow (#68).


def _many_stub_thread(sizes, n_comments=40, words=60, width=13, selftext="Body " * 300):
    stubs = [more([f"{chr(97 + s)}{'9' * (width - 6)}{i:05d}" for i in range(n)], 100) for s, n in enumerate(sizes)]
    kids = [comment(f"k{i}", "word " * words) for i in range(n_comments)] + stubs
    return FakeReddit({("GET", "/comments/abc123"): ok(thread_payload(kids, selftext=selftext))})


def _assert_root_lines_have_routes(out):
    lines = [x for x in out.split("\n") if x.startswith("[more top-level:")]
    assert lines, out
    for line in lines:
        if "not listed" in line:
            routes = ("lists them all]", "no single get_post call", "a smaller comment_limit")
            assert any(r in line for r in routes), line
    return lines


@pytest.mark.parametrize("sizes", [[300, 2000, 40], [40, 2000, 2000, 2000, 40, 300], [5, 40, 300, 2000, 300, 11]])
@pytest.mark.parametrize("budget", [2000, 3000])
def test_several_top_level_stubs_fit_a_small_budget(sizes, budget):
    server.set_client(_many_stub_thread(sizes))
    out = run_raw(server.get_post(post="abc123", max_chars=budget))
    assert len(out) <= budget, (len(out), out)
    lines = _assert_root_lines_have_routes(out)
    # Every stub keeps a line, or one summary line counts every id of every stub.
    assert len(lines) == len(sizes) or (len(lines) == 1 and f"{sum(sizes)} ids not listed" in lines[0]), lines


def test_stub_lines_keep_ten_ids_when_the_budget_has_room():
    server.set_client(_many_stub_thread([300, 2000, 40], n_comments=3, words=5, selftext=""))
    out = run_raw(server.get_post(post="abc123", max_chars=3000))
    assert len(out) <= 3000
    for line in _assert_root_lines_have_routes(out):
        assert line.count(",") >= 9, line  # the 10-id floor holds while it fits


def test_small_budget_stub_line_that_lists_no_id_still_names_the_route_that_lists_them():
    sizes = [40, 2000, 2000, 2000, 40, 300]
    server.set_client(_many_stub_thread(sizes))
    out = run_raw(server.get_post(post="abc123", max_chars=2000))
    assert len(out) <= 2000
    lines = _assert_root_lines_have_routes(out)
    m = re.search(r'-> get_post\(post="abc123", max_chars=(\d+)\) lists them all\]', "\n".join(lines))
    assert m, lines
    again = run_raw(server.get_post(post="abc123", max_chars=int(m.group(1))))
    got = [x for x in again.split("\n") if x.startswith("[more top-level:")]
    assert len(got) == len(sizes) and not any("not listed" in x for x in got), got


def test_many_stub_shapes_stay_within_budget():
    import random

    rnd = random.Random(68)
    for _ in range(400):
        sizes = [rnd.choice([5, 11, 40, 300, 2000]) for _ in range(rnd.choice([1, 2, 3, 4, 6]))]
        server.set_client(_many_stub_thread(
            sizes, n_comments=rnd.choice([0, 3, 40]), words=rnd.choice([5, 60, 400]),
            width=rnd.choice([6, 13]), selftext="Body " * rnd.choice([0, 300, 2000])))
        budget = rnd.choice([2000, 2500, 3000, 5000, 20000])
        out = run_raw(server.get_post(post="abc123", max_chars=budget, body_chars=rnd.choice([0, 6000])))
        assert len(out) <= budget, (sizes, budget, len(out))
        _assert_root_lines_have_routes(out)


@pytest.mark.parametrize("n_stubs", [8, 10, 16])
def test_many_stubs_at_the_smallest_budget_share_one_line_with_the_route(n_stubs):
    server.set_client(_many_stub_thread([2000] * n_stubs))
    out = run_raw(server.get_post(post="abc123", max_chars=2000))
    assert len(out) <= 2000, len(out)
    lines = _assert_root_lines_have_routes(out)
    joined = "\n".join(lines)
    assert f"in {n_stubs} stubs; {2000 * n_stubs} ids not listed" in joined or len(lines) == n_stubs, lines


def test_three_stubs_at_the_smallest_budget_keep_their_own_lines_with_fewer_ids():
    server.set_client(_many_stub_thread([40, 40, 40], n_comments=3, words=5))
    out = run_raw(server.get_post(post="abc123", max_chars=2000))
    assert len(out) <= 2000
    lines = _assert_root_lines_have_routes(out)
    assert len(lines) == 3 and all(" ids: " in x for x in lines), lines  # each still lists ids to expand


def test_many_stubs_at_a_small_budget_leave_room_for_loaded_comments():
    server.set_client(_many_stub_thread([2000] * 10, n_comments=10, words=5))
    out = run_raw(server.get_post(post="abc123", max_chars=3000))
    assert len(out) <= 3000
    assert "Shown 0 of" not in out and "[k0]" in out, out


def test_many_small_stubs_name_a_max_chars_that_lists_them_all():
    stubs = [more([f"s{j:03d}9999999{i}" for i in range(3)], 5) for j in range(1000)]
    server.set_client(FakeReddit({("GET", "/comments/abc123"): ok(thread_payload([comment("k1", "hi")] + stubs))}))
    out = run_raw(server.get_post(post="abc123", max_chars=2000))
    assert len(out) <= 2000 and "no single get_post call" not in out, out[-800:]
    m = re.search(r"max_chars=(\d+)\) lists them all\]", out)
    assert m, out[-800:]
    again = run_raw(server.get_post(post="abc123", max_chars=int(m.group(1))))
    assert len(again) <= int(m.group(1)) and "not listed" not in again.split("\n\nShown")[0]


def test_summary_line_says_at_least_when_a_stub_count_is_missing():
    stubs = [more([f"a{j}9999999{i}" for i in range(2000)], c) for j, c in enumerate([50, None] * 6)]
    server.set_client(FakeReddit({("GET", "/comments/abc123"): ok(thread_payload(stubs))}))
    out = run_raw(server.get_post(post="abc123", max_chars=2000))
    assert len(out) <= 2000 and "at least 300 comments in 12 stubs" in out, out[-900:]
