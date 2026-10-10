"""What needs attention, by code: the morning summary, the full list and the check
before studying (scratch database; any model call fails the test)."""
from __future__ import annotations

from datetime import timedelta

import pytest
from test_intelligence import NOW, add, ask, scratch_conn

from jarvis import attention, intent
from jarvis import items as it


@pytest.fixture
def conn(tmp_path):
    c = scratch_conn(tmp_path)
    yield c
    c.close()


@pytest.fixture
def week(conn):
    """A Friday: an exam Monday 9:00, a lab report Tuesday, a present Thursday, an
    overdue reply, two undated tasks and something far off."""
    return {"exam": add(conn, "Calculus exam", due="2026-10-12T09:00"),
            "dog": add(conn, "Shower the dog"),
            "lab": add(conn, "Lab report", due="2026-10-13"),
            "dana": add(conn, "Reply to Dana", due="2026-10-07"),
            "gift": add(conn, "Buy a present for Mom", due="2026-10-15"),
            "far": add(conn, "Renew passport", due="2026-11-20")}


@pytest.mark.parametrize(("question", "span"), [
    ("Good morning", "brief"), ("morning!", "brief"), ("Give me my daily summary", "brief"),
    ("Brief me", "brief"), ("What does my day look like?", "brief"),
    ("What needs my attention?", "attention"), ("Am I forgetting anything?", "attention"),
    ("hmm, what am I missing this week?", "attention"), ("Is anything at risk?", "attention"),
    ("I'm about to study", "study"), ("What should I study?", "study"),
    ("Before I start studying, anything I should know?", "study"),
    ("Starting my study session now", "study"),
])
def test_routes(question, span):
    r = intent.route_by_rules(question, NOW.date())
    assert (r.intent, r.span) == ("attention", span)


@pytest.mark.parametrize("question", ["Brief me on the lidar project", "What's due today?",
                                      "Help me plan my study week", "remind me to study"])
def test_other_requests_keep_their_routes(conn, question):
    assert intent.route_by_rules(question, NOW.date(), conn).intent != "attention"


def test_morning_summary_is_the_top_three_with_reasons_and_no_model(conn, tmp_path, week):
    a, model = ask(conn, tmp_path, "Good morning")
    assert a.tier == "direct" and a.intent == "attention" and not model.requests and not model.classified
    assert a.text == (
        "**Friday Oct 9**: what needs your attention\n\n"
        "1. Calculus exam Mon Oct 12, 9:00 AM: 3 days left before it (today, Sat, Sun)\n"
        "2. Reply to Dana: 2 days overdue (was due Wed Oct 7)\n"
        "3. Lab report is due Tue Oct 13, the day after Calculus exam: it needs time before "
        "then too\n\n"
        "Nothing is due today.")
    assert {s.ref for s in a.sources} == {f"item:{week[k].id}" for k in ("exam", "dana", "lab")}


def test_the_summary_caps_and_counts_the_rest(conn, week):
    for k in range(3):
        add(conn, f"Form {k}", due="2026-10-10")             # tomorrow, a cluster
    text = attention.daily_brief(conn, NOW).text
    assert text.count("\n1. ") == 1 and "\n4. " not in text
    assert "*1 more to watch: ask “what needs my attention” for the full list.*" in text


def test_today_comes_first(conn, week):
    add(conn, "Dentist", due="2026-10-09T15:00")
    first = attention.daily_brief(conn, NOW).text.splitlines()[2]
    assert first == "1. Dentist 3:00 PM today"


def test_signals(conn, week):
    for k in range(3):
        add(conn, f"Form {k}", due="2026-10-10")
    old = add(conn, "Sort the garage")
    conn.execute("UPDATE items SET updated_at = '2026-09-01T10:00:00+00:00' WHERE id = ?",
                 (old.id,))
    pic = attention.picture(conn, NOW)
    kinds = [s.kind for s in pic.signals]
    # the three forms are listed in their group, not again one by one
    assert kinds == ["event", "overdue", "squeeze", "cluster", "stale"]
    cluster = next(s for s in pic.signals if s.kind == "cluster")
    assert (cluster.text, cluster.details) == ("3 things due tomorrow",
                                               ("Form 0", "Form 1", "Form 2"))
    stale = next(s for s in pic.signals if s.kind == "stale")
    assert stale.text == ("1 undated item untouched for 3+ weeks (oldest: Sort the garage, "
                          "5 weeks)")
    assert all(f"item:{week['far'].id}" not in s.refs for s in pic.signals)   # far off


def test_dated_exam_without_a_time_is_an_event(conn):
    add(conn, "Physics midterm", due="2026-10-14")
    s = attention.picture(conn, NOW).signals[0]
    assert (s.kind, s.text) == ("event", "Physics midterm Wed Oct 14: 5 days left before it "
                                          "(today, Sat, Sun, Mon, Tue)")


def test_done_items_and_github_issues_are_never_signals(conn, week):
    it.complete_item(conn, week["dana"].id, now=NOW)
    pic = attention.picture(conn, NOW)
    assert all("Dana" not in s.text for s in pic.signals)
    assert all(r.startswith("item:") for s in pic.signals for r in s.refs)
    assert "#31" not in attention.attention_answer(conn, NOW).text


def test_empty_list(conn):
    assert "Nothing is due, overdue or coming up" in attention.daily_brief(conn, NOW).text
    assert attention.study_check(conn, NOW).text.endswith(
        "*Suggestion: nothing on your list is pressing by date: a good time for longer-term "
        "work.*")


def test_study_check(conn, tmp_path, week):
    a, model = ask(conn, tmp_path, "I'm about to study, anything I should know?")
    assert a.tier == "direct" and not model.requests and not model.classified
    assert a.text == (
        "**Before you study** (by the dates on your list)\n\n"
        "**Exams and fixed commitments**\n"
        "- Calculus exam, Mon Oct 12, 9:00 AM: 3 days left before it (today, Sat, Sun)\n\n"
        "**Due in the next week**\n"
        "- **Tue Oct 13**\n   - Lab report\n"
        "- **Thu Oct 15**\n   - Buy a present for Mom\n\n"
        "**Overdue**\n- Reply to Dana (2 days)\n\n"
        "*Suggestion: Calculus exam is the soonest fixed commitment, so it is the natural "
        "focus; Lab report is due right after it.*")


def test_study_check_without_an_exam_points_at_the_soonest_deadline(conn):
    add(conn, "Lab report", due="2026-10-13")
    text = attention.study_check(conn, NOW + timedelta(hours=1)).text
    assert "\n\nNo exam or other fixed commitment in the next 7 days.\n\n" in text
    assert text.endswith("*Suggestion: Lab report is due soonest (Tue Oct 13).*")


def test_nothing_is_written(conn, week):
    before = [tuple(r) for r in conn.execute("SELECT * FROM items ORDER BY id")]
    for f in (attention.daily_brief, attention.attention_answer, attention.study_check):
        f(conn, NOW)
    assert [tuple(r) for r in conn.execute("SELECT * FROM items ORDER BY id")] == before


def test_a_request_to_plan_study_time_stays_a_plan():
    r = intent.route_by_rules("Schedule my study time. I'm free Monday 8am to noon.",
                              NOW.date())
    assert r.intent == "planning"
