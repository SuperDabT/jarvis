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
from jarvis.gui.window import MainWindow, ref_to_node  # noqa: E402
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
    win = MainWindow(cfg, conn, auto_refresh=False, watch=False, start="projects", scheme="light")
    win.resize(1400, 920)
    win.show()
    app.processEvents()
    yield win, conn, ids
    win.close()
    conn.close()


def test_window_shows_cockpit(setup):
    win, conn, ids = setup
    # the briefing floats over the graph
    assert win.hud.headline.text() == "Core demo due Oct 18, 0 of 9 closed"
    assert [r.sentence.uncertain for r in win.hud.rows] == [False, True]
    assert "unconfirmed" in win.hud.rows[1].text_label.text()
    assert win.hud.isVisible() and win.hud.expanded
    # graph: core, the milestone, the two cards
    g = win.graph.graph
    assert g.node("jarvis") and g.node("ms:1")
    assert {n.card_id for n in g.nodes if n.kind == "card"} == set(ids)
    assert "nodes" in win.counts.text() and "links" in win.counts.text()
    # columns
    assert set(win.vitals.values) == {"deadline", "progress", "risk"}
    assert win.vitals.values["progress"].text() == "0 / 0 closed"   # the seed milestone is empty
    assert win.vitals.values["risk"].text() == "No issues"
    assert len(win.milestones.rows) == 1 and win.dash.active.title == "Core demo"
    assert not hasattr(win, "stream")                   # not part of Projects any more
    assert "new suggestion" in " ".join(i.text for i in win.brain.stream)   # still recorded
    assert win.home_view.data.activity                  # Home's "recent activity" shows it
    assert "briefing by gemma4:26b" in win.status_text.text()
    assert win.right.isVisible() and not win.chat.isVisible()  # chat starts closed
    assert win.state_pill.text() == "idle"
    for w in (win.graph, win.commits):
        assert not w.grab().isNull()


def test_window_without_hardware_says_why(app, tmp_path):
    cfg = _cfg(tmp_path, hardware=False)
    conn = db.connect(cfg.db_path)
    win = MainWindow(cfg, conn, auto_refresh=False, watch=False, start="projects")
    pills = [win.status_pills.itemAt(i).widget() for i in range(win.status_pills.count())]
    hw = next(p for p in pills if p.text() == "no hardware checks")
    assert "hardware.yaml" in hw.toolTip().lower()
    assert win.hud.headline.text() == "No briefing yet"
    win.close()
    conn.close()


def _card_node(win, card_id):
    return next(n.id for n in win.graph.graph.nodes if n.card_id == card_id)


def test_hover_inspects_click_locks_esc_releases(setup, app):
    win, conn, ids = setup
    from PySide6.QtGui import QShortcut

    nid = _card_node(win, ids[1])
    pt = win.graph.screen_pos(nid)
    assert win.graph.node_at(pt.x(), pt.y()) == nid
    QTest.mouseMove(win.graph, pt.toPoint())
    app.processEvents()
    assert win.inspector.node is not None and win.inspector.node.id == nid
    assert win.inspector.card is not None and win.inspector.card.id == ids[1]
    QTest.mouseClick(win.graph, Qt.MouseButton.LeftButton, pos=pt.toPoint())
    app.processEvents()
    assert win.graph.lock_id == nid and "locked" in win.inspector.hint.text()
    esc = next(s for s in win.findChildren(QShortcut) if s.key().toString() == "Esc")
    esc.activated.emit()
    assert win.graph.lock_id is None
    assert win.isVisible()  # Esc never closes the window


def test_done_from_inspector_writes_db_and_drops_node(setup, app):
    win, conn, ids = setup
    win.select_node(_card_node(win, ids[0]))
    assert win.inspector.card.id == ids[0]
    win.inspector.done.emit(win.inspector.card)
    app.processEvents()
    assert cards.get_card(conn, ids[0]).status == "done"
    assert "Marked done" in win.status_label.text()
    win.reload()
    assert {n.card_id for n in win.graph.graph.nodes if n.kind == "card"} == {ids[1]}


def test_not_useful_records_reason(setup, app):
    win, conn, ids = setup
    win.select_node(_card_node(win, ids[1]))
    win.inspector.feedback_actions["vague"].trigger()
    app.processEvents()
    c = cards.get_card(conn, ids[1])
    assert (c.status, c.feedback) == ("done", "vague")


def test_copy_prompt_sets_clipboard(setup, app):
    win, conn, ids = setup
    win.select_node(_card_node(win, ids[0]))
    win.inspector.copy_btn.click()
    app.processEvents()
    assert QGuiApplication.clipboard().text() == cards.get_card(conn, ids[0]).claude_prompt
    assert "copied" in win.inspector.copy_btn.text()
    assert "Copied the prompt" in win.status_label.text()
    win.inspector._unflash()
    assert win.inspector.copy_btn.text() == "copy prompt"


def test_filters_hide_kinds_and_stream_click_selects(setup, app):
    win, conn, ids = setup
    win.filter_buttons["card"].setChecked(False)
    assert "card" in win.graph.hidden_kinds
    nid = _card_node(win, ids[0])
    pt = win.graph.screen_pos(nid)
    assert win.graph.node_at(pt.x(), pt.y()) != nid
    win.home_view.open_node.emit(nid)  # Home's recent activity shows its node (un-hides its kind)
    assert win.filter_buttons["card"].isChecked() and win.graph.lock_id == nid


def test_chat_answer_lights_up_cited_nodes(setup, app):
    win, conn, ids = setup
    from jarvis.chat import Answer, Source

    a = Answer(text="x", sources=[], unverified_numbers=[], notes=[], model="m", duration=1,
               cited_sources=[Source(f"card:{ids[0]}", "c", None), Source("#999", "x", None)])
    win._on_answer(a)
    assert win.graph.highlight == {f"card:{ids[0]}"}  # unknown refs are ignored
    assert ref_to_node("#7") == "issue:7" and ref_to_node("commit:02fb2d2abc") == \
        "commit:02fb2d2" and ref_to_node("fact:x") is None


# ---------------------------------------------------------------- chat


def _answer(text="Core demo has 9 open issues.", **kw):
    from jarvis.chat import Answer, Source

    base = dict(text=text, sources=[Source("#7", "#7 Create track.c", f"https://github.com/{REPO}/issues/7"),
                                    Source("commit:02fb2d2abc", "commit 02fb2d2", None)],
                unverified_numbers=[], notes=["Note: 12 is not in the synced data."],
                model="gemma4:26b", duration=12.0)
    base.update(kw)
    base.setdefault("cited_sources", list(base["sources"]))  # what the answer cited
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

    win = MainWindow(cfg, conn, auto_refresh=False, watch=False, start="projects", ask_fn=fake_ask)
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

    win = MainWindow(cfg, conn, auto_refresh=False, watch=False, start="projects", ask_fn=broken)
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
    assert {"Ctrl+R", "F5", "Ctrl+Q", "Esc", "Ctrl+K", "Ctrl+J", "Ctrl+B"} <= keys
    assert QApplication.instance().desktopFileName() == "jarvis"
    win.chat_btn.click()
    assert win.chat.isVisible() and not win.right.isVisible()  # the chat takes the column
    win.chat_btn.click()
    assert not win.chat.isVisible() and win.right.isVisible()
    win.focus_chat()  # Ctrl+K opens it again
    assert win.chat.isVisible()
    esc = next(s for s in win.findChildren(QShortcut) if s.key().toString() == "Esc")
    esc.activated.emit()  # nothing locked, no voice: Esc closes the chat
    assert not win.chat.isVisible()


def test_real_ask_is_wired_with_session(app, tmp_path, monkeypatch):
    """The default ask_fn goes through escalate.answer_with_escalation with no
    fast tier (typed chat), on_token, the mode, on_phase and the session id."""
    from jarvis.gui import chat_panel

    seen = {}

    def fake(conn, cfg, question, history, *, mode, fast_client, deep_client, on_token,
             on_phase, session_id):
        seen.update(question=question, session=session_id, mode=mode, fast=fast_client,
                    deep=deep_client, phase=on_phase)
        on_token("hi")
        return _answer(text="hi")

    monkeypatch.setattr("jarvis.escalate.answer_with_escalation", fake)
    out = chat_panel.default_ask(None, _cfg(tmp_path), "q?", [], lambda t: None, "window-1",
                                 mode="think", on_phase=print)
    assert out.text == "hi"
    assert seen == {"question": "q?", "session": "window-1", "mode": "think", "fast": None,
                    "deep": None, "phase": print}


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

    win = MainWindow(cfg, conn, auto_refresh=False, watch=False, start="projects", refresh_fn=fake_refresh)
    assert win.hud.data is None and win.graph.graph.count("card") == 0
    win.refresh()
    assert win.refreshing and not win.refresh_btn.isEnabled()
    assert win.state_pill.text() == "syncing"
    win.wait_for_refresh()
    assert calls == [True]
    assert not win.refreshing and win.refresh_btn.isEnabled()
    assert win.hud.data is not None
    assert win.graph.graph.count("card") == 1
    assert win.status_label.text() == "Ollama unavailable, showing template briefing"
    win.close()
    conn.close()


def test_refresh_error_goes_to_status_line(app, tmp_path):
    cfg = _cfg(tmp_path)
    conn = db.connect(cfg.db_path)

    def broken(_conn, _cfg):
        raise RuntimeError("GitHub exploded")

    win = MainWindow(cfg, conn, auto_refresh=False, watch=False, start="projects", refresh_fn=broken)
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
    win = MainWindow(cfg, conn, auto_refresh=False, watch=False, start="projects",
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
    win = MainWindow(cfg, conn, auto_refresh=False, watch=False, start="projects")
    assert win.pulse_text.text() == "not watching" and win.state_pill.text() == "idle"
    tick_at = (datetime.now(UTC) - timedelta(minutes=2)).isoformat(timespec="seconds")
    db.update_daemon_state(conn, pid=1234, interval_s=180, last_tick_at=tick_at)
    win.reload()
    assert win.pulse_text.text() == "watching · checked 2 min ago"
    assert win.state_pill.text() == "watching" and win.watch_dot.breathing
    db.update_daemon_state(conn, pid=None)  # daemon stopped
    win.reload()
    assert win.pulse_text.text() == "not watching"
    win.close()
    conn.close()
