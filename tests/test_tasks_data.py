"""Task groups, row labels and Home's view model (no Qt), on in-memory databases."""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from jarvis import db
from jarvis import items as it
from jarvis.facts import NextStep
from jarvis.gui import home_data as hd
from jarvis.gui import tasks_data as td

LA = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 9, 15, 30, tzinfo=LA)    # Friday, 3:30 PM
TODAY = date(2026, 10, 9)


@pytest.fixture
def conn():
    return db.connect(":memory:")


def mk(conn, title, **kw):
    return it.create_item(conn, title, now=NOW, tz=LA, **kw)


def board(conn):
    mk(conn, "late lab", due="2026-10-07", priority=2)
    mk(conn, "late call", due="2026-10-09T09:00")
    mk(conn, "study", due="2026-10-09")
    mk(conn, "cat food", kind="reminder", remind="2026-10-09T17:00")
    mk(conn, "filter", due="2026-10-09T18:00", project="lidar", priority=1)
    mk(conn, "servos", due="2026-10-12", project="lidar")
    mk(conn, "homework", due="2026-10-15")
    mk(conn, "dentist", due="2026-11-02T10:30", remind="2026-11-01T18:00")
    mk(conn, "server", kind="idea")
    mk(conn, "datasheet", project="lidar")
    it.complete_item(conn, mk(conn, "done one"))
    it.delete_item(conn, mk(conn, "deleted one"))


def titles(rows):
    return [r.title for r in rows]


# ---------------------------------------------------------------- items groups


def test_every_open_item_is_in_exactly_one_group(conn):
    board(conn)
    g = it.grouped_items(conn, NOW, LA)
    assert list(g) == list(it.GROUPS)
    assert [i.title for i in g["overdue"]] == ["late lab", "late call"]
    assert [i.title for i in g["today"]] == ["study", "cat food", "filter"]
    assert [i.title for i in g["upcoming"]] == ["servos", "homework", "dentist"]
    assert [i.title for i in g["someday"]] == ["server", "datasheet"]
    ids = [i.id for rows in g.values() for i in rows]
    assert sorted(ids) == sorted(i.id for i in it.open_items(conn))  # no gaps, no doubles


def test_groups_move_at_local_midnight(conn):
    board(conn)
    midnight = datetime(2026, 10, 10, 0, 0, tzinfo=LA)
    g = it.grouped_items(conn, midnight, LA)
    assert [i.title for i in g["today"]] == []           # Saturday: nothing that day
    assert "study" in [i.title for i in g["overdue"]]
    assert g["upcoming"][0].title == "servos"


def test_someday_sorts_by_priority_then_age(conn):
    mk(conn, "a")
    mk(conn, "b", priority=3)
    mk(conn, "c", kind="idea")
    assert [i.title for i in it.someday_items(conn)] == ["b", "a", "c"]


def test_deleted_items_newest_first_and_filtered(conn):
    a, b = mk(conn, "a", project="lidar"), mk(conn, "b", kind="idea")
    it.delete_item(conn, a, now=NOW)
    it.delete_item(conn, b, now=datetime(2026, 10, 9, 16, 0, tzinfo=LA))
    assert [i.title for i in it.deleted_items(conn)] == ["b", "a"]
    assert [i.title for i in it.deleted_items(conn, project="lidar")] == ["a"]
    assert [i.title for i in it.deleted_items(conn, kinds="idea")] == ["b"]


# ---------------------------------------------------------------- task rows


def test_load_tasks_rows_and_labels(conn):
    board(conn)
    d = td.load_tasks(conn, NOW, LA)
    assert d.summary() == "2 overdue · 3 today · 3 upcoming · 2 someday"
    late_lab, late_call = d.group("overdue")
    assert (late_lab.when, late_lab.late) == ("Wed Oct 7", "2 days late")
    assert (late_call.when, late_call.late) == ("today 9:00 AM", "6 h late")
    cat = d.group("today")[1]
    assert (cat.when, cat.remind, cat.late) == ("remind today 5:00 PM", "", "")
    filt = d.group("today")[2]
    assert (filt.project, filt.project_name, filt.item.priority) == ("lidar", "LiDAR Scanner", 1)
    dentist = d.group("upcoming")[2]
    assert (dentist.when, dentist.remind) == ("Mon Nov 2 10:30 AM", "remind Sun Nov 1 6:00 PM")
    assert titles(d.done) == ["done one"] and titles(d.deleted) == ["deleted one"]
    assert [p.slug for p in d.projects] == ["lidar"]


def test_filters(conn):
    board(conn)
    d = td.load_tasks(conn, NOW, LA, project="lidar")
    assert d.summary() == "1 today · 1 upcoming · 1 someday"
    d = td.load_tasks(conn, NOW, LA, kind="idea")
    assert d.open_count == 1 and titles(d.group("someday")) == ["server"]
    d = td.load_tasks(conn, NOW, LA, kind="reminder", project="lidar")
    assert d.open_count == 0 and d.summary() == "nothing open"
    with pytest.raises(it.ItemError):
        td.load_tasks(conn, NOW, LA, kind="note")


def test_load_tasks_never_lists_github_issues(conn):
    db.upsert_issue(conn, {"number": 7, "title": "Track filter", "state": "open"})
    d = td.load_tasks(conn, NOW, LA)
    assert d.open_count == 0 and d.done == () and d.deleted == ()


@pytest.mark.parametrize("stamp, label", [
    ("2026-10-09", "today"), ("2026-10-10", "tomorrow"), ("2026-10-08", "yesterday"),
    ("2026-10-15T17:00", "Thu Oct 15 5:00 PM"), ("2026-10-09T00:05", "today 12:05 AM"),
    ("2027-01-04", "Mon Jan 4, 2027"), (None, ""),
])
def test_when_labels(stamp, label):
    assert td.when_label(stamp, TODAY) == label


@pytest.mark.parametrize("stamp, label", [
    ("2026-10-08", "1 day late"), ("2026-10-01", "8 days late"),
    ("2026-10-09T15:10", "20 min late"), ("2026-10-09T13:00", "2 h late"),
])
def test_late_labels(stamp, label):
    assert td.late_label(stamp, NOW) == label


@pytest.mark.parametrize("text, need_time, out", [
    ("", False, None),
    ("2026-10-15", False, "2026-10-15"),
    ("2026-10-15 17:00", False, "2026-10-15T17:00"),
    ("Thursday", False, "2026-10-15"),
    ("tomorrow 5pm", True, "2026-10-10T17:00"),
    ("at 5pm", True, "2026-10-09T17:00"),       # later today
    ("at 9am", True, "2026-10-10T09:00"),       # already past: tomorrow
])
def test_parse_when_input(text, need_time, out):
    assert td.parse_when_input(text, NOW, need_time=need_time) == out


@pytest.mark.parametrize("text, need_time, words", [
    ("next Thursday", False, "could mean"),
    ("whenever", False, "couldn't read"),
    ("2026-02-30", False, "couldn't read"),
    ("tomorrow", True, "needs a time"),
])
def test_parse_when_input_errors(text, need_time, words):
    with pytest.raises(it.ItemError, match=words):
        td.parse_when_input(text, NOW, need_time=need_time)


# ---------------------------------------------------------------- home


def test_home_with_nothing(conn):
    h = hd.build_home(conn, None, None, NOW, LA, project_name="LiDAR Scanner")
    assert (h.overdue, h.due_today, h.deadlines, h.next_steps, h.activity) == ((),) * 5
    assert h.headline is None and h.errors == ()
    assert h.attention == "Nothing due today" and h.greeting == "Good afternoon."


def test_home_with_data(conn):
    board(conn)
    db.upsert_milestone(conn, {"number": 1, "title": "Core demo", "state": "open",
                               "due_on": "2026-10-18T07:00:00Z"})
    db.upsert_milestone(conn, {"number": 2, "title": "Far away", "state": "open",
                               "due_on": "2027-06-01T07:00:00Z"})
    db.upsert_milestone(conn, {"number": 3, "title": "Shipped", "state": "closed",
                               "due_on": "2026-10-12T07:00:00Z"})
    steps = tuple(NextStep(rank=n, kind="milestone_issue", text=f"step {n}", reason="r")
                  for n in range(1, 6))
    data = SimpleNamespace(briefing=SimpleNamespace(headline="Core demo due Oct 18",
                                                    source_label="gemma4:26b"),
                           next_steps=steps)
    vit = lambda k: SimpleNamespace(key=k, value="1", live=False, level=None)  # noqa: E731
    brain = SimpleNamespace(vitals=tuple(vit(k) for k in ("issues", "papers", "checks")),
                            stream=tuple(range(9)))
    h = hd.build_home(conn, data, brain, NOW, LA, project_name="LiDAR Scanner")
    assert titles(h.overdue) == ["late lab", "late call"]
    assert titles(h.due_today) == ["study", "cat food", "filter"]
    assert h.attention == "2 overdue · 3 due today"
    assert [(d.label, d.title, d.kind) for d in h.deadlines] == [
        ("Mon Oct 12", "servos", "task"), ("Thu Oct 15", "homework", "task"),
        ("Sun Oct 18", "Core demo", "milestone")]   # dentist is >14 days out; others filtered
    assert h.headline == "Core demo due Oct 18" and h.briefing_by == "gemma4:26b"
    assert [s.text for s in h.next_steps] == ["step 1", "step 2", "step 3"]
    assert [v.key for v in h.vitals] == ["issues", "checks"]
    assert h.activity == (0, 1, 2, 3, 4)


def test_home_sections_fail_on_their_own(conn):
    conn.execute("DROP TABLE milestones")
    mk(conn, "today thing", due="2026-10-09")
    h = hd.build_home(conn, None, None, NOW, LA)
    assert titles(h.due_today) == ["today thing"]
    assert len(h.errors) == 1 and "milestones" in h.errors[0]


@pytest.mark.parametrize("hour, text", [(6, "Good morning."), (12, "Good afternoon."),
                                        (19, "Good evening."), (2, "Good evening.")])
def test_greeting(hour, text):
    assert hd.greeting(datetime(2026, 10, 9, hour, tzinfo=LA)) == text
