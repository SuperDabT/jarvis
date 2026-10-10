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


# ---------------------------------------------------------------- the dashboard


def _canvas(conn, *entries):
    from jarvis import canvas
    canvas.store(conn, [canvas.CanvasEvent(f"event-assignment-{k}", "assignment", t,
                                           "COMSC-230-5148", w)
                        for k, (t, w) in enumerate(entries)], NOW)


def test_dashboard_focus_matches_the_attention_ranking(conn):
    from jarvis import attention
    board(conn)
    h = hd.build_home(conn, None, None, NOW, LA)
    sigs = attention.picture(conn, NOW).signals
    assert [f.head for f in h.focus] == [s.head for s in sigs[:3]]
    assert h.top_priority == h.focus[0]
    assert (h.focus[0].head, h.focus[0].note, h.focus[0].tone) == (
        "study", "due today", "amber")
    assert h.top_priority.item_id is not None
    assert h.top_why == "Due today."


def test_dashboard_glance_and_recommendation(conn):
    board(conn)
    h = hd.build_home(conn, None, None, NOW, LA)
    # overdue as the Today list counts it; due this week = today's commitments still
    # ahead (cat food, filter) and what is due later (servos, homework)
    assert h.glance == hd.Glance(overdue=2, due_week=4, at_risk=0, done_week=1)
    # late call (9:00) has passed: cat food (5:00 PM) is the next fixed commitment
    assert h.recommendation.text == ("Cat food is the soonest fixed commitment, so it is the "
                                     "natural focus; study is due the same day.")
    assert h.recommendation.ask == "I'm about to study, what should I focus on?"


def test_dashboard_stats_are_real_or_say_not_tracked(conn):
    board(conn)
    done = mk(conn, "done thing")
    it.complete_item(conn, done.id, now=NOW)
    h = hd.build_home(conn, None, None, NOW, LA)
    stats = {s.label: s for s in h.stats}
    assert (stats["Tasks completed"].value, stats["Tasks completed"].delta) == (
        "1", "+1 vs last week")      # board's "done one" is completed at the real clock
    assert stats["Overdue"].value == "2" and stats["Overdue"].tone == "bad"
    assert [stats[k].value for k in ("Focus time", "Sleep", "Training")] == [None] * 3


def test_dashboard_week_counts_whats_due_by_category(conn):
    board(conn)
    _canvas(conn, ("Video Quiz 3", "2026-10-06"), ("Lab 4", "2026-10-09T23:59"))
    h = hd.build_home(conn, None, None, NOW, LA)
    assert [b.label for b in h.week] == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    fri = h.week[4]
    assert (fri.today, fri.study, fri.work, fri.personal) == (True, 1, 1, 3)
    assert (h.week[1].study, h.week[2].personal) == (1, 1)     # Tue quiz; Wed late lab


def test_dashboard_schedule_lists_the_day_late_timed_then_any_time(conn):
    board(conn)
    _canvas(conn, ("Lab 4", "2026-10-09T23:59"), ("Reading", "2026-10-09"))
    mk(conn, "Physics midterm", due="2026-10-10T10:00")
    h = hd.build_home(conn, None, None, NOW, LA)
    day, slots = h.schedule[0]
    assert day == TODAY
    assert [(s.time, s.title, s.category, s.late) for s in slots] == [
        ("Wed Oct 7", "late lab", "Personal", True), ("Late", "late call", "Personal", True),
        ("5:00 PM", "cat food", "Personal", False), ("6:00 PM", "filter", "Work", False),
        ("11:59 PM", "Lab 4 (COMSC-230)", "Study", False),
        ("", "study", "Personal", False), ("", "Reading (COMSC-230)", "Study", False)]
    assert [(s.time, s.title, s.category) for s in h.schedule[1][1]] == [
        ("10:00 AM", "Physics midterm", "Exam")]
    assert len(h.schedule) == hd.SCHEDULE_DAYS


def test_dashboard_canvas_in_deadlines_and_upcoming(conn):
    board(conn)
    _canvas(conn, ("Video Quiz 3", "2026-10-13"))
    h = hd.build_home(conn, None, None, NOW, LA)
    quiz = next(d for d in h.deadlines if d.title == "Video Quiz 3")
    assert (quiz.kind, quiz.project) == ("canvas", "COMSC-230")
    assert all(d.day > TODAY for d in h.upcoming) and len(h.upcoming) <= hd.UPCOMING_MAX


def test_dashboard_recent_activity(conn):
    done = mk(conn, "Submit form")
    it.complete_item(conn, done.id, now=NOW)
    brain = SimpleNamespace(stream=(SimpleNamespace(kind="commit", when="2026-10-09T20:00:00+00:00",
                                                    text="Fix the servo jitter"),))
    h = hd.build_home(conn, None, brain, NOW, LA)
    texts = [a.text for a in h.recent]
    assert texts == ["Completed: Submit form", "Added: Submit form",
                     "Commit: Fix the servo jitter"]
    assert "Commit: Fix the servo jitter" in texts and len(h.recent) <= hd.RECENT_MAX
    assert next(a for a in h.recent if a.text == "Commit: Fix the servo jitter").ago == "2h ago"


def test_dashboard_name_quote_and_salutation(conn):
    h = hd.build_home(conn, None, None, NOW, LA, name="Tal")
    assert h.name == "Tal" and hd.salutation(NOW, h.name) == "Good afternoon, Tal"
    assert hd.salutation(NOW) == "Good afternoon"
    assert h.quote == hd.quote_of(TODAY) and h.quote != hd.quote_of(date(2026, 10, 10))


def test_dashboard_never_states_a_duration(conn):
    import re
    board(conn)
    h = hd.build_home(conn, None, None, NOW, LA)
    text = repr(h)
    # elapsed facts ("6 h late", "2h ago") are fine; an estimate ("2h", "1.5 hours") isn't
    assert not re.search(r"\b\d+(?:\.\d+)?\s*(?:h|hrs?|hours?|min|minutes?)\b"
                         r"(?! (?:ago|late))", text)


def test_dashboard_project_progress_and_risk(conn):
    board(conn)
    db.upsert_milestone(conn, {"number": 1, "title": "Core demo", "state": "open",
                               "due_on": "2026-10-12T07:00:00Z", "open_issues": 4,
                               "closed_issues": 1})
    conn.execute("UPDATE projects SET github_repo = 'SuperDabT/stm32-lidar-scanner'")
    h = hd.build_home(conn, None, None, NOW, LA, repo="SuperDabT/stm32-lidar-scanner")
    p = next(x for x in h.projects if x.slug == "lidar")
    assert (p.milestone_closed, p.milestone_total, p.progress) == (1, 5, 0.2)
    assert (p.risk, p.risk_tone) == ("At risk", "warn") and p.last_activity
    assert h.glance.at_risk == 1


def test_dashboard_empty(conn):
    h = hd.build_home(conn, None, None, NOW, LA)
    assert (h.focus, h.top_priority, h.recent, h.upcoming) == ((), None, (), ())
    assert h.glance == hd.Glance(0, 0, 0, 0)
    assert all(not slots for _, slots in h.schedule)
    assert h.recommendation.text.startswith("Nothing on your list is pressing")


# ---------------------------------------------------------------- the board


def test_board_columns_split_upcoming_at_a_week(conn):
    board(conn)
    mk(conn, "far thing", due="2026-10-20")
    d = td.load_tasks(conn, NOW, LA)
    cols = dict(d.columns())
    assert list(cols) == list(td.COLUMNS)
    assert titles(cols["week"]) == ["servos", "homework"]
    assert titles(cols["later"]) == ["far thing", "dentist"]
    assert titles(cols["someday"]) == titles(d.group("someday"))


def test_board_done_column_is_the_last_week(conn):
    old = mk(conn, "old")
    it.complete_item(conn, old, now=datetime(2026, 9, 20, 12, tzinfo=LA))
    new = mk(conn, "new")
    it.complete_item(conn, new, now=NOW)
    d = td.load_tasks(conn, NOW, LA)
    assert titles(d.done_recent()) == ["new"]


@pytest.mark.parametrize(("query", "hit"), [("lab", True), ("LAB late", True),
                                            ("gym", False), ("before", True)])
def test_board_search(conn, query, hit):
    i = mk(conn, "late lab", due="2026-10-07", body="before the deadline")
    row = td.row_view(i, "overdue", NOW, {})
    assert td.matches(row, query) is hit


def test_board_category(conn):
    assert td.category(td.row_view(mk(conn, "Physics midterm"), "someday", NOW, {})) == "Exam"
    assert td.category(td.row_view(mk(conn, "filter", project="lidar"), "someday", NOW, {})) \
        == "Work"
    assert td.category(td.row_view(mk(conn, "dog"), "someday", NOW, {})) == "Personal"


def test_drop_target_keeps_the_time_and_checks_the_column(conn):
    timed = mk(conn, "call", due="2026-10-07T09:30")
    assert td.drop_target(timed, "today", TODAY) == ("due", "2026-10-09T09:30")
    assert td.drop_target(timed, "week", TODAY, date(2026, 10, 12)) == ("due", "2026-10-12T09:30")
    assert td.drop_target(timed, "later", TODAY, date(2026, 11, 2)) == ("due", "2026-11-02T09:30")
    assert td.drop_target(timed, "someday", TODAY) == ("due", None)
    rem = mk(conn, "cat food", kind="reminder", remind="2026-10-09T17:00")
    assert td.drop_target(rem, "week", TODAY, date(2026, 10, 10)) == ("remind",
                                                                       "2026-10-10T17:00")
    for column, day, words in (("overdue", None, "can't be moved"),
                               ("week", None, "pick a day"),
                               ("week", date(2026, 10, 30), "next 7 days"),
                               ("later", date(2026, 10, 12), "after the next 7 days")):
        with pytest.raises(ValueError, match=words):
            td.drop_target(timed, column, TODAY, day)
    with pytest.raises(ValueError, match="needs a date"):
        td.drop_target(rem, "someday", TODAY)


@pytest.mark.parametrize(("typed", "title", "due"), [
    ("lab report friday 5pm", "lab report", "2026-10-09T17:00"),   # NOW is a Friday, 3:30 PM
    ("lab report monday 9am", "lab report", "2026-10-12T09:00"),
    ("buy cat food tomorrow", "buy cat food", "2026-10-10"),
    ("hand in the form by Thursday", "hand in the form", "2026-10-15"),
    ("call mom", "call mom", None),
    ("tomorrow", "tomorrow", None),            # nothing left of the title: kept as typed
    ("meeting next thursday", "meeting next thursday", None),   # ambiguous: left in
])
def test_parse_title_date(typed, title, due):
    got_title, got_due, _ = td.parse_title_date(typed, NOW)
    assert (got_title, got_due) == (title, due)
