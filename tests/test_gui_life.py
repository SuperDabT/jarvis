"""The Tasks and Home workspaces in the window, offscreen, with a fixed clock.

Databases under tmp_path (conftest.py refuses the real one); writes go through
jarvis.items on the window's connection.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from test_gui_window import _offscreen_ok  # noqa: E402

if not _offscreen_ok():
    pytest.skip("PySide6 cannot start on the offscreen platform", allow_module_level=True)

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from test_capture import form  # noqa: E402
from test_gui_capture import Brain  # noqa: E402
from test_gui_data import REPO, seed  # noqa: E402
from test_gui_window import _cfg  # noqa: E402

from jarvis import db  # noqa: E402
from jarvis import items as it  # noqa: E402
from jarvis.gui.app import make_app  # noqa: E402
from jarvis.gui.window import MainWindow  # noqa: E402

LA = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 9, 15, 30, tzinfo=LA)


@pytest.fixture(scope="module")
def app():
    return make_app([], scheme="light")


@pytest.fixture
def make(app, tmp_path):
    made = []

    def build(items=True, capture_fn=None, data=True):
        cfg = _cfg(tmp_path)
        conn = db.connect(cfg.db_path)
        if data:
            seed(conn, n_cards=2)
            conn.execute("UPDATE milestones SET due_on = '2026-10-18T07:00:00Z'")
            conn.execute("UPDATE projects SET github_repo = ?", (REPO,))
        if items:
            mk = lambda t, **kw: it.create_item(conn, t, now=NOW, tz=LA, **kw)  # noqa: E731
            mk("late lab", due="2026-10-07", priority=2)
            mk("study", due="2026-10-09", body="before the gym")
            mk("filter", due="2026-10-09T18:00", project="lidar")
            mk("servos", due="2026-10-12", project="lidar")
            mk("server", kind="idea")
        conn.commit()
        win = MainWindow(cfg, conn, auto_refresh=False, watch=False, clock=lambda: NOW,
                         capture_fn=capture_fn)
        win.resize(1500, 940)
        win.show()
        app.processEvents()
        made.append((win, conn))
        return win, conn

    yield build
    for w, c in made:
        w.close()
        c.close()


def shown(view, key):
    sec = view.sections.get(key)
    return [] if sec is None else [
        sec.rows_box.itemAt(i).widget().row.title for i in range(sec.rows_box.count())]


def row(view, title):
    return next(r for r in view.rows.values() if r.row.title == title)


# ---------------------------------------------------------------- tasks


def test_opens_on_home_and_tasks_shows_the_groups(make):
    win, _ = make()
    assert win.workspace == "home"
    win.show_workspace("tasks")
    v = win.tasks_view
    assert shown(v, "overdue") == ["late lab"]
    assert shown(v, "today") == ["study", "filter"]
    assert shown(v, "upcoming") == ["servos"]
    assert shown(v, "someday") == ["server"]
    assert v.summary.text() == "1 overdue · 2 today · 1 upcoming · 1 someday"
    late = row(v, "late lab")
    assert "2 days late" in late.meta.text() and late.property("late") == "true"
    assert late.prio.text() == "!!"
    assert "#lidar" in row(v, "filter").meta.text()
    assert row(v, "study").body.text() == "before the gym"


def test_complete_and_reopen(make):
    win, conn = make()
    v = win.tasks_view
    row(v, "study").check.click()
    assert it.get_item(conn, row(v, "late lab").row.id).done is False
    assert shown(v, "today") == ["filter"]
    assert win.status_label.text() == "Done: “study”"
    v.done_toggle.setChecked(True)
    assert shown(v, "done") == ["study"]
    row(v, "study").check.click()      # the done row's check reopens it
    assert shown(v, "today") == ["study", "filter"]


def test_delete_undo_and_restore(make):
    win, conn = make()
    v = win.tasks_view
    sid = row(v, "study").row.id
    row(v, "study").action.click()
    assert shown(v, "today") == ["filter"] and not v.undo_bar.isHidden()
    assert it.get_item(conn, sid, include_deleted=True).deleted
    v.undo_btn.click()
    assert shown(v, "today") == ["study", "filter"] and v.undo_bar.isHidden()
    # restore from the deleted list
    row(v, "server").action.click()
    v.deleted_toggle.setChecked(True)
    assert shown(v, "deleted") == ["server"]
    row(v, "server").action.click()    # "restore"
    assert shown(v, "someday") == ["server"]
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 5   # never duplicated


def test_delete_key_removes_the_selected_row(make, app):
    win, _ = make()
    win.show_workspace("tasks")
    v = win.tasks_view
    v.select(row(v, "servos").row.id)
    win.activateWindow()   # shortcuts only reach the active window
    app.processEvents()
    v.scroll.setFocus()
    QTest.keyClick(v.scroll, Qt.Key.Key_Delete)
    app.processEvents()
    assert shown(v, "upcoming") == [] and v.selected_id is None


def test_edit_in_the_side_panel(make):
    win, conn = make()
    win.show_workspace("tasks")
    v = win.tasks_view
    sid = row(v, "servos").row.id
    QTest.mouseClick(row(v, "servos"), Qt.MouseButton.LeftButton)
    e = v.editor
    assert v.selected_id == sid and not v.editor_scroll.isHidden()
    assert e.title.text() == "servos" and e.due.text() == "2026-10-12"
    assert e.project.currentText().startswith("#lidar")
    e.title.setText("Order spare servos")
    e.due.setText("Thursday")              # a phrase, worked out without a model
    e.remind.setText("tomorrow 9am")
    e.priority.setCurrentIndex(3)
    e.kind.setCurrentText("reminder")
    e.project.setCurrentIndex(0)           # no project
    e.body.setPlainText("two MG90S")
    e.save_btn.click()
    item = it.get_item(conn, sid)
    assert (item.title, item.due_at, item.remind_at, item.priority, item.kind,
            item.project_id, item.body) == ("Order spare servos", "2026-10-15",
                                            "2026-10-10T09:00", 3, "reminder", None, "two MG90S")
    assert shown(v, "upcoming") == ["Order spare servos"] and v.selected_id == sid
    assert win.status_label.text() == "Saved “Order spare servos”"


@pytest.mark.parametrize("field, value, words", [
    ("title", "  ", "title can't be empty"),
    ("due", "next Thursday", "could mean"),
    ("due", "someday soon", "couldn't read"),
    ("remind", "tomorrow", "needs a time"),
])
def test_editor_explains_bad_input_and_saves_nothing(make, field, value, words):
    win, conn = make()
    v = win.tasks_view
    sid = row(v, "servos").row.id
    v.select(sid)
    before = it.get_item(conn, sid)
    getattr(v.editor, field).setText(value)
    v.editor.save_btn.click()
    assert words in v.editor.error.text() and not v.editor.error.isHidden()
    assert it.get_item(conn, sid) == before


def test_filters_by_kind_and_project(make):
    win, _ = make()
    v = win.tasks_view
    v.kind_buttons["idea"].click()
    assert v.kind_filter == "idea" and v.data.open_count == 1
    assert shown(v, "someday") == ["server"] and v.kind_buttons["idea"].isChecked()
    assert not v.kind_buttons["all"].isChecked()
    v.kind_buttons["all"].click()
    v.project_box.setCurrentIndex(v.project_box.findData("lidar"))
    assert v.project_filter == "lidar"
    assert shown(v, "today") == ["filter"] and shown(v, "upcoming") == ["servos"]
    v.kind_buttons["reminder"].click()
    assert v.data.open_count == 0 and not v.empty.isHidden()
    assert v.empty_title.text() == "Nothing matches these filters."


def test_empty_tasks_points_to_capture(make, app):
    win, _ = make(items=False)
    win.show_workspace("tasks")
    v = win.tasks_view
    assert v.data.open_count == 0 and v.empty_title.text() == "Nothing open."
    v.empty_btn.click()
    app.processEvents()
    assert win.capture.isVisible()


def test_items_added_elsewhere_show_up_on_reload(make, tmp_path):
    win, _ = make()
    other = db.connect(win.cfg.db_path)          # e.g. `jarvis add` in a terminal
    it.create_item(other, "from the CLI", due="2026-10-09", now=NOW, tz=LA)
    other.close()
    win.reload()
    assert "from the CLI" in shown(win.tasks_view, "today")
    assert "from the CLI" in [r.row.title for r in win.home_view.today_rows]


def test_capture_updates_tasks_and_home_at_once(make):
    brain = Brain(form(title="Buy cat food", kind="reminder", remind_phrase="today at 5pm",
                       remind_date="2026-10-09", remind_time="17:00"), now=NOW, tz=LA)
    win, _ = make(capture_fn=brain)
    win.open_capture()
    win.capture.input.setText("remind me to buy cat food today at 5pm")
    win.capture.submit()
    win.capture.wait()
    assert win.capture.state == "saved"
    assert "Buy cat food" in shown(win.tasks_view, "today")
    assert "Buy cat food" in [r.row.title for r in win.home_view.today_rows]


# ---------------------------------------------------------------- home


def test_home_populated(make):
    win, _ = make()
    h = win.home_view
    assert h.greeting.text() == "Good afternoon."
    assert "1 overdue" in h.attention.text() and "2 due today" in h.attention.text()
    assert [r.row.title for r in h.today_rows] == ["late lab", "study", "filter"]
    assert h.today.head.text() == "Today · 3"
    dl = [r.findChildren(type(h.greeting))[1].text() for r in h.deadline_rows]
    assert dl == ["servos", "Core demo"]
    # the inbox: open, no date, no project
    assert [r.row.title for r in h.inbox_rows] == ["server"]
    assert h.inbox.head.text() == "Inbox · 1"
    # projects: open work and the next item by date; the GitHub one has its milestone
    lidar = next(r.summary for r in h.project_rows if r.summary.slug == "lidar")
    assert (lidar.open, lidar.done, lidar.next_title) == (2, 0, "filter")
    assert lidar.next_when == "today" and lidar.milestone == "Core demo"
    assert lidar.milestone_due == "due Sun Oct 18"
    # overview: real counts
    o = h.data.overview
    assert (o.done_week, o.open, o.overdue) == (0, 5, 1)
    assert h.stats["overdue"].text() == "1" and h.stats["overdue"].property("tone") == "bad"


def test_home_empty(make):
    win, _ = make(items=False, data=False)
    h = win.home_view
    assert h.attention.text() == "Nothing due today"
    assert h.today_rows == [] and h.deadline_rows == [] and h.inbox_rows == []
    texts = [w.text() for w in h.findChildren(type(h.greeting)) if w.objectName() == "homeEmpty"]
    assert "Nothing due today or overdue." in texts and "Inbox is clear." in texts
    assert [h.stats[k].text() for k in ("done", "open", "overdue")] == ["0", "0", "0"]


def test_home_ticks_off_and_links(make, app):
    win, conn = make()
    h = win.home_view
    first = h.today_rows[0]
    first.check.click()
    assert it.get_item(conn, first.row.id).done
    assert [r.row.title for r in win.home_view.today_rows] == ["study", "filter"]
    h.today_link.click()
    assert win.workspace == "tasks"
    win.show_workspace("home")
    h.project_link.click()
    assert win.workspace == "projects"
    win.show_workspace("home")
    h.capture_btn.click()
    app.processEvents()
    assert win.capture.isVisible()


def test_home_opens_items_and_projects_and_writes_nothing(make):
    win, conn = make()
    h = win.home_view
    before = conn.total_changes
    win.reload()
    assert conn.total_changes == before              # reading Home changes nothing
    row = next(r for r in h.today_rows if r.row.title == "study")
    row.selected.emit(row.row.id)
    assert win.workspace == "tasks" and win.tasks_view.selected_id == row.row.id
    win.show_workspace("home")
    next(r for r in h.project_rows if r.summary.slug == "lidar").click()
    assert win.workspace == "tasks" and win.tasks_view.project_filter == "lidar"


def test_home_done_counts_only_the_last_week(make):
    win, conn = make()
    late = it.create_item(conn, "old", now=NOW - timedelta(days=10), tz=LA)
    it.complete_item(conn, late, now=NOW - timedelta(days=9))
    it.complete_item(conn, next(r.row.id for r in win.home_view.today_rows), now=NOW)
    conn.commit()
    win.reload()
    o = win.home_view.data.overview
    assert (o.done_week, o.overdue) == (1, 0)


def test_home_reflows_at_the_minimum_size(make, app):
    win, _ = make()
    h = win.home_view
    assert h.narrow is False
    win.resize(980, 600)
    app.processEvents()
    assert h.narrow is True
    x = lambda w, pt: w.mapTo(h, pt).x()  # noqa: E731 - panels sit in column stacks
    assert x(h.today, h.today.rect().topRight()) < x(h.project, h.project.rect().topLeft())
    win.resize(1500, 940)
    app.processEvents()
    assert h.narrow is False
