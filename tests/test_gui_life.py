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


def _texts(w) -> list[str]:
    from PySide6.QtWidgets import QLabel
    return [lb.text() for lb in w.findChildren(QLabel)]


def test_home_populated(make):
    win, _ = make()
    h = win.home_view
    assert h.greeting.text() == "Good afternoon"
    assert h.scroll.widget().objectName() == "homePage"       # Home's own palette
    # Today's Schedule: overdue first, then timed, then any time; ticking needs items
    assert [r.row.title for r in h.today_rows] == ["late lab", "filter", "study"]
    assert [r.row.time for r in h.today_rows] == ["Wed Oct 7", "6:00 PM", ""]
    # Today's Focus follows attention.picture; the deadlines card counts what's ahead
    assert [r.title_text for r in h.focus_rows] == [f.head for f in h.data.focus]
    assert [r.title_text for r in h.deadline_rows] == ["servos", "Core demo"]
    assert h.deadline_card.count.text() == "2"
    # projects: progress and the GitHub milestone's risk
    lidar = next(r.summary for r in h.project_rows if r.summary.slug == "lidar")
    assert lidar.milestone == "Core demo" and lidar.milestone_due == "due Sun Oct 18"
    # quick stats: real numbers, and what Jarvis can't know says so
    assert h.stats["Overdue"].text() == "1"
    assert [h.stats[k].text() for k in ("Focus time", "Sleep", "Training")] == \
        ["Not tracked"] * 3
    # the glance, top priority and recommendation
    assert h.count_labels["overdue"].text() == "1 overdue item"
    assert h.priority_title.text() == h.data.top_priority.head
    assert h.recommend_text.text() == h.data.recommendation.text
    assert "Weather not set up" in _texts(h.banner)


def test_home_empty(make):
    win, _ = make(items=False, data=False)
    h = win.home_view
    assert h.today_rows == [] and h.focus_rows == [] and h.deadline_rows == []
    texts = _texts(h)
    for line in ("Nothing needs attention right now.", "Nothing due in the next two weeks.",
                 "Nothing on this day.", "Nothing in the last week."):
        assert line in texts
    assert h.priority_title.text() == "Nothing pressing" and h.priority_btn.isHidden()
    assert h.count_labels["overdue"].text() == "0 overdue items"


def test_home_ticks_off_and_links(make, app):
    win, conn = make()
    h = win.home_view
    first = h.today_rows[0]
    first.check.click()
    assert it.get_item(conn, first.row.item_id).done
    assert [r.row.title for r in win.home_view.today_rows] == ["filter", "study"]
    h.today_link.click()
    assert win.workspace == "tasks"
    win.show_workspace("home")
    h.project_link.click()
    assert win.workspace == "projects"
    win.show_workspace("home")
    h.capture_btn.click()
    app.processEvents()
    assert win.capture.isVisible()


def test_home_buttons_open_the_chat_with_a_request(make, app, monkeypatch):
    win, _ = make()
    asked: list[str] = []
    monkeypatch.setattr(win.chat, "ask", asked.append)
    h = win.home_view
    h.actions["Before I study"].click()
    h.plan_btn.click()
    h.actions["Ask Jarvis"].click()
    app.processEvents()
    assert win.chat.isVisible()
    assert asked == ["I'm about to study, what should I focus on?",
                     h.data.recommendation.ask]


def test_home_schedule_steps_through_days(make):
    win, _ = make()
    h = win.home_view
    assert not h.prev_day.isEnabled() and h.schedule_card.title.text() == "Today's Schedule"
    for _ in range(3):
        h.next_day.click()
    assert h.day_label.text() == "Mon Oct 12" and h.schedule_card.title.text() == "Schedule"
    assert [r.row.title for r in h.today_rows] == ["servos"]


def test_home_opens_items_and_projects_and_writes_nothing(make):
    win, conn = make()
    h = win.home_view
    before = conn.total_changes
    win.reload()
    assert conn.total_changes == before              # reading Home changes nothing
    row = next(r for r in h.today_rows if r.row.title == "study")
    row.selected.emit(row.row.item_id)
    assert win.workspace == "tasks" and win.tasks_view.selected_id == row.row.item_id
    win.show_workspace("home")
    next(r for r in h.project_rows if r.summary.slug == "lidar").click()
    assert win.workspace == "tasks" and win.tasks_view.project_filter == "lidar"


def test_home_done_counts_only_the_last_week(make):
    win, conn = make()
    late = it.create_item(conn, "old", now=NOW - timedelta(days=10), tz=LA)
    it.complete_item(conn, late, now=NOW - timedelta(days=9))
    it.complete_item(conn, next(r.row.item_id for r in win.home_view.today_rows), now=NOW)
    conn.commit()
    win.reload()
    o = win.home_view.data.overview
    assert (o.done_week, o.overdue) == (1, 0)


def test_home_reflows_at_the_minimum_size(make, app):
    win, _ = make()
    h = win.home_view
    assert h.narrow is False and h.columns == 2
    x = lambda w: w.mapTo(h, w.rect().topLeft()).x()  # noqa: E731
    assert x(h.schedule_card) > x(h.stats_card)          # the right column beside
    win.resize(980, 600)
    app.processEvents()
    assert h.narrow is True and h.columns == 1
    y = lambda w: w.mapTo(h, w.rect().topLeft()).y()  # noqa: E731
    assert y(h.schedule_card) > y(h.week_card)            # ... and under the cards
    win.resize(1500, 940)
    app.processEvents()
    assert h.narrow is False


def test_chat_capture_answer_prefills_capture_and_saves_nothing(make, app):
    from jarvis.assistant import capture_answer
    from jarvis.gui.chat_panel import BotMessage

    win, conn = make()
    before = len(it.list_items(conn, status="all"))
    reply = capture_answer("shower the dog tomorrow")
    win.chat.ask_fn = lambda *a, **kw: reply
    win.set_chat_open(True)
    win.chat.ask("Remind me to shower the dog tomorrow")
    win.chat.wait()
    bot = win.chat.findChildren(BotMessage)[-1]
    assert bot.action_btn is not None and "from your data" in bot.meta.text()
    bot.action_btn.click()
    app.processEvents()
    assert win.capture.isVisible() and win.capture.input.text() == "shower the dog tomorrow"
    assert len(it.list_items(conn, status="all")) == before    # saving is the user's Enter


def _proposal_reply(conn, item_id, due):
    from jarvis import actions
    from jarvis.chat import Answer

    p, errors = actions.build_proposal(conn, [{"op": "set_remind", "item_id": item_id,
                                               "remind": due, "reason": "free then"}], NOW, LA)
    assert not errors
    return Answer(text="Proposed: study on Saturday.", sources=[], unverified_numbers=[],
                  notes=[], model="m", duration=0.1, tier="agent",
                  action={"kind": "proposal", "proposal": p.to_dict()})


@pytest.mark.parametrize("apply", [True, False])
def test_chat_proposal_card_applies_or_dismisses(make, app, apply):
    from jarvis.gui.chat_panel import BotMessage

    win, conn = make()
    study = next(i for i in it.open_items(conn) if i.title == "study")
    reply = _proposal_reply(conn, study.id, "2026-10-10T10:00")   # built here: this thread's conn
    win.chat.ask_fn = lambda *a, **kw: reply
    win.set_chat_open(True)
    win.chat.ask("schedule my studying")
    win.chat.wait()
    card = win.chat.findChildren(BotMessage)[-1].proposal
    assert card is not None and "not saved" in card.findChildren(type(win.home_view.greeting))[0].text()
    assert it.get_item(conn, study.id).remind_at is None           # nothing before the click
    (card.apply_btn if apply else card.dismiss_btn).click()
    app.processEvents()
    if apply:
        assert it.get_item(conn, study.id).remind_at == "2026-10-10T10:00"
        assert it.get_item(conn, study.id).due_at == "2026-10-09"   # the deadline stays
        assert "Saved and checked 1 change." in card.result.text()
        assert "✓ study: due Fri Oct 9 · reminder Sat Oct 10, 10:00 AM" in card.result.text()
    else:
        assert it.get_item(conn, study.id).remind_at is None
        assert "Nothing was changed" in card.result.text()
    assert not card.apply_btn.isEnabled()


def test_home_photos_rotate_by_day_and_fall_back(app, tmp_path):
    from jarvis.gui import home_cards as hc
    bundled = sorted(hc.IMAGES.glob("banner-*.jpg"))
    assert len(bundled) >= 2 and (hc.IMAGES / "CREDITS.md").is_file()
    for f in [*bundled, *hc.IMAGES.glob("quote-*.jpg")]:
        assert f.name in (hc.IMAGES / "CREDITS.md").read_text()      # every photo credited
    a = hc.photo("banner", 0, hc.IMAGES)
    b = hc.photo("banner", 1, hc.IMAGES)
    assert a is not None and b is not None and a.cacheKey() != b.cacheKey()
    assert hc.photo("banner", 0, tmp_path) is None                   # none: the gradient
    assert hc.photo("quote", 3, hc.IMAGES) is not None
