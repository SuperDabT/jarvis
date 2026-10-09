from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from jarvis.db import now_iso

FEEDBACK_REASONS = ("vague", "wrong", "repeat", "other")


@dataclass(frozen=True)
class Card:
    id: int
    type: str
    title: str
    why: str
    source_url: str | None
    source_ref: str | None
    check_id: str | None
    paper_id: str | None
    claude_prompt: str
    status: str
    fingerprint: str
    feedback: str | None
    created_at: str
    updated_at: str


def fingerprint(type_: str, source_ref: str, key: str) -> str:
    """type + source + check_id (electrical) or paper_id (research)."""
    return f"{type_}|{source_ref}|{key}"


def _row_to_card(row: sqlite3.Row) -> Card:
    return Card(**{k: row[k] for k in row.keys()})


def list_cards(conn: sqlite3.Connection, status: str = "active") -> list[Card]:
    rows = conn.execute(
        "SELECT * FROM cards WHERE status = ? ORDER BY type, created_at DESC, id DESC",
        (status,),
    ).fetchall()
    return [_row_to_card(r) for r in rows]


def get_card(conn: sqlite3.Connection, card_id: int) -> Card | None:
    row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    return _row_to_card(row) if row else None


def mark_done(conn: sqlite3.Connection, card_id: int) -> None:
    conn.execute(
        "UPDATE cards SET status = 'done', updated_at = ? WHERE id = ?",
        (now_iso(), card_id),
    )
    conn.commit()


def set_feedback(conn: sqlite3.Connection, card_id: int, reason: str) -> None:
    """Record "not useful" with a reason; the card is closed like done."""
    if reason not in FEEDBACK_REASONS:
        raise ValueError(f"unknown feedback reason: {reason}")
    conn.execute(
        "UPDATE cards SET feedback = ?, status = 'done', updated_at = ? WHERE id = ?",
        (reason, now_iso(), card_id),
    )
    conn.commit()


def upsert_card(
    conn: sqlite3.Connection,
    *,
    type_: str,
    title: str,
    why: str,
    source_ref: str,
    source_url: str | None,
    claude_prompt: str,
    check_id: str | None = None,
    paper_id: str | None = None,
) -> tuple[int, bool]:
    """Insert a card unless its fingerprint exists.

    An active card with the same fingerprint gets its text refreshed. A done
    card is never touched or reopened. An auto-resolved card is reopened,
    since the problem came back. Returns (card id, created).
    """
    key = check_id if type_ == "electrical" else paper_id
    fp = fingerprint(type_, source_ref, key or "")
    now = now_iso()
    row = conn.execute("SELECT id, status FROM cards WHERE fingerprint = ?", (fp,)).fetchone()
    if row is None:
        cur = conn.execute(
            """INSERT INTO cards (type, title, why, source_url, source_ref, check_id,
                   paper_id, claude_prompt, status, fingerprint, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
            (type_, title, why, source_url, source_ref, check_id, paper_id,
             claude_prompt, fp, now, now),
        )
        conn.commit()
        return cur.lastrowid, True
    if row["status"] == "done":
        return row["id"], False
    conn.execute(
        """UPDATE cards SET title = ?, why = ?, source_url = ?, claude_prompt = ?,
               status = 'active', updated_at = ? WHERE id = ?""",
        (title, why, source_url, claude_prompt, now, row["id"]),
    )
    conn.commit()
    return row["id"], False


def auto_resolve(conn: sqlite3.Connection, fingerprints: list[str]) -> int:
    """Mark these active cards auto_resolved. Returns how many changed."""
    if not fingerprints:
        return 0
    marks = ",".join("?" * len(fingerprints))
    cur = conn.execute(
        f"UPDATE cards SET status = 'auto_resolved', updated_at = ? "
        f"WHERE status = 'active' AND fingerprint IN ({marks})",
        (now_iso(), *fingerprints),
    )
    conn.commit()
    return cur.rowcount
