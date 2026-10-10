"""Projects as a workspace: the actionable inspector, "Ask Jarvis about this ..."
with the item's context, no thought stream, narrow windows, and the shared
interaction feedback. Offscreen, scratch data; the ask function is a fake."""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from test_gui_window import _offscreen_ok  # noqa: E402

if not _offscreen_ok():
    pytest.skip("PySide6 cannot start on the offscreen platform", allow_module_level=True)

from PySide6.QtCore import QEvent, QPointF  # noqa: E402
from PySide6.QtGui import QEnterEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from test_gui_dashboard import _window  # noqa: E402

from jarvis.gui import motion  # noqa: E402
from jarvis.gui.app import make_app  # noqa: E402
from jarvis.gui.brain_data import Node  # noqa: E402
from jarvis.gui.cockpit import Inspector, RelationRow, StreamPanel  # noqa: E402
from jarvis.gui.theme import DARK  # noqa: E402
from jarvis.gui.window import CHAT_OVERLAY_W, MIN_SIZE  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return make_app([], scheme="dark")


@pytest.fixture
def setup(app, tmp_path):
    w, conn, opened = _window(tmp_path)
    calls = []

    def ask(conn, cfg, question, history, on_token, sid, **kw):
        calls.append({"question": question, "focus": tuple(kw.get("focus", ()))})
        on_token("Looking at it. ")
        return SimpleNamespace(text="Looking at it.", sources=[], notes=[], model="m",
                               duration=0.1)

    w.chat.ask_fn = ask
    yield w, conn, calls
    w.close()
    conn.close()


def settle(win, size):
    win.resize(*size)
    for _ in range(4):
        QApplication.processEvents()


def right(w, win) -> int:
    return w.mapTo(win, w.rect().topRight()).x()


# ---------------------------------------------------------------- inspector


def test_inspector_header_action_and_priority_only_from_labels(setup):
    win, *_ = setup
    win.graph.lock("issue:12")                     # labelled "blocker"
    insp = win.inspector
    assert insp.node.id == "issue:12" and insp.priority_tag.text() == "blocker"
    assert insp.ask_btn.text() == "✦ Ask Jarvis about this issue"
    win.graph.lock("issue:31")                     # no labels: no priority shown
    assert insp.node.id == "issue:31"
    assert insp.focus.label.startswith("#31")
    assert not any(w.objectName() == "issueTag" for w in insp.scroll.widget().findChildren(
        type(insp.hint)) if w.isVisibleTo(insp))
    win.graph.lock("ms:1")
    assert insp.ask_btn.text() == "✦ Ask Jarvis about this milestone"
    win.graph.lock("jarvis")                       # the core: nothing item-specific to ask
    assert insp.ask_btn is None


def test_ask_jarvis_sends_the_selected_items_context_and_keeps_it(setup):
    win, _, calls = setup
    win.graph.lock("issue:12")
    win.inspector.ask_btn.click()
    win.chat.wait()
    assert win.chat.isVisible() and calls
    first = calls[0]
    assert first["question"].startswith("Help me with issue #12")
    assert any(line.startswith("Issue #12:") for line in first["focus"])
    assert any("Milestone: Core demo" in line for line in first["focus"])
    chip = win.chat.focus_chip
    assert not chip.isHidden() and "#12" in win.chat.focus_label.text()
    # a follow-up keeps the same context
    win.chat.ask("And what about the wiring?")
    win.chat.wait()
    assert calls[1]["focus"] == first["focus"]
    # the chip shows the item in the graph, the chat stays open
    win.graph.lock(None)
    win.chat.focus_label.click()
    assert win.graph.lock_id == "issue:12" and win.chat.isVisible()
    # cleared: questions go without it
    win.chat.focus_clear.click()
    win.chat.ask("Anything else?")
    win.chat.wait()
    assert calls[2]["focus"] == () and chip.isHidden()
    # a new conversation starts without context
    win.graph.lock("issue:12")
    win.ask_about("issue:12")
    win.chat.wait()
    win.chat.new_chat()
    assert win.chat.focus is None


def test_related_rows_say_how_certain_and_navigate(app, setup):
    win, *_ = setup
    named = RelationRow("mentions", Node("commit:02fb2d2", "commit", "02fb2d2", "Fix #12"), DARK)
    maybe = RelationRow("maybe", Node("commit:b455d42", "commit", "b455d42", "Rework"), DARK)
    assert named.note.text() == "the commit names the issue" and not named.uncertain
    assert "unconfirmed" in maybe.note.text() and maybe.uncertain
    assert maybe.note.property("uncertain") == "true"
    assert maybe.name == "b455d42 Rework"          # readable, not just the id
    win.graph.lock("issue:12")
    rows = win.inspector.related_rows
    ms = next(r for r in rows if r.other.id == "ms:1")
    assert ms.note.text() == "milestone on GitHub"
    ms.click()
    assert win.graph.lock_id == "ms:1" and win.inspector.node.id == "ms:1"


def test_inspector_switches_nodes_cleanly(setup, motion_on):
    win, *_ = setup
    insp = win.inspector
    win.graph.lock("issue:12")
    before = motion.created_count()
    win.graph.lock("issue:7")
    assert insp.node.id == "issue:7" and motion.created_count() > before
    motion.finish_all()
    assert insp.scroll.widget().graphicsEffect() is None       # settled, nothing left over
    assert len(insp.scroll.widget().findChildren(Inspector)) == 0


@pytest.fixture
def motion_on(app):
    motion.set_enabled(True)
    yield
    motion.finish_all()
    motion.set_enabled(None)


# ---------------------------------------------------------------- layout


def test_thought_stream_is_not_part_of_projects(setup):
    win, *_ = setup
    projects = win.workspaces["projects"]
    assert not projects.findChildren(StreamPanel) and not hasattr(win, "stream")
    lay = win.right.layout()
    assert [lay.itemAt(i).widget() for i in range(lay.count())] == [win.inspector]
    assert win.brain.stream                             # events are still recorded


@pytest.mark.parametrize("size", [(1500, 940), (980, 600), (800, 600)])
def test_nothing_leaves_the_window(setup, size):
    win, *_ = setup
    win.graph.lock("issue:12")
    for chat in (False, True):
        win.set_chat_open(chat)
        for ws in ("projects", "tasks", "home", "school", "knowledge"):
            win.show_workspace(ws)
            settle(win, size)
            page = win.centralWidget()
            assert page.minimumSizeHint().width() <= max(size[0], MIN_SIZE[0]), (ws, chat)
            if chat:
                assert right(win.chat, win) <= win.width(), (ws, size)
                assert win.chat.composer.send_btn.isVisible()


def test_narrow_projects_shows_one_side_column_with_a_toggle(setup):
    win, *_ = setup
    win.show_workspace("projects")
    settle(win, (1500, 940))
    assert not win.side_toggle.isVisible()
    assert win.left_column.isVisible() and win.right.isVisible()
    settle(win, (800, 600))
    assert win.side_toggle.isVisible()
    assert win.right.isVisible() and not win.left_column.isVisible()   # inspector first
    assert win.graph.width() >= 300
    win.side_buttons["dashboard"].click()
    assert win.left_column.isVisible() and not win.right.isVisible()
    win.issues.rows[0].click()                       # picking an issue shows the inspector
    assert win.right.isVisible() and win.inspector.node.id == "issue:12"


def test_narrow_chat_floats_and_keeps_the_workspace_width(setup):
    win, *_ = setup
    settle(win, (800, 600))
    stack_before = win.stack.width()
    win.set_chat_open(True)
    settle(win, (800, 600))
    assert win.chat_overlay and win.chat.width() == CHAT_OVERLAY_W
    assert win.stack.width() == stack_before          # not squeezed
    assert right(win.chat, win) <= win.width()
    settle(win, (1500, 940))
    assert not win.chat_overlay and win.chat.isVisible()
    assert right(win.stack, win) < win.chat.mapTo(win, win.chat.rect().topLeft()).x() + 1


def test_briefing_folds_on_a_narrow_graph_and_comes_back(setup):
    win, *_ = setup
    settle(win, (1500, 940))
    assert win.hud.expanded
    settle(win, (800, 600))
    assert not win.hud.expanded
    settle(win, (1500, 940))
    assert win.hud.expanded


def test_ticker_hint_shortens_with_an_ellipsis(setup):
    win, *_ = setup
    settle(win, (800, 600))
    win._fit_hint()
    assert win.hint_label.text().startswith("…") and win.hint_label.toolTip().startswith("hover")
    settle(win, (1500, 940))
    win._fit_hint()
    assert win.hint_label.text() == win.hint_label.toolTip()


# ---------------------------------------------------------------- feedback


def test_nav_buttons_fade_their_hover_and_switching_fades_in(setup, motion_on):
    win, *_ = setup
    b = win.rail.buttons["tasks"]
    QApplication.sendEvent(b, QEnterEvent(QPointF(2, 2), QPointF(2, 2), QPointF(2, 2)))
    assert 0 <= b.hover < 1.0
    motion.finish_all()
    assert b.hover == 1.0
    QApplication.sendEvent(b, QEvent(QEvent.Type.Leave))
    motion.finish_all()
    assert b.hover == 0.0
    before = motion.created_count()
    win.show_workspace("tasks")
    assert motion.created_count() > before
    motion.finish_all()
    assert win.workspaces["tasks"].graphicsEffect() is None   # settled


def test_reduced_motion_makes_it_all_instant(setup):
    motion.set_enabled(False)
    try:
        win, *_ = setup
        before = motion.created_count()
        b = win.rail.buttons["tasks"]
        QApplication.sendEvent(b, QEnterEvent(QPointF(2, 2), QPointF(2, 2), QPointF(2, 2)))
        assert b.hover == 1.0
        win.show_workspace("tasks")
        win.show_workspace("projects")
        win.graph.lock("issue:12")
        win.ask_about("issue:12")
        win.chat.wait()
        assert motion.created_count() == before
    finally:
        motion.set_enabled(None)


def test_disabled_and_focus_states_exist_in_the_shared_style(app):
    from jarvis.gui import theme

    css = theme.stylesheet(theme.DARK)
    for rule in ("QPushButton:hover", "QPushButton:pressed", "QPushButton:focus",
                 "QPushButton:disabled", "QPushButton#primary:disabled",
                 "QPushButton#primary:pressed", "QPushButton#segment:checked"):
        assert rule in css, rule
    button = css.split("QPushButton {", 1)[1].split("}", 1)[0]
    assert f'font-family: "{theme.family("sans")}"' in button      # buttons speak sans
