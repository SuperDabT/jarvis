"""Versioned schema migrations, backups and restore (jarvis.db).

Every test works on a temporary database. "v0" databases are built from
fixtures/schema_v0.sql, the frozen schema written before versioning existed.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from jarvis import db
from jarvis.lock import migration_lock

V0_SQL = (Path(__file__).parent / "fixtures" / "schema_v0.sql").read_text()


def make_v0(path: Path) -> None:
    """A pre-versioning database with rows in most tables (user_version 0)."""
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA journal_mode=WAL")
    raw.executescript(V0_SQL)
    raw.executescript("""
        INSERT INTO milestones (number, title, state, due_on) VALUES (1, 'Core demo', 'open', '2026-10-18');
        INSERT INTO issues (number, title, state, labels, milestone_number, body)
            VALUES (7, 'Track filter', 'open', '["phase-2"]', 1, 'body ü'), (28, 'Servo', 'closed', '[]', NULL, '');
        INSERT INTO commits (sha, message, author, committed_at) VALUES ('abc1234def', 'Fix #7', 'tal', '2026-10-01T10:00:00+00:00');
        INSERT INTO cards (type, title, why, fingerprint, status) VALUES ('electrical', 'Servo voltage', 'w', 'fp1', 'active'),
            ('research', 'Paper', 'w', 'fp2', 'done');
        INSERT INTO briefings (facts_hash, headline, sentences_json, source) VALUES ('h', 'Headline', '[]', 'template');
        INSERT INTO chat_messages (session_id, role, content, created_at) VALUES ('s', 'user', 'What is left?', 'now');
        INSERT INTO daemon_state (id, pid, research_summary) VALUES (1, 42, 'two papers');
        INSERT INTO research_papers (paper_id, title, embedding) VALUES ('2401.00001', 'Kalman', x'00ff10');
        INSERT INTO sync_state (key, value) VALUES ('commits_since', '2026-01-01');
    """)
    raw.commit()
    raw.close()


def dump(path: Path) -> tuple[dict, list]:
    """(every row of every table, the schema SQL) of a database file."""
    c = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = {t: c.execute(f'SELECT * FROM "{t}" ORDER BY rowid').fetchall()
                for t in db.table_counts(c)}
        schema = c.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
        return rows, schema
    finally:
        c.close()


def version_of(path: Path) -> int:
    c = sqlite3.connect(path)
    try:
        return c.execute("PRAGMA user_version").fetchone()[0]
    finally:
        c.close()


def backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.bak-*"))


# ---------------------------------------------------------------- upgrade


def test_fresh_file_gets_the_latest_schema_and_no_backup(tmp_path):
    path = tmp_path / "jarvis.db"
    conn = db.connect(path)
    assert db.schema_version(conn) == db.latest_version() >= 1
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert {"issues", "cards", "chat_messages", "research_papers"} <= set(db.table_counts(conn))
    assert "research_summary" in db._columns(conn, "daemon_state")
    assert backups(path) == []


def test_memory_database_is_upgraded_without_backup():
    conn = db.connect(":memory:")
    assert db.schema_version(conn) == db.latest_version()
    db.log_sync(conn, "issues", 200, True)


def test_v0_database_upgrades_with_every_row_kept(tmp_path):
    path = tmp_path / "jarvis.db"
    make_v0(path)
    before, _ = dump(path)
    conn = db.connect(path)
    assert db.schema_version(conn) == db.latest_version()
    conn.close()
    after, _ = dump(path)
    assert {t: after[t] for t in before} == before
    assert before["issues"] and before["research_papers"][0][-2] == b"\x00\xff\x10"


def test_upgrade_makes_a_verified_backup_of_the_old_database(tmp_path):
    path = tmp_path / "jarvis.db"
    make_v0(path)
    before, schema_before = dump(path)
    db.connect(path).close()
    [bak] = backups(path)
    assert bak.name.startswith("jarvis.db.bak-v0-")
    assert bak.stat().st_mode & 0o777 == 0o600
    assert not Path(f"{bak}-wal").exists()  # one self-contained file
    assert version_of(bak) == 0
    assert dump(bak) == (before, schema_before)
    assert db.verify_backup(bak) == {t: len(r) for t, r in before.items()}


def test_second_connect_changes_nothing(tmp_path):
    path = tmp_path / "jarvis.db"
    make_v0(path)
    db.connect(path).close()
    state = dump(path)
    made = backups(path)
    for _ in range(2):
        db.connect(path).close()
    assert dump(path) == state
    assert backups(path) == made


def test_old_database_without_research_columns_still_migrates(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(V0_SQL.split("ALTER TABLE")[0])  # before the research columns
    raw.execute("INSERT INTO daemon_state (id, pid) VALUES (1, 42)")
    raw.commit()
    raw.close()
    conn = db.connect(path)
    assert {"last_research_at", "next_research_at", "research_summary"} <= db._columns(
        conn, "daemon_state")
    assert db.get_daemon_state(conn)["pid"] == 42


# ---------------------------------------------------------------- failure


def _boom(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE half_done (x)")
    conn.execute("ALTER TABLE issues ADD COLUMN half TEXT")
    conn.execute("INSERT INTO issues (number, title) VALUES (999, 'half')")
    raise sqlite3.OperationalError("simulated failure")


def test_failed_step_rolls_back_everything(tmp_path, monkeypatch):
    path = tmp_path / "jarvis.db"
    make_v0(path)
    state = dump(path)
    monkeypatch.setattr(db, "MIGRATIONS", (*db.MIGRATIONS, db.Migration(2, "boom", _boom)))
    with pytest.raises(db.MigrationError) as e:
        db.connect(path)
    msg = str(e.value)
    assert "step 2 (boom)" in msg and "left at version 0" in msg and "simulated" in msg
    # step 1 was rolled back too: the file is exactly as it was
    assert version_of(path) == 0 and dump(path) == state
    [bak] = backups(path)
    assert dump(bak) == state


def test_failure_from_an_upgraded_database_keeps_its_version(tmp_path, monkeypatch):
    path = tmp_path / "jarvis.db"
    db.connect(path).close()
    monkeypatch.setattr(db, "MIGRATIONS", (*db.MIGRATIONS, db.Migration(2, "boom", _boom)))
    with pytest.raises(db.MigrationError, match="left at version 1"):
        db.connect(path)
    assert version_of(path) == 1
    c = sqlite3.connect(path)
    assert "half" not in {r[1] for r in c.execute("PRAGMA table_info(issues)")}
    assert c.execute("SELECT name FROM sqlite_master WHERE name = 'half_done'").fetchone() is None
    c.close()


def test_newer_database_is_refused_and_untouched(tmp_path):
    path = tmp_path / "jarvis.db"
    make_v0(path)
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA user_version = 99")
    raw.close()
    state = dump(path)
    with pytest.raises(db.MigrationError, match="newer than this Jarvis"):
        db.connect(path)
    assert version_of(path) == 99 and dump(path) == state and backups(path) == []


def test_steps_must_be_numbered_in_order(monkeypatch):
    monkeypatch.setattr(db, "MIGRATIONS", (db.MIGRATIONS[0], db.Migration(3, "gap", _boom)))
    with pytest.raises(db.MigrationError, match="numbered"):
        db.connect(":memory:")


def test_lock_timeout_leaves_the_database_alone(tmp_path):
    path = tmp_path / "jarvis.db"
    make_v0(path)
    raw = sqlite3.connect(path)
    with migration_lock(path), pytest.raises(db.MigrationError, match="upgrading"):
        db.migrate(raw, path, lock_timeout=0.2)
    raw.close()
    assert version_of(path) == 0 and backups(path) == []


def test_writes_between_backup_and_upgrade_force_a_fresh_backup(tmp_path, monkeypatch):
    path = tmp_path / "jarvis.db"
    make_v0(path)
    real = db.verify_backup
    calls = []

    def stale_first(p):
        counts = real(p)
        calls.append(p)
        return {**counts, "issues": counts["issues"] + 1} if len(calls) == 1 else counts

    monkeypatch.setattr(db, "verify_backup", stale_first)
    db.connect(path).close()
    assert len(calls) == 2  # the stale copy was replaced, not kept beside the new one
    assert backups(path) == [calls[1]] and version_of(path) == db.latest_version()
    assert db.verify_backup(calls[1])["issues"] == 2


def test_a_database_that_never_settles_is_not_upgraded(tmp_path, monkeypatch):
    path = tmp_path / "jarvis.db"
    make_v0(path)
    monkeypatch.setattr(db, "verify_backup", lambda p: {"never": -1})
    with pytest.raises(db.MigrationError, match="kept changing"):
        db.connect(path)
    assert version_of(path) == 0 and backups(path) == []


# ---------------------------------------------------------------- backups / restore


def test_restore_puts_the_old_database_back(tmp_path):
    path = tmp_path / "jarvis.db"
    make_v0(path)
    original = dump(path)
    conn = db.connect(path)
    [bak] = backups(path)
    conn.execute("DELETE FROM issues")
    conn.execute("INSERT INTO cards (type, title, fingerprint) VALUES ('electrical', 'x', 'fp9')")
    conn.commit()
    conn.close()
    counts = db.restore_backup(bak, path)
    assert dump(path) == original and version_of(path) == 0
    assert counts["issues"] == 2
    # and it upgrades again, with a new backup, keeping the rows
    db.connect(path).close()
    assert version_of(path) == db.latest_version() and len(backups(path)) == 2
    assert dump(path)[0]["issues"] == original[0]["issues"]


def test_verify_backup_rejects_missing_and_damaged_files(tmp_path):
    with pytest.raises(db.MigrationError, match="does not exist"):
        db.verify_backup(tmp_path / "nope.bak")
    junk = tmp_path / "junk.bak"
    junk.write_bytes(b"not a database" * 400)
    with pytest.raises(db.MigrationError, match="not a readable database"):
        db.verify_backup(junk)
    path = tmp_path / "jarvis.db"
    make_v0(path)
    with pytest.raises(db.MigrationError):
        db.restore_backup(junk, path)
    assert version_of(path) == 0 and dump(path)[0]["issues"]


def test_statements_split_the_baseline():
    stmts = db._statements(db.BASELINE_SCHEMA)
    assert len(stmts) == db.BASELINE_SCHEMA.count("CREATE ")
    assert db._statements("-- only a comment\n") == []
    with pytest.raises(ValueError, match="unterminated"):
        db._statements("CREATE TABLE x (a")


# ---------------------------------------------------------------- concurrency

WORKER = """
import sys
from jarvis import db
conn = db.connect(sys.argv[1])
print(db.schema_version(conn), conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0])
conn.close()
"""

WRITER = """
import sqlite3, sys, time
c = sqlite3.connect(sys.argv[1], timeout=30)
for i in range(int(sys.argv[2])):
    c.execute("INSERT INTO sync_log (ran_at, resource, status, changed) VALUES ('t', 'w', 200, 0)")
    c.commit()
    time.sleep(0.005)
c.close()
"""


def test_concurrent_processes_migrate_once_without_damage(tmp_path):
    """Six processes open a v0 database at the same moment while another process
    keeps writing to it: one upgrades it, the others wait and find it done."""
    path = tmp_path / "jarvis.db"
    make_v0(path)
    writes = 250  # ~1.5 s: still writing while the migration runs
    with migration_lock(path):  # hold everyone at the lock, then let them race
        writer = subprocess.Popen([sys.executable, "-c", WRITER, str(path), str(writes)])
        procs = [subprocess.Popen([sys.executable, "-c", WORKER, str(path)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for _ in range(6)]
        time.sleep(0.5)
    outs = [p.communicate(timeout=60) for p in procs]
    assert writer.wait(timeout=60) == 0
    for (out, err), p in zip(outs, procs, strict=True):
        assert p.returncode == 0, err
        assert out.split() == [str(db.latest_version()), "2"]
    assert len(backups(path)) == 1
    c = sqlite3.connect(path)
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert c.execute("SELECT COUNT(*) FROM sync_log WHERE resource = 'w'").fetchone()[0] == writes
    assert c.execute("PRAGMA user_version").fetchone()[0] == db.latest_version()
    c.close()
