"""Items in the project graph inside the window: inspect, edit in Tasks, done,
the legend filter and the unfiled toggle, and staying in step with Tasks.
Offscreen, fixed clock, databases under tmp_path."""
from __future__ import annotations

import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from test_gui_window import _offscreen_ok  # noqa: E402

if not _offscreen_ok():
    pytest.skip("PySide6 cannot start on the offscreen platform", allow_module_level=True)

from test_graph_items import big, link  # noqa: E402
from test_gui_data import REPO, seed  # noqa: E402
from test_gui_window import _cfg  # noqa: E402

from jarvis import db  # noqa: E402
from jarvis import items as it  # noqa: E402
from jarvis.gui.app import make_app  # noqa: E402
from jarvis.gui.window import MainWindow  # noqa: E402

LA = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 9, 15, 30, tzinfo=LA)


@pytest.fixture(scope="module")
def app():
    return make_app([], scheme="light")


@pytest.fixture
def setup(app, tmp_path):
    cfg = _cfg(tmp_path)
    conn = db.connect(cfg.db_path)
    seed(conn, n_cards=2)
    conn.execute("UPDATE projects SET github_repo = ?", (REPO,))
    mk = lambda t, **kw: it.create_item(conn, t, now=NOW, tz=LA, **kw)  # noqa: E731
    ids = {
        "filter": mk("Review tracking filter", due="2026-10-09T18:00", project="lidar").id,
        "rail": mk("Re-measure the 5V rail", due="2026-10-07", project="lidar").id,
        "idea": mk("Adaptive Kalman noise", kind="idea", project="lidar").id,
        "loose": mk("Buy cat food", kind="reminder", remind="2026-10-09T17:00").id,
    }
    link(conn, ("item", ids["filter"]), ("item", ids["idea"]))
    conn.commit()
    win = MainWindow(cfg, conn, auto_refresh=False, watch=False, clock=lambda: NOW,
                     start="projects")
    win.resize(1500, 940)
    win.show()
    app.processEvents()
    yield win, conn, ids
    win.close()
    conn.close()


def item_ids(win):
    return {n.item_id for n in win.graph.graph.nodes if n.kind == "item"}


def test_items_are_in_the_graph_with_their_project(setup):
    win, _, ids = setup
    g = win.graph.graph
    assert item_ids(win) == {ids["filter"], ids["rail"], ids["idea"]}   # not the loose one
    assert g.node("project:lidar") and g.node("jarvis")
    assert g.node(f"item:{ids['rail']}").status == "overdue"
    assert "item" in win.filter_buttons and win.filter_buttons["item"].isChecked()
    assert not win.unfiled_btn.isHidden() and "1 hidden" in win.unfiled_btn.toolTip()


def test_selecting_an_item_inspects_it(setup, app):
    win, _, ids = setup
    win.graph.lock(f"item:{ids['filter']}")
    app.processEvents()
    insp = win.inspector
    texts = [w.text() for w in insp.findChildren(type(win.status_label))]
    assert "task · today · live" in texts and "Review tracking filter" in texts
    assert any(t.startswith("due today 6:00 PM") for t in texts)
    links = [b.text() for b in insp.findChildren(type(win.refresh_btn))
             if b.objectName() == "filter"]
    assert "in project → lidar" in links and "related to → Adaptive Kalman noise" in links
    assert insp.edit_btn.text() == "edit in Tasks" and insp.done_btn.text() == "done"


def test_edit_in_tasks_opens_the_item_there(setup, app):
    win, _, ids = setup
    win.tasks_view.set_kind("idea")                 # a filter that would hide it
    win.graph.lock(f"item:{ids['rail']}")
    app.processEvents()
    win.inspector.edit_btn.click()
    app.processEvents()
    assert win.workspace == "tasks"
    assert win.tasks_view.kind_filter == "all" and win.tasks_view.selected_id == ids["rail"]
    assert win.tasks_view.editor.title.text() == "Re-measure the 5V rail"


def test_done_from_the_graph_removes_it_everywhere(setup, app):
    win, conn, ids = setup
    win.graph.lock(f"item:{ids['rail']}")
    app.processEvents()
    win.inspector.done_btn.click()
    app.processEvents()
    assert it.get_item(conn, ids["rail"]).done
    assert ids["rail"] not in item_ids(win) and win.graph.lock_id is None
    assert "Re-measure the 5V rail" not in [r.row.title for r in win.home_view.today_rows]
    assert win.tasks_view.data.group("overdue") == ()


def test_github_nodes_keep_their_inspector(setup, app):
    win, _, _ = setup
    win.graph.lock("ms:1")
    app.processEvents()
    texts = [w.text() for w in win.inspector.findChildren(type(win.status_label))]
    assert "milestone · open" in texts or any(t.startswith("milestone") for t in texts)
    buttons = {b.text() for b in win.inspector.findChildren(type(win.refresh_btn))}
    assert "edit in Tasks" not in buttons
    win.graph.lock("card:1")
    app.processEvents()
    assert win.inspector.copy_btn is not None              # card actions as before


def test_item_legend_filter_hides_items(setup, app):
    win, _, ids = setup
    win.filter_buttons["item"].setChecked(False)
    app.processEvents()
    assert "item" in win.graph.hidden_kinds
    pt = win.graph.screen_pos(f"item:{ids['filter']}")
    assert win.graph.node_at(pt.x(), pt.y()) != f"item:{ids['filter']}"
    win.filter_buttons["item"].setChecked(True)
    assert win.graph.node_at(pt.x(), pt.y()) == f"item:{ids['filter']}"


def test_unfiled_toggle_reveals_loose_items(setup, app):
    win, _, ids = setup
    win.unfiled_btn.setChecked(True)
    app.processEvents()
    assert ids["loose"] in item_ids(win) and win.graph.graph.node("hub:unfiled")
    assert win.graph.graph.loose == 0
    win.unfiled_btn.setChecked(False)
    app.processEvents()
    assert ids["loose"] not in item_ids(win)


def test_graph_follows_item_changes(setup, app):
    win, conn, ids = setup
    v = win.tasks_view
    v.delete(ids["idea"])
    assert ids["idea"] not in item_ids(win)
    assert not any(e.kind == "related" for e in win.graph.graph.edges)
    v.undo_delete()
    assert ids["idea"] in item_ids(win)
    assert sum(e.kind == "related" for e in win.graph.graph.edges) == 1   # once, as before
    v.save_item(ids["loose"], {"project": it.get_project(conn, "lidar").id})
    assert ids["loose"] in item_ids(win)                    # filed: now it belongs
    v.save_item(ids["filter"], {"title": "Tune the tracking filter"})
    assert win.graph.graph.node(f"item:{ids['filter']}").title == "Tune the tracking filter"


def test_five_hundred_nodes_paint_and_hover_fast(app, tmp_path):
    cfg = _cfg(tmp_path)
    conn = db.connect(cfg.db_path)
    seed(conn, n_cards=2)
    conn.execute("UPDATE projects SET github_repo = ?", (REPO,))
    big(conn)
    win = MainWindow(cfg, conn, auto_refresh=False, watch=False, clock=lambda: NOW,
                     start="projects")
    win.unfiled_btn.setChecked(True)
    win.resize(1500, 940)
    win.show()
    app.processEvents()
    g = win.graph
    assert len(g.graph.nodes) >= 450
    t0 = time.perf_counter()
    g.grab()
    t1 = time.perf_counter()
    for x in range(0, 800, 8):
        g.node_at(x, 300)
    t2 = time.perf_counter()
    # generous bounds; measured numbers are in CURRENT_STATE.md
    assert t1 - t0 < 0.5 and (t2 - t1) / 100 < 0.01, (t1 - t0, (t2 - t1) / 100)
    win.close()
    conn.close()
