"""The brain view models: graph, layout, stream, vitals, pulse (no Qt)."""
from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

from jarvis import cards, db
from jarvis.gui import brain_data as bd

REPO = "SuperDabT/stm32-lidar-scanner"
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def iso(days_ago: float = 0, hours_ago: float = 0) -> str:
    return (NOW - timedelta(days=days_ago, hours=hours_ago)).isoformat()


def seeded(n_commits: int = 3):
    conn = db.connect(":memory:")
    db.upsert_milestone(conn, {"number": 1, "title": "Core demo", "state": "open",
                               "open_issues": 2, "closed_issues": 1, "due_on": "2026-10-18",
                               "html_url": f"https://github.com/{REPO}/milestone/1"})
    for n, state, ms in ((7, "open", 1), (28, "closed", 1), (40, "open", None)):
        conn.execute(
            "INSERT INTO issues (number, title, state, labels, milestone_number, body, html_url,"
            " created_at, updated_at, closed_at) VALUES (?, ?, ?, '[]', ?, 'body', ?, ?, ?, ?)",
            (n, f"Issue {n}", state, ms, f"https://github.com/{REPO}/issues/{n}", iso(20),
             iso(0, 3) if n == 7 else iso(10), iso(2) if state == "closed" else None))
    for i in range(n_commits):
        msg = "Fix #7 track filter" if i == 0 else f"Work {i}"
        conn.execute("INSERT INTO commits (sha, message, author, committed_at, html_url) "
                     "VALUES (?, ?, 'tal', ?, ?)",
                     (f"{i:07x}abcdef", msg, iso(i), f"https://github.com/{REPO}/commit/{i}"))
    conn.execute("INSERT INTO electrical_findings (check_id, status, title, why, updated_at) "
                 "VALUES ('voltage_window:MG90S', 'fail', 'MG90S out of window', 'why', ?)",
                 (iso(0, 1),))
    cards.upsert_card(conn, type_="electrical", title="Servo voltage", why="w",
                      source_ref="hardware.yaml", source_url=None, claude_prompt="p",
                      check_id="voltage_window:MG90S")
    conn.commit()
    return conn


def test_graph_nodes_and_links_come_from_the_data():
    g = bd.build_graph(seeded(), REPO, NOW)
    ids = {n.id for n in g.nodes}
    assert {"jarvis", "ms:1", "issue:7", "issue:28", "issue:40", "hub:commits",
            "hub:hardware", "check:voltage_window:MG90S", "part:MG90S"} <= ids
    edges = {(e.a, e.b, e.kind) for e in g.edges}
    assert ("issue:7", "ms:1", "milestone") in edges
    assert ("issue:40", "jarvis", "hub") in edges            # no milestone: to the core
    assert ("commit:0000000", "issue:7", "mentions") in edges  # "Fix #7" in the message
    assert ("check:voltage_window:MG90S", "part:MG90S", "check") in edges
    card = next(n for n in g.nodes if n.kind == "card")
    assert (card.id, "check:voltage_window:MG90S", "card") in edges
    # live = activity in the last 48 h; a failing check is red, not live-mint
    assert g.node("issue:7").live and not g.node("issue:40").live
    assert g.node("check:voltage_window:MG90S").status == "fail"
    # the commit strand: newest first, from the hub
    assert bd._strand(g) == ["commit:0000000", "commit:0000001", "commit:0000002"]
    assert [o.id for _, o in g.neighbours("issue:7")].count("ms:1") == 1


def test_graph_caps_commits():
    g = bd.build_graph(seeded(n_commits=bd.COMMITS_MAX + 15), REPO, NOW)
    assert g.count("commit") == bd.COMMITS_MAX


def test_parts_of_check_ids():
    assert bd._parts_of("logic_level:NUCLEO-F446RE.PA0->MG90S.SIG") == ["NUCLEO-F446RE", "MG90S"]
    assert bd._parts_of("rail_budget:5V") == ["5V rail"]
    assert bd._parts_of("missing:rails.5V.max_current_a") == ["5V rail"]
    assert bd._parts_of("unknown:x") == []


def test_layout_is_stable_and_in_the_unit_disc():
    conn = seeded()
    g = bd.build_graph(conn, REPO, NOW)
    a, b = bd.layout(g), bd.layout(g)
    assert a == b and set(a) == {n.id for n in g.nodes}
    assert a["jarvis"] == (0.0, 0.0)
    assert all(math.hypot(x, y) <= 1.0 + 1e-9 for x, y in a.values())
    # a new issue does not throw the others around
    conn.execute("INSERT INTO issues (number, title, state, labels, created_at, updated_at) "
                 "VALUES (41, 'New', 'open', '[]', ?, ?)", (iso(30), iso(30)))
    c = bd.layout(bd.build_graph(conn, REPO, NOW))
    moved = [k for k in a if math.dist(a[k], c[k]) > 0.25]
    assert len(moved) <= len(a) // 3


def test_stream_is_newest_first_with_origins():
    conn = seeded()
    db.insert_briefing(conn, "h", "Headline", "[]", "model", iso(0, 2), "gemma4:26b")
    conn.execute("INSERT INTO chat_messages (session_id, role, content, created_at) VALUES "
                 "('s', 'user', 'What is left?', ?), ('s', 'assistant', 'Two issues.', ?), "
                 "('s', 'user', 'And?', ?), ('s', 'assistant', '', ?)",
                 (iso(0, 1), iso(0, 1), iso(0, 0.5), iso(0, 0.5)))
    items = bd.load_stream(conn, NOW)
    assert [i.when for i in items] == sorted((i.when for i in items), reverse=True)
    texts = [i.text for i in items]
    assert any(t.startswith("answered “What is left?”: Two issues.") for t in texts)
    assert not any("“And?”" in t for t in texts)  # empty answers are left out
    assert any(t.startswith("closed #28") for t in texts)
    assert any(t.startswith("briefing rewritten (gemma4:26b)") for t in texts)
    assert any(i.kind == "check" and i.node_id == "check:voltage_window:MG90S" for i in items)
    assert items[0].live


def test_vitals_and_pulse():
    conn = seeded()
    v = {x.key: x for x in bd.vitals(conn, NOW)}
    assert v["issues"].value == "2 open · 1 closed"
    assert v["checks"].level == "fail" and "1 fail" in v["checks"].value
    p = bd.pulse(conn, date(2026, 10, 9), days=10)
    assert len(p.counts) == 10 and p.total == sum(p.counts) and p.total >= 3


def test_ago():
    assert bd.ago(NOW - timedelta(seconds=20), NOW) == "20s ago"
    assert bd.ago(NOW - timedelta(minutes=9), NOW) == "9m ago"
    assert bd.ago(NOW - timedelta(hours=2), NOW) == "2h ago"
    assert bd.ago(NOW - timedelta(days=3), NOW) == "3d ago"
    assert bd.ago(None, NOW) == ""
