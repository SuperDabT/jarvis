"""The Projects dashboard in the window: Vitals, Issues, Milestones, Commit
activity. Offscreen, a database under tmp_path, UI settings in a scratch file
(conftest); URLs are caught, never opened."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from test_gui_window import _offscreen_ok  # noqa: E402

if not _offscreen_ok():
    pytest.skip("PySide6 cannot start on the offscreen platform", allow_module_level=True)

from PySide6.QtWidgets import QApplication  # noqa: E402
from test_dashboard_data import issue  # noqa: E402
from test_gui_data import REPO, seed  # noqa: E402
from test_gui_think import _settings  # noqa: E402
from test_gui_window import _cfg  # noqa: E402

from jarvis import db  # noqa: E402
from jarvis.gui.app import make_app  # noqa: E402
from jarvis.gui.window import ACTIVE_MILESTONE_KEY, MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return make_app([], scheme="dark")


def _window(tmp_path, commits=True, milestones=True):
    cfg = _cfg(tmp_path)
    conn = db.connect(cfg.db_path)
    seed(conn, n_cards=0)
    conn.execute("DELETE FROM issues")
    conn.execute("DELETE FROM commits")
    now = datetime.now(UTC)
    if milestones:
        conn.execute("UPDATE milestones SET due_on = ?, open_issues = 3, closed_issues = 1",
                     ((now + timedelta(days=9)).isoformat(),))
        db.upsert_milestone(conn, {"number": 2, "title": "Field test", "state": "open",
                                   "due_on": (now + timedelta(days=40)).isoformat(),
                                   "open_issues": 1, "closed_issues": 0,
                                   "html_url": f"https://github.com/{REPO}/milestone/2"})
    else:
        conn.execute("DELETE FROM milestones")
    rows = [issue(7, ["phase-1"]), issue(12, ["blocker"]), issue(31), issue(40, ms=2)]
    for i in rows:
        conn.execute("INSERT INTO issues (number, title, state, labels, milestone_number, "
                     "html_url) VALUES (:number, :title, :state, :labels, :milestone_number, "
                     ":html_url)", i)
    if commits:
        for k in range(5):
            conn.execute("INSERT INTO commits (sha, message, author, committed_at) VALUES "
                         "(?, ?, 't', ?)", (f"{k:07x}beef", f"Commit {k}",
                                            (now - timedelta(days=k)).isoformat()))
        db.set_sync_state(conn, "commits_since", (now - timedelta(days=183)).isoformat())
    conn.commit()
    win = MainWindow(cfg, conn, auto_refresh=False, watch=False, start="projects",
                     voice={"settings": _settings(tmp_path), "mic_ok": False})
    opened = []
    win.open_url = lambda url: opened.append(url.toString())
    win.resize(1500, 940)
    win.show()
    QApplication.processEvents()
    return win, conn, opened


@pytest.fixture
def made(app, tmp_path):
    out = []

    def build(**kw):
        out.append(_window(tmp_path, **kw))
        return out[-1]

    yield build
    for w, c, _ in out:
        w.close()
        c.close()


def test_left_column_is_vitals_issues_milestones_commits(made):
    win, *_ = made()
    lay = win.left_column.widget().layout()
    tiles = [lay.itemAt(i).widget() for i in range(lay.count()) if lay.itemAt(i).widget()]
    assert tiles == [win.vitals, win.issues, win.milestones, win.commits]
    assert not hasattr(win, "power") and not hasattr(win, "pulse")
    assert [h.head.text() for h in tiles] == ["Vitals", "Issues", "Milestones",
                                              "Commit activity"]


def test_vitals_issues_and_milestones_show_the_active_milestone(made):
    win, *_ = made()
    v = win.vitals.values
    assert v["deadline"].text().endswith("left") and "Core demo" in win.vitals.subs[
        "deadline"].text()
    assert v["progress"].text() == "1 / 4 closed"
    assert v["risk"].text() == "Too early to tell"          # nothing closed in 14 days, 9 days
    assert "no pace" in win.vitals.subs["risk"].text()
    assert win.issues.count.text() == "4"                   # all open issues in the repo
    assert [r.row.number for r in win.issues.rows] == [12, 7, 31]   # blocker, phase, number
    assert win.milestones.hint.text() == "nearest due"
    assert not win.milestones.expanded and win.milestones.others.isHidden()   # folded


def test_switching_the_milestone_updates_every_tile(made):
    win, *_ = made()
    win.milestones.toggle_btn.click()
    assert win.milestones.expanded and not win.milestones.others.isHidden()
    assert win.milestones.toggle_btn.text() == "▾ hide 1 other"
    win.milestones.rows[1].click()                           # "Field test"
    assert win.chat.settings.value(ACTIVE_MILESTONE_KEY) in (2, "2")
    assert win.dash.active.title == "Field test" and win.milestones.hint.text() == "picked"
    assert "Field test" in win.vitals.subs["deadline"].text()
    assert win.vitals.values["progress"].text() == "0 / 1 closed"
    assert [r.row.number for r in win.issues.rows] == [40]
    assert win.reload() is None and win.dash.active.title == "Field test"   # stays picked
    win.pick_milestone(-1)
    assert win.dash.active.title == "Core demo"
    assert win.chat.settings.value(ACTIVE_MILESTONE_KEY) is None


def test_picked_milestone_that_closed_falls_back(made):
    win, conn, _ = made()
    win.pick_milestone(2)
    conn.execute("UPDATE milestones SET state = 'closed' WHERE number = 2")
    conn.commit()
    win.reload()
    assert win.dash.active.title == "Core demo" and not win.dash.picked


def test_issue_rows_inspect_and_view_all_opens_the_milestone(made):
    win, _, opened = made()
    row = win.issues.rows[0]
    in_graph = win.graph.graph.node("issue:12") is not None
    row.click()
    QApplication.processEvents()
    if in_graph:
        assert win.graph.lock_id == "issue:12" and win.inspector.node.id == "issue:12"
    else:
        assert opened == [f"https://github.com/{REPO}/issues/12"]
    win.issues.view_btn.click()
    assert opened[-1] == f"https://github.com/{REPO}/milestone/1"


def test_issue_not_in_the_graph_opens_its_page(made):
    win, _, opened = made()
    real = win.graph.graph
    win.graph.graph = SimpleNamespace(node=lambda nid: None)   # e.g. not in the graph
    try:
        win.select_issue(31)
    finally:
        win.graph.graph = real
    assert opened == [f"https://github.com/{REPO}/issues/31"]


def test_commit_heatmap_counts_commits_and_explains_a_day(made):
    win, *_ = made()
    p = win.commits
    assert not p.heatmap.isHidden() and p.empty.isHidden()
    assert p.hint.text() == "5 commits"
    grid = p.heatmap.grid
    today = next(d for d in grid.days if d.day == grid.today)
    step = p.heatmap.cell() + p.heatmap.GAP
    hit = p.heatmap.day_at(today.col * step + 1, today.row * step + 1)
    assert hit == today and "Commit 0" in grid.tooltip(hit)
    assert p.heatmap.width() <= win.left_column.width()


def test_empty_states(made):
    win, *_ = made(commits=False, milestones=False)
    assert win.commits.heatmap.isHidden() and not win.commits.empty.isHidden()
    assert "No commit history" in win.commits.empty.text()
    assert win.vitals.values["deadline"].text() == "none"
    assert "no open milestones" in win.vitals.subs["deadline"].text()
    assert win.issues.count.text() == "4" and len(win.issues.rows) == 4


def test_fits_the_narrow_window_with_the_chat_open(made):
    win, *_ = made()
    win.set_chat_open(True)
    win.resize(1200, 700)
    QApplication.processEvents()
    inner = win.left_column.widget()
    assert inner.width() <= win.left_column.viewport().width() + 6
    for r in win.issues.rows:
        assert r.mapTo(inner, r.rect().topRight()).x() <= inner.width()
