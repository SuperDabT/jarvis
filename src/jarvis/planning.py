"""Structured plans: what is in scope, what the model decided, and whether that
decision respects the records (no Qt, no model).

For a plan or schedule request Jarvis decides by code which of the user's items
the plan has to account for (``build_brief``): overdue ones, those due in the
period or in the week after it, and the undated ones (the period is the days
asked about). The model answers with ``submit_plan``: one decision per item, by
id, plus the availability windows the user stated (each quoting their words),
suggestions that are not tasks, and assumptions. ``check_plan`` validates it
against the brief:

- every item in scope is decided exactly once: set_due (an undated item gets a
  date), work_on (a suggested day, optionally a time), leave (stays as it is,
  with a reason) or exclude (out of this plan, with a reason). Nothing is
  silently dropped; coverage is by id, never by matching words in prose;
- planned days are inside the period asked about; deadlines are limits: work
  on an item is planned on or before its due day (overdue items any day), and
  existing deadlines are never moved;
- availability is only what the user said: a window counts only if its quote
  is found in their messages; a time of day is only allowed inside a window
  on that day, and when the user gave any windows, every planned day needs
  one. With no windows, the plan stays at day level (provisional);
- there are no durations anywhere, so none can be invented.

Errors go back to the model to fix. An accepted plan is rendered by code
(``render``): facts (deadlines on record), proposed changes, suggested work
days (not saved), what was left or excluded and why, suggestions, assumptions
and the availability used. A "schedule" goal turns its decisions into a
proposal (``actions``): set_due for undated items, a reminder for a timed work
session; a "plan" goal saves nothing.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from jarvis import items as it

AHEAD_DAYS = 7           # deadlines this long after the period still shape it
MAX_UNDATED = 12         # undated items in scope (highest priority first)
DECISIONS = ("set_due", "work_on", "leave", "exclude")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True)
class BriefItem:
    id: int
    title: str
    kind: str
    due: str | None           # stored form
    remind: str | None
    project: str | None
    status: str               # overdue / due_in_period / due_soon / undated

    @property
    def due_day(self) -> date | None:
        return date.fromisoformat(self.due[:10]) if self.due else None


@dataclass
class Brief:
    start: date
    end: date
    horizon: date             # end + AHEAD_DAYS
    today: date
    items: dict[int, BriefItem]
    more_undated: int = 0     # undated items left out of scope (count only)

    def lines(self) -> list[str]:
        """The brief as the model sees it."""
        out = [f"Period: {self.start:%a} {self.start} to {self.end:%a} {self.end}. "
               f"Deadlines up to {self.horizon} are included.",
               "Items in scope (decide every one by item_id):"]
        for b in self.items.values():
            when = (f"due {b.due}" if b.due else "no date") + (
                f", reminder {b.remind}" if b.remind else "")
            proj = f", project {b.project}" if b.project else ""
            out.append(f"- item_id {b.id}: {b.title} [{b.kind}; {b.status}; {when}{proj}]")
        if not self.items:
            out.append("- (none)")
        if self.more_undated:
            out.append(f"({self.more_undated} more undated items are not in scope)")
        return out


def build_brief(conn: sqlite3.Connection, start: date, end: date, today: date) -> Brief:
    """The user's items a plan for ``start``..``end`` has to account for."""
    start = max(start, today)
    end = max(end, start)
    horizon = end + timedelta(days=AHEAD_DAYS)
    projects = {p.id: p.slug for p in it.list_projects(conn, include_archived=True)}
    scope: dict[int, BriefItem] = {}
    undated: list[it.Item] = []
    for i in it.open_items(conn):
        when = i.due_at or i.remind_at
        if not i.due_at:
            if i.remind_at and date.fromisoformat(i.remind_at[:10]) < today:
                status = "overdue"
            elif not when:
                undated.append(i)
                continue
            elif date.fromisoformat(when[:10]) <= horizon:
                status = "due_in_period" if date.fromisoformat(when[:10]) <= end else "due_soon"
            else:
                continue
        else:
            d = date.fromisoformat(i.due_at[:10])
            if d < today:
                status = "overdue"
            elif d <= end:
                status = "due_in_period"
            elif d <= horizon:
                status = "due_soon"
            else:
                continue
        scope[i.id] = BriefItem(i.id, i.title, i.kind, i.due_at, i.remind_at,
                                projects.get(i.project_id), status)
    undated.sort(key=lambda i: (-i.priority, i.id))
    for i in undated[:MAX_UNDATED]:
        scope[i.id] = BriefItem(i.id, i.title, i.kind, None, None,
                                projects.get(i.project_id), "undated")
    return Brief(start, end, horizon, today, scope, max(0, len(undated) - MAX_UNDATED))


# ---------------------------------------------------------------- the plan


@dataclass(frozen=True)
class Window:
    day: date
    start: str               # "HH:MM"
    end: str
    quote: str


@dataclass(frozen=True)
class Decision:
    item_id: int
    action: str
    reason: str
    day: date | None = None
    time: str | None = None  # "HH:MM", only inside a supplied window


@dataclass
class Plan:
    summary: str
    decisions: list[Decision]
    windows: list[Window] = field(default_factory=list)
    suggestions: list[dict[str, str]] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _hhmm(value: Any, what: str) -> str:
    m = _TIME_RE.match(str(value or "").strip())
    if not m:
        raise ValueError(f"{what} must be a time like 14:00")
    return f"{int(m[1]):02d}:{m[2]}"


def _day(value: Any, what: str) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise ValueError(f"{what} must be a date like 2026-10-10") from None


def check_plan(brief: Brief, raw: dict[str, Any], user_text: str) -> tuple[Plan | None, list[str]]:
    """(the plan, errors): None when anything is wrong, with every problem listed
    so the model can fix them all at once."""
    errors: list[str] = []
    said = _norm(user_text)
    windows: list[Window] = []
    for k, w in enumerate(raw.get("availability") or [], 1):
        try:
            if not isinstance(w, dict):
                raise ValueError("must be an object")  # noqa: TRY004 - a message for the model
            quote = str(w.get("quote") or "").strip()
            if not quote or _norm(quote) not in said:
                raise ValueError("its quote isn't the user's words; only availability the user "
                                 "stated counts (leave it out otherwise)")
            day = _day(w.get("date"), "date")
            if not brief.start <= day <= brief.end:
                raise ValueError(f"{day} is outside the period")
            s, e = _hhmm(w.get("start"), "start"), _hhmm(w.get("end"), "end")
            if s >= e:
                raise ValueError("start must be before end")
            windows.append(Window(day, s, e, quote))
        except ValueError as ex:
            errors.append(f"availability {k}: {ex}")
    by_day: dict[date, list[Window]] = {}
    for w in windows:
        by_day.setdefault(w.day, []).append(w)

    decisions: list[Decision] = []
    seen: set[int] = set()
    for k, d in enumerate(raw.get("decisions") or [], 1):
        try:
            if not isinstance(d, dict):
                raise ValueError("must be an object")  # noqa: TRY004 - a message for the model
            try:
                item_id = int(d.get("item_id"))
            except (TypeError, ValueError):
                raise ValueError("needs the item_id of an item in scope") from None
            b = brief.items.get(item_id)
            if b is None:
                raise ValueError(f"item_id {item_id} isn't in scope")
            if item_id in seen:
                raise ValueError(f"item_id {item_id} is decided twice")
            seen.add(item_id)
            action = d.get("action")
            if action not in DECISIONS:
                raise ValueError(f"action must be one of {', '.join(DECISIONS)}")
            reason = str(d.get("reason") or "").strip()[:240]
            if not reason:
                raise ValueError(f"“{b.title}” needs a reason")
            day = time = None
            if action in ("set_due", "work_on"):
                day = _day(d.get("date"), "date")
                if not max(brief.start, brief.today) <= day <= brief.end:
                    raise ValueError(f"{day} for “{b.title}” is outside the period "
                                     f"{brief.start} to {brief.end}; plan inside it or leave "
                                     "the item")
                if action == "set_due" and b.due:
                    raise ValueError(f"“{b.title}” already has a deadline ({b.due}); it isn't "
                                     "moved: use work_on to plan work on it")
                if b.due_day and b.due_day >= brief.today and day > b.due_day:
                    raise ValueError(f"“{b.title}” is due {b.due_day}: work on it on or "
                                     "before that day")
                if d.get("time"):
                    time = _hhmm(d.get("time"), "time")
                    if not any(w.start <= time < w.end for w in by_day.get(day, [])):
                        raise ValueError(f"no availability the user stated covers {day} "
                                         f"{time}; give the day only, or ask")
                if windows and day not in by_day:
                    raise ValueError(f"the user gave times only for "
                                     f"{', '.join(str(x) for x in sorted(by_day))}; {day} "
                                     "isn't one of them (if they gave a time for that day, "
                                     "add its window)")
            decisions.append(Decision(item_id, action, reason, day, time))
        except ValueError as ex:
            errors.append(f"decision {k}: {ex}")
    missing = [b for i, b in brief.items.items() if i not in seen]
    if missing:
        errors.append("not decided: " + "; ".join(f"item_id {b.id} {b.title}"
                                                  for b in missing)
                      + " (decide each: set_due, work_on, leave or exclude, with a reason)")
    if errors:
        return None, errors
    suggestions = [{"text": str(s.get("text") or "")[:200], "reason": str(s.get("reason")
                                                                        or "")[:200]}
                   for s in raw.get("suggestions") or [] if isinstance(s, dict) and s.get("text")]
    return Plan(str(raw.get("summary") or "")[:500], decisions, windows, suggestions[:4],
                [str(a)[:200] for a in raw.get("assumptions") or []][:5]), []


def changes_for(brief: Brief, plan: Plan) -> list[dict[str, Any]]:
    """The proposal a "schedule" plan implies: set_due for an undated item given a
    day, a reminder for a timed work session. Day-only work stays a suggestion."""
    out = []
    for d in plan.decisions:
        if d.action == "set_due" and d.day:
            out.append({"op": "set_due", "item_id": d.item_id, "due": d.day.isoformat(),
                        "reason": d.reason})
        if d.time and d.day and d.action in ("set_due", "work_on"):
            out.append({"op": "set_remind", "item_id": d.item_id,
                        "remind": f"{d.day.isoformat()}T{d.time}", "reason": d.reason})
    return out


# ---------------------------------------------------------------- rendering


def _when(stored: str | None) -> str:
    from jarvis.actions import human_when
    return human_when(stored)


def _clock(hhmm: str) -> str:
    h, m = int(hhmm[:2]), hhmm[3:]
    return f"{(h % 12) or 12}:{m} {'AM' if h < 12 else 'PM'}"


def render(brief: Brief, plan: Plan, scheduling: bool) -> str:
    """The plan in words, built from the validated decisions (not the model's prose)."""
    items = brief.items
    out = [f"**Jarvis's read** (a suggestion): {plan.summary}"] if plan.summary else []
    fixed = [b for b in items.values() if b.due]
    if fixed:
        out.append("**On record** (fixed): " + "; ".join(
            f"{b.title} {'overdue since' if b.status == 'overdue' else 'due'} {_when(b.due)}"
            for b in sorted(fixed, key=lambda b: b.due or "")))
    days: dict[date, list[str]] = {}
    for d in plan.decisions:
        b = items[d.item_id]
        if d.action == "set_due" and d.day:
            what = (f"due date {d.day:%a %b} {d.day.day}"
                    + (" (proposed)" if scheduling else " (suggested, not saved)"))
        elif d.action == "work_on" and d.day:
            what = "work on it" + (f" at {_clock(d.time)}" if d.time else "")
            what += (" (reminder proposed)" if scheduling and d.time
                     else " (suggested day, not saved)")
        else:
            continue
        days.setdefault(d.day, []).append(f"- {b.title}: {what}. {d.reason}")
    for day in sorted(days):
        out.append(f"**{day:%A %b} {day.day}**\n" + "\n".join(days[day]))
    for action, head in (("leave", "Left as they are"), ("exclude", "Not in this plan")):
        rows = [f"- {items[d.item_id].title}: {d.reason}" for d in plan.decisions
                if d.action == action]
        if rows:
            out.append(f"**{head}**\n" + "\n".join(rows))
    if plan.suggestions:
        out.append("**Suggestions** (not tasks, nothing saved)\n" + "\n".join(
            f"- {s['text']}" + (f" ({s['reason']})" if s["reason"] else "")
            for s in plan.suggestions))
    if plan.windows:
        out.append("**Availability you gave**: " + "; ".join(
            f"{w.day:%a} {_clock(w.start)}–{_clock(w.end)}" for w in plan.windows))
    else:
        out.append("**Availability**: not known, so the days are provisional and no times "
                   "are set.")
    if plan.assumptions:
        out.append("**Assumptions**: " + "; ".join(plan.assumptions))
    if brief.more_undated:
        out.append(f"({brief.more_undated} more undated items weren't considered.)")
    return "\n\n".join(out)


def render_brief(brief: Brief, why: str) -> str:
    """When no valid plan came back: what is on record, complete, by code."""
    out = [f"I couldn't put together a plan that fits your records ({why}). Here is what is "
           f"on record for {brief.start:%a %b} {brief.start.day} to "
           f"{brief.end:%a %b} {brief.end.day}:"]
    labels = {"overdue": "overdue", "due_in_period": "due", "due_soon": "due soon",
              "undated": "no date"}
    for b in brief.items.values():
        out.append(f"- {b.title} ({labels[b.status]}" + (f", {_when(b.due)}" if b.due else "")
                   + ")")
    return "\n".join(out)

