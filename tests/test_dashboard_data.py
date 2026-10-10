"""The Projects dashboard's data (no Qt): risk, the active milestone, issue
ranking from labels, and the commit heatmap's days. In-memory databases."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest
from test_gui_data import REPO

from jarvis import db
from jarvis.gui import data
from jarvis.gui.dashboard_data import (
    ISSUES_SHOWN,
    assess_risk,
    build_dashboard,
    issue_rank,
    pick_active,
    rank_issues,
)
from jarvis.gui.data import MilestoneBar, build_grid

TODAY = date(2026, 10, 9)


def bar(closed=3, total=9, due_in: int | None = 10, number=1, title="Core demo", url=None):
    due = TODAY + timedelta(days=due_in) if due_in is not None else None
    return MilestoneBar(number, title, due, closed, total, 0, url)


def ago(*days):
    return [TODAY - timedelta(days=d) for d in days]


# ---------------------------------------------------------------- risk


@pytest.mark.parametrize("b, closed_on, level, words", [
    (bar(closed=9), ago(1), "done", "Every issue"),
    (bar(closed=0, total=0), [], "unknown", "no issues"),
    (bar(due_in=None), ago(1, 2), "unknown", "no due date"),
    (bar(due_in=-2), ago(1, 2, 3), "overdue", "6 issues still open"),
    # 6 open, 6 closed in 14 days -> 14 days needed, due in 20: fits
    (bar(due_in=20), ago(*range(6)), "on_track", "take about 14 days; due in 20 days"),
    # 6 open, 2 closed in 14 days -> 42 days needed, due in 10: doesn't fit
    (bar(due_in=10), ago(1, 5), "at_risk", "take about 42 days; due in 10 days"),
    # nothing closed recently, due soon: at risk; due later: can't tell
    (bar(due_in=5), ago(30), "at_risk", "No issues closed in the last 14 days, 6 open"),
    (bar(due_in=30), ago(30), "unknown", "no pace to project from"),
    (bar(due_in=30), [], "unknown", "no pace"),
])
def test_risk(b, closed_on, level, words):
    r = assess_risk(b, closed_on, TODAY)
    assert r.level == level and words in r.why


def test_risk_does_not_follow_open_issues_alone():
    # many open issues, but closing fast enough: on track, not "at risk"
    r = assess_risk(bar(closed=10, total=30, due_in=14), ago(*range(14)) * 2, TODAY)
    assert r.level == "on_track"


def test_risk_window_is_the_last_14_days():
    assert assess_risk(bar(due_in=30), ago(14), TODAY).level == "unknown"    # day 14: outside
    assert assess_risk(bar(due_in=300), ago(13), TODAY).level == "on_track"


# ---------------------------------------------------------------- issues


def issue(n, labels=(), ms=1, state="open", closed_at=None):
    return {"number": n, "title": f"Issue {n}", "state": state, "labels": json.dumps(list(labels)),
            "milestone_number": ms, "html_url": f"https://github.com/{REPO}/issues/{n}",
            "closed_at": closed_at}


@pytest.mark.parametrize("labels, tag, kind", [
    (["blocker"], "blocker", "blocker"),
    (["P0"], "P0", "priority"),
    (["priority: high"], "high", "priority"),
    (["Priority-Low"], "low", "priority"),
    (["high priority"], "high", "priority"),
    (["phase-2", "phase-1"], "phase-1", "phase"),
    (["blocked", "P0"], "blocked", "blocked"),
    (["bug", "enhancement"], "", ""),
    (["highway"], "", ""),                      # not a priority word
])
def test_issue_tags(labels, tag, kind):
    _, t, k = issue_rank(labels)
    assert (t, k) == (tag, kind)


def test_ranking_uses_labels_then_number_and_only_the_milestone():
    rows = rank_issues([
        issue(9), issue(3, ["phase-2"]), issue(5, ["low"]), issue(8, ["P1"]),
        issue(2, ["blocker"]), issue(4, ["blocked", "P0"]), issue(7, ["critical"]),
        issue(6, ["phase-1"]), issue(1, ms=2, labels=["blocker"]), issue(10, state="closed"),
        {**issue(11), "labels": "not json"},
    ], 1)
    assert [r.number for r in rows] == [2, 7, 8, 5, 6, 3, 9, 11, 4]


def test_pick_active_milestone():
    a, b = bar(number=1), bar(number=2, due_in=40)
    assert pick_active((a, b), None) is a                 # nearest due first (timeline order)
    assert pick_active((a, b), 2) is b
    assert pick_active((a, b), 99) is a                   # picked one closed/gone: nearest
    assert pick_active((), 2) is None


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    now = datetime.now(UTC)
    for m, title, due in ((1, "Core demo", 9), (2, "Field test", 40)):
        db.upsert_milestone(c, {"number": m, "title": title, "state": "open",
                                "due_on": (now + timedelta(days=due)).isoformat(),
                                "open_issues": 5 if m == 1 else 1, "closed_issues": 1,
                                "html_url": f"https://github.com/{REPO}/milestone/{m}"})
    for i in [issue(n, ["P1"] if n == 4 else []) for n in range(1, 6)] + [issue(6, ms=2)]:
        c.execute("INSERT INTO issues (number, title, state, labels, milestone_number, "
                  "html_url) VALUES (:number, :title, :state, :labels, :milestone_number, "
                  ":html_url)", i)
    c.execute("INSERT INTO issues (number, title, state, labels, milestone_number, closed_at) "
              "VALUES (7, 'done', 'closed', '[]', 1, ?)", ((now - timedelta(days=2)).isoformat(),))
    return c


def test_dashboard_follows_the_active_milestone(conn):
    tl = data.load_timeline(conn, [], datetime.now().date())
    today = datetime.now().date()
    d = build_dashboard(conn, REPO, tl, None, today)
    assert d.active.title == "Core demo" and not d.picked
    assert d.open_total == 6 and d.milestone_open == 5
    assert [r.number for r in d.issues] == [4, 1, 2, 3][:ISSUES_SHOWN]
    assert d.risk.level == "at_risk"          # 1 closed in 14 days, 5 open, ~9 days left
    assert d.view_all_url == f"https://github.com/{REPO}/milestone/1"
    assert [b.title for b in d.others] == ["Field test"]
    d2 = build_dashboard(conn, REPO, tl, 2, today)
    assert d2.picked and d2.active.title == "Field test"
    assert [r.number for r in d2.issues] == [6] and d2.milestone_open == 1
    assert d2.risk.level == "unknown"         # nothing closed, due in ~40 days
    assert [b.title for b in d2.others] == ["Core demo"]


def test_dashboard_without_milestones_lists_all_open_issues(conn):
    conn.execute("DELETE FROM milestones")
    d = build_dashboard(conn, REPO, data.load_timeline(conn, [], TODAY), None, TODAY)
    assert d.active is None and d.risk is None and d.milestone_open == 6
    assert d.view_all_url == f"https://github.com/{REPO}/issues"


# ---------------------------------------------------------------- commit heatmap


def test_commit_log_and_grid_days():
    c = db.connect(":memory:")
    now = datetime.now().astimezone()
    for k, (days, hour) in enumerate([(0, 10), (0, 11), (3, 9), (40, 9)]):
        ts = (now.replace(hour=hour, minute=0) - timedelta(days=days)).astimezone(UTC)
        c.execute("INSERT INTO commits (sha, message, author, committed_at) VALUES (?, ?, 't', ?)",
                  (f"{k:07x}aaaa", f"Commit {k}\n\nbody", ts.isoformat()))
    log = data.commit_log(c)
    today = now.date()
    assert log[today] == ["0000001 Commit 1", "0000000 Commit 0"]       # newest first
    assert data.history_start(c, log) == today - timedelta(days=40)     # no marker: oldest
    db.set_sync_state(c, "commits_since", (now - timedelta(days=60)).isoformat())
    since = data.history_start(c, log)
    assert since == (now - timedelta(days=60)).date()
    g = build_grid([d for d, cs in log.items() for _ in cs], today, log=log, covered_from=since)
    assert g.total == 4 and g.available
    by_day = {d.day: d for d in g.days}
    assert by_day[today].count == 2 and by_day[today].level == 2
    assert "2 commits" in g.tooltip(by_day[today]) and "Commit 1" in g.tooltip(by_day[today])
    old = by_day[since - timedelta(days=1)]
    assert not old.known and "no data" in g.tooltip(old)
    assert by_day[since].known and by_day[since].count == 0


def test_no_history_is_unavailable_not_zero():
    c = db.connect(":memory:")
    wd = data.load_window_data(c, REPO)
    assert wd.grid is not None and not wd.grid.available and wd.grid.total == 0
