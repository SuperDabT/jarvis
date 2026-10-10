"""Startup entrance and panel transitions: they play once / when asked, never
block input, settle into the right final state when interrupted, and are
instant with reduced motion. Offscreen, scratch data."""
from __future__ import annotations

import os

import pytest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from test_gui_window import _offscreen_ok  # noqa: E402

if not _offscreen_ok():
    pytest.skip("PySide6 cannot start on the offscreen platform", allow_module_level=True)

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QGraphicsOpacityEffect  # noqa: E402
from test_gui_data import seed  # noqa: E402
from test_gui_think import _settings  # noqa: E402
from test_gui_window import _cfg  # noqa: E402

from jarvis import db  # noqa: E402
from jarvis.gui import motion  # noqa: E402
from jarvis.gui.app import make_app  # noqa: E402
from jarvis.gui.window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return make_app([], scheme="dark")


@pytest.fixture
def build(app, tmp_path):
    made = []

    def make(motion_on: bool):
        motion.set_enabled(motion_on)
        cfg = _cfg(tmp_path)
        conn = db.connect(cfg.db_path)
        seed(conn, n_cards=2)
        w = MainWindow(cfg, conn, auto_refresh=False, watch=False, start="projects",
                       voice={"settings": _settings(tmp_path), "mic_ok": False})
        w.resize(1500, 940)
        made.append((w, conn))
        return w

    yield make
    motion.finish_all()
    motion.set_enabled(None)
    for w, c in made:
        w.close()
        c.close()


def effects(*ws) -> list:
    return [w for w in ws if isinstance(w.graphicsEffect(), QGraphicsOpacityEffect)]


def test_entrance_plays_once_and_settles(build):
    win = build(True)
    before = motion.created_count()
    win.show()
    QApplication.processEvents()              # the entrance starts after the first show
    assert motion.created_count() >= before + 4
    page = win.stack.currentWidget()
    assert effects(win.topbar, win.rail, page)        # mid-entrance
    assert win.graph.graph.nodes                       # the data was there all along
    motion.finish_all()
    assert not effects(win.topbar, win.rail, page, win.ticker)
    assert win.rail.x() == 0                           # back in its layout place
    n = motion.created_count()
    win.hide()
    win.show()
    QApplication.processEvents()
    assert motion.created_count() == n                 # not again


def test_typing_works_during_the_entrance(build):
    win = build(True)
    win.show()
    win.set_chat_open(True)
    QApplication.processEvents()
    win.activateWindow()
    win.chat.focus_input()
    QTest.keyClicks(win.chat.composer.edit, "still typing")
    assert win.chat.composer.edit.toPlainText() == "still typing"   # mid-animation
    motion.finish_all()


def test_reduced_motion_has_no_entrance(build):
    win = build(False)
    before = motion.created_count()
    win.show()
    QApplication.processEvents()
    win.set_chat_open(True)
    win.set_chat_open(False)
    win.open_capture()
    win.capture.close_overlay()
    assert motion.created_count() == before
    assert win.chat.isHidden() and win.capture.isHidden()
    assert not effects(win.topbar, win.rail, win.stack.currentWidget(), win.chat)


def test_chat_closing_then_reopening_settles_open(build):
    win = build(True)
    win.show()
    QApplication.processEvents()
    motion.finish_all()
    win.set_chat_open(True)
    win.set_chat_open(False)                  # fading out...
    win.set_chat_open(True)                   # ...and back before it finished
    motion.finish_all()
    QApplication.processEvents()
    assert win.chat.isVisible() and not effects(win.chat)
    win.set_chat_open(False)
    motion.finish_all()
    assert win.chat.isHidden() and win.right.isVisible()   # inspector back once it's gone


def test_capture_fades_but_types_at_once(build):
    win = build(True)
    win.show()
    QApplication.processEvents()
    motion.finish_all()
    win.activateWindow()
    win.open_capture()
    QTest.keyClicks(win.capture.input, "buy solder")
    assert win.capture.input.text() == "buy solder"         # while it fades in
    win.capture.close_overlay()
    win.open_capture()                                       # reopened mid-fade
    motion.finish_all()
    assert win.capture.isVisible() and not effects(win.capture)
    win.capture.close_overlay()
    motion.finish_all()
    assert win.capture.isHidden()
    QTest.keyClick(win, Qt.Key.Key_Escape)


def test_toolkit_effects_follow_the_motion_setting(app):
    motion.set_enabled(False)
    make_app([])
    assert not QApplication.isEffectEnabled(Qt.UIEffect.UI_FadeMenu)
    motion.set_enabled(True)
    make_app([])
    assert QApplication.isEffectEnabled(Qt.UIEffect.UI_FadeMenu)
    motion.set_enabled(None)
