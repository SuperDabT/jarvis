from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
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
"""

# Columns added after a table first shipped: (table, column, type). Applied with
# ALTER TABLE ADD COLUMN when missing, so an existing database migrates in place.
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
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


def connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        path = Path(path).expanduser()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.commit()
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Add any MIGRATIONS column that is missing. Idempotent."""
    for table, column, type_ in MIGRATIONS:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_}")
            except sqlite3.OperationalError as e:  # another process added it meanwhile
                if "duplicate column" not in str(e).lower():
                    raise


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
