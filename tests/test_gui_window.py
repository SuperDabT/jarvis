"""Qt smoke test for the window, rendered offscreen."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


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

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from jarvis import cards, db  # noqa: E402
from jarvis.config import Config  # noqa: E402
from jarvis.gui.app import make_app  # noqa: E402
from jarvis.gui.window import EMPTY_CARDS, MainWindow  # noqa: E402
from test_gui_data import REPO, seed  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return make_app([], scheme="light")


@pytest.fixture
def setup(app, tmp_path):
    cfg = Config(repo=REPO, db_path=tmp_path / "jarvis.db")
    conn = db.connect(cfg.db_path)
    ids = seed(conn, n_cards=2)
    win = MainWindow(cfg, conn, auto_refresh=False, watch=False, scheme="light")
    win.show()
    app.processEvents()
    yield win, conn, ids
    win.close()
    conn.close()


def test_window_shows_briefing_and_cards(setup):
    win, conn, ids = setup
    assert win.briefing_view.headline.text() == "Core demo due Oct 18, 0 of 9 closed"
    assert [r.sentence.uncertain for r in win.briefing_view.rows] == [False, True]
    assert win.briefing_view.rows[0].pill is None
    assert win.briefing_view.rows[1].pill is not None
    assert "heuristic" in win.briefing_view.rows[1].pill.toolTip()
    assert set(win.card_widgets) == set(ids)
    assert set(win.group_titles) == {"electrical", "research"}
    assert win.count_label.text() == "2"
    assert "gemma4:26b" in win.meta_label.text()


def test_done_removes_card_and_writes_db(setup, app):
    win, conn, ids = setup
    w = win.card_widgets[ids[0]]
    w.done_btn.click()
    app.processEvents()
    assert cards.get_card(conn, ids[0]).status == "done"
    assert ids[0] not in win.card_widgets
    assert "electrical" not in win.group_titles  # its only card is gone
    assert "Marked done" in win.status_label.text()
    win.reload()  # a later DB-driven reload agrees
    assert set(win.card_widgets) == {ids[1]}


def test_not_useful_records_reason(setup, app):
    win, conn, ids = setup
    win.card_widgets[ids[1]].feedback_actions["vague"].trigger()
    app.processEvents()
    c = cards.get_card(conn, ids[1])
    assert (c.status, c.feedback) == ("done", "vague")
    assert ids[1] not in win.card_widgets


def test_last_card_gone_shows_empty_state(setup, app):
    win, conn, ids = setup
    for cid in ids:
        win.card_widgets[cid].done_btn.click()
    app.processEvents()
    assert win.card_widgets == {}
    assert win.empty_label.text() == EMPTY_CARDS


def test_copy_button_sets_clipboard_and_flashes(setup, app):
    win, conn, ids = setup
    w = win.card_widgets[ids[0]]
    w.copy_btn.click()
    app.processEvents()
    assert QGuiApplication.clipboard().text() == w.card.claude_prompt
    assert w.copy_btn.text() == "Copied"
    w._unflash()
    assert w.copy_btn.text() == "Copy prompt for Claude"


def test_refresh_runs_in_background_and_reloads(app, tmp_path):
    cfg = Config(repo=REPO, db_path=tmp_path / "jarvis.db")
    conn = db.connect(cfg.db_path)
    calls = []

    def fake_refresh(worker_conn, cfg_):
        calls.append(worker_conn is not conn)
        seed(worker_conn, n_cards=1)  # written from the worker's own connection
        from types import SimpleNamespace
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
    cfg = Config(repo=REPO, db_path=tmp_path / "jarvis.db")
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


def test_shortcuts_exist(setup):
    win, _, _ = setup
    from PySide6.QtGui import QShortcut

    keys = {s.key().toString() for s in win.findChildren(QShortcut)}
    assert {"Ctrl+R", "Ctrl+Q", "Esc"} <= keys
    assert QApplication.instance().desktopFileName() == "jarvis"
    assert Qt.ScrollBarPolicy.ScrollBarAlwaysOff == win.scroll.horizontalScrollBarPolicy()
