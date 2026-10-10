"""The inspector: a summary first, findings with their actions. Offscreen, a
database and hardware.yaml under tmp_path; "open source" is caught, never run."""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from test_gui_window import _offscreen_ok  # noqa: E402

if not _offscreen_ok():
    pytest.skip("PySide6 cannot start on the offscreen platform", allow_module_level=True)

from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from test_finding_view import HW  # noqa: E402
from test_gui_data import REPO, seed  # noqa: E402
from test_gui_think import _settings  # noqa: E402

from jarvis import db  # noqa: E402
from jarvis.config import Config  # noqa: E402
from jarvis.gui.app import make_app  # noqa: E402
from jarvis.gui.window import MainWindow  # noqa: E402
from jarvis.pipeline import run_checks  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return make_app([], scheme="dark")


@pytest.fixture
def setup(app, tmp_path):
    hw = tmp_path / "hardware.yaml"
    hw.write_text(HW)
    cfg = Config(repo=REPO, db_path=tmp_path / "jarvis.db", hardware_path=hw)
    conn = db.connect(cfg.db_path)
    seed(conn, n_cards=0)
    run_checks(conn, cfg, None)
    conn.commit()
    asked = []

    def ask(conn, cfg, question, history, on_token, sid, **kw):
        asked.append(question)
        return SimpleNamespace(text="It means...", sources=[], notes=[], model="m", duration=1)

    win = MainWindow(cfg, conn, auto_refresh=False, watch=False, start="projects", ask_fn=ask,
                     voice={"settings": _settings(tmp_path), "mic_ok": False})
    opened = []
    win.open_url = lambda url: opened.append(url.toString())
    win.resize(1500, 940)
    win.show()
    app.processEvents()
    yield win, conn, hw, asked, opened
    win.close()
    conn.close()


def lock(win, nid):
    win.graph.lock(nid)
    QApplication.processEvents()
    return win.inspector


def test_hub_shows_a_summary_and_folds_the_rest(setup):
    win, *_ = setup
    insp = lock(win, "hub:hardware")
    assert "needs attention" in insp.attention.text() and "unsure" in insp.attention.text()
    rows = insp.related_rows
    shown = [r for r in rows if r.isVisibleTo(insp)]
    assert 0 < len(shown) < len(rows)                      # the rest are under "details"
    assert all(r.other.status in ("unknown", "warn", "fail") for r in shown)   # problems first
    assert all("unsure" in r.note.text() or "fail" in r.note.text() for r in shown)
    assert insp.details.isHidden()
    insp.details_btn.click()
    assert not insp.details.isHidden() and insp.details_btn.text() == "details ▾"


def test_check_shows_verified_text_source_and_next_step(setup):
    win, _, hw, *_ = setup
    insp = lock(win, "check:common_ground:5V")
    assert insp.finding is not None
    assert insp.verified.text().startswith("MG90S on the 5V rail")
    assert "multimeter" in insp.next_text.text()
    assert str(hw) in insp.source_text.text() and "rails.5V.ground_common · line 7" in \
        insp.source_text.text()
    assert "doesn't edit files" in insp.edit_note.text()
    assert insp.ask_btn.text() == "✦ Ask Jarvis about this finding"
    assert [b.text() for b in (insp.source_btn, insp.fix_btn)] == [
        "open source", "copy fix prompt"]


def test_actions_explain_open_and_copy_change_nothing(setup):
    win, _, hw, asked, opened = setup
    before = hw.read_bytes()
    insp = lock(win, "check:common_ground:5V")
    insp.ask_btn.click()
    win.chat.wait()
    assert win.chat.isVisible() and asked and asked[0].startswith("Explain this finding")
    assert win.chat.focus is not None and win.chat.focus.node_id == "check:common_ground:5V"
    assert any("common_ground:5V" in line for line in win.chat.focus.lines)
    insp = lock(win, "check:common_ground:5V")
    insp.source_btn.click()
    assert opened == [hw.resolve().as_uri()]
    insp.fix_btn.click()
    assert QGuiApplication.clipboard().text() == insp.finding.prompt and "5V" in insp.finding.prompt
    assert "Nothing was changed" in win.status_label.text()
    assert hw.read_bytes() == before                 # no action writes the file


def test_card_about_a_check_gets_the_finding_and_its_own_prompt(setup):
    win, conn, *_ = setup
    card_id = conn.execute("SELECT id FROM cards WHERE check_id = 'common_ground:5V'"
                           ).fetchone()[0]
    insp = lock(win, f"card:{card_id}")
    assert insp.card is not None and insp.finding is not None
    assert insp.fix_btn is None and insp.copy_btn is not None   # one prompt button, not two
    assert not insp.prompt.isVisibleTo(insp)                    # the raw prompt is folded


def test_without_hardware_file_the_check_still_inspects(setup):
    win, _, hw, *_ = setup
    hw.unlink()
    insp = lock(win, "check:common_ground:5V")
    assert insp.finding is None and insp.node.id == "check:common_ground:5V"
