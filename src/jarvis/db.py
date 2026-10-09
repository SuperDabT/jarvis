"""The SQLite database: schema, versioned migrations, backups and small helpers.

Schema versions
---------------
The schema version is SQLite's ``PRAGMA user_version``. ``MIGRATIONS`` is the
ordered list of steps; step N takes a version N-1 database to version N.
Version 0 is a database made before versioning (it may already have every
table); step 1 brings it, or an empty file, to the baseline.

``connect()`` upgrades a database that is behind (``migrate``):

1. Take ``<db>.migrate.lock`` (waits, so two processes never migrate at once;
   the second one finds the work done).
2. If the file already has tables, copy it with SQLite's backup API to
   ``<db>.bak-v<N>-<time>`` and verify the copy (integrity check and the same
   row count in every table) before touching anything.
3. Run every pending step and set user_version in ONE ``BEGIN IMMEDIATE``
   transaction. SQLite DDL is transactional, so a failing step rolls back to
   the old version with nothing half-applied, and ``MigrationError`` is raised.

A database at the current version costs one PRAGMA read on connect. A database
newer than this code (an older Jarvis checkout) is refused, not touched.
``restore_backup`` puts a backup back.

Never edit a released step (or ``BASELINE_SCHEMA`` / ``LIFE_OS_SCHEMA``); add a
new one.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jarvis.lock import MigrationLockTimeout, migration_lock

# Version 1: every table up to the brain UI (phases 1-7). Frozen.
BASELINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS etags (
    url TEXT PRIMARY KEY,
    etag TEXT,
    last_modified TEXT,
    fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS issues (
    number INTEGER PRIMARY KEY,
    title TEXT,
    state TEXT,
    labels TEXT,
    milestone_number INTEGER,
    body TEXT,
    html_url TEXT,
    created_at TEXT,
    updated_at TEXT,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS milestones (
    number INTEGER PRIMARY KEY,
    title TEXT,
    state TEXT,
    description TEXT,
    due_on TEXT,
    open_issues INTEGER,
    closed_issues INTEGER,
    html_url TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS commits (
    sha TEXT PRIMARY KEY,
    message TEXT,
    author TEXT,
    committed_at TEXT,
    html_url TEXT
);

CREATE TABLE IF NOT EXISTS repo_files (
    path TEXT PRIMARY KEY,
    sha TEXT,
    content TEXT,
    fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at TEXT,
    resource TEXT,
    status INTEGER,
    changed INTEGER
);

CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT CHECK (type IN ('electrical', 'research')),
    title TEXT,
    why TEXT,
    source_url TEXT,
    source_ref TEXT,
    check_id TEXT,
    paper_id TEXT,
    claude_prompt TEXT,
    status TEXT CHECK (status IN ('active', 'done', 'auto_resolved')) DEFAULT 'active',
    fingerprint TEXT UNIQUE NOT NULL,
    feedback TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS briefings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    facts_hash TEXT NOT NULL,
    headline TEXT,
    sentences_json TEXT,
    source TEXT CHECK (source IN ('model', 'template')),
    model TEXT,
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS briefings_hash ON briefings (facts_hash, id);

-- Latest electrical findings (phase 4), one row per check_id, plus the cached
-- model wording for that exact finding (keyed by finding_hash).
CREATE TABLE IF NOT EXISTS electrical_findings (
    check_id TEXT PRIMARY KEY,
    finding_hash TEXT,
    status TEXT,
    title TEXT,
    why TEXT,
    numbers_json TEXT,
    model_hash TEXT,
    model_result TEXT,
    model_title TEXT,
    model_why TEXT,
    model_issue INTEGER,
    model TEXT,
    updated_at TEXT
);

-- Phase 5: the fast loop's heartbeat and change-detection state (one row, id = 1).
-- pid/started_at/interval_s/next_tick_at belong to a running `jarvis watch` and are
-- cleared when it exits; the hashes are shared by `jarvis watch --once`.
CREATE TABLE IF NOT EXISTS daemon_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    pid INTEGER,
    started_at TEXT,
    last_tick_at TEXT,
    last_change_at TEXT,
    last_error TEXT,
    interval_s INTEGER,
    next_tick_at TEXT,
    hardware_hash TEXT,
    facts_hash TEXT,
    model_pending INTEGER DEFAULT 0
);

-- Phase 6: the research loop.
-- Papers seen on arXiv (and their cached embedding for one embed model).
CREATE TABLE IF NOT EXISTS research_papers (
    paper_id TEXT PRIMARY KEY,
    title TEXT,
    authors_json TEXT,
    abstract TEXT,
    published TEXT,
    url TEXT,
    categories TEXT,
    embed_model TEXT,
    embedding BLOB,
    fetched_at TEXT
);

-- Every (paper, issue) pair the model judged, accepted or not, so it is never
-- judged twice. Model errors are not stored.
CREATE TABLE IF NOT EXISTS research_seen (
    paper_id TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    verdict TEXT,
    confidence TEXT,
    applies_to TEXT,
    why TEXT,
    reason TEXT,
    similarity REAL,
    judged_at TEXT,
    model TEXT,
    PRIMARY KEY (paper_id, issue_number)
);

-- Model-proposed search queries per issue, keyed by a hash of the issue text.
CREATE TABLE IF NOT EXISTS research_queries (
    issue_number INTEGER PRIMARY KEY,
    key TEXT,
    worthy INTEGER,
    reason TEXT,
    queries_json TEXT,
    model TEXT,
    created_at TEXT
);

-- arXiv answers per search string (paper ids in rank order), reused for a while so
-- re-runs do not ask arXiv again.
CREATE TABLE IF NOT EXISTS research_searches (
    query TEXT PRIMARY KEY,
    paper_ids TEXT,
    fetched_at TEXT
);

-- Library release baselines: the newest tag seen per repo.
CREATE TABLE IF NOT EXISTS research_releases (
    repo TEXT PRIMARY KEY,
    tag TEXT,
    url TEXT,
    source TEXT,
    baseline_at TEXT,
    updated_at TEXT
);
-- Chat: questions asked in the window and Jarvis's answers, per window session.
-- sources_json (assistant rows): {"sources": [...], "notes": [...], "unverified_numbers": [...]}.
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT CHECK (role IN ('user', 'assistant')),
    content TEXT,
    sources_json TEXT,
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS chat_messages_session ON chat_messages (session_id, id);

-- Small key/value state for sync bookkeeping, e.g. how far back the commit
-- history has been fetched ("commits_since").
CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);
"""

# Columns added after a table first shipped, before versioning: (table, column, type).
# Part of the baseline: step 1 adds any that a version 0 database is missing.
BASELINE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("daemon_state", "last_research_at", "TEXT"),
    ("daemon_state", "next_research_at", "TEXT"),
    ("daemon_state", "research_summary", "TEXT"),
)

DAEMON_STATE_COLUMNS = (
    "pid", "started_at", "last_tick_at", "last_change_at", "last_error", "interval_s",
    "next_tick_at", "hardware_hash", "facts_hash", "model_pending",
    "last_research_at", "next_research_at", "research_summary",
)

ISSUE_COLUMNS = (
    "number", "title", "state", "labels", "milestone_number", "body",
    "html_url", "created_at", "updated_at", "closed_at",
)
MILESTONE_COLUMNS = (
    "number", "title", "state", "description", "due_on", "open_issues",
    "closed_issues", "html_url", "updated_at",
)
COMMIT_COLUMNS = ("sha", "message", "author", "committed_at", "html_url")


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


MIGRATE_LOCK_TIMEOUT_S = 60.0
MIGRATE_BUSY_TIMEOUT_MS = 30_000  # how long BEGIN IMMEDIATE waits for other writers
BACKUP_ATTEMPTS = 3               # the source changed between backup and lock: retry


class MigrationError(RuntimeError):
    """The schema could not be upgraded (the database was left as it was), or a
    backup is missing or unreadable."""


@dataclass(frozen=True)
class Migration:
    version: int          # the version this step produces (steps are 1, 2, 3, ...)
    name: str
    apply: Callable[[sqlite3.Connection], None]  # runs inside the migration transaction


def _statements(script: str) -> list[str]:
    """A SQL script as single statements, so they can run inside one transaction
    (``executescript`` would commit first)."""
    out: list[str] = []
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf.strip())
            buf = ""
    rest = [ln for ln in buf.splitlines() if ln.strip() and not ln.strip().startswith("--")]
    if rest:
        raise ValueError(f"unterminated SQL statement: {buf.strip()[:80]!r}")
    return out


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _v1_baseline(conn: sqlite3.Connection) -> None:
    for stmt in _statements(BASELINE_SCHEMA):
        conn.execute(stmt)
    for table, column, type_ in BASELINE_COLUMNS:
        if column not in _columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_}")


# Version 2: the Life OS core (projects, items, links). Frozen.
#
# Times. Bookkeeping times (created_at, updated_at, completed_at, deleted_at) are
# UTC instants like every other table ("2026-10-09T22:12:04+00:00", ``now_iso``).
# Times the user gives (due_at, remind_at) are local wall-clock times, stored as
# typed: "YYYY-MM-DD" (due that day, no time) or "YYYY-MM-DDTHH:MM". They sort as
# text, and a date-only due sorts before the timed ones of that day.
#
# References in ``links``. A row says "src --kind--> dst". Both ends are typed:
#   item / project   the row's id as decimal text ("12"); must exist (trigger), and
#                    the link goes when the record is deleted (trigger).
#   issue            "owner/name#N", a GitHub issue ("SuperDabT/stm32-lidar-scanner#7")
#   commit           "owner/name@sha" with 7-40 lowercase hex characters
# External ends (issue, commit) are checked for format only, never copied and not
# required to exist locally: an issue may not be synced yet, and the issues table
# stays GitHub's mirror. Readers resolve them against the mirror when shown.
# Links cannot be updated (delete and re-add). Kinds: "about" (the source is
# about the target, e.g. a task about an issue) and "related".
LIFE_OS_SCHEMA = """
CREATE TABLE projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE
        CHECK (length(slug) BETWEEN 1 AND 40 AND slug NOT GLOB '*[^a-z0-9-]*'
               AND slug NOT GLOB '-*' AND slug NOT GLOB '*-'),
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    github_repo TEXT COLLATE NOCASE UNIQUE
        CHECK (github_repo IS NULL OR (github_repo GLOB '?*/?*'
               AND github_repo NOT GLOB '*/*/*'
               AND github_repo NOT GLOB '*[^A-Za-z0-9._/-]*')),
    description TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'archived')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('task', 'idea', 'reminder')),
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    body TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
    priority INTEGER NOT NULL DEFAULT 0 CHECK (priority BETWEEN 0 AND 3),
    due_at TEXT CHECK (due_at IS NULL
        OR (due_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(due_at) IS due_at)
        OR (due_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-2][0-9]:[0-5][0-9]'
            AND substr(due_at, 12, 2) < '24'
            AND strftime('%Y-%m-%dT%H:%M', due_at) IS due_at)),
    remind_at TEXT CHECK (remind_at IS NULL
        OR (remind_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-2][0-9]:[0-5][0-9]'
            AND substr(remind_at, 12, 2) < '24'
            AND strftime('%Y-%m-%dT%H:%M', remind_at) IS remind_at)),
    project_id INTEGER REFERENCES projects (id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    deleted_at TEXT,
    CHECK ((status = 'done') = (completed_at IS NOT NULL))
) STRICT;

CREATE INDEX items_project ON items (project_id);
CREATE INDEX items_open_due ON items (due_at) WHERE status = 'open' AND deleted_at IS NULL;
CREATE INDEX items_open_remind ON items (remind_at)
    WHERE remind_at IS NOT NULL AND status = 'open' AND deleted_at IS NULL;

CREATE TABLE links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src_type TEXT NOT NULL CHECK (src_type IN ('item', 'project')),
    src_id TEXT NOT NULL CHECK (src_id GLOB '[1-9]*' AND src_id NOT GLOB '*[^0-9]*'),
    dst_type TEXT NOT NULL CHECK (dst_type IN ('item', 'project', 'issue', 'commit')),
    dst_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('about', 'related')),
    created_at TEXT NOT NULL,
    UNIQUE (src_type, src_id, dst_type, dst_id, kind),
    CHECK (NOT (src_type = dst_type AND src_id = dst_id)),
    CHECK (CASE dst_type
        WHEN 'issue' THEN dst_id GLOB '?*/?*#[1-9]*'
            AND dst_id NOT GLOB '*/*/*' AND dst_id NOT GLOB '*#*#*'
            AND substr(dst_id, instr(dst_id, '#') + 1) NOT GLOB '*[^0-9]*'
            AND substr(dst_id, 1, instr(dst_id, '#') - 1) NOT GLOB '*[^A-Za-z0-9._/-]*'
        WHEN 'commit' THEN dst_id GLOB '?*/?*@*'
            AND dst_id NOT GLOB '*/*/*' AND dst_id NOT GLOB '*@*@*'
            AND length(substr(dst_id, instr(dst_id, '@') + 1)) BETWEEN 7 AND 40
            AND substr(dst_id, instr(dst_id, '@') + 1) NOT GLOB '*[^0-9a-f]*'
            AND substr(dst_id, 1, instr(dst_id, '@') - 1) NOT GLOB '*[^A-Za-z0-9._/-]*'
        ELSE dst_id GLOB '[1-9]*' AND dst_id NOT GLOB '*[^0-9]*'
    END)
) STRICT;

CREATE INDEX links_dst ON links (dst_type, dst_id);

CREATE TRIGGER links_ends_exist BEFORE INSERT ON links
BEGIN
    SELECT RAISE(ABORT, 'links: the source record does not exist')
    WHERE (NEW.src_type = 'item'
           AND NOT EXISTS (SELECT 1 FROM items WHERE id = CAST(NEW.src_id AS INTEGER)))
       OR (NEW.src_type = 'project'
           AND NOT EXISTS (SELECT 1 FROM projects WHERE id = CAST(NEW.src_id AS INTEGER)));
    SELECT RAISE(ABORT, 'links: the target record does not exist')
    WHERE (NEW.dst_type = 'item'
           AND NOT EXISTS (SELECT 1 FROM items WHERE id = CAST(NEW.dst_id AS INTEGER)))
       OR (NEW.dst_type = 'project'
           AND NOT EXISTS (SELECT 1 FROM projects WHERE id = CAST(NEW.dst_id AS INTEGER)));
END;

CREATE TRIGGER links_immutable BEFORE UPDATE ON links
BEGIN
    SELECT RAISE(ABORT, 'links cannot be changed; delete the link and add a new one');
END;

CREATE TRIGGER items_keep_id BEFORE UPDATE OF id ON items WHEN NEW.id IS NOT OLD.id
BEGIN
    SELECT RAISE(ABORT, 'items: the id cannot change');
END;

CREATE TRIGGER projects_keep_id BEFORE UPDATE OF id ON projects WHEN NEW.id IS NOT OLD.id
BEGIN
    SELECT RAISE(ABORT, 'projects: the id cannot change');
END;

CREATE TRIGGER items_drop_links AFTER DELETE ON items
BEGIN
    DELETE FROM links WHERE (src_type = 'item' AND src_id = CAST(OLD.id AS TEXT))
                         OR (dst_type = 'item' AND dst_id = CAST(OLD.id AS TEXT));
END;

CREATE TRIGGER projects_drop_links AFTER DELETE ON projects
BEGIN
    DELETE FROM links WHERE (src_type = 'project' AND src_id = CAST(OLD.id AS TEXT))
                         OR (dst_type = 'project' AND dst_id = CAST(OLD.id AS TEXT));
END;
"""

# The project Jarvis was built for, registered as the first project. A literal on
# purpose (not config.DEFAULT_REPO): a released step must not change.
FIRST_PROJECT = {"slug": "lidar", "name": "LiDAR Scanner",
                 "github_repo": "SuperDabT/stm32-lidar-scanner"}


def _v2_life_os_core(conn: sqlite3.Connection) -> None:
    for stmt in _statements(LIFE_OS_SCHEMA):
        conn.execute(stmt)
    now = now_iso()
    conn.execute(
        "INSERT INTO projects (slug, name, github_repo, created_at, updated_at) "
        "VALUES (:slug, :name, :github_repo, :now, :now) ON CONFLICT DO NOTHING",
        {**FIRST_PROJECT, "now": now})


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "baseline: GitHub mirror, cards, briefings, checks, daemon, research, chat",
              _v1_baseline),
    Migration(2, "Life OS core: projects, items, links", _v2_life_os_core),
)


def latest_version() -> int:
    return MIGRATIONS[-1].version if MIGRATIONS else 0


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Rows per table (SQLite's own tables left out)."""
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name")]
    return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}


def _check_steps() -> None:
    versions = [m.version for m in MIGRATIONS]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(f"migration steps must be numbered 1, 2, 3, ...: {versions}")


def _version_or_refuse(conn: sqlite3.Connection, target: int) -> int:
    v = schema_version(conn)
    if v > target:
        raise MigrationError(
            f"the database is schema version {v}, newer than this Jarvis knows ({target}); "
            "update Jarvis, or restore a backup made before the upgrade")
    return v


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the database (WAL, foreign keys on), upgrading its schema if it is
    behind. Raises ``MigrationError`` if an upgrade fails (nothing is changed)."""
    memory = str(path) == ":memory:"
    if not memory:
        path = Path(path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        migrate(conn, None if memory else path)
    except BaseException:
        conn.close()
        raise
    return conn


def migrate(conn: sqlite3.Connection, db_path: str | Path | None = None, *,
            lock_timeout: float = MIGRATE_LOCK_TIMEOUT_S) -> Path | None:
    """Bring the database to ``latest_version()`` (see the module docstring).

    Returns the verified backup's path when an existing database was upgraded,
    else None. ``db_path`` None (an in-memory database): no lock and no backup.
    """
    _check_steps()
    target = latest_version()
    if _version_or_refuse(conn, target) == target:
        return None
    if db_path is None:
        _apply(conn, target, None)
        return None
    db_path = Path(db_path)
    try:
        with migration_lock(db_path, lock_timeout):
            if _version_or_refuse(conn, target) == target:
                return None  # another process upgraded it while we waited
            for _ in range(BACKUP_ATTEMPTS):
                backup = counts = None
                if table_counts(conn):
                    backup = backup_database(conn, db_path)
                    counts = verify_backup(backup)
                if _apply(conn, target, counts):
                    return backup
                if backup is not None:  # our own copy, now stale
                    backup.unlink(missing_ok=True)
    except MigrationLockTimeout as e:
        raise MigrationError(str(e)) from e
    raise MigrationError("the database kept changing while it was being backed up; "
                         "nothing was upgraded, try again")


def _apply(conn: sqlite3.Connection, target: int, expect_counts: dict[str, int] | None) -> bool:
    """Run the pending steps and set user_version in one write transaction.

    False (nothing done) if the row counts no longer match ``expect_counts``, i.e.
    someone wrote between the backup and this transaction.
    """
    level = conn.isolation_level
    conn.commit()
    conn.isolation_level = None  # explicit BEGIN / COMMIT below
    busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    conn.execute(f"PRAGMA busy_timeout = {MIGRATE_BUSY_TIMEOUT_MS}")
    current = schema_version(conn)
    step: Migration | None = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = _version_or_refuse(conn, target)
        if expect_counts is not None and table_counts(conn) != expect_counts:
            conn.execute("ROLLBACK")
            return False
        for step in MIGRATIONS[current:]:
            step.apply(conn)
        conn.execute(f"PRAGMA user_version = {target}")
        conn.execute("COMMIT")
        return True
    except BaseException as e:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        if isinstance(e, MigrationError) or not isinstance(e, Exception):
            raise
        where = f" at step {step.version} ({step.name})" if step else ""
        raise MigrationError(f"schema upgrade from version {current} to {target} failed"
                             f"{where}: {e}. The database was left at version {current}."
                             ) from e
    finally:
        conn.execute(f"PRAGMA busy_timeout = {busy}")
        conn.isolation_level = level


# ---------------------------------------------------------------- backups


def backup_database(conn: sqlite3.Connection, db_path: str | Path,
                    now: datetime | None = None) -> Path:
    """Copy the database with SQLite's backup API to ``<db>.bak-v<N>-<UTC time>``
    (a single file, mode 600). Not verified here: see ``verify_backup``."""
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    base = f"{Path(db_path)}.bak-v{schema_version(conn)}-{stamp}"
    dest, n = Path(base), 1
    while dest.exists():
        dest, n = Path(f"{base}.{n}"), n + 1
    target = sqlite3.connect(dest)
    try:
        conn.backup(target)
        target.execute("PRAGMA journal_mode=DELETE")  # one self-contained file
    finally:
        target.close()
    dest.chmod(0o600)
    return dest


def verify_backup(path: str | Path) -> dict[str, int]:
    """Open a backup read-only, run SQLite's integrity check, and return its rows
    per table. Raises ``MigrationError`` if it is missing, unreadable or damaged."""
    path = Path(path)
    if not path.is_file():
        raise MigrationError(f"backup {path} does not exist")
    try:
        b = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            result = [r[0] for r in b.execute("PRAGMA integrity_check")]
            if result != ["ok"]:
                raise MigrationError(f"backup {path} failed SQLite's integrity check: "
                                     f"{'; '.join(result[:3])}")
            return table_counts(b)
        finally:
            b.close()
    except sqlite3.DatabaseError as e:
        raise MigrationError(f"backup {path} is not a readable database: {e}") from e


def restore_backup(backup: str | Path, db_path: str | Path, *,
                   lock_timeout: float = MIGRATE_LOCK_TIMEOUT_S) -> dict[str, int]:
    """Replace the database's contents with a verified backup's, and check that
    every table has the backup's row count. Returns those counts.

    Stop ``jarvis watch`` and close the window first. The restored database keeps
    the backup's schema version; the next ``connect`` upgrades it again (with a
    new backup).
    """
    expected = verify_backup(backup)
    db_path = Path(db_path).expanduser()
    try:
        with migration_lock(db_path, lock_timeout):
            src = sqlite3.connect(f"{Path(backup).resolve().as_uri()}?mode=ro", uri=True)
            dst = sqlite3.connect(db_path)
            try:
                src.backup(dst)
                dst.execute("PRAGMA journal_mode=WAL")
                got = table_counts(dst)
            finally:
                src.close()
                dst.close()
    except MigrationLockTimeout as e:
        raise MigrationError(str(e)) from e
    if got != expected:
        raise MigrationError(f"restore from {backup} does not match it: {got} != {expected}")
    return got


def _upsert(
    conn: sqlite3.Connection,
    table: str,
    key: str,
    columns: tuple[str, ...],
    row: dict[str, Any],
    commit: bool = True,
) -> None:
    values = {c: row.get(c) for c in columns}
    names = ", ".join(columns)
    marks = ", ".join(f":{c}" for c in columns)
    updates = ", ".join(f"{c}=excluded.{c}" for c in columns if c != key)
    conn.execute(
        f"INSERT INTO {table} ({names}) VALUES ({marks}) "
        f"ON CONFLICT({key}) DO UPDATE SET {updates}",
        values,
    )
    if commit:
        conn.commit()


def get_etag(conn: sqlite3.Connection, url: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM etags WHERE url = ?", (url,)).fetchone()


def set_etag(
    conn: sqlite3.Connection, url: str, etag: str | None, last_modified: str | None = None
) -> None:
    conn.execute(
        "INSERT INTO etags (url, etag, last_modified, fetched_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(url) DO UPDATE SET etag=excluded.etag, "
        "last_modified=excluded.last_modified, fetched_at=excluded.fetched_at",
        (url, etag, last_modified, now_iso()),
    )
    conn.commit()


def clear_etag(conn: sqlite3.Connection, url: str) -> None:
    conn.execute("DELETE FROM etags WHERE url = ?", (url,))
    conn.commit()


def upsert_issue(conn: sqlite3.Connection, issue: dict[str, Any], commit: bool = True) -> None:
    issue = {**issue, "labels": json.dumps(issue.get("labels") or [])}
    _upsert(conn, "issues", "number", ISSUE_COLUMNS, issue, commit)


def upsert_milestone(
    conn: sqlite3.Connection, milestone: dict[str, Any], commit: bool = True
) -> None:
    _upsert(conn, "milestones", "number", MILESTONE_COLUMNS, milestone, commit)


def upsert_commit(conn: sqlite3.Connection, commit_row: dict[str, Any], commit: bool = True) -> None:
    _upsert(conn, "commits", "sha", COMMIT_COLUMNS, commit_row, commit)


def upsert_repo_file(
    conn: sqlite3.Connection, path: str, sha: str, content: str, commit: bool = True
) -> None:
    _upsert(
        conn,
        "repo_files",
        "path",
        ("path", "sha", "content", "fetched_at"),
        {"path": path, "sha": sha, "content": content, "fetched_at": now_iso()},
        commit,
    )


def log_sync(conn: sqlite3.Connection, resource: str, status: int, changed: bool) -> None:
    conn.execute(
        "INSERT INTO sync_log (ran_at, resource, status, changed) VALUES (?, ?, ?, ?)",
        (now_iso(), resource, status, int(changed)),
    )
    conn.commit()


def latest_briefing(conn: sqlite3.Connection, facts_hash: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM briefings WHERE facts_hash = ? ORDER BY id DESC LIMIT 1", (facts_hash,)
    ).fetchone()


def insert_briefing(
    conn: sqlite3.Connection, facts_hash: str, headline: str, sentences_json: str,
    source: str, created_at: str, model: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO briefings (facts_hash, headline, sentences_json, source, model, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (facts_hash, headline, sentences_json, source, model, created_at),
    )
    conn.commit()
    return cur.lastrowid


def get_sync_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_sync_state(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    conn.execute(
        "INSERT INTO sync_state (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, now_iso()),
    )
    conn.commit()


def get_daemon_state(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM daemon_state WHERE id = 1").fetchone()


def update_daemon_state(conn: sqlite3.Connection, **fields: Any) -> None:
    """Set some columns of the single daemon_state row (created if missing)."""
    bad = set(fields) - set(DAEMON_STATE_COLUMNS)
    if bad:
        raise ValueError(f"unknown daemon_state columns: {sorted(bad)}")
    conn.execute("INSERT OR IGNORE INTO daemon_state (id) VALUES (1)")
    if fields:
        sets = ", ".join(f"{k} = :{k}" for k in fields)
        conn.execute(f"UPDATE daemon_state SET {sets} WHERE id = 1", fields)
    conn.commit()
