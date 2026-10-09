"""gui/data.py: view models loaded from SQLite, no Qt needed."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from jarvis import cards, db
from jarvis.gui import data

REPO = "me/robot"
NOW = datetime(2026, 10, 8, 16, 0, tzinfo=UTC)

EVIDENCE = {
    "milestone_deadline:1": ["milestone:Core demo"],
    "maybe_done:28": ["#28", "commit:b455d42"],
    "adjusted_progress:1": ["milestone:Core demo", "#28"],
}


def seed(conn, *, briefing: bool = True, n_cards: int = 2) -> list[int]:
    """A briefing (one certain, one uncertain sentence) and ``n_cards`` cards."""
    db.upsert_milestone(conn, {"number": 1, "title": "Core demo", "state": "open",
                               "html_url": f"https://github.com/{REPO}/milestone/1"})
    if briefing:
        sentences = [
            {"text": "Core demo is due Oct 18, with 0 of 9 issues closed.",
             "fact_ids": ["milestone_deadline:1"], "uncertain": False},
            {"text": "#28 may already be done, since commit b455d42 looks like the same work.",
             "fact_ids": ["maybe_done:28", "adjusted_progress:1"], "uncertain": True},
        ]
        db.insert_briefing(conn, "h1", "Old headline", "[]", "template",
                           "2026-10-07T10:00:00+00:00")
        db.insert_briefing(conn, "h2", "Core demo due Oct 18, 0 of 9 closed",
                           json.dumps(sentences), "model", "2026-10-08T15:30:00+00:00",
                           "gemma4:26b")
    ids = []
    specs = [
        ("electrical", "Power check: 5 V rail rating unknown, worst case 1.55 A",
         "Two servos and the LiDAR share the 5 V rail.", "#12",
         f"https://github.com/{REPO}/issues/12", "rail_5v"),
        ("research", "Paper: sparse LiDAR scan matching", "Relates to #30.", "#30",
         None, None),
        ("electrical", "Logic level: TF-Luna TX into a 3.3 V pin", "UART levels differ.",
         "hardware.yaml", None, "uart_level"),
    ]
    for type_, title, why, ref, url, check in specs[:n_cards]:
        cid, _ = cards.upsert_card(conn, type_=type_, title=title, why=why, source_ref=ref,
                                   source_url=url, claude_prompt=f"Prompt for {title}",
                                   check_id=check, paper_id=None if check else "2410.00001")
        ids.append(cid)
    return ids


def test_latest_briefing_with_uncertain_flags_and_links():
    conn = db.connect(":memory:")
    seed(conn)
    b = data.load_briefing(conn, REPO, evidence=EVIDENCE)
    assert b is not None
    assert b.headline == "Core demo due Oct 18, 0 of 9 closed"
    assert b.source == "model" and b.source_label == "gemma4:26b"
    assert [s.uncertain for s in b.sentences] == [False, True]
    first, second = b.sentences
    assert first.links == (data.EvidenceLink(
        "Core demo milestone", f"https://github.com/{REPO}/milestone/1"),)
    labels = [lk.label for lk in second.links]
    # issues first, then commits, then milestones; duplicates (#28 twice) collapsed
    assert labels == ["#28", "b455d42", "Core demo milestone"]
    assert second.links[0].url == f"https://github.com/{REPO}/issues/28"
    assert second.links[1].url == f"https://github.com/{REPO}/commit/b455d42"


def test_template_source_label_and_unknown_facts_give_no_links():
    conn = db.connect(":memory:")
    db.insert_briefing(conn, "h", "Status", json.dumps(
        [{"text": "Something.", "fact_ids": ["gone:1"], "uncertain": False}]),
        "template", "2026-10-08T15:30:00+00:00")
    b = data.load_briefing(conn, REPO, evidence={})
    assert b.source_label == "template"
    assert b.sentences[0].links == ()


def test_briefing_links_from_real_facts():
    """Without an explicit map, evidence comes from facts.compute_facts."""
    conn = db.connect(":memory:")
    due = (datetime.now(UTC) + timedelta(days=5)).strftime("%Y-%m-%dT07:00:00Z")
    db.upsert_milestone(conn, {"number": 1, "title": "Core demo", "state": "open",
                               "due_on": due, "open_issues": 3, "closed_issues": 1,
                               "html_url": f"https://github.com/{REPO}/milestone/1"})
    db.insert_briefing(conn, "h", "Core demo soon", json.dumps(
        [{"text": "Core demo is due soon.", "fact_ids": ["milestone_deadline:1"],
          "uncertain": False}]), "template", "2026-10-08T15:30:00+00:00")
    b = data.load_briefing(conn, REPO)
    assert [lk.url for lk in b.sentences[0].links] == [f"https://github.com/{REPO}/milestone/1"]


def test_cards_grouped_by_type_in_fixed_order():
    conn = db.connect(":memory:")
    seed(conn, n_cards=3)
    groups = data.load_card_groups(conn)
    assert [(g.type, g.label) for g in groups] == [("electrical", "Electrical"),
                                                   ("research", "Research")]
    assert len(groups[0].cards) == 2 and len(groups[1].cards) == 1
    assert all(cv.is_new for g in groups for cv in g.cards)  # just created
    later = datetime.now(UTC) + timedelta(days=3)
    assert not any(cv.is_new for g in data.load_card_groups(conn, now=later) for cv in g.cards)


def test_done_cards_are_not_listed():
    conn = db.connect(":memory:")
    ids = seed(conn, n_cards=2)
    cards.mark_done(conn, ids[0])
    groups = data.load_card_groups(conn)
    assert [g.type for g in groups] == ["research"]


def test_empty_db():
    conn = db.connect(":memory:")
    wd = data.load_window_data(conn, REPO)
    assert wd.briefing is None
    assert wd.groups == ()
    assert wd.card_count == 0
    assert wd.last_sync is None and wd.updated_at is None
    assert wd.errors == ()


def test_window_data_updated_at_is_newest_of_sync_and_briefing():
    conn = db.connect(":memory:")
    seed(conn)
    conn.execute("INSERT INTO sync_log (ran_at, resource, status, changed) "
                 "VALUES ('2026-10-08T15:45:00+00:00', 'issues', 304, 0)")
    wd = data.load_window_data(conn, REPO)
    assert wd.updated_at == "2026-10-08T15:45:00+00:00"
    assert wd.card_count == 2
    # equal when nothing changed, so the window can skip re-rendering
    assert wd == data.load_window_data(conn, REPO)


def test_local_stamp():
    now = datetime.now(UTC)
    assert data.local_stamp(now.isoformat(), now) == now.astimezone().strftime("%H:%M")
    old = now - timedelta(days=3)
    assert data.local_stamp(old.isoformat(), now).startswith(old.astimezone().strftime("%b"))
    assert data.local_stamp(None) == ""


def _result(sync=None, reason=None, new=0, resolved=0, notes=()):
    return SimpleNamespace(sync=sync or {"issues": "unchanged"},
                           briefing=SimpleNamespace(fallback_reason=reason),
                           cards_new=new, cards_resolved=resolved, notes=list(notes))


def test_refresh_message():
    level, text = data.refresh_message(_result(new=2, resolved=1))
    assert level == "ok" and "2 new suggestions" in text and "1 resolved" in text
    level, text = data.refresh_message(_result(reason="model unavailable: cannot reach"))
    assert level == "warn" and text == "Ollama unavailable, showing template briefing"
    level, text = data.refresh_message(_result(sync={"issues": "error: GitHub 401: Bad"}))
    assert level == "warn" and "issues" in text
    level, text = data.refresh_message(_result(reason="model off", notes=["hardware.yaml missing"]))
    assert level == "ok" and text.endswith("hardware.yaml missing")


def test_describe_error():
    import httpx

    assert "Could not reach GitHub" in data.describe_error(httpx.ConnectError("boom"))
    assert data.describe_error(RuntimeError("x" * 300)).endswith("...")
