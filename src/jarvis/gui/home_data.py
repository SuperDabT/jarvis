"""View model for the Home workspace. No Qt, no model calls.

Home answers "what needs me today": overdue and today's items, what is due in
the next two weeks (items and project milestones), the project's briefing
headline and vitals, its computed next steps (facts.compute_next_steps, never
the model) and the latest activity. Each section is read on its own; one that
fails becomes an error line and an empty section, never a blank page.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

from jarvis import items as it
from jarvis.gui.tasks_data import TaskRowView, day_label, row_view

UPCOMING_DAYS = 14        # items due within this many days
MILESTONE_DAYS = 60       # open milestones due within this many days
DEADLINES_MAX = 6
NEXT_STEPS_MAX = 3
ACTIVITY_MAX = 5
VITAL_KEYS = ("issues", "suggestions", "checks", "commits 7d")


@dataclass(frozen=True)
class Deadline:
    day: date
    label: str               # "Thu Oct 15", "tomorrow"
    time: str                # "5:00 PM" or ""
    title: str
    kind: str                # task / idea / reminder / milestone
    item_id: int | None = None
    project: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class HomeData:
    today: date
    greeting: str                          # "Good afternoon."
    overdue: tuple[TaskRowView, ...]
    due_today: tuple[TaskRowView, ...]
    deadlines: tuple[Deadline, ...]
    headline: str | None                   # the latest project briefing headline
    briefing_by: str | None                # "gemma4:26b" / "template"
    project_name: str
    vitals: tuple[Any, ...]                # brain_data.Vital
    next_steps: tuple[Any, ...]            # facts.NextStep
    activity: tuple[Any, ...]              # brain_data.StreamItem
    errors: tuple[str, ...] = ()

    @property
    def attention(self) -> str:
        """"2 overdue · 3 due today", "Nothing due today"."""
        bits = []
        if self.overdue:
            bits.append(f"{len(self.overdue)} overdue")
        if self.due_today:
            bits.append(f"{len(self.due_today)} due today")
        return " · ".join(bits) if bits else "Nothing due today"


def greeting(now: datetime) -> str:
    h = now.hour
    if 5 <= h < 12:
        return "Good morning."
    return "Good afternoon." if 12 <= h < 18 else "Good evening."


def _milestone_day(due_on: str | None) -> date | None:
    """A GitHub milestone's due date (GitHub stores the end of the day in UTC)."""
    if not due_on:
        return None
    try:
        return date.fromisoformat(due_on[:10])
    except ValueError:
        return None


def build_home(conn: sqlite3.Connection, data: Any = None, brain: Any = None,
               now: datetime | None = None, tz: tzinfo | None = None,
               project_name: str = "") -> HomeData:
    """Home from the database plus what the window already loaded (``data``:
    gui.data.WindowData, ``brain``: brain_data.BrainData; either may be None)."""
    local = it.local_now(now, tz)
    today = local.date()
    errors: list[str] = []
    overdue: tuple[TaskRowView, ...] = ()
    due_today: tuple[TaskRowView, ...] = ()
    deadlines: list[Deadline] = []
    try:
        projects = {p.id: p for p in it.list_projects(conn, include_archived=True)}
        overdue = tuple(row_view(i, "overdue", local, projects)
                        for i in it.overdue_items(conn, local, tz))
        due_today = tuple(row_view(i, "today", local, projects)
                          for i in it.today_items(conn, local, tz))
        horizon = (today + timedelta(days=UPCOMING_DAYS)).isoformat()
        for i in it.upcoming_items(conn, local, tz):
            if i.when[:10] > horizon:
                break
            d = date.fromisoformat(i.when[:10])
            p = projects.get(i.project_id) if i.project_id is not None else None
            hhmm = i.when[11:16]
            deadlines.append(Deadline(d, day_label(d, today), _clock(hhmm), i.title, i.kind,
                                      item_id=i.id, project=p.slug if p else None))
    except (sqlite3.Error, it.ItemError) as e:
        errors.append(f"Could not read your items: {e}")
    try:
        until = today + timedelta(days=MILESTONE_DAYS)
        for r in conn.execute("SELECT title, due_on, html_url FROM milestones "
                              "WHERE state = 'open' AND due_on IS NOT NULL"):
            d = _milestone_day(r["due_on"])
            if d is not None and today <= d <= until:
                deadlines.append(Deadline(d, day_label(d, today), "", r["title"], "milestone",
                                          url=r["html_url"]))
    except sqlite3.Error as e:
        errors.append(f"Could not read the milestones: {e}")
    deadlines.sort(key=lambda x: (x.day, x.time or "99", x.kind != "milestone", x.title))

    briefing = getattr(data, "briefing", None)
    vitals = tuple(v for v in (getattr(brain, "vitals", None) or ()) if v.key in VITAL_KEYS)
    return HomeData(
        today=today, greeting=greeting(local), overdue=overdue, due_today=due_today,
        deadlines=tuple(deadlines[:DEADLINES_MAX]),
        headline=briefing.headline if briefing else None,
        briefing_by=briefing.source_label if briefing else None,
        project_name=project_name, vitals=vitals,
        next_steps=tuple((getattr(data, "next_steps", None) or ())[:NEXT_STEPS_MAX]),
        activity=tuple((getattr(brain, "stream", None) or ())[:ACTIVITY_MAX]),
        errors=tuple(errors))


def _clock(hhmm: str) -> str:
    if not hhmm:
        return ""
    h, m = map(int, hhmm.split(":"))
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"
