import pytest
from fastmcp.exceptions import ToolError
from fixtures import FakeReddit, comment, cont, listing, more, ok, post, run, run_raw

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

    fake = FakeReddit({("GET", "/api/morechildren"): things})
    server.set_client(fake)
    ids = [f"e{i}" for i in range(150)]
    out = run(server.expand_comments(post="abc123", comment_ids=ids))
    assert fake.paths() == ["/api/morechildren", "/api/morechildren"]
    first, second = fake.calls[0][2], fake.calls[1][2]
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
    ]}}})})
    server.set_client(fake)
    ids = [f"e{i}" for i in range(620)]
    out = run(server.expand_comments(post="abc123", comment_ids=",".join(ids)))
    assert len(fake.calls) == 5
    assert "Not fetched yet (120 ids)" in out
    assert "-- top-level comments --" in out


def test_expand_comments_empty_result():
    fake = FakeReddit({("GET", "/api/morechildren"): ok({"json": {"errors": [], "data": {"things": []}}})})
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
    ]}}})})
    server.set_client(fake)
    out = run_raw(server.expand_comments(post="abc123", comment_ids=[f"e{i}" for i in range(700)], max_chars=2000))
    assert len(out) <= 2000
    assert "Not fetched yet (600 ids)" in out and "Output budget reached" in out
    # stops fetching once the budget is spent; the second call explains the ids not returned
    assert fake.paths() == ["/api/morechildren", "/api/info"]


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
    fake = FakeReddit({("GET", "/api/morechildren"): ok({"json": {"errors": [], "data": {"things": things}}})})
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
    fake = FakeReddit({("GET", "/api/morechildren"): ok({"json": {"errors": [], "data": {"things": []}}})})
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
