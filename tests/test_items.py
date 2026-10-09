"""The item service (jarvis.items) on in-memory databases."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from jarvis import db
from jarvis import items as it
from jarvis.items import ItemError, ItemNotFound

LA = ZoneInfo("America/Los_Angeles")
T0 = datetime(2026, 10, 9, 19, 0, tzinfo=UTC)  # Oct 9, 12:00 in Los Angeles
REPO = "SuperDabT/stm32-lidar-scanner"


@pytest.fixture
def conn():
    return db.connect(":memory:")


def mk(conn, title="Solder the header", **kw):
    kw.setdefault("now", T0)
    kw.setdefault("tz", LA)
    return it.create_item(conn, title, **kw)


def titles(items):
    return [i.title for i in items]


# ---------------------------------------------------------------- create / read


def test_create_and_get(conn):
    a = mk(conn, "  Order servos  ", kind="Reminder", body="  two MG90S \n", priority=2,
           due="2026-10-10", remind="2026-10-10 08:30", project="#lidar")
    assert (a.kind, a.title, a.body, a.priority, a.status) == (
        "reminder", "Order servos", "two MG90S", 2, "open")
    assert (a.due_at, a.remind_at, a.when) == ("2026-10-10", "2026-10-10T08:30", "2026-10-10")
    assert a.project_id == it.get_project(conn, "lidar").id
    assert a.created_at == a.updated_at == "2026-10-09T19:00:00+00:00"
    assert not a.done and not a.deleted and a.completed_at is None
    assert it.get_item(conn, a.id) == a == it.get_item(conn, a)


def test_defaults(conn):
    a = it.create_item(conn, "Idea without anything", kind="idea")
    assert (a.body, a.priority, a.due_at, a.remind_at, a.project_id) == (None, 0, None, None,
                                                                           None)
    assert a.created_at.endswith("+00:00")


@pytest.mark.parametrize("value, stored", [
    (date(2026, 10, 10), "2026-10-10"),
    (datetime(2026, 10, 10, 17, 5, 42), "2026-10-10T17:05"),            # seconds dropped
    (datetime(2026, 10, 11, 0, 30, tzinfo=UTC), "2026-10-10T17:30"),   # aware -> LA
    ("2026-10-10", "2026-10-10"),
    ("2026-10-10T17:00", "2026-10-10T17:00"),
    (" 2026-10-10 17:00 ", "2026-10-10T17:00"),
    ("2028-02-29", "2028-02-29"),
])
def test_due_inputs(conn, value, stored):
    assert mk(conn, due=value).due_at == stored


@pytest.mark.parametrize("kw, field, words", [
    ({"title": ""}, "title", "empty"),
    ({"title": "   "}, "title", "empty"),
    ({"title": None}, "title", "text"),
    ({"title": "x" * 501}, "title", "longer"),
    ({"body": 5}, "body", "text"),
    ({"body": "x" * 20_001}, "body", "longer"),
    ({"kind": "note"}, "kind", "one of task, idea, reminder"),
    ({"kind": None}, "kind", "one of"),
    ({"priority": 4}, "priority", "0 to 3"),
    ({"priority": -1}, "priority", "0 to 3"),
    ({"priority": True}, "priority", "0 to 3"),
    ({"priority": "high"}, "priority", "0 to 3"),
    ({"due": "2026-02-30"}, "due", "not a valid date"),
    ({"due": "2026-10-10T24:00"}, "due", "not a valid date and time"),
    ({"due": "2026-10-10T17:00:00"}, "due", "not a valid date and time"),
    ({"due": "tomorrow"}, "due", "not a valid"),
    ({"due": ""}, "due", "not a valid"),
    ({"due": 20261010}, "due", "a date, a datetime or text"),
    ({"remind": "2026-10-10"}, "remind", "needs a time"),
    ({"remind": date(2026, 10, 10)}, "remind", "needs a time"),
    ({"project": "school"}, "project", "no project"),
    ({"project": 99}, "project", "no project"),
])
def test_bad_input_is_explained_before_the_database_sees_it(conn, kw, field, words):
    title = kw.pop("title", "Valid")
    with pytest.raises(ItemError) as e:
        mk(conn, title, **kw)
    assert e.value.field == field and words in str(e.value)
    assert "CHECK constraint" not in str(e.value)
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


def test_get_missing_and_bad_ids(conn):
    with pytest.raises(ItemNotFound, match="no item 5"):
        it.get_item(conn, 5)
    for bad in (0, -1, "1", True, None):
        with pytest.raises(ItemError, match="positive whole number"):
            it.get_item(conn, bad)


def test_projects(conn):
    lidar = it.get_project(conn, "lidar")
    assert lidar.github_repo == REPO and lidar.name == "LiDAR Scanner"
    for ref in (lidar.id, "#lidar", "LIDAR", REPO.lower(), lidar):
        assert it.get_project(conn, ref) == lidar
    for ref in ("", "nope", 0, True, "a/b"):
        with pytest.raises(ItemNotFound):
            it.get_project(conn, ref)
    assert it.list_projects(conn) == [lidar]
    conn.execute("UPDATE projects SET status = 'archived'")
    assert it.list_projects(conn) == [] and it.list_projects(conn, include_archived=True)


# ---------------------------------------------------------------- update / status


def test_update_changes_only_what_is_given(conn):
    a = mk(conn, body="b", due="2026-10-10", project="lidar")
    later = T0 + timedelta(hours=1)
    b = it.update_item(conn, a.id, title="New title", priority=3, now=later)
    assert (b.title, b.priority, b.body, b.due_at, b.project_id) == (
        "New title", 3, "b", "2026-10-10", a.project_id)
    assert b.updated_at == "2026-10-09T20:00:00+00:00" and b.created_at == a.created_at
    c = it.update_item(conn, a.id, body=None, due=None, project=None, kind="idea", now=later)
    assert (c.body, c.due_at, c.project_id, c.kind) == (None, None, None, "idea")


def test_update_with_nothing_new_writes_nothing(conn):
    a = mk(conn, due="2026-10-10")
    b = it.update_item(conn, a, due=date(2026, 10, 10), title=" Solder the header ",
                       now=T0 + timedelta(days=1))
    assert b == a  # updated_at untouched


def test_update_rejects_bad_values_and_keeps_the_item(conn):
    a = mk(conn)
    with pytest.raises(ItemError, match="empty"):
        it.update_item(conn, a.id, title=" ")
    with pytest.raises(ItemError, match="no project"):
        it.update_item(conn, a.id, project="nope")
    assert it.get_item(conn, a.id) == a


def test_complete_and_reopen(conn):
    a = mk(conn)
    done_at = T0 + timedelta(minutes=5)
    d = it.complete_item(conn, a.id, now=done_at)
    assert d.done and d.completed_at == d.updated_at == "2026-10-09T19:05:00+00:00"
    assert it.complete_item(conn, a.id, now=T0 + timedelta(hours=9)) == d  # idempotent
    r = it.reopen_item(conn, a.id, now=T0 + timedelta(minutes=9))
    assert not r.done and r.completed_at is None
    assert it.reopen_item(conn, a.id) == r


def test_soft_delete_and_restore(conn):
    a = mk(conn)
    gone = it.delete_item(conn, a.id, now=T0 + timedelta(minutes=1))
    assert gone.deleted and gone.deleted_at == "2026-10-09T19:01:00+00:00"
    assert it.delete_item(conn, a.id) == gone  # idempotent
    with pytest.raises(ItemNotFound, match="is deleted"):
        it.get_item(conn, a.id)
    for op in (it.update_item, it.complete_item, it.reopen_item):
        with pytest.raises(ItemNotFound, match="is deleted"):
            op(conn, a.id)
    assert it.open_items(conn) == [] and it.list_items(conn, status="all") == []
    assert it.list_items(conn, status="all", include_deleted=True) == [gone]
    back = it.restore_item(conn, a.id)
    assert not back.deleted and it.open_items(conn) == [back]
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1  # never removed


# ---------------------------------------------------------------- lists


def test_open_items_order_and_filters(conn):
    mk(conn, "no date, low")
    mk(conn, "no date, high", priority=3)
    mk(conn, "Oct 12", due="2026-10-12")
    mk(conn, "Oct 10 09:00", due="2026-10-10T09:00", project="lidar")
    mk(conn, "Oct 10 all day", due="2026-10-10", kind="idea")
    mk(conn, "remind Oct 11", remind="2026-10-11T08:00", kind="reminder")
    done = mk(conn, "finished", due="2026-10-01")
    it.complete_item(conn, done.id, now=T0)
    assert titles(it.open_items(conn)) == [
        "Oct 10 all day", "Oct 10 09:00", "remind Oct 11", "Oct 12", "no date, high",
        "no date, low"]
    assert titles(it.open_items(conn, project="lidar")) == ["Oct 10 09:00"]
    assert titles(it.open_items(conn, kinds="idea")) == ["Oct 10 all day"]
    assert titles(it.open_items(conn, kinds=["reminder", "IDEA"])) == [
        "Oct 10 all day", "remind Oct 11"]
    assert titles(it.list_items(conn, status="done")) == ["finished"]
    assert len(it.list_items(conn, status="all")) == 7
    with pytest.raises(ItemError, match="status"):
        it.list_items(conn, status="closed")
    with pytest.raises(ItemError, match="kinds is empty"):
        it.open_items(conn, kinds=[])
    with pytest.raises(ItemNotFound):
        it.open_items(conn, project="school")


def test_done_items_newest_first(conn):
    a, b = mk(conn, "a"), mk(conn, "b")
    it.complete_item(conn, b.id, now=T0)
    it.complete_item(conn, a.id, now=T0 + timedelta(hours=1))
    assert titles(it.list_items(conn, status="done")) == ["a", "b"]


def _day_board(conn):
    """Items around Oct 9 (the T0 day in Los Angeles)."""
    mk(conn, "yesterday", due="2026-10-08")
    mk(conn, "today all day", due="2026-10-09")
    mk(conn, "today 08:00", due="2026-10-09T08:00")
    mk(conn, "today 18:00", due="2026-10-09T18:00")
    mk(conn, "today 23:59", due="2026-10-09T23:59")
    mk(conn, "tomorrow 00:00", due="2026-10-10T00:00")
    mk(conn, "reminder today 07:00", remind="2026-10-09T07:00")
    mk(conn, "reminder today 20:00", remind="2026-10-09T20:00", kind="reminder")
    mk(conn, "no date")
    done = mk(conn, "done yesterday", due="2026-10-08")
    it.complete_item(conn, done.id)
    gone = mk(conn, "deleted today", due="2026-10-09")
    it.delete_item(conn, gone.id)


def test_today_and_overdue_at_noon(conn):
    _day_board(conn)
    assert titles(it.overdue_items(conn, T0, LA)) == [
        "yesterday", "reminder today 07:00", "today 08:00"]
    assert titles(it.today_items(conn, T0, LA)) == [
        "today all day", "today 18:00", "reminder today 20:00", "today 23:59"]


def test_local_midnight_boundary(conn):
    _day_board(conn)
    before = datetime(2026, 10, 10, 6, 59, tzinfo=UTC)   # Oct 9, 23:59 in LA
    after = datetime(2026, 10, 10, 7, 0, tzinfo=UTC)     # Oct 10, 00:00 in LA
    assert titles(it.today_items(conn, before, LA)) == ["today all day", "today 23:59"]
    assert "today all day" not in titles(it.overdue_items(conn, before, LA))
    # one minute later every Oct 9 item is overdue and Oct 10 00:00 is "today", not late
    assert titles(it.overdue_items(conn, after, LA)) == [
        "yesterday", "today all day", "reminder today 07:00", "today 08:00", "today 18:00",
        "reminder today 20:00", "today 23:59"]
    assert titles(it.today_items(conn, after, LA)) == ["tomorrow 00:00"]


def test_the_same_instant_in_other_zones(conn):
    """Wall-clock times are compared with the clock in tz, not in UTC."""
    _day_board(conn)
    instant = datetime(2026, 10, 9, 23, 30, tzinfo=UTC)
    # Los Angeles: Oct 9, 16:30
    assert "today 18:00" in titles(it.today_items(conn, instant, LA))
    # UTC: Oct 9, 23:30 -> 18:00 has passed
    assert "today 18:00" in titles(it.overdue_items(conn, instant, UTC))
    # Kolkata (+05:30): already Oct 10, 05:00
    assert titles(it.today_items(conn, instant, ZoneInfo("Asia/Kolkata"))) == []
    assert titles(it.today_items(conn, instant, timezone(timedelta(hours=14)))) == []


def test_daylight_saving_change(conn):
    """Nov 1 2026, 01:00-02:00 happens twice in Los Angeles."""
    mk(conn, "01:30", due="2026-11-01T01:30")
    first = datetime(2026, 11, 1, 8, 45, tzinfo=UTC)    # 01:45 PDT
    second = datetime(2026, 11, 1, 9, 15, tzinfo=UTC)   # 01:15 PST (clock went back)
    assert titles(it.overdue_items(conn, first, LA)) == ["01:30"]
    assert titles(it.today_items(conn, second, LA)) == ["01:30"]


def test_naive_now_is_read_in_tz(conn):
    mk(conn, "18:00", due="2026-10-09T18:00")
    assert titles(it.today_items(conn, datetime(2026, 10, 9, 17, 0), LA)) == ["18:00"]
    assert titles(it.overdue_items(conn, datetime(2026, 10, 9, 18, 1), LA)) == ["18:00"]
    assert it.local_now(datetime(2026, 10, 9, 17, 0), LA).utcoffset() == timedelta(hours=-7)


def test_dated_lists_filter_by_project_and_kind(conn):
    mk(conn, "lidar late", due="2026-10-08", project="lidar")
    mk(conn, "other late", due="2026-10-08", kind="idea")
    assert titles(it.overdue_items(conn, T0, LA, project="lidar")) == ["lidar late"]
    assert titles(it.overdue_items(conn, T0, LA, kinds="idea")) == ["other late"]


# ---------------------------------------------------------------- related links


def test_related_is_two_way_and_stored_once(conn):
    a, b, c = mk(conn, "a"), mk(conn, "b"), mk(conn, "c")
    assert it.link_related(conn, b.id, a.id) is True
    assert it.link_related(conn, a, b) is False   # the same pair, the other way
    assert it.link_related(conn, b, a) is False
    rows = conn.execute("SELECT src_id, dst_id, kind FROM links").fetchall()
    assert [tuple(r) for r in rows] == [(str(a.id), str(b.id), "related")]  # smaller id first
    it.link_related(conn, c, a)
    assert titles(it.related_items(conn, a)) == ["b", "c"]
    assert titles(it.related_items(conn, b)) == ["a"]
    assert titles(it.related_items(conn, c)) == ["a"]


def test_a_reverse_row_written_elsewhere_still_counts(conn):
    a, b = mk(conn, "a"), mk(conn, "b")
    conn.execute("INSERT INTO links (src_type, src_id, dst_type, dst_id, kind, created_at) "
                 "VALUES ('item', ?, 'item', ?, 'related', 'x')", (str(b.id), str(a.id)))
    assert it.link_related(conn, a, b) is False
    assert titles(it.related_items(conn, a)) == ["b"]
    assert it.unlink_related(conn, a, b) is True
    assert conn.execute("SELECT COUNT(*) FROM links").fetchone()[0] == 0
    assert it.unlink_related(conn, a, b) is False


def test_related_rules(conn):
    a, b = mk(conn, "a"), mk(conn, "b")
    with pytest.raises(ItemError, match="itself"):
        it.link_related(conn, a, a)
    with pytest.raises(ItemError, match="itself"):
        it.unlink_related(conn, a, a)
    with pytest.raises(ItemNotFound):
        it.link_related(conn, a, 99)
    it.link_related(conn, a, b)
    it.delete_item(conn, b)
    with pytest.raises(ItemNotFound, match="deleted"):
        it.link_related(conn, a, b)
    assert it.related_items(conn, a) == []                       # hidden while deleted
    assert titles(it.related_items(conn, a, include_deleted=True)) == ["b"]  # but kept
    it.restore_item(conn, b)
    assert titles(it.related_items(conn, a)) == ["b"]
    it.complete_item(conn, b)
    c = mk(conn, "c")
    it.link_related(conn, a, c)
    assert titles(it.related_items(conn, a)) == ["c", "b"]       # open before done


# ---------------------------------------------------------------- isolation from GitHub data


def test_item_operations_never_touch_the_github_mirror(conn):
    db.upsert_issue(conn, {"number": 7, "title": "Track filter", "state": "open"})
    db.upsert_commit(conn, {"sha": "02fb2d2abc", "message": "Fix #7"})
    keep = ("issues", "milestones", "commits", "cards", "repo_files", "etags", "sync_log",
            "briefings", "electrical_findings", "chat_messages", "projects")
    before = {t: conn.execute(f"SELECT * FROM {t}").fetchall() for t in keep}
    a, b = mk(conn, "#7 follow-up", due="2026-10-08", project="lidar"), mk(conn, "b")
    it.update_item(conn, a, title="Finish #7")
    it.complete_item(conn, a)
    it.reopen_item(conn, a)
    it.link_related(conn, a, b)
    it.delete_item(conn, b)
    it.overdue_items(conn, T0, LA)
    assert {t: conn.execute(f"SELECT * FROM {t}").fetchall() for t in keep} == before


def test_a_constraint_the_checks_missed_becomes_an_item_error(conn, monkeypatch):
    a = mk(conn)
    monkeypatch.setattr(it, "_priority", lambda v: v)  # let a bad value through
    with pytest.raises(ItemError, match="database refused"):
        it.update_item(conn, a.id, priority=9)
    assert it.get_item(conn, a.id).priority == 0
    assert not conn.in_transaction
