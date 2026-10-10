"""The chat context for a selected graph node (``jarvis.focus``) and its place in
the chat context (``chat.build_context(focus=...)``). In-memory databases."""
from __future__ import annotations

import json

import pytest
from test_gui_data import REPO

from jarvis import chat, db
from jarvis.config import Config
from jarvis.focus import NO_SOURCE, build_focus
from jarvis.gui.brain_data import Edge, Graph, Node


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.upsert_milestone(c, {"number": 1, "title": "Core demo", "state": "open",
                            "due_on": "2026-10-18T07:00:00Z", "open_issues": 3,
                            "closed_issues": 1})
    c.execute("INSERT INTO issues (number, title, state, labels, milestone_number, body, "
              "html_url) VALUES (12, '5V rail browns out when both servos stall', 'open', ?, 1, "
              "?, ?)", (json.dumps(["blocker", "phase-1"]),
                        "The 5V rail drops below 4.5 V. Suspect servo_pwm.c and power.h.",
                        f"https://github.com/{REPO}/issues/12"))
    c.execute("INSERT INTO commits (sha, message, author, committed_at) VALUES "
              "('02fb2d2aaaa', 'Add bulk capacitor on the 5V rail (#12)', 'tal', "
              "'2026-10-08T10:00:00Z'), ('b455d42bbbb', 'Rework servo power wiring', 'tal', "
              "'2026-10-07T10:00:00Z')")
    return c


def graph():
    issue = Node("issue:12", "issue", "#12", "5V rail browns out when both servos stall",
                 status="open")
    named = Node("commit:02fb2d2", "commit", "02fb2d2", "Add bulk capacitor on the 5V rail (#12)")
    maybe = Node("commit:b455d42", "commit", "b455d42", "Rework servo power wiring")
    ms = Node("ms:1", "milestone", "Core demo", "Milestone Core demo")
    return Graph((issue, named, maybe, ms), (
        Edge("commit:02fb2d2", "issue:12", "mentions"),
        Edge("commit:b455d42", "issue:12", "maybe", dashed=True),
        Edge("issue:12", "ms:1", "milestone")))


def test_issue_focus_carries_the_issue_and_how_certain_each_link_is(conn):
    g = graph()
    f = build_focus(conn, REPO, g.node("issue:12"), g)
    text = "\n".join(f.lines)
    assert f.node_id == "issue:12" and f.action == "Ask Jarvis about this issue"
    assert f.label == "#12 5V rail browns out when both servos stall"
    assert f"Repository: {REPO}" in text and "Issue #12: 5V rail browns out" in text
    assert "State: open" in text and "Milestone: Core demo (due 2026-10-18)" in text
    assert "Labels: blocker, phase-1" in text and "Suspect servo_pwm.c" in text
    named = next(ln for ln in f.lines if "02fb2d2" in ln or "bulk capacitor" in ln)
    maybe = next(ln for ln in f.lines if "Rework servo power" in ln)
    assert "not proof of a fix" in named
    assert "UNCONFIRMED" in maybe and "not proof" in maybe
    assert NO_SOURCE in f.lines                      # says the code isn't available
    assert "Files mentioned: power.h, servo_pwm.c" in text   # only what the text names
    assert "#12" in f.question and "related commits" in f.question


def test_other_kinds_get_their_own_context(conn):
    g = graph()
    c = build_focus(conn, REPO, g.node("commit:02fb2d2"), g)
    assert c.action == "Ask Jarvis about this commit" and "by tal on 2026-10-08" in c.lines[1]
    m = build_focus(conn, REPO, g.node("ms:1"), g)
    assert m.action == "Ask Jarvis about this milestone"
    assert "Issues: 1 closed, 3 open" in m.lines
    check = Node("check:common_ground:5V", "check", "common_ground", "Wiring check: 5V ground",
                 "MG90S on the 5V rail ...", status="unknown")
    k = build_focus(conn, REPO, check, None)
    assert k.action == "Ask Jarvis about this finding" and "(id common_ground:5V)" in k.lines[1]
    assert build_focus(conn, REPO, Node("jarvis", "core", "jarvis", "jarvis"), None) is None
    assert build_focus(conn, REPO, Node("issue:999", "issue", "#999", "gone"), None) is None


def test_build_context_puts_the_selection_first_and_always_keeps_it(conn, tmp_path):
    cfg = Config(repo=REPO, db_path=tmp_path / "j.db", hardware_path=tmp_path / "none.yaml")
    g = graph()
    f = build_focus(conn, REPO, g.node("issue:12"), g)
    ctx = chat.build_context(conn, cfg, "what should I do?", focus=f.lines, budget_tokens=200)
    assert chat.FOCUS_TITLE in ctx.text                     # kept even with a tiny budget
    assert ctx.text.index("PROJECT:") < ctx.text.index(chat.FOCUS_TITLE) < \
        ctx.text.index("FACTS")
    plain = chat.build_context(conn, cfg, "what should I do?")
    assert chat.FOCUS_TITLE not in plain.text                # nothing selected: unchanged
