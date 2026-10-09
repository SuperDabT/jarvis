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
"""

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
    conn.commit()
    return conn


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
