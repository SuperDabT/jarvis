"""The application shell: navigation rail, workspaces, and the cockpit and chat
inside it. Offscreen; databases under tmp_path (conftest.py refuses the real one).
"""
from __future__ import annotations

import os

import pytest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from test_gui_window import _offscreen_ok  # noqa: E402

if not _offscreen_ok():
    pytest.skip("PySide6 cannot start on the offscreen platform", allow_module_level=True)

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QFontMetrics  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from test_gui_data import seed  # noqa: E402
from test_gui_window import _cfg  # noqa: E402

from jarvis import db  # noqa: E402
from jarvis.gui import motion, shell, theme  # noqa: E402
from jarvis.gui.app import make_app  # noqa: E402
from jarvis.gui.shell import PlaceholderPage  # noqa: E402
from jarvis.gui.window import (  # noqa: E402
    IDLE_HINT,
    LEFT_W,
    LEFT_W_NARROW,
    RIGHT_W,
    RIGHT_W_NARROW,
    SHELL_HINT,
    MainWindow,
)

KEYS = ("home", "tasks", "school", "projects", "knowledge")
DIGITS = (Qt.Key.Key_1, Qt.Key.Key_2, Qt.Key.Key_3, Qt.Key.Key_4, Qt.Key.Key_5)


@pytest.fixture(scope="module")
def app():
    return make_app([], scheme="light")


@pytest.fixture
def win(app, tmp_path):
    cfg = _cfg(tmp_path)
    conn = db.connect(cfg.db_path)
    seed(conn, n_cards=2)
    w = MainWindow(cfg, conn, auto_refresh=False, watch=False)
    w.resize(1500, 940)
    w.show()
    app.processEvents()
    yield w
    w.close()
    conn.close()


def test_rail_lists_the_five_workspaces_and_starts_on_home(win):
    assert shell.KEYS == KEYS
    assert list(win.rail.buttons) == list(KEYS) == list(win.workspaces)
    assert [b.ws.name for b in win.rail.buttons.values()] == [
        "Home", "Tasks", "School", "Projects", "Knowledge"]
    assert win.workspace == "home" and win.rail.current == "home"
    assert win.stack.count() == 5
    assert win.crumb.text() == "/ home"
    assert win.hint_label.text() == SHELL_HINT
    win.show_workspace("projects")
    assert win.crumb.text() == "/ projects / robot"
    assert win.hint_label.text() == IDLE_HINT
    assert win.rail.buttons["tasks"].toolTip() == "Tasks (Ctrl+2)"


def test_clicking_the_rail_switches_workspaces(win, app):
    for key in KEYS:
        QTest.mouseClick(win.rail.buttons[key], Qt.MouseButton.LeftButton)
        app.processEvents()
        assert win.workspace == key and win.rail.current == key
        assert win.stack.currentWidget() is win.workspaces[key]
        assert sum(b.isChecked() for b in win.rail.buttons.values()) == 1
    assert win.crumb.text() == "/ knowledge" and win.hint_label.text() == SHELL_HINT


@pytest.mark.parametrize("n", range(5))
def test_ctrl_digit_switches(win, app, n):
    QTest.keyClick(win, DIGITS[n], Qt.KeyboardModifier.ControlModifier)
    app.processEvents()
    assert win.workspace == KEYS[n] and win.rail.current == KEYS[n]


def test_shortcuts_work_while_typing_in_the_chat(win, app):
    win.focus_chat()
    app.processEvents()
    assert win.chat.composer.edit.hasFocus()
    QTest.keyClick(win.chat.composer.edit, Qt.Key.Key_1, Qt.KeyboardModifier.ControlModifier)
    app.processEvents()
    assert win.workspace == "home" and win.chat.isVisible()


def test_unknown_workspace_is_an_error(win):
    with pytest.raises(KeyError):
        win.show_workspace("garage")


def test_placeholders_say_so_and_offer_real_actions(win, app):
    for key in ("school", "knowledge"):
        page = win.workspaces[key]
        assert isinstance(page, PlaceholderPage)
        assert page.badge.text() == "not built yet"
        assert page.title.text() == key.capitalize() and page.plan.text()
    assert not isinstance(win.workspaces["home"], PlaceholderPage)
    assert not isinstance(win.workspaces["tasks"], PlaceholderPage)
    school = win.workspaces["school"]
    win.show_workspace("school")
    school.capture_btn.click()
    app.processEvents()
    assert win.capture.isVisible()
    win._on_escape()
    school.projects_btn.click()
    assert win.workspace == "projects"


@pytest.fixture
def motion_on(app):
    motion.set_enabled(True)  # the graph only animates with motion on
    yield
    motion.finish_all()
    motion.set_enabled(None)


def test_cockpit_lives_in_projects_and_pauses_when_hidden(win, app, motion_on):
    win.show_workspace("projects")
    app.processEvents()
    win.graph._sync_timer()
    projects = win.workspaces["projects"]
    for w in (win.graph, win.hud, win.inspector, win.stream, win.right, win.vitals,
              win.issues, win.milestones, win.commits, win.status_text):
        assert projects.isAncestorOf(w)
    assert win.graph.isVisible() and win.graph._timer.isActive()
    win.show_workspace("home")
    app.processEvents()
    assert not win.graph.isVisible() and not win.graph._timer.isActive()
    win.show_workspace("projects")
    app.processEvents()
    assert win.graph._timer.isActive()


def test_chat_opens_beside_any_workspace_and_esc_closes_it(win, app):
    for key in KEYS:
        win.show_workspace(key)
        QTest.keyClick(win, Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier)
        app.processEvents()
        assert win.chat.isVisible() and win.workspace == key
        assert not win.workspaces["projects"].isAncestorOf(win.chat)  # shell level
        win._on_escape()
        assert not win.chat.isVisible()


def test_chat_keeps_its_place_across_switches(win, app):
    win.set_chat_open(True)
    win.show_workspace("school")
    win.show_workspace("projects")
    app.processEvents()
    assert win.chat.isVisible() and not win.right.isVisible()  # it still takes the column
    win.set_chat_open(False)
    assert win.right.isVisible()


def test_a_graph_reference_brings_projects_back(win, app):
    win.show_workspace("tasks")
    win.select_node("ms:1")
    app.processEvents()
    assert win.workspace == "projects" and win.graph.lock_id == "ms:1"
    win._on_escape()   # releases the lock, as before
    assert win.graph.lock_id is None


def test_esc_order_is_unchanged(win, app):
    win.set_chat_open(True)
    win.graph.lock("ms:1")
    win.open_capture()
    win._on_escape()
    assert win.capture.isHidden() and win.graph.lock_id == "ms:1" and win.chat.isVisible()
    win._on_escape()
    assert win.graph.lock_id is None and win.chat.isVisible()
    win._on_escape()
    assert not win.chat.isVisible()


def test_layout_at_desktop_and_minimum_size(win, app):
    win.show_workspace("projects")
    app.processEvents()
    assert win.rail.wide and win.rail.width() == shell.RAIL_WIDE
    assert win.left_column.width() == LEFT_W and win.right.width() == RIGHT_W
    assert win.minimumWidth() == 980 and win.minimumHeight() == 600
    win.resize(980, 600)
    app.processEvents()
    assert not win.rail.wide and win.rail.width() == shell.RAIL_COMPACT
    assert win.left_column.width() == LEFT_W_NARROW and win.right.width() == RIGHT_W_NARROW
    stage = win.stage
    assert stage.width() >= 300                          # the graph keeps room
    assert win.legend.geometry().right() <= stage.width()
    assert win.hud.geometry().right() <= stage.width()
    caption = theme.pick_font()
    caption.setPixelSize(10)                              # as NavButton paints it
    for b in win.rail.buttons.values():                  # captions fit the compact rail
        assert QFontMetrics(caption).horizontalAdvance(b.ws.name) < b.width()
    win.resize(1500, 940)
    app.processEvents()
    assert win.rail.wide and win.left_column.width() == LEFT_W
    assert all(b.text().startswith("●") for b in win.filter_buttons.values())
