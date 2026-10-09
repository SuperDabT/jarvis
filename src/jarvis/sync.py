"""Pull GitHub data into SQLite. Each resource is written in one transaction."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any
from collections.abc import Callable

from jarvis import db
from jarvis.github import FetchResult, GitHubError, decode_file_content

HARDWARE_PATH = "hardware.yaml"
HISTORY_DAYS = 183  # how far back the commit history goes (the window's 26-week grid)
HISTORY_KEY = "commits_since"  # sync_state: oldest date the stored history covers
# An incremental fetch starts this long before the newest stored commit, so a
# commit whose committer date is a little older (rebase, clock skew) is not missed.
INCREMENTAL_MARGIN = timedelta(days=1)


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
        # A file deleted from the repo must not leave a stale copy behind.
        conn.execute("DELETE FROM repo_files WHERE path = ?", (name,))
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


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _newest_commit(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute("SELECT MAX(committed_at) FROM commits").fetchone()
    if not row or not row[0]:
        return None
    try:
        dt = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def history_since(conn: sqlite3.Connection, first_page: str, newest_before: datetime | None,
                  now: datetime) -> tuple[str, str] | None:
    """(mode, since) for the commit history fetch this sync needs, or None.

    - "backfill": the stored history does not reach back HISTORY_DAYS yet (first
      run, or the marker is missing): fetch everything since then, once.
    - "incremental": the first page of commits changed, so new commits arrived;
      fetch since the newest commit stored before this sync, all pages, so a push
      of more than one page of commits leaves no gap.
    An unchanged first page (a 304) costs nothing more.
    """
    want = now - timedelta(days=HISTORY_DAYS)
    covered = db.get_sync_state(conn, HISTORY_KEY)
    try:
        covered_dt = datetime.fromisoformat(covered.replace("Z", "+00:00")) if covered else None
    except ValueError:
        covered_dt = None
    if covered_dt is None or covered_dt > want + timedelta(days=1):
        return "backfill", _iso(want)
    if first_page == "changed" and newest_before is not None:
        return "incremental", _iso(newest_before - INCREMENTAL_MARGIN)
    return None


def _sync_history(conn: sqlite3.Connection, client: Any, first_page: str,
                  newest_before: datetime | None, now: datetime) -> str:
    """Fetch older/missed commits; returns the merged outcome for "commits"."""
    plan = history_since(conn, first_page, newest_before, now)
    if plan is None:
        return first_page
    mode, since = plan
    try:
        result = client.commit_history(since)
    except GitHubError as e:
        # The newest page is stored either way; history is retried next sync.
        return first_page if first_page == "changed" else f"error: history: {e}"
    known = {r[0] for r in conn.execute("SELECT sha FROM commits")}
    try:
        _write_commits(conn, result.data or [])
        if mode == "backfill":
            db.set_sync_state(conn, HISTORY_KEY, since)  # commits
        else:
            conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return first_page if first_page == "changed" else f"error: history: {e}"
    added = any(item.get("sha") not in known for item in result.data or [])
    return "changed" if added or first_page == "changed" else first_page


def sync(conn: sqlite3.Connection, client: Any, now: datetime | None = None) -> dict[str, str]:
    """Sync every resource; returns resource -> "changed" | "unchanged" | "missing" | "error: ...".

    Commits: the newest page is a conditional request (a 304 when nothing moved).
    A client with ``commit_history`` also keeps HISTORY_DAYS of history: one
    backfill on the first run, then an incremental fetch only when the newest
    page changed. New commits from either make the outcome "changed".
    """
    now = now or datetime.now(UTC)
    newest_before = _newest_commit(conn)
    out: dict[str, str] = {}
    out["issues"] = _sync_one(conn, client, "issues", client.issues, _write_issues)
    out["milestones"] = _sync_one(conn, client, "milestones", client.milestones,
                                  _write_milestones)
    commits = _sync_one(conn, client, "commits", client.commits, _write_commits)
    if hasattr(client, "commit_history") and not commits.startswith("error"):
        commits = _sync_history(conn, client, commits, newest_before, now)
    out["commits"] = commits
    out[HARDWARE_PATH] = _sync_one(conn, client, HARDWARE_PATH,
                                   lambda: client.file(HARDWARE_PATH), _write_hardware)
    return out
