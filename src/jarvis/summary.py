"""Plain-text status summary rendered from the local database."""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from jarvis.config import Config


def _d(ts: str | None) -> str:
    return ts[:10] if ts else "?"


def _local(ts: str | None) -> str:
    """ISO timestamp -> local 'YYYY-MM-DD HH:MM'."""
    if not ts:
        return "never"
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def render_summary(
    conn: sqlite3.Connection, days: int = 7, repo: str | None = None, now: datetime | None = None
) -> str:
    repo = repo or Config.from_env().repo
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=days)).isoformat(timespec="seconds")
    out: list[str] = []
    last = conn.execute("SELECT MAX(ran_at) FROM sync_log").fetchone()[0]
    out.append(f"Jarvis summary: {repo}")
    out.append(f"Last sync: {_local(last)}")

    out += ["", "Open milestones"]
    ms = conn.execute(
        "SELECT * FROM milestones WHERE state = 'open' ORDER BY due_on IS NULL, due_on, number"
    ).fetchall()
    for m in ms:
        closed, opened = m["closed_issues"] or 0, m["open_issues"] or 0
        due = _d(m["due_on"]) if m["due_on"] else "no due date"
        out.append(f"  {m['title']}: {closed}/{closed + opened} closed, due {due}")
    if not ms:
        out.append("  none")

    out += ["", "Open issues"]
    issues = conn.execute(
        "SELECT * FROM issues WHERE state = 'open' ORDER BY number"
    ).fetchall()

    def line(i: sqlite3.Row) -> str:
        labels = json.loads(i["labels"] or "[]")
        return f"    #{i['number']} {i['title']}" + (f" [{', '.join(labels)}]" if labels else "")

    shown: set[int] = set()
    for m in ms:
        group = [i for i in issues if i["milestone_number"] == m["number"]]
        if group:
            out.append(f"  {m['title']}")
            out += [line(i) for i in group]
            shown.update(i["number"] for i in group)
    rest = [i for i in issues if i["number"] not in shown]
    if rest:
        out.append("  No milestone")
        out += [line(i) for i in rest]
    if not issues:
        out.append("  none")

    out += ["", f"Closed in the last {days} days"]
    closed_rows = conn.execute(
        "SELECT * FROM issues WHERE state = 'closed' AND closed_at >= ? ORDER BY closed_at DESC",
        (cutoff,),
    ).fetchall()
    out += [f"  #{i['number']} {i['title']} ({_d(i['closed_at'])})" for i in closed_rows]
    if not closed_rows:
        out.append("  none")

    out += ["", f"Commits in the last {days} days"]
    commits = conn.execute(
        "SELECT * FROM commits WHERE committed_at >= ? ORDER BY committed_at DESC", (cutoff,)
    ).fetchall()
    for c in commits:
        first = (c["message"] or "").splitlines()[0] if c["message"] else ""
        out.append(f"  {c['sha'][:7]} {first} ({_d(c['committed_at'])})")
    if not commits:
        out.append("  none")

    out += ["", "hardware.yaml"]
    hw = conn.execute("SELECT fetched_at FROM repo_files WHERE path = 'hardware.yaml'").fetchone()
    if hw:
        out.append(f"  present, fetched {_d(hw['fetched_at'])}")
    else:
        out.append("  not in repo yet — add it before phase 2")
    return "\n".join(out) + "\n"
