"""View models for the Tasks workspace and Home's task lists. No Qt.

Everything comes from jarvis.items (the same ``now`` / ``tz`` rules): open items
in the four groups, the most recently done, and the deleted ones to restore.
GitHub issues are not tasks and never appear here.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo

from jarvis import items as it
from jarvis.capture import resolve_phrase

GROUP_LABELS = {"overdue": "overdue", "today": "today", "upcoming": "upcoming",
                "someday": "someday"}
KIND_FILTERS = ("all", *it.KINDS)
DONE_LIMIT = 15
DELETED_LIMIT = 15


def _time(hhmm: str) -> str:
    h, m = map(int, hhmm.split(":"))
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def day_label(d: date, today: date) -> str:
    """"today", "tomorrow", "yesterday", "Thu Oct 15", "Mon Jan 4, 2027"."""
    delta = (d - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    label = f"{d:%a %b} {d.day}"
    return label if d.year == today.year else f"{label}, {d.year}"


def when_label(stamp: str | None, today: date) -> str:
    """A stored wall-clock time as words: "today 5:00 PM", "Thu Oct 15"."""
    if not stamp:
        return ""
    d = date.fromisoformat(stamp[:10])
    return f"{day_label(d, today)} {_time(stamp[11:16])}" if len(stamp) > 10 else day_label(d, today)


def late_label(stamp: str, now: datetime) -> str:
    """How late an overdue time is: "3 days late", "1 day late", "2 h late"."""
    today = now.date()
    d = date.fromisoformat(stamp[:10])
    days = (today - d).days
    if days >= 1:
        return f"{days} day{'s' if days != 1 else ''} late"
    if len(stamp) > 10:
        due = datetime.fromisoformat(stamp).replace(tzinfo=now.tzinfo)
        mins = max(1, int((now - due).total_seconds() // 60))
        return f"{mins // 60} h late" if mins >= 60 else f"{mins} min late"
    return "late"


@dataclass(frozen=True)
class TaskRowView:
    item: it.Item
    group: str            # overdue / today / upcoming / someday / done / deleted
    when: str             # "Thu Oct 15", "today 5:00 PM" (due, else reminder)
    late: str             # "2 days late" for overdue, else ""
    remind: str           # "remind tomorrow 5:00 PM" when there is also a due time
    project: str | None   # project slug
    project_name: str | None

    @property
    def id(self) -> int:
        return self.item.id

    @property
    def title(self) -> str:
        return self.item.title


def row_view(item: it.Item, group: str, now: datetime,
             projects: dict[int, it.Project]) -> TaskRowView:
    today = now.date()
    when = when_label(item.when, today)
    remind = ""
    if item.due_at and item.remind_at:
        remind = f"remind {when_label(item.remind_at, today)}"
    elif item.remind_at and not item.due_at:
        when = f"remind {when}"
    late = late_label(item.when, now) if group == "overdue" and item.when else ""
    p = projects.get(item.project_id) if item.project_id is not None else None
    return TaskRowView(item, group, when, late, remind, p.slug if p else None,
                       p.name if p else None)


@dataclass(frozen=True)
class TasksData:
    today: date
    groups: tuple[tuple[str, tuple[TaskRowView, ...]], ...]   # in GROUPS order
    done: tuple[TaskRowView, ...]
    deleted: tuple[TaskRowView, ...]
    projects: tuple[it.Project, ...]
    project: str | None = None     # the filter (slug) or None for all
    kind: str = "all"              # the filter

    def group(self, key: str) -> tuple[TaskRowView, ...]:
        return dict(self.groups)[key]

    @property
    def open_count(self) -> int:
        return sum(len(rows) for _, rows in self.groups)

    def summary(self) -> str:
        """"2 overdue · 3 today · 5 upcoming · 4 someday" (empty groups left out)."""
        parts = [f"{len(rows)} {GROUP_LABELS[k]}" for k, rows in self.groups if rows]
        return " · ".join(parts) if parts else "nothing open"


def load_tasks(conn: sqlite3.Connection, now: datetime | None = None, tz: tzinfo | None = None,
               *, project: str | None = None, kind: str = "all",
               done_limit: int = DONE_LIMIT, deleted_limit: int = DELETED_LIMIT) -> TasksData:
    """The Tasks workspace's rows. ``project``: a slug (or None for all);
    ``kind``: "all" or one of items.KINDS."""
    local = it.local_now(now, tz)
    if kind not in KIND_FILTERS:
        raise it.ItemError(f"kind filter must be one of {', '.join(KIND_FILTERS)}", "kind")
    kinds = None if kind == "all" else kind
    projects = tuple(it.list_projects(conn, include_archived=True))
    by_id = {p.id: p for p in projects}
    grouped = it.grouped_items(conn, local, tz, project=project, kinds=kinds)
    groups = tuple((k, tuple(row_view(i, k, local, by_id) for i in grouped[k]))
                   for k in it.GROUPS)
    done = tuple(row_view(i, "done", local, by_id) for i in it.list_items(
        conn, status="done", project=project, kinds=kinds)[:done_limit])
    deleted = tuple(row_view(i, "deleted", local, by_id) for i in it.deleted_items(
        conn, project=project, kinds=kinds, limit=deleted_limit))
    return TasksData(local.date(), groups, done, deleted,
                     tuple(p for p in projects if p.status != "archived"), project, kind)


def parse_when_input(text: str, now: datetime, *, need_time: bool) -> str | None:
    """What the user typed in a due / remind field, as the stored text. Accepts
    the stored forms ("2026-10-15", "2026-10-15 17:00") and the phrases capture
    works out without a model ("tomorrow 5pm", "Thursday", "in 3 days").
    Empty -> None. Raises ItemError with a plain message otherwise."""
    field = "remind" if need_time else "due"
    text = (text or "").strip()
    if not text:
        return None
    try:
        return it.wall_clock(text, field, need_time=need_time)
    except it.ItemError:
        pass
    today = now.date()
    r = resolve_phrase(text, today)
    if r.options:
        choices = " or ".join(label for label, _ in r.options)
        raise it.ItemError(f"“{text}” could mean {choices}; type the date", field)
    if not r.recognized:
        raise it.ItemError(f"couldn't read “{text}”; try 2026-10-15, 2026-10-15 17:00 or "
                           "“tomorrow 5pm”", field)
    day, hhmm = r.day, r.time
    if day is None and hhmm is not None:
        day = today if hhmm > now.strftime("%H:%M") else today + timedelta(days=1)
    if need_time and hhmm is None:
        raise it.ItemError("a reminder needs a time of day", field)
    return f"{day.isoformat()}T{hhmm}" if hhmm else day.isoformat()
