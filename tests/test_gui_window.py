"""Qt smoke test for the window, rendered offscreen."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

# Always offscreen: a desktop session sets QT_QPA_PLATFORM=wayland, and the tests must
# neither open real windows nor touch the real clipboard.
os.environ["QT_QPA_PLATFORM"] = "offscreen"


def _offscreen_ok() -> bool:
    """Probe in a subprocess: a failing Qt platform plugin aborts the process."""
    code = "from PySide6.QtWidgets import QApplication; QApplication([])"
    try:
        r = subprocess.run([sys.executable, "-c", code], env={**os.environ},
                           capture_output=True, timeout=60)
    except Exception:  # noqa: BLE001
        return False
    return r.returncode == 0


if not _offscreen_ok():
    pytest.skip("PySide6 cannot start on the offscreen platform", allow_module_level=True)

import threading  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from jarvis import cards, db  # noqa: E402
from jarvis.config import Config  # noqa: E402
from jarvis.gui.app import make_app  # noqa: E402
from jarvis.gui.window import EMPTY_CARDS, MainWindow  # noqa: E402
from test_gui_data import HW_YAML, REPO, seed  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return make_app([], scheme="light")


def _cfg(tmp_path, hardware: bool = True) -> Config:
    hw = tmp_path / "hardware.yaml"
    if hardware:
        hw.write_text(HW_YAML)
    return Config(repo=REPO, db_path=tmp_path / "jarvis.db", hardware_path=hw)


@pytest.fixture
def setup(app, tmp_path):
    cfg = _cfg(tmp_path)
    conn = db.connect(cfg.db_path)
    ids = seed(conn, n_cards=2)
    win = MainWindow(cfg, conn, auto_refresh=False, watch=False, scheme="light")
    win.resize(1400, 920)
    win.show()
    app.processEvents()
    yield win, conn, ids
    win.close()
    conn.close()


def test_window_shows_dashboard(setup):
    win, conn, ids = setup
    assert win.briefing_view.headline.text() == "Core demo due Oct 18, 0 of 9 closed"
    assert [r.sentence.uncertain for r in win.briefing_view.rows] == [False, True]
    assert "unconfirmed" in win.briefing_view.rows[1].text_label.text()
    assert "unconfirmed" not in win.briefing_view.rows[0].text_label.text()
    assert len(win.kpi_row.tiles) == 4
    assert win.timeline.data is not None and len(win.timeline.data.bars) == 1
    assert win.grid_chart.data is not None and "commits in the last 6 months" in \
        win.grid_caption.text()
    assert win.phase_chart.data is not None
    assert len(win.budget_charts) == 2 and len(win.window_charts) == 2
    assert set(win.card_widgets) == set(ids)
    assert win.suggestions_head.head.text() == "SUGGESTIONS  ·  2"
    assert "briefing by gemma4:26b" in win.meta_label.text()
    assert win.chat.isVisible()
    for w in (win.timeline, win.grid_chart, win.phase_chart, *win.budget_charts,
              *win.window_charts):
        assert not w.grab().isNull(), type(w).__name__
        assert w.hits or w is win.phase_chart, type(w).__name__  # tooltips (no issues seeded)


def test_window_without_hardware_says_why(app, tmp_path):
    cfg = _cfg(tmp_path, hardware=False)
    conn = db.connect(cfg.db_path)
    win = MainWindow(cfg, conn, auto_refresh=False, watch=False)
    assert win.budget_charts == []
    assert "No hardware.yaml" in win.power_widgets[0].text()
    assert win.empty_label.text() == EMPTY_CARDS
    win.close()
    conn.close()


def _click(widget):
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton)
    QApplication.instance().processEvents()


def test_row_click_opens_peek_and_esc_closes_it(setup):
    win, conn, ids = setup
    row = win.card_widgets[ids[1]]
    _click(row)
    assert win.peek.isVisible() and win.peek.row.id == ids[1]
    assert win.peek.title.text() == row.card.title
    assert win.peek.prompt.toPlainText() == row.card.claude_prompt
    assert row.property("selected") == "true"
    from PySide6.QtGui import QShortcut

    esc = next(s for s in win.findChildren(QShortcut) if s.key().toString() == "Esc")
    esc.activated.emit()
    QApplication.instance().processEvents()
    assert not win.peek.isVisible()
    assert win.isVisible()  # Esc never closes the window
    assert row.property("selected") == "false"
    esc.activated.emit()  # nothing open: Esc does nothing
    assert win.isVisible()


def test_done_in_peek_writes_db_and_removes_row(setup, app):
    win, conn, ids = setup
    _click(win.card_widgets[ids[0]])
    win.peek.done_btn.click()
    app.processEvents()
    assert cards.get_card(conn, ids[0]).status == "done"
    assert ids[0] not in win.card_widgets
    assert not win.peek.isVisible()
    assert "Marked done" in win.status_label.text()
    win.reload()  # a later DB-driven reload agrees
    assert set(win.card_widgets) == {ids[1]}


def test_not_useful_records_reason(setup, app):
    win, conn, ids = setup
    _click(win.card_widgets[ids[1]])
    win.peek.feedback_actions["vague"].trigger()
    app.processEvents()
    c = cards.get_card(conn, ids[1])
    assert (c.status, c.feedback) == ("done", "vague")
    assert ids[1] not in win.card_widgets


def test_last_card_gone_shows_empty_state(setup, app):
    win, conn, ids = setup
    for cid in ids:
        win.open_peek(win.card_widgets[cid].row)
        win.peek.done_btn.click()
    app.processEvents()
    assert win.card_widgets == {}
    assert win.empty_label.text() == EMPTY_CARDS


def test_copy_buttons_set_clipboard(setup, app):
    win, conn, ids = setup
    w = win.card_widgets[ids[0]]
    w.copy_btn.click()  # the row's own button copies without opening the peek
    app.processEvents()
    assert QGuiApplication.clipboard().text() == w.card.claude_prompt
    assert w.copy_btn.text() == "Copied" and not win.peek.isVisible()
    w._unflash()
    assert w.copy_btn.text() == "Copy"
    QGuiApplication.clipboard().setText("")
    win.open_peek(win.card_widgets[ids[1]].row)
    win.peek.copy_btn.click()
    assert QGuiApplication.clipboard().text() == win.card_widgets[ids[1]].card.claude_prompt
    assert "Copied" in win.peek.copy_btn.text()
    assert "Copied the prompt" in win.status_label.text()


# ---------------------------------------------------------------- chat


def _answer(text="Core demo has 9 open issues.", **kw):
    from jarvis.chat import Answer, Source

    base = dict(text=text, sources=[Source("#7", "#7 Create track.c", f"https://github.com/{REPO}/issues/7"),
                                    Source("commit:02fb2d2abc", "commit 02fb2d2", None)],
                unverified_numbers=[], notes=["Note: 12 is not in the synced data."],
                model="gemma4:26b", duration=12.0)
    base.update(kw)
    return Answer(**base)


def test_chat_streams_tokens_then_shows_answer_and_sources(app, tmp_path):
    cfg = _cfg(tmp_path)
    conn = db.connect(cfg.db_path)
    gate = threading.Event()
    calls = []

    def fake_ask(c, cfg_, question, history, on_token, session_id):
        calls.append((question, list(history), session_id, c is not conn))
        on_token("Core demo has ")
        on_token("9 open")
        gate.wait(5)
        return _answer()

    win = MainWindow(cfg, conn, auto_refresh=False, watch=False, ask_fn=fake_ask)
    chat = win.chat
    assert chat.empty.isVisibleTo(chat) and len(chat.suggestion_buttons) == 3
    chat.composer.edit.setPlainText("What's left for Core demo?")
    QTest.keyClick(chat.composer.edit, Qt.Key.Key_Return)  # Enter sends
    assert chat.busy and not chat.composer.send_btn.isEnabled()
    assert not chat.empty.isVisibleTo(chat)
    chat.ask("a second question while busy")  # ignored: one at a time
    for _ in range(50):
        app.processEvents()
        if chat.messages[-1].streamed:
            break
        threading.Event().wait(0.01)
    bot = chat.messages[-1]
    assert bot.streamed == "Core demo has 9 open"
    assert bot.body.text().startswith("Core demo has 9 open")
    gate.set()
    chat.wait()
    assert not chat.busy and len(chat.messages) == 2
    assert calls[0][0] == "What's left for Core demo?" and calls[0][1] == [] and calls[0][3]
    assert "Core demo has 9 open issues." in bot.body.text()
    assert [b.text() for b in bot.chip_buttons] == ["#7", "02fb2d2"]
    assert "12 is not in the synced data" in bot.notes.text()
    assert "gemma4:26b" in bot.meta.text()
    # the next question carries the conversation and the same session
    gate.set()
    chat.ask("And v1.0?")
    chat.wait()
    assert calls[1][1] == [("user", "What's left for Core demo?"),
                           ("assistant", "Core demo has 9 open issues.")]
    assert calls[1][2] == calls[0][2]
    win.close()
    conn.close()


def test_chat_errors_show_inline(app, tmp_path):
    cfg = _cfg(tmp_path)
    conn = db.connect(cfg.db_path)

    def broken(*_a):
        raise RuntimeError("socket closed")

    win = MainWindow(cfg, conn, auto_refresh=False, watch=False, ask_fn=broken)
    win.chat.suggestion_buttons[1].click()  # a suggested question
    win.chat.wait()
    bot = win.chat.messages[-1]
    assert win.chat.messages[0].text == "Is my 5V rail OK?"
    assert "socket closed" in bot.body.text() and bot.body.objectName() == "botError"
    assert not win.chat.busy and win.chat.history == []
    # a model failure reported by ask() (no exception) is muted too
    win.chat.ask_fn = lambda *a: _answer(text="The local model isn't reachable.", sources=[],
                                         notes=[], error="cannot reach")
    win.chat.ask("hello")
    win.chat.wait()
    assert win.chat.messages[-1].body.objectName() == "botError"
    win.close()
    conn.close()


def test_chat_toggle_and_shortcuts(setup, app):
    win, _, _ = setup
    from PySide6.QtGui import QShortcut

    keys = {s.key().toString() for s in win.findChildren(QShortcut)}
    assert {"Ctrl+R", "F5", "Ctrl+Q", "Esc", "Ctrl+K", "Ctrl+J"} <= keys
    assert QApplication.instance().desktopFileName() == "jarvis"
    assert Qt.ScrollBarPolicy.ScrollBarAlwaysOff == win.scroll.horizontalScrollBarPolicy()
    win.chat_btn.click()
    assert not win.chat.isVisible()
    win.focus_chat()  # Ctrl+K opens it again
    assert win.chat.isVisible()


def test_real_ask_is_wired_with_session(app, tmp_path, monkeypatch):
    """The default ask_fn calls jarvis.chat.ask with on_token and the session id."""
    from jarvis.gui import chat_panel

    seen = {}

    def fake(conn, cfg, question, history, client, on_token=None, now=None, *,
             session_id=None, use_embeddings=False):
        seen.update(question=question, session=session_id, client=client)
        on_token("hi")
        return _answer(text="hi")

    monkeypatch.setattr("jarvis.chat.ask", fake)
    out = chat_panel.default_ask(None, _cfg(tmp_path), "q?", [], lambda t: None, "window-1")
    assert out.text == "hi" and seen == {"question": "q?", "session": "window-1", "client": None}


# ---------------------------------------------------------------- refresh


def test_refresh_runs_in_background_and_reloads(app, tmp_path):
    cfg = _cfg(tmp_path)
    conn = db.connect(cfg.db_path)
    calls = []

    def fake_refresh(worker_conn, cfg_):
        calls.append(worker_conn is not conn)
        seed(worker_conn, n_cards=1)  # written from the worker's own connection
        return SimpleNamespace(sync={"issues": "changed"},
                               briefing=SimpleNamespace(fallback_reason="model unavailable: x"),
                               cards_new=1, cards_resolved=0, notes=[])

    win = MainWindow(cfg, conn, auto_refresh=False, watch=False, refresh_fn=fake_refresh)
    assert win.briefing_view.data is None and win.card_widgets == {}
    win.refresh()
    assert win.refreshing and not win.refresh_btn.isEnabled()
    win.wait_for_refresh()
    assert calls == [True]
    assert not win.refreshing and win.refresh_btn.isEnabled()
    assert win.briefing_view.data is not None
    assert len(win.card_widgets) == 1
    assert win.status_label.text() == "Ollama unavailable, showing template briefing"
    win.close()
    conn.close()


def test_refresh_error_goes_to_status_line(app, tmp_path):
    cfg = _cfg(tmp_path)
    conn = db.connect(cfg.db_path)

    def broken(_conn, _cfg):
        raise RuntimeError("GitHub exploded")

    win = MainWindow(cfg, conn, auto_refresh=False, watch=False, refresh_fn=broken)
    win.refresh()
    win.wait_for_refresh()
    assert "GitHub exploded" in win.status_label.text()
    assert win.refresh_btn.isEnabled()
    win.close()
    conn.close()


def test_refresh_while_daemon_holds_lock_reports_busy(app, tmp_path):
    from jarvis.gui.window import BUSY_TEXT
    from jarvis.lock import refresh_lock

    cfg = _cfg(tmp_path)
    conn = db.connect(cfg.db_path)
    calls = []
    win = MainWindow(cfg, conn, auto_refresh=False, watch=False,
                     refresh_fn=lambda c, _cfg: calls.append(1))
    with refresh_lock(cfg.db_path):  # e.g. `jarvis watch` mid-tick
        win.refresh()
        win.wait_for_refresh()
    assert calls == []  # never ran a second refresh
    assert win.status_label.text() == BUSY_TEXT
    assert win.refresh_btn.isEnabled() and not win.refreshing
    win.refresh()  # lock free again: runs
    win.wait_for_refresh()
    assert calls == [1]
    win.close()
    conn.close()


def test_header_shows_watch_heartbeat(app, tmp_path):
    from datetime import UTC, datetime, timedelta

    cfg = _cfg(tmp_path)
    conn = db.connect(cfg.db_path)
    win = MainWindow(cfg, conn, auto_refresh=False, watch=False)
    assert not win.watch_label.isVisibleTo(win)  # never watched: nothing shown
    tick_at = (datetime.now(UTC) - timedelta(minutes=2)).isoformat(timespec="seconds")
    db.update_daemon_state(conn, pid=1234, interval_s=180, last_tick_at=tick_at)
    win.reload()
    assert win.watch_label.text() == "watching · checked 2 min ago"
    assert win.watch_label.isVisibleTo(win) and win.watch_dot.isVisibleTo(win)
    db.update_daemon_state(conn, pid=None)  # daemon stopped
    win.reload()
    assert win.watch_label.text() == "not watching"
    win.close()
    conn.close()


def test_dark_theme_switch_rebuilds(setup, app):
    win, _, ids = setup
    win.apply_scheme("dark")
    assert win.tokens.name == "dark" and win.timeline.tokens.name == "dark"
    assert set(win.card_widgets) == set(ids)
    win.apply_scheme("light")
