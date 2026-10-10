"""Canvas: the user's course calendar, imported from its private feed (no Qt).

Canvas gives every student a calendar feed (Calendar > Calendar Feed): an
iCalendar (.ics) link with every assignment's due date and the course events.
Jarvis reads it (``JARVIS_CANVAS_ICS`` in ``~/.config/jarvis/env``) and keeps
a copy in ``canvas_events``: one row per Canvas id, so a re-import updates a
row instead of duplicating it; a row that leaves the feed is marked removed.
Canvas is only read; the user's own items are never touched. A row the user
marks done in Jarvis stays hidden although Canvas keeps listing it.

The feed link works like a password: it is never printed, logged or put in an
error message (``FeedError`` says what failed, never the link).

Times are stored like items' dates, as local wall clock ("2026-10-13" or
"2026-10-13T23:59"). A Canvas entry whose title names an exam, midterm and the
like is a fixed commitment (``CanvasEvent.commitment``); assignments are
deadlines. The feed doesn't say whether something was submitted.
"""
from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jarvis import planning
from jarvis.db import now_iso

ENV_VAR = "JARVIS_CANVAS_ICS"
SYNC_INTERVAL_S = 3600
FETCH_TIMEOUT_S = 30
MAX_FEED_BYTES = 5_000_000
KEEP_PAST_DAYS = 30         # older entries in the feed aren't imported
# "Lab 3 [ECE 201 - Fall 2026]": Canvas puts the course in brackets at the end
_COURSE_RE = re.compile(r"\s*\[([^\[\]]{1,120})\]\s*$")


ENV_FILE = "~/.config/jarvis/env"


def feed_url(env: dict[str, str] | None = None, env_file: str | Path = ENV_FILE) -> str | None:
    """The feed link: ``$JARVIS_CANVAS_ICS``, else its line in the env file (which the
    service loads, but a terminal doesn't). None when it isn't set."""
    value = (env if env is not None else os.environ).get(ENV_VAR, "").strip()
    if value:
        return value
    try:
        text = Path(env_file).expanduser().read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        key, sep, val = line.strip().removeprefix("export ").partition("=")
        if sep and key.strip() == ENV_VAR:
            return val.strip().strip("'\"") or None
    return None


class FeedError(RuntimeError):
    """The feed couldn't be read (the message never contains the link)."""


@dataclass(frozen=True)
class CanvasEvent:
    uid: str
    kind: str                  # "assignment" | "event"
    title: str
    course: str | None
    when: str                  # local wall clock: "YYYY-MM-DD" or "YYYY-MM-DDTHH:MM"
    url: str | None = None
    done: bool = False

    @property
    def ref(self) -> str:
        return f"canvas:{self.uid}"

    @property
    def day(self) -> date:
        return date.fromisoformat(self.when[:10])

    @property
    def label(self) -> str:
        """The title with its course: "Lab 3 (ECE 201)"."""
        return f"{self.title} ({self.course})" if self.course else self.title

    @property
    def commitment(self) -> bool:
        """An exam, midterm, presentation and the like: something to prepare for.
        A quiz is a deadline like any assignment."""
        return bool(planning._EVENT_RE.search(self.title))

    @property
    def short_id(self) -> str:
        """The number in the Canvas id ("event-assignment-123" -> "123")."""
        m = re.search(r"(\d+)\D*$", self.uid)
        return m[1] if m else self.uid


# ---------------------------------------------------------------- parsing


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def _unescape(v: str) -> str:
    return (v.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",")
            .replace("\\;", ";").replace("\\\\", "\\")).strip()


def _when(value: str, params: dict[str, str], tz: tzinfo | None) -> str:
    """An iCalendar DTSTART as local wall clock."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return f"{value[:4]}-{value[4:6]}-{value[6:8]}"
    m = re.fullmatch(r"(\d{8})T(\d{4})(\d{2})?(Z)?", value)
    if not m:
        raise ValueError(f"unreadable time {value!r}")
    naive = datetime.strptime(m[1] + m[2], "%Y%m%d%H%M")
    if m[4]:
        aware = naive.replace(tzinfo=UTC)
    elif "TZID" in params:
        try:
            aware = naive.replace(tzinfo=ZoneInfo(params["TZID"]))
        except (ZoneInfoNotFoundError, ValueError):
            aware = naive.astimezone()          # an unknown zone: the system's
    else:
        aware = naive.astimezone()              # floating time: the system's zone
    return aware.astimezone(tz).strftime("%Y-%m-%dT%H:%M")


def parse_ics(text: str, tz: tzinfo | None = None) -> list[CanvasEvent]:
    """The events in an iCalendar text. FeedError if it isn't one."""
    lines = _unfold(text)
    if not lines or lines[0].strip().upper() != "BEGIN:VCALENDAR":
        raise FeedError("the feed isn't a calendar (expected an .ics file)")
    out: list[CanvasEvent] = []
    cur: dict[str, tuple[str, dict[str, str]]] | None = None
    for line in lines:
        name, _, value = line.partition(":")
        key, *raw_params = name.split(";")
        key = key.upper()
        params = dict(p.split("=", 1) for p in raw_params if "=" in p)
        if key == "BEGIN" and value.upper() == "VEVENT":
            cur = {}
        elif key == "END" and value.upper() == "VEVENT" and cur is not None:
            ev = _event(cur, tz)
            if ev is not None:
                out.append(ev)
            cur = None
        elif cur is not None and key not in cur:
            cur[key] = (value, params)
    return out


def _event(props: dict[str, tuple[str, dict[str, str]]], tz: tzinfo | None) -> CanvasEvent | None:
    uid = _unescape(props.get("UID", ("", {}))[0])
    summary = _unescape(props.get("SUMMARY", ("", {}))[0])
    if not uid or not summary or "DTSTART" not in props:
        return None
    try:
        when = _when(*props["DTSTART"], tz)
    except ValueError:
        return None
    course = None
    m = _COURSE_RE.search(summary)
    if m:
        course = m[1].strip()
        summary = summary[:m.start()].strip() or summary
    kind = "assignment" if "assignment" in uid.lower() else "event"
    url = _unescape(props["URL"][0]) if "URL" in props else None
    return CanvasEvent(uid[:200], kind, summary[:300], course, when,
                       url if url and url.startswith("https://") else None)


# ---------------------------------------------------------------- syncing


@dataclass
class SyncResult:
    added: int = 0
    updated: int = 0
    removed: int = 0
    total: int = 0
    error: str | None = None

    def summary(self) -> str:
        if self.error:
            return f"failed: {self.error}"
        return (f"{self.total} in the feed: {self.added} new, {self.updated} changed, "
                f"{self.removed} removed")


def fetch(url: str, client: Any = None) -> str:
    """The feed's text. FeedError (without the link) on any failure."""
    if not url.startswith(("https://", "webcal://")):
        raise FeedError(f"{ENV_VAR} must be an https:// link (Canvas > Calendar > "
                        "Calendar Feed)")
    url = "https://" + url[len("webcal://"):] if url.startswith("webcal://") else url
    import httpx
    own = client is None
    client = client or httpx.Client(timeout=FETCH_TIMEOUT_S, follow_redirects=True)
    try:
        r = client.get(url)
    except httpx.HTTPError as e:
        raise FeedError(f"couldn't reach Canvas ({type(e).__name__})") from None
    finally:
        if own:
            client.close()
    if r.status_code != 200:
        raise FeedError(f"Canvas answered HTTP {r.status_code}"
                        + (" (the feed link may have been reset)" if r.status_code in (
                            401, 403, 404) else ""))
    if len(r.content) > MAX_FEED_BYTES:
        raise FeedError("the feed is unexpectedly large")
    return r.text


def store(conn: sqlite3.Connection, events: list[CanvasEvent], now: datetime) -> SyncResult:
    """Upsert the feed's events and mark the ones that left it removed, in one
    transaction. Entries older than KEEP_PAST_DAYS are left out."""
    res = SyncResult()
    stamp = now_iso()
    floor = (now.date() - timedelta(days=KEEP_PAST_DAYS)).isoformat()
    keep = {e.uid: e for e in events if e.when[:10] >= floor}
    res.total = len(keep)
    with conn:
        old = {r["uid"]: r for r in conn.execute("SELECT * FROM canvas_events")}
        for e in keep.values():
            row = old.get(e.uid)
            if row is None:
                conn.execute(
                    "INSERT INTO canvas_events (uid, kind, title, course, starts_at, url, "
                    "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (e.uid, e.kind, e.title, e.course, e.when, e.url, stamp, stamp))
                res.added += 1
                continue
            changed = (row["title"], row["course"], row["starts_at"], row["url"],
                       row["removed_at"]) != (e.title, e.course, e.when, e.url, None)
            conn.execute(
                "UPDATE canvas_events SET kind = ?, title = ?, course = ?, starts_at = ?, "
                "url = ?, last_seen_at = ?, removed_at = NULL WHERE uid = ?",
                (e.kind, e.title, e.course, e.when, e.url, stamp, e.uid))
            res.updated += changed
        for uid, row in old.items():
            if uid not in keep and row["removed_at"] is None and row["starts_at"][:10] >= floor:
                conn.execute("UPDATE canvas_events SET removed_at = ? WHERE uid = ?",
                             (stamp, uid))
                res.removed += 1
    return res


def sync(conn: sqlite3.Connection, url: str | None, now: datetime,
         tz: tzinfo | None = None, client: Any = None) -> SyncResult:
    """Fetch the feed and store it. Never raises for a feed problem: the result
    carries the error (without the link)."""
    if not url:
        return SyncResult(error=f"no feed link: set {ENV_VAR} in ~/.config/jarvis/env")
    try:
        events = parse_ics(fetch(url.strip(), client), tz)
    except FeedError as e:
        return SyncResult(error=str(e))
    return store(conn, events, now)


# ---------------------------------------------------------------- reading


def _row(r: sqlite3.Row) -> CanvasEvent:
    return CanvasEvent(r["uid"], r["kind"], r["title"], r["course"], r["starts_at"], r["url"],
                       r["done_at"] is not None)


def has_table(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND "
                        "name = 'canvas_events'").fetchone() is not None


def upcoming(conn: sqlite3.Connection, start: date, end: date) -> list[CanvasEvent]:
    """Open Canvas entries (not removed, not done) dated ``start``..``end``,
    soonest first. [] when the database has no Canvas table yet."""
    if not has_table(conn):
        return []
    rows = conn.execute(
        "SELECT * FROM canvas_events WHERE removed_at IS NULL AND done_at IS NULL "
        "AND substr(starts_at, 1, 10) BETWEEN ? AND ? ORDER BY starts_at, uid",
        (start.isoformat(), end.isoformat()))
    return [_row(r) for r in rows]


def find(conn: sqlite3.Connection, key: str) -> CanvasEvent:
    """One entry by its Canvas id or its number. LookupError if none (or several)."""
    key = key.strip().removeprefix("canvas:")
    rows = [r for r in conn.execute("SELECT * FROM canvas_events")
            if r["uid"] == key or _row(r).short_id == key]
    if len(rows) != 1:
        raise LookupError(f"no Canvas entry {key!r}" if not rows
                          else f"{key!r} matches several entries; use the full id")
    return _row(rows[0])


def set_done(conn: sqlite3.Connection, key: str, done: bool = True) -> CanvasEvent:
    e = find(conn, key)
    with conn:
        conn.execute("UPDATE canvas_events SET done_at = ? WHERE uid = ?",
                     (now_iso() if done else None, e.uid))
    return find(conn, e.uid)
