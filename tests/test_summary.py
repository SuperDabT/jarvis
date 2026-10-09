from __future__ import annotations

from datetime import UTC, datetime

from jarvis import db
from jarvis.summary import render_summary

NOW = datetime(2026, 10, 8, tzinfo=UTC)


def seed(conn):
    db.upsert_milestone(conn, {"number": 1, "title": "M1", "state": "open", "open_issues": 1,
                               "closed_issues": 3, "due_on": "2026-11-01T00:00:00Z"})
    db.upsert_issue(conn, {"number": 5, "title": "Servo jitter", "state": "open",
                           "labels": ["servo"], "milestone_number": 1})
    db.upsert_issue(conn, {"number": 6, "title": "Loose", "state": "open", "labels": []})
    db.upsert_issue(conn, {"number": 7, "title": "Done recently", "state": "closed",
                           "closed_at": "2026-10-05T00:00:00Z"})
    db.upsert_issue(conn, {"number": 8, "title": "Done long ago", "state": "closed",
                           "closed_at": "2026-01-05T00:00:00Z"})
    db.upsert_commit(conn, {"sha": "abcdef123456", "message": "Fix PWM\n\ndetails",
                            "committed_at": "2026-10-06T10:00:00Z"})
    db.upsert_commit(conn, {"sha": "oldoldold", "message": "ancient",
                            "committed_at": "2026-01-01T00:00:00Z"})
    db.log_sync(conn, "issues", 200, True)


def test_render_seeded():
    conn = db.connect(":memory:")
    seed(conn)
    text = render_summary(conn, days=7, repo="o/r", now=NOW)
    assert "o/r" in text and "Last sync: 20" in text
    assert "M1: 3/4 closed, due 2026-11-01" in text
    assert "#5 Servo jitter [servo]" in text
    assert "No milestone" in text and "#6 Loose" in text
    assert "#7 Done recently" in text and "Done long ago" not in text
    assert "abcdef1 Fix PWM (2026-10-06)" in text and "ancient" not in text
    assert "not in repo yet — add it before phase 2" in text


def test_hardware_present_and_empty_db():
    conn = db.connect(":memory:")
    assert "Last sync: never" in render_summary(conn, repo="o/r", now=NOW)
    db.upsert_repo_file(conn, "hardware.yaml", "s", "x: 1")
    assert "present, fetched" in render_summary(conn, repo="o/r", now=NOW)
