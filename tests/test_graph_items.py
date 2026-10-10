"""Life OS items in the project graph (brain_data.build_graph): which items are
shown, their edges, layout and speed. No Qt; in-memory databases."""
from __future__ import annotations

import math
import random
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from test_brain_data import REPO, seeded

from jarvis import items as it
from jarvis.gui import brain_data as bd

LA = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 9, 15, 30, tzinfo=LA)


def mk(conn, title, **kw):
    return it.create_item(conn, title, now=NOW, tz=LA, **kw)


def link(conn, src, dst, kind="related"):
    (st, si), (dt, di) = src, dst
    conn.execute("INSERT INTO links (src_type, src_id, dst_type, dst_id, kind, created_at) "
                 "VALUES (?, ?, ?, ?, ?, 'x')", (st, str(si), dt, str(di), kind))


def graph(conn, **kw):
    return bd.build_graph(conn, REPO, NOW, **kw)


def items_of(g):
    return {n.title for n in g.nodes if n.kind == "item"}


def edge_set(g):
    return {(e.a, e.b, e.kind) for e in g.edges}


@pytest.fixture
def conn():
    c = seeded()
    c.execute("UPDATE projects SET github_repo = ?", (REPO,))
    return c


# ---------------------------------------------------------------- visibility rules


def test_project_items_hang_off_their_project_hub(conn):
    a = mk(conn, "Review tracking filter", due="2026-10-09T18:00", project="lidar")
    g = graph(conn)
    node = g.node(f"item:{a.id}")
    assert (node.kind, node.sub, node.status, node.item_id, node.live) == (
        "item", "task", "today", a.id, True)
    assert node.label == "Review tracking filter" and "due today 6:00 PM" in node.detail
    long = mk(conn, "Order the spare MG90S servos for the pan unit", project="lidar")
    assert graph(conn).node(f"item:{long.id}").label == "Order the spare MG90S…"
    hub = g.node("project:lidar")
    assert hub.kind == "hub" and hub.title == "LiDAR Scanner"
    assert (f"item:{a.id}", "project:lidar", "project") in edge_set(g)
    assert ("project:lidar", "jarvis", "hub") in edge_set(g)


def test_unlinked_items_stay_out_until_asked_for(conn):
    mk(conn, "Buy cat food", kind="reminder", remind="2026-10-09T17:00")
    mk(conn, "Call mom")
    mk(conn, "Filter", project="lidar")
    g = graph(conn)
    assert items_of(g) == {"Filter"} and g.loose == 2
    assert g.node("hub:unfiled") is None
    g = graph(conn, loose=True)
    assert items_of(g) == {"Filter", "Buy cat food", "Call mom"} and g.loose == 0
    assert ("hub:unfiled", "jarvis", "hub") in edge_set(g)
    unfiled = [n.id for _, n in g.neighbours("hub:unfiled")]
    assert len(unfiled) == 3   # the two items and the core


def test_explicit_links_to_github_show_items_without_a_project(conn):
    a = mk(conn, "Ask about the track filter")
    b = mk(conn, "Re-read the servo fix")
    c = mk(conn, "Another repo's issue")
    link(conn, ("item", a.id), ("issue", f"{REPO}#7"), "about")
    link(conn, ("item", b.id), ("commit", f"{REPO}@0000000abc"), "about")
    link(conn, ("item", c.id), ("issue", "someone/else#7"), "about")
    g = graph(conn)
    assert items_of(g) == {"Ask about the track filter", "Re-read the servo fix"}
    assert (f"item:{a.id}", "issue:7", "about") in edge_set(g)
    assert (f"item:{b.id}", "commit:0000000", "about") in edge_set(g)
    assert g.loose == 1                                   # the other repo's one
    assert g.node("issue:7").title == "#7 Issue 7"        # GitHub nodes untouched


def test_related_items_follow_a_shown_one(conn):
    a = mk(conn, "Filter", project="lidar")
    b = mk(conn, "Kalman notes")
    c = mk(conn, "Paper to read")
    d, e = mk(conn, "Lonely A"), mk(conn, "Lonely B")
    link(conn, ("item", a.id), ("item", b.id))
    link(conn, ("item", c.id), ("item", b.id))            # a chain: a - b - c
    link(conn, ("item", d.id), ("item", e.id))            # a pair with no anchor
    g = graph(conn)
    assert items_of(g) == {"Filter", "Kalman notes", "Paper to read"} and g.loose == 2
    assert (f"item:{a.id}", f"item:{b.id}", "related") in edge_set(g)


def test_edges_are_never_duplicated(conn):
    a = mk(conn, "Filter", project="lidar")
    b = mk(conn, "Notes", project="lidar")
    link(conn, ("item", a.id), ("item", b.id))
    link(conn, ("item", b.id), ("item", a.id))            # the same relation, reversed
    link(conn, ("item", a.id), ("issue", f"{REPO}#7"), "about")
    link(conn, ("item", a.id), ("project", it.get_project(conn, "lidar").id), "about")
    g = graph(conn)
    keys = [(min(e.a, e.b), max(e.a, e.b), e.kind) if e.kind in bd.UNDIRECTED
            else (e.a, e.b, e.kind) for e in g.edges]
    assert len(keys) == len(set(keys))
    related = [e for e in g.edges if e.kind == "related"]
    assert len(related) == 1
    assert (f"item:{a.id}", "project:lidar", "project") in edge_set(g)
    assert (f"item:{a.id}", "project:lidar", "about") in edge_set(g)  # a different relation


def test_done_and_deleted_items_leave_and_come_back(conn):
    a = mk(conn, "Filter", project="lidar")
    b = mk(conn, "Notes", project="lidar")
    it.complete_item(conn, a)
    it.delete_item(conn, b)
    g = graph(conn)
    assert items_of(g) == set() and g.node("project:lidar") is None and g.loose == 0
    it.reopen_item(conn, a)
    it.restore_item(conn, b)
    assert items_of(graph(conn)) == {"Filter", "Notes"}


def test_project_items_are_capped_most_urgent_first_but_links_are_not(conn):
    late = mk(conn, "late one", due="2026-10-01", project="lidar")
    for n in range(bd.ITEMS_PER_PROJECT + 5):
        mk(conn, f"someday {n:02d}", project="lidar")
    linked = mk(conn, "someday linked", project="lidar")
    link(conn, ("item", linked.id), ("issue", f"{REPO}#40"), "about")
    g = graph(conn)
    shown = items_of(g)
    assert "late one" in shown and "someday linked" in shown
    assert g.node(f"item:{late.id}").status == "overdue"
    assert len(shown) == bd.ITEMS_PER_PROJECT + 1           # the cap, plus the linked one
    assert g.loose == 47 - len(shown)                      # left out, counted


def test_items_by_kind_and_status(conn):
    mk(conn, "t", due="2026-10-07", project="lidar")
    mk(conn, "i", kind="idea", project="lidar")
    mk(conn, "r", kind="reminder", remind="2026-10-12T09:00", project="lidar", priority=2)
    g = graph(conn)
    got = {n.title: (n.sub, n.status) for n in g.nodes if n.kind == "item"}
    assert got == {"t": ("task", "overdue"), "i": ("idea", "someday"),
                   "r": ("reminder", "upcoming")}
    r = next(n for n in g.nodes if n.title == "r")
    assert "remind Mon Oct 12 9:00 AM" in r.detail and "priority !!" in r.detail


def test_github_graph_is_unchanged_without_items(conn):
    before = graph(conn)
    a = mk(conn, "Filter", project="lidar")
    it.delete_item(conn, a)
    after = graph(conn)
    assert before == after
    assert not {n.kind for n in after.nodes} & {"item"}
    assert after.node("project:lidar") is None


# ---------------------------------------------------------------- layout


def test_items_sit_in_their_project_cluster(conn):
    ids = [mk(conn, f"task {n}", project="lidar").id for n in range(12)]
    pos = bd.layout(graph(conn))
    hub = pos["project:lidar"]
    core = pos["jarvis"]
    for i in ids:
        assert math.dist(pos[f"item:{i}"], hub) < math.dist(pos[f"item:{i}"], core)
    assert all(math.hypot(x, y) <= 1 + 1e-9 for x, y in pos.values())


def test_linked_item_without_project_sits_with_its_issue(conn):
    a = mk(conn, "Ask about the track filter")
    link(conn, ("item", a.id), ("issue", f"{REPO}#7"), "about")
    pos = bd.layout(graph(conn))
    assert math.dist(pos[f"item:{a.id}"], pos["ms:1"]) < 0.6   # issue 7's milestone


def test_a_new_item_does_not_throw_the_graph_around(conn):
    for n in range(6):
        mk(conn, f"task {n}", project="lidar")
    a = bd.layout(graph(conn))
    mk(conn, "one more", project="lidar")
    b = bd.layout(graph(conn))
    moved = [k for k in a if math.dist(a[k], b[k]) > 0.25]
    assert len(moved) <= len(a) // 3


# ---------------------------------------------------------------- index + speed


def test_node_index_and_neighbours_match_a_plain_scan(conn):
    for n in range(8):
        mk(conn, f"t{n}", project="lidar")
    g = graph(conn)
    for n in g.nodes:
        assert g.node(n.id) is n
        brute = sorted((e.kind, (e.b if e.a == n.id else e.a)) for e in g.edges
                       if n.id in (e.a, e.b))
        assert sorted((e.kind, o.id) for e, o in g.neighbours(n.id)) == brute
    assert g.node("nope") is None and g.neighbours("nope") == []


def big(conn, n_items: int = 380, seed: int = 7):
    """A realistic mix: GitHub data plus hundreds of items, some linked."""
    rnd = random.Random(seed)
    for n in range(100, 160):
        conn.execute("INSERT INTO issues (number, title, state, labels, milestone_number, "
                     "created_at, updated_at) VALUES (?, ?, 'open', '[]', 1, ?, ?)",
                     (n, f"Issue {n}", NOW.isoformat(), NOW.isoformat()))
    for n in range(80):
        conn.execute("INSERT INTO commits (sha, message, author, committed_at) VALUES "
                     "(?, ?, 'tal', ?)", (f"{n + 100:07x}deadbeef", f"Work on #{100 + n % 60}",
                                         (NOW - timedelta(hours=n)).isoformat()))
    for n in range(30):
        conn.execute("INSERT INTO research_papers (paper_id, title, fetched_at) VALUES "
                     "(?, ?, ?)", (f"2401.{n:05d}", f"Paper {n}", NOW.isoformat()))
    slugs = ["school", "home", "gym", "robot-arm", "garden", "car", "taxes"]
    for slug in slugs:
        conn.execute("INSERT INTO projects (slug, name, created_at, updated_at) VALUES "
                     "(?, ?, 'x', 'x')", (slug, slug.title()))
    ids = []
    for n in range(n_items):
        proj = rnd.choice(["lidar", *slugs, None])
        due = rnd.choice([None, "2026-10-05", "2026-10-09", "2026-10-20"])
        ids.append(mk(conn, f"item {n}", project=proj, due=due,
                      kind=rnd.choice(it.KINDS)).id)
    for _ in range(60):
        a = rnd.choice(ids)
        if rnd.random() < 0.5:
            link(conn, ("item", a), ("issue", f"{REPO}#{rnd.randrange(100, 160)}"), "about")
        else:
            b = rnd.choice(ids)
            if a != b:
                conn.execute("INSERT OR IGNORE INTO links (src_type, src_id, dst_type, dst_id, "
                             "kind, created_at) VALUES ('item', ?, 'item', ?, 'related', 'x')",
                             (str(a), str(b)))
    conn.commit()


def test_five_hundred_nodes_build_and_lay_out_quickly(conn):
    big(conn)
    t0 = time.perf_counter()
    g = graph(conn, loose=True)
    t1 = time.perf_counter()
    pos = bd.layout(g)
    t2 = time.perf_counter()
    for n in g.nodes:
        g.neighbours(n.id)
    t3 = time.perf_counter()
    assert len(g.nodes) >= 500, len(g.nodes)
    assert set(pos) == {n.id for n in g.nodes}
    # generous bounds (a slow CI box); typical numbers are reported in CURRENT_STATE.md
    assert t1 - t0 < 2.0 and t2 - t1 < 1.0 and t3 - t2 < 0.5, (t1 - t0, t2 - t1, t3 - t2)
    assert datetime.now(UTC)  # (keeps the UTC import honest)
