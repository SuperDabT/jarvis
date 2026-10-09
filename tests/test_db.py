import sqlite3

import pytest

from jarvis import db


def test_wal_on_for_file_db(tmp_path):
    conn = db.connect(tmp_path / "sub" / "jarvis.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_schema_idempotent(tmp_path):
    path = tmp_path / "jarvis.db"
    db.connect(path).close()
    conn = db.connect(path)
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"etags", "issues", "milestones", "commits", "repo_files", "sync_log", "cards"} <= tables


def test_memory_db():
    conn = db.connect(":memory:")
    db.log_sync(conn, "issues", 304, False)
    row = conn.execute("SELECT * FROM sync_log").fetchone()
    assert (row["resource"], row["status"], row["changed"]) == ("issues", 304, 0)


def test_upsert_issue_replaces():
    conn = db.connect(":memory:")
    db.upsert_issue(conn, {"number": 1, "title": "old", "labels": ["servo"]})
    db.upsert_issue(conn, {"number": 1, "title": "new", "labels": ["servo", "bug"]})
    rows = conn.execute("SELECT * FROM issues").fetchall()
    assert len(rows) == 1
    assert rows[0]["title"] == "new"
    assert rows[0]["labels"] == '["servo", "bug"]'


def test_upsert_other_tables_replace():
    conn = db.connect(":memory:")
    for title in ("a", "b"):
        db.upsert_milestone(conn, {"number": 2, "title": title})
        db.upsert_commit(conn, {"sha": "abc", "message": title})
        db.upsert_repo_file(conn, "hardware.yaml", "s1", title)
    for table, col in (("milestones", "title"), ("commits", "message"), ("repo_files", "content")):
        rows = conn.execute(f"SELECT {col} FROM {table}").fetchall()
        assert [r[0] for r in rows] == ["b"]


def test_etag_round_trip():
    conn = db.connect(":memory:")
    assert db.get_etag(conn, "u") is None
    db.set_etag(conn, "u", '"abc"', "Mon")
    db.set_etag(conn, "u", '"def"', None)
    row = db.get_etag(conn, "u")
    assert row["etag"] == '"def"'
    assert row["last_modified"] is None
    assert row["fetched_at"]


def test_cards_fingerprint_unique_and_defaults():
    conn = db.connect(":memory:")
    sql = "INSERT INTO cards (type, title, fingerprint) VALUES ('electrical', 't', ?)"
    conn.execute(sql, ("fp1",))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, ("fp1",))
    assert conn.execute("SELECT status FROM cards").fetchone()["status"] == "active"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO cards (type, title, fingerprint) VALUES ('bad', 't', 'fp2')")
