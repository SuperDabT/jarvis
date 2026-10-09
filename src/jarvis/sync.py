"""Pull GitHub data into SQLite. Each resource is written in one transaction."""
from __future__ import annotations

import sqlite3
from typing import Any, Callable

from jarvis import db
from jarvis.github import FetchResult, GitHubError, decode_file_content

HARDWARE_PATH = "hardware.yaml"


def _issue_row(item: dict[str, Any]) -> dict[str, Any]:
    milestone = item.get("milestone") or {}
    return {
        "number": item["number"],
        "title": item.get("title"),
        "state": item.get("state"),
        "labels": [
            lb["name"] if isinstance(lb, dict) else str(lb) for lb in item.get("labels") or []
        ],
        "milestone_number": milestone.get("number"),
        "body": item.get("body"),
        "html_url": item.get("html_url"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "closed_at": item.get("closed_at"),
    }


def _commit_row(item: dict[str, Any]) -> dict[str, Any]:
    c = item.get("commit") or {}
    author = c.get("author") or {}
    committer = c.get("committer") or {}
    return {
        "sha": item["sha"],
        "message": c.get("message"),
        "author": author.get("name"),
        "committed_at": committer.get("date") or author.get("date"),
        "html_url": item.get("html_url"),
    }


def _write_issues(conn: sqlite3.Connection, data: Any) -> None:
    for item in data:
        if "pull_request" in item:
            continue
        db.upsert_issue(conn, _issue_row(item), commit=False)


def _write_milestones(conn: sqlite3.Connection, data: Any) -> None:
    for item in data:
        db.upsert_milestone(conn, {k: item.get(k) for k in db.MILESTONE_COLUMNS}, commit=False)


def _write_commits(conn: sqlite3.Connection, data: Any) -> None:
    for item in data:
        db.upsert_commit(conn, _commit_row(item), commit=False)


def _write_hardware(conn: sqlite3.Connection, data: Any) -> None:
    db.upsert_repo_file(
        conn, HARDWARE_PATH, data.get("sha"), decode_file_content(data), commit=False
    )


def _sync_one(
    conn: sqlite3.Connection,
    client: Any,
    name: str,
    fetch: Callable[[], FetchResult],
    write: Callable[[sqlite3.Connection, Any], None],
) -> str:
    try:
        result = fetch()
    except GitHubError as e:
        return f"error: {e}"
    if result.status == 404:
        db.log_sync(conn, name, 404, False)
        return "missing"
    if not result.changed:
        db.log_sync(conn, name, result.status, False)
        return "unchanged"
    try:
        write(conn, result.data)
        db.log_sync(conn, name, result.status, True)  # commits
    except Exception as e:  # noqa: BLE001 - any failure must invalidate the saved ETag
        conn.rollback()
        db.clear_etag(conn, client.resource_key(name))
        return f"error: {type(e).__name__}: {e}"
    return "changed"


def sync(conn: sqlite3.Connection, client: Any) -> dict[str, str]:
    steps = [
        ("issues", client.issues, _write_issues),
        ("milestones", client.milestones, _write_milestones),
        ("commits", client.commits, _write_commits),
        (HARDWARE_PATH, lambda: client.file(HARDWARE_PATH), _write_hardware),
    ]
    return {name: _sync_one(conn, client, name, fetch, write) for name, fetch, write in steps}
