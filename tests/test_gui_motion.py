"""Motion system and the arc-reactor orb (offscreen; motion forced on where checked)."""
from __future__ import annotations

import os

import pytest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from test_gui_window import _offscreen_ok  # noqa: E402

if not _offscreen_ok():
    pytest.skip("PySide6 cannot start on the offscreen platform", allow_module_level=True)

import time  # noqa: E402

from PySide6.QtCore import QAbstractAnimation  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QWidget  # noqa: E402

from jarvis import cards, db  # noqa: E402
from jarvis.gui import motion, orb, theme  # noqa: E402
from jarvis.gui.app import make_app  # noqa: E402
from jarvis.gui.window import MainWindow  # noqa: E402
from test_gui_data import seed  # noqa: E402
from test_gui_window import _cfg  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return make_app([], scheme="light")


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


def _pump(n: int = 3) -> None:
    for _ in range(n):
        QApplication.instance().processEvents()


def test_env_turns_motion_off(monkeypatch):
    monkeypatch.setenv("JARVIS_REDUCE_MOTION", "1")
    assert not motion.enabled()
    monkeypatch.setenv("JARVIS_REDUCE_MOTION", "0")
    assert motion.enabled()
    motion.set_enabled(False)
    try:
        assert not motion.enabled()  # the explicit switch (--no-animations) wins
    finally:
        motion.set_enabled(None)


def test_no_animations_flag(monkeypatch):
    from jarvis.gui import app as app_mod

    seen = {}
    monkeypatch.setattr(app_mod, "make_app", lambda **kw: seen.setdefault("app", _FakeApp()))
    monkeypatch.setattr("jarvis.gui.window.MainWindow", _FakeWin)
    try:
        assert app_mod.main(["--no-animations", "--no-refresh"]) == 0
        assert not motion.enabled()
    finally:
        motion.set_enabled(None)


class _FakeApp:
    def exec(self):
        return 0


class _FakeWin:
    def __init__(self, *a, **kw):
        pass

    def show(self):
        pass


@pytest.mark.parametrize("value, mid, final", [
    ("10 days", 5.0, "5 days"),
    ("0.9/day", 0.45, "0.5/day"),  # keeps one decimal
    ("3 / 9", 1.0, "1 / 9"),
    ("12", 6.0, "6"),
])
def test_count_formats(value, mid, final):
    fmt = motion.number_formatter(value)
    assert fmt is not None
    assert fmt(mid) == final
    assert fmt(motion.split_number(value)[1]) == value


def test_non_numbers_are_not_counted():
    for v in ("overdue", "today", "–", "none", ""):
        assert motion.split_number(v) is None and motion.number_formatter(v) is None


def test_easing_helpers():
    assert motion.ease_out(0) == 0 and motion.ease_out(1) == 1
    assert motion.ease_out(0.5) > 0.5  # decelerating
    assert motion.phase(0.5, 0.0, 1.0) == 0.5 and motion.phase(2, 0, 1) == 1.0
    # staggered items: the first starts at 0, the last ends exactly at p == 1
    assert motion.stagger(0.0, 0, 5) == 0.0 and motion.stagger(1.0, 4, 5) == 1.0
    assert motion.stagger(0.3, 0, 5) > motion.stagger(0.3, 4, 5)
    assert motion.pulse(0) == 0 and abs(motion.pulse(0.5) - 1) < 1e-9


# ---------------------------------------------------------------- motion off


def test_motion_off_jumps_to_end_states(motion_off):
    before = motion.created_count()
    lb = QLabel("x")
    assert motion.count_up(lb, 0, 7, "{:.0f} days") is None
    assert lb.text() == "7 days"
    assert motion.count_up_text(lb, "0.9/day") is None and lb.text() == "0.9/day"
    w = QWidget()
    assert motion.fade_in(w) is None and w.graphicsEffect() is None
    assert motion.fade_rise(w) is None
    done = []
    assert motion.collapse(w, on_done=lambda: done.append(1)) is None
    assert done == [1]  # removal happens right away
    from jarvis.gui.brain import PulseBars

    ch = PulseBars(theme.DARK)
    ch.set_data((1, 2, 3), animate=True)
    assert ch.progress == 1.0
    assert motion.created_count() == before and motion.live() == []


# ---------------------------------------------------------------- motion on


def test_count_up_runs_and_ends_on_exact_text(motion_on):
    lb = QLabel()
    a = motion.count_up_text(lb, "10 days", duration=400)
    assert a is not None and lb.text() == "0 days"
    _pump()
    assert a.state() == QAbstractAnimation.State.Running
    a.setCurrentTime(200)
    mid = int(lb.text().split()[0])
    assert 0 < mid < 10
    motion.finish_all()
    assert lb.text() == "10 days" and a not in motion.live()


def test_count_up_really_runs_in_the_event_loop(motion_on):
    from PySide6.QtTest import QTest

    lb = QLabel()
    motion.count_up(lb, 0, 3, "{:.0f}", duration=60)
    QTest.qWait(250)
    assert lb.text() == "3" and not motion.live()


def test_fade_rise_ends_opaque_in_place(motion_on):
    parent = QWidget()
    child = QLabel("hello", parent)
    child.move(10, 20)
    parent.show()
    a = motion.fade_rise(child, dy=8, duration=200)
    eff = child.graphicsEffect()
    assert eff is not None and eff.opacity() == 0.0  # start state set at once
    _pump()
    a.setCurrentTime(100)
    assert 0 < eff.opacity() < 1 and child.y() > 20  # still rising
    motion.finish_all()
    assert child.graphicsEffect() is None  # effect removed: plain rendering again
    assert child.pos().y() == 20
    parent.close()


def test_pulse_progress_animates_and_changes_the_paint(motion_on):
    from jarvis.gui.brain import PulseBars

    ch = PulseBars(theme.DARK)
    ch.resize(260, 46)
    ch.set_data((0, 1, 3, 0, 2, 5, 1), animate=True)
    assert ch.progress == 0.0
    ch.show()
    _pump()
    ch.progress = 0.5
    half = ch.grab().toImage()
    ch.progress = 1.0
    full = ch.grab().toImage()
    assert half != full
    ch.progress = 0.0
    motion.finish_all()
    assert ch.progress == 1.0
    assert ch.grab().toImage() == full
    ch.close()


def test_graph_paints_mid_reveal(app, tmp_path, motion_off):
    cfg = _cfg(tmp_path)
    conn = db.connect(cfg.db_path)
    seed(conn)
    win = MainWindow(cfg, conn, auto_refresh=False, watch=False)
    win.show()
    _pump()
    g = win.graph
    full = g.grab().toImage()
    for p in (0.0, 0.3, 0.6):
        g.progress = p
        assert not g.grab().isNull()
    g.progress = 0.3
    assert g.grab().toImage() != full
    g.progress = 1.0
    assert g.grab().toImage() == full
    win.close()
    conn.close()


# ---------------------------------------------------------------- window integration


def _window(tmp_path, n_cards=2):
    cfg = _cfg(tmp_path)
    conn = db.connect(cfg.db_path)
    ids = seed(conn, n_cards=n_cards)
    win = MainWindow(cfg, conn, auto_refresh=False, watch=False, scheme="light")
    win.resize(1400, 920)
    win.show()
    _pump()
    return win, conn, ids


def test_animations_on_first_show_and_data_change_only(motion_on, tmp_path):
    before = motion.created_count()
    win, conn, ids = _window(tmp_path)
    assert motion.created_count() > before  # first show animates
    assert win.graph.progress < 1.0
    motion.finish_all()
    assert win.graph.progress == 1.0 and win.pulse.bars.progress == 1.0
    assert all(r.graphicsEffect() is None for r in win.hud.rows)

    n = motion.created_count()
    win.reload()  # the 30 s poll, nothing changed
    win.reload(force=False)
    assert motion.created_count() == n and motion.live() == []

    # new data: only the changed part animates
    db.insert_briefing(conn, "h3", "A new headline", "[]", "model", "2026-10-08T17:00:00+00:00",
                       "gemma4:26b")
    win.reload()
    assert motion.created_count() > n
    assert win.graph.progress == 1.0  # the graph did not re-reveal
    motion.finish_all()
    assert win.hud.headline.text() == "A new headline"
    win.close()
    conn.close()


def test_graph_timer_runs_only_while_visible(motion_on, tmp_path):
    win, conn, ids = _window(tmp_path)
    assert win.graph.running
    win.hide()
    assert not win.graph.running
    win.show()
    _pump()
    assert win.graph.running
    win.close()
    conn.close()


def test_new_node_grows_in(motion_on, tmp_path):
    win, conn, ids = _window(tmp_path)
    motion.finish_all()
    cards.upsert_card(conn, type_="research", title="New paper", why="w", source_ref="x",
                      source_url=None, claude_prompt="p", paper_id="2410.99999")
    win.reload()
    new = [n.id for n in win.graph.graph.nodes if n.kind == "card" and n.id not in
           {f"card:{i}" for i in ids}]
    assert new and new[0] in win.graph._born
    win.close()
    conn.close()


def test_watch_dot_breathes_only_when_watching_and_visible(motion_on, tmp_path):
    from datetime import UTC, datetime, timedelta

    win, conn, _ = _window(tmp_path)
    tick = (datetime.now(UTC) - timedelta(minutes=1)).isoformat(timespec="seconds")
    db.update_daemon_state(conn, pid=1234, interval_s=180, last_tick_at=tick)
    win.reload()
    dot = win.watch_dot
    assert dot.breathing and dot.running
    win.hide()
    assert not dot.running  # no timer while the window is hidden
    win.show()
    _pump()
    assert dot.running
    db.update_daemon_state(conn, pid=None)
    win.reload()
    assert not dot.breathing and not dot.running
    win.close()
    conn.close()


def test_refresh_icon_spins_only_while_refreshing(motion_on, tmp_path):
    import threading

    win, conn, _ = _window(tmp_path)
    gate = threading.Event()
    win.refresh_fn = lambda c, cfg: gate.wait(5)
    assert not win._spin_timer.isActive()
    win.refresh()
    assert win._spin_timer.isActive()
    win.hide()
    assert not win._spin_timer.isActive()
    win.show()
    assert win._spin_timer.isActive()
    gate.set()
    win.wait_for_refresh()
    assert not win._spin_timer.isActive()
    win.close()
    conn.close()


def test_chat_has_orb_and_thinking_orb(app, tmp_path, motion_off):
    import threading

    cfg = _cfg(tmp_path)
    conn = db.connect(cfg.db_path)
    gate = threading.Event()
    from test_gui_window import _answer

    def slow(*_a):
        gate.wait(5)
        return _answer()

    win = MainWindow(cfg, conn, auto_refresh=False, watch=False, ask_fn=slow)
    win.show()
    chat = win.chat
    assert isinstance(chat.orb, orb.Orb) and chat.orb.state == "idle"
    chat.ask("hi")
    bot = chat.messages[-1]
    assert bot.is_thinking and bot.thinking_orb.state == "thinking"
    assert "thinking" in bot.thinking_text.text()
    gate.set()
    chat.wait()
    assert not bot.is_thinking and not bot.thinking_orb.running
    win.close()
    conn.close()


# ---------------------------------------------------------------- orb


def test_orb_params_per_state():
    idle = orb.params_for("idle")
    thinking = orb.params_for("thinking")
    assert idle.fps == orb.IDLE_FPS and thinking.fps == orb.ACTIVE_FPS
    assert abs(thinking.spin_outer) > 5 * abs(idle.spin_outer)
    assert thinking.spin_inner < 0 < thinking.spin_outer  # counter-rotating
    # listening: the rings move outwards with the mic level
    quiet, loud = orb.params_for("listening", 0.0), orb.params_for("listening", 1.0)
    assert all(b > a for a, b in zip(quiet.rings, loud.rings, strict=True))
    # speaking: the glow and the core pulse with the voice level
    s0, s1 = orb.params_for("speaking", 0.0), orb.params_for("speaking", 1.0)
    assert s1.glow > s0.glow and s1.core > s0.core
    assert s1.glow <= 1.0
    # error: a brief amber flash, then nothing
    assert orb.params_for("error", error_elapsed_ms=150).warm > 0.5
    assert orb.params_for("error", error_elapsed_ms=orb.ERROR_MS + 1).warm == 0.0
    assert orb.error_warmth(0) == 0.0 and orb.error_warmth(orb.ERROR_MS) == 0.0


def test_level_smoothing_attack_faster_than_decay():
    up = orb.smooth_level(0.0, 1.0, 0.05)
    down = 1.0 - orb.smooth_level(1.0, 0.0, 0.05)
    assert 0 < down < up < 1
    assert orb.smooth_level(0.3, 5.0, 10.0) == pytest.approx(1.0)  # clamped target
    assert orb.smooth_level(0.3, 0.8, 0.0) == 0.3


def test_orb_api_and_static_when_motion_off(app, motion_off):
    o = orb.Orb(theme.LIGHT)
    o.show()
    assert not o.running  # no timer at all with motion off
    o.set_state("speaking")
    o.set_level(0.7)
    assert o.level == pytest.approx(0.7) and not o.running
    with pytest.raises(ValueError):
        o.set_state("dancing")
    o.set_expanded(True)
    assert o.expanded and o.width() == orb.EXPANDED
    o.set_expanded(False)
    assert o.width() == orb.COMPACT
    o.close()


def test_orb_timer_runs_only_when_visible(app, motion_on):
    holder = QWidget()
    o = orb.Orb(theme.DARK, parent=holder)
    assert not o.running  # not shown yet
    holder.show()
    _pump()
    assert o.running and o._timer.interval() == round(1000 / orb.IDLE_FPS)
    o.set_state("thinking")
    assert o._timer.interval() == round(1000 / orb.ACTIVE_FPS)
    holder.hide()
    assert not o.running
    holder.show()
    _pump()
    assert o.running
    holder.showMinimized()
    _pump()
    o._tick()  # a tick on a minimised window stops the timer
    holder.close()
    assert not o.running


def test_orb_animates_size_and_frames_differ(app, motion_on):
    holder = QWidget()
    o = orb.Orb(theme.LIGHT, parent=holder)
    holder.resize(200, 200)
    holder.show()
    _pump()
    o.set_state("thinking")
    a = o.grab().toImage()
    o.advance(0.25)
    assert o.grab().toImage() != a  # the arcs turned
    o.set_expanded(True)
    _pump()
    motion.finish_all()
    assert o.width() == orb.EXPANDED
    o.set_state("error")
    assert o.params().warm > 0
    t0 = time.monotonic()
    while o.state == "error" and time.monotonic() - t0 < 3:
        _pump()
    assert o.state == "idle"  # the flash ends by itself
    holder.close()


def test_orb_paints_every_state_in_both_themes(app, motion_off):
    for tk in (theme.LIGHT, theme.DARK):
        for size in (orb.COMPACT, orb.EXPANDED):
            for st in orb.STATES:
                o = orb.Orb(tk, size=size, state=st)
                o.set_level(0.5)
                assert not o.grab().isNull()


def test_chat_orb_moves_to_stage_and_back(app, tmp_path, motion_on):
    cfg = _cfg(tmp_path)
    conn = db.connect(cfg.db_path)
    win = MainWindow(cfg, conn, auto_refresh=False, watch=False, chat_open=True)
    win.show()
    _pump()
    chat = win.chat
    assert chat.orb.parentWidget() is not chat.stage and not chat.stage.isVisible()
    chat.set_orb_staged(True)
    assert chat.orb_staged and chat.stage.isVisible()
    assert chat.orb.parentWidget() is chat.stage
    _pump()
    motion.finish_all()
    assert chat.orb.width() == orb.EXPANDED and chat.orb.expanded
    chat.set_orb_staged(False)
    assert chat.orb.parentWidget() is chat.stage  # still shrinking on the stage
    motion.finish_all()
    assert chat.orb.width() == orb.COMPACT
    assert chat.orb.parentWidget() is not chat.stage and not chat.stage.isVisible()
    win.close()
    conn.close()
