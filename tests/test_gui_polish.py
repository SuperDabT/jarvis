"""Visual polish: the type scale, hover / expand / selection transitions, and that
reduced motion turns them into instant changes. Offscreen, scratch data."""
from __future__ import annotations

import os

import pytest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from test_gui_window import _offscreen_ok  # noqa: E402

if not _offscreen_ok():
    pytest.skip("PySide6 cannot start on the offscreen platform", allow_module_level=True)

from PySide6.QtCore import QEvent, QPointF  # noqa: E402
from PySide6.QtGui import QEnterEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QGraphicsOpacityEffect, QLabel, QWidget  # noqa: E402
from test_gui_dashboard import _window  # noqa: E402

from jarvis.gui import motion, theme  # noqa: E402
from jarvis.gui.app import make_app  # noqa: E402
from jarvis.gui.widgets import HoverRow  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return make_app([], scheme="dark")


@pytest.fixture
def motion_on(app):
    motion.set_enabled(True)
    yield
    motion.finish_all()
    motion.set_enabled(None)


@pytest.fixture
def motion_off(app):
    motion.set_enabled(False)
    yield
    motion.set_enabled(None)


@pytest.fixture
def win(app, tmp_path):
    w, conn, _ = _window(tmp_path)
    yield w
    w.close()
    conn.close()


def enter(w):
    QApplication.sendEvent(w, QEnterEvent(QPointF(2, 2), QPointF(2, 2), QPointF(2, 2)))


def leave(w):
    QApplication.sendEvent(w, QEvent(QEvent.Type.Leave))


# ---------------------------------------------------------------- type


def test_durations_stay_in_the_small_interaction_range():
    assert 100 <= motion.HOVER <= 180 and 150 <= motion.EXPAND <= 220
    assert 150 <= motion.SELECT <= 220


def test_type_scale_sans_for_ui_mono_for_technical(win):
    sans, mono = theme.family("sans"), theme.family("mono")
    assert win.vitals.head.font().family() == sans and win.vitals.head.property("type") == "head"
    assert win.vitals.values["risk"].font().family() == sans
    assert win.vitals.subs["risk"].font().pixelSize() == theme.TYPE["meta"][1]
    row = win.issues.rows[0]
    num = next(lb for lb in row.findChildren(QLabel) if lb.objectName() == "issueNum")
    assert num.font().family() == mono                      # "#12" is an id
    assert row.title_label.font().family() == sans
    assert all(px >= 10 for _, px, _ in theme.TYPE.values())   # nothing tiny


# ---------------------------------------------------------------- hover


def test_hover_row_fades_with_motion(motion_on):
    r = HoverRow()
    r.resize(200, 30)
    r.show()
    before = motion.created_count()
    enter(r)
    assert motion.created_count() == before + 1 and r.hover < 1.0   # fading in, not snapped
    motion.finish_all()
    assert r.hover == 1.0
    leave(r)
    motion.finish_all()
    assert r.hover == 0.0
    r.set_selected(True)
    assert r.selected and not r.grab().isNull()


def test_hover_row_is_instant_with_reduced_motion(motion_off):
    r = HoverRow()
    r.show()
    before = motion.created_count()
    enter(r)
    assert r.hover == 1.0 and motion.created_count() == before
    leave(r)
    assert r.hover == 0.0


# ---------------------------------------------------------------- expand / collapse


def test_reveal_animates_then_settles(motion_on):
    holder = QWidget()
    w = QLabel("a\nb\nc", holder)
    w.hide()
    motion.reveal(w, True)
    assert not w.isHidden() and w.maximumHeight() < w.sizeHint().height()   # opening
    motion.finish_all()
    assert not w.isHidden() and w.maximumHeight() == motion.QWIDGETSIZE_MAX
    motion.reveal(w, False)
    motion.finish_all()
    assert w.isHidden() and w.maximumHeight() == motion.QWIDGETSIZE_MAX


def test_reveal_is_instant_with_reduced_motion(motion_off):
    w = QLabel("x")
    w.hide()
    assert motion.reveal(w, True) is None and not w.isHidden()
    motion.reveal(w, False)
    assert w.isHidden()


def test_milestones_expand_and_new_active_settles_in(win, motion_on):
    m = win.milestones
    m.toggle_btn.click()
    assert m.expanded and not m.others.isHidden()
    motion.finish_all()
    assert m.toggle_btn.text().startswith("▾")
    before = motion.created_count()
    win.pick_milestone(2)
    assert motion.created_count() > before                 # the new active block fades in
    motion.finish_all()
    assert win.dash.active.title == "Field test" and m.others.isHidden()


def test_inspector_details_open_smoothly(win, motion_on):
    win.graph.lock("ms:1")                    # Core demo: more links than the summary shows
    QApplication.processEvents()
    insp = win.inspector
    assert insp.details_btn is not None and insp.details.isHidden()
    insp.details_btn.click()
    assert not insp.details.isHidden()
    motion.finish_all()
    assert insp.details_btn.text() == "details ▾"


# ---------------------------------------------------------------- selection


def test_selecting_fades_the_inspector_but_hovering_does_not(win, motion_on):
    insp = win.inspector
    body = insp.scroll.widget()
    win._inspect("issue:7", locked=False)                    # hover
    assert not isinstance(body.graphicsEffect(), QGraphicsOpacityEffect)
    before = motion.created_count()
    win.graph.lock("issue:7")
    assert motion.created_count() > before                   # a new selection settles in
    motion.finish_all()
    before = motion.created_count()
    win._inspect("issue:7", locked=True)                     # same selection, refreshed
    assert motion.created_count() == before


def test_graph_lock_marks_the_issue_row_and_the_ring(win):
    t0 = win.graph._lock_t
    win.graph.lock("issue:12")
    assert win.graph._lock_t > t0
    assert [r.selected for r in win.issues.rows] == [r.row.number == 12 for r in win.issues.rows]
    t1 = win.graph._lock_t
    win.graph.lock("issue:12")                               # same node: no new ring
    assert win.graph._lock_t == t1
    win.graph.lock(None)
    assert not any(r.selected for r in win.issues.rows)
    assert not win.graph.grab().isNull()


def test_reduced_motion_selection_is_instant(win, motion_off):
    before = motion.created_count()
    win.graph.lock("issue:7")
    win.milestones.toggle_btn.click()
    win.pick_milestone(2)
    assert motion.created_count() == before
    assert not isinstance(win.inspector.scroll.widget().graphicsEffect(),
                          QGraphicsOpacityEffect)
