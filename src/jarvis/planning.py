"""Structured plans: what is in scope, what the model decided, and whether that
decision respects the records (no Qt, no model).

For a plan or schedule request Jarvis decides by code which of the user's items
the plan has to account for (``build_brief``): overdue ones, those dated up to
the horizon, and the undated ones (the period is the days asked about). The model answers with ``submit_plan``: one decision per item, by
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
  is found in their messages (and names the window's weekday, if it names one);
  a time of day is only allowed inside a window on that day, never within an
  hour of a fixed commitment on record, and when the user gave any windows,
  every planned day needs one. With no windows, the plan stays at day level
  (provisional);
- the free text (summary, reasons, suggestions, assumptions) may not state a
  time of day or a duration nobody gave, a wrong weekday for an item's
  deadline, or that something was saved; raw item ids are stripped.

The brief is computed, not left to the model: the horizon is the later of a
week after the period and two weeks from today (``HORIZON_DAYS``, shared with
the agenda), fixed commitments (timed items, exams) are listed with the days
of the period before them, and each item carries its allowed days. Important
items (overdue, due in the period) that get no time are listed first, with
the records' reason when no allowed day or stated window exists
(``Brief.no_room``). ``question_problem`` refuses clarifying questions about
what the user already said or the brief holds.

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

HORIZON_DAYS = 14        # dated items this far from today always shape a plan
AHEAD_DAYS = 7           # and deadlines this long after the period
MAX_UNDATED = 12         # undated items in scope (highest priority first)
DECISIONS = ("set_due", "work_on", "leave", "exclude")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
# a dated item with one of these words is an event to prepare for (an exam), even
# without a time; a timed item is a fixed commitment whatever its title
_EVENT_RE = re.compile(r"\b(?:exams?|midterms?|finals?|quiz(?:zes)?|interviews?|"
                       r"presentations?|defen[cs]e|viva|appointments?)\b", re.I)
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


@dataclass(frozen=True)
class BriefItem:
    id: int
    title: str
    kind: str
    due: str | None           # stored form
    remind: str | None
    project: str | None
    status: str               # overdue / due_in_period / due_soon / reminder / undated
    event: bool = False       # a fixed commitment: timed, or an exam-like dated item

    @property
    def due_day(self) -> date | None:
        return date.fromisoformat(self.due[:10]) if self.due else None

    @property
    def when(self) -> str | None:
        """Its date on record: the deadline, else the reminder."""
        return self.due or self.remind

    @property
    def important(self) -> bool:
        """Must be visible in any plan: overdue, due in the period, or an event."""
        return self.status in ("overdue", "due_in_period") or self.event


@dataclass
class Brief:
    start: date
    end: date
    horizon: date             # the later of end + AHEAD_DAYS and today + HORIZON_DAYS
    today: date
    items: dict[int, BriefItem]
    more_undated: int = 0     # undated items left out of scope (count only)

    @property
    def first(self) -> date:
        return max(self.start, self.today)

    def allowed(self, b: BriefItem) -> tuple[date, date] | None:
        """The days work on ``b`` may be planned: inside the period and, for an
        item with a deadline still ahead, on or before it. None: no such day."""
        last = self.end
        if b.due_day and b.due_day >= self.today:
            last = min(last, b.due_day)
        return (self.first, last) if self.first <= last else None

    def events(self) -> list[BriefItem]:
        """Fixed commitments on record from today to the horizon, soonest first."""
        return sorted((b for b in self.items.values() if b.event and b.when
                       and date.fromisoformat(b.when[:10]) >= self.today),
                      key=lambda b: b.when or "")

    def no_room(self, b: BriefItem, windows: list[Window] | None = None) -> str | None:
        """Why an important item can't get time in this plan, by the records (and
        the user's windows); None if it can."""
        if not b.important or b.event:
            return None
        span = self.allowed(b)
        if span is None:
            return f"due {_short(b.due_day)}, before any day of this plan"
        if windows and not any(span[0] <= w.day <= span[1] for w in windows):
            return f"due {_short(b.due_day)}; none of the time you gave is on or before it"
        return None

    def lines(self) -> list[str]:
        """The brief as the model sees it."""
        out = [f"Period: {self.start:%a} {self.start} to {self.end:%a} {self.end}. "
               f"Deadlines up to {self.horizon} are included."]
        events = self.events()
        if events:
            out.append("Fixed commitments on record (dates are facts; plan around them, "
                       "keep time before them; never move them):")
            for b in events:
                day = date.fromisoformat(b.when[:10])
                before = _days(self.first, min(self.end, day - timedelta(days=1)))
                lead = (f"days of the period before it: {', '.join(f'{d:%a}' for d in before)}"
                        if before else "no day of the period comes before it")
                out.append(f"- {day:%a} {b.when.replace('T', ' ')} {b.title} "
                           f"(item_id {b.id}); {lead}")
        out.append("Items in scope (decide every one by item_id):")
        for b in self.items.values():
            when = (f"due {b.due}" if b.due else "no date") + (
                f", reminder {b.remind}" if b.remind else "")
            proj = f", project {b.project}" if b.project else ""
            span = self.allowed(b)
            room = (f"allowed days {span[0]:%a} {span[0]}–{span[1]:%a} {span[1]}" if span
                    else "no allowed day: its deadline comes before this period, leave it")
            out.append(f"- item_id {b.id}: {b.title} [{b.kind}; {b.status}; {when}{proj}; "
                       f"{room}]")
        if not self.items:
            out.append("- (none)")
        if self.more_undated:
            out.append(f"({self.more_undated} more undated items are not in scope)")
        return out


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=k) for k in range((last - first).days + 1)]


def _short(d: date | None) -> str:
    return f"{d:%a %b} {d.day}" if d else "with no date"


def build_brief(conn: sqlite3.Connection, start: date, end: date, today: date) -> Brief:
    """The user's items a plan for ``start``..``end`` has to account for: every
    open item dated (deadline or reminder) up to the horizon, everything overdue,
    and the undated ones."""
    start = max(start, today)
    end = max(end, start)
    horizon = max(end + timedelta(days=AHEAD_DAYS), today + timedelta(days=HORIZON_DAYS))
    projects = {p.id: p.slug for p in it.list_projects(conn, include_archived=True)}
    scope: dict[int, BriefItem] = {}
    undated: list[it.Item] = []
    for i in it.open_items(conn):
        due = date.fromisoformat(i.due_at[:10]) if i.due_at else None
        remind = date.fromisoformat(i.remind_at[:10]) if i.remind_at else None
        if due is None and remind is None:
            undated.append(i)
            continue
        if due is not None and due < today or due is None and remind < today:
            status = "overdue"
        elif due is not None and due <= end:
            status = "due_in_period"
        elif due is not None and due <= horizon:
            status = "due_soon"
        elif remind is not None and today <= remind <= end:
            status = "reminder"        # its deadline is later, its reminder is now
        elif due is None and remind <= horizon:
            status = "due_in_period" if remind <= end else "due_soon"
        else:
            continue
        when = i.due_at or i.remind_at or ""
        event = "T" in when and (i.due_at is not None or i.kind == "reminder") or bool(
            i.due_at and _EVENT_RE.search(i.title))
        scope[i.id] = BriefItem(i.id, i.title, i.kind, i.due_at, i.remind_at,
                                projects.get(i.project_id), status, event)
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


# times of day and durations in free text: a plan may only repeat ones the user
# said or the records hold (Jarvis has no calendar, nothing knows how long work takes)
_CLOCK_RE = re.compile(r"\b(\d{1,2})(?::([0-5]\d))?\s*([ap])\.?m\b\.?|\b([01]?\d|2[0-3]):([0-5]\d)\b",
                       re.I)
_DURATION_RE = re.compile(
    r"\b(?:\d+(?:\.\d+)?(?:\s*(?:-|to)\s*\d+)?|an?|one|two|three|four|five|few|couple of|"
    r"half an?)\s*(?:hours?|hrs?|minutes?|mins?)\b", re.I)
_CLAIM_RE = re.compile(r"\bi(?:'ve| have)\s+(?:\w+\s+)?(?:scheduled|added|saved|set|moved|"
                       r"created|booked|blocked)\b", re.I)
_DUE_DAY_RE = re.compile(r"\bdue\s+(?:on\s+|by\s+|this\s+|next\s+)?(" + "|".join(_WEEKDAYS)
                         + r")\b", re.I)
_ID_TEXT_RE = re.compile(r"\s*\(?\bitem[_ ]?id\s*[:#]?\s*\d+\)?|\s*\(item:\d+\)", re.I)
# a question about when the user is free, which tasks, or what is due
_ASKS_FREE_RE = re.compile(r"\b(?:free|available|availability|busy|what times?|which times?|"
                           r"when (?:can|could|would|will|are|do) you|time slots?)\b", re.I)
_ASKS_SET_RE = re.compile(r"\b(?:which|what) (?:tasks|items|things|ones)\b|\bwhat do you "
                          r"(?:need|have) to (?:do|get done)\b", re.I)
_ASKS_DUE_RE = re.compile(r"\bwhen (?:is|are) .{1,60}\bdue\b|\bdeadlines?\b", re.I)
_SAID_FREE_RE = re.compile(r"\b(?:free|available|busy|after \d|before \d|until \d|from \d|"
                           r"between \d|mornings?|afternoons?|evenings?|all day|tonight|"
                           r"no time)\b", re.I)


def _clocks(text: str) -> set[str]:
    """Every time of day in ``text``, as HH:MM."""
    out = set()
    for m in _CLOCK_RE.finditer(text):
        if m[3]:
            h = int(m[1]) % 12 + (12 if m[3].lower() == "p" else 0)
            if int(m[1]) <= 12:
                out.add(f"{h:02d}:{m[2] or '00'}")
        else:
            out.add(f"{int(m[4]):02d}:{m[5]}")
    return out


def _durations(text: str) -> set[str]:
    return {re.sub(r"\s+", " ", m[0].lower()) for m in _DURATION_RE.finditer(text)}


def _weekday_named(quote: str, today: date) -> int | None:
    """The weekday a quote names (today / tomorrow included), or None."""
    q = quote.lower()
    for k, name in enumerate(_WEEKDAYS):
        if re.search(rf"\b{name[:3]}(?:{name[3:]})?\b", q):
            return k
    if re.search(r"\btoday\b|\btonight\b", q):
        return today.weekday()
    if re.search(r"\btomorrow\b", q):
        return (today + timedelta(days=1)).weekday()
    return None


def question_problem(brief: Brief | None, user_text: str, question: str) -> str | None:
    """Why ``question`` shouldn't be asked (one question only, never what the
    user already said or the records hold); None if it is a fair question."""
    q = question.strip()
    if not q:
        return "the question is empty"
    if q.count("?") > 1:
        return "ask one question only (one '?')"
    if brief is not None and _ASKS_SET_RE.search(q):
        return "the items in scope are already decided (the brief); don't ask which tasks"
    if brief is not None and _ASKS_DUE_RE.search(q) and not _ASKS_FREE_RE.search(q):
        return "deadlines are in the brief; don't ask for them"
    if _ASKS_FREE_RE.search(q) and (m := _SAID_FREE_RE.search(user_text)):
        line = next((ln for ln in user_text.splitlines() if m[0] in ln), user_text)
        return (f"the user already said when they are free (“{line.strip()[:120]}”); "
                "plan with that instead of asking")
    return None


def _prose_problems(brief: Brief, texts: list[tuple[str, str]], user_text: str,
                    known_times: set[str], decided: dict[int, Decision]) -> list[str]:
    """Facts in the plan's free text that nobody stated: times of day, durations,
    wrong deadline weekdays, and claims that something was saved."""
    said_times, said_durations = _clocks(user_text), _durations(user_text)
    errors = []
    for where, text in texts:
        bad = sorted(_clocks(text) - said_times - known_times)
        if bad:
            errors.append(f"{where} mentions {', '.join(bad)}, a time nobody stated (Jarvis has "
                          "no calendar): remove it")
        bad = sorted(_durations(text) - said_durations)
        if bad:
            errors.append(f"{where} says “{bad[0]}”: how long things take is unknown; remove "
                          "durations the user didn't give")
        if _CLAIM_RE.search(text):
            errors.append(f"{where} says something was done; nothing is saved by a plan: "
                          "describe it as a suggestion")
    for d in decided.values():
        b = brief.items[d.item_id]
        for m in _DUE_DAY_RE.finditer(d.reason):
            if b.due_day and _WEEKDAYS.index(m[1].lower()) != b.due_day.weekday():
                errors.append(f"the reason for “{b.title}” says due {m[1]}; it is due "
                              f"{b.due_day:%A} {b.due_day}")
    return errors


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
            named = _weekday_named(quote, brief.today)
            if named is not None and named != day.weekday():
                raise ValueError(f"the quote names {_WEEKDAYS[named].title()}, but {day} is a "
                                 f"{day:%A}")
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
                    clash = _clash(brief, day, time, item_id)
                    if clash:
                        raise ValueError(f"{day} {time} is when “{clash}” is on record; pick "
                                         "another time or give the day only")
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
    suggestions = [{"text": str(s.get("text") or "")[:200], "reason": str(s.get("reason")
                                                                        or "")[:200]}
                   for s in raw.get("suggestions") or [] if isinstance(s, dict) and s.get("text")]
    summary = str(raw.get("summary") or "")[:500]
    assumptions = [str(a)[:200] for a in raw.get("assumptions") or []][:5]
    known = {b.when[11:16] for b in brief.items.values() if b.when and "T" in b.when}
    known |= {t for w in windows for t in (w.start, w.end)} | {d.time for d in decisions
                                                                if d.time}
    texts = [("the summary", summary), *((f"suggestion {k}", f"{x['text']} {x['reason']}")
                                         for k, x in enumerate(suggestions, 1)),
             *((f"assumption {k}", a) for k, a in enumerate(assumptions, 1)),
             *((f"the reason for “{brief.items[d.item_id].title}”", d.reason)
               for d in decisions)]
    errors += _prose_problems(brief, texts, user_text, known, {d.item_id: d for d in decisions})
    if errors:
        return None, errors
    return Plan(_ID_TEXT_RE.sub("", summary).strip(), decisions, windows, suggestions[:4],
                assumptions), []


def _clash(brief: Brief, day: date, time: str, item_id: int) -> str | None:
    """The fixed commitment on record within an hour of ``day`` ``time``, if any."""
    t = int(time[:2]) * 60 + int(time[3:])
    for b in brief.events():
        if b.id == item_id or not b.when or "T" not in b.when or b.when[:10] != day.isoformat():
            continue
        if abs(int(b.when[11:13]) * 60 + int(b.when[14:16]) - t) < 60:
            return b.title
    return None


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


def _clean(text: str) -> str:
    return _ID_TEXT_RE.sub("", text).strip()


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
    # important items the plan gives no time: what the records say, then the model's reason
    unfitted = []
    for d in plan.decisions:
        b = items[d.item_id]
        if d.action in ("leave", "exclude") and b.important and not b.event:
            fact = brief.no_room(b, plan.windows) or (
                f"overdue since {_when(b.due or b.remind)}" if b.status == "overdue"
                else f"due {_when(b.due or b.remind)}")
            unfitted.append(f"- {b.title} ({fact}): {_clean(d.reason)}")
    if unfitted:
        out.append("**Needs attention, not fitted in this plan**\n" + "\n".join(unfitted))
    days: dict[date, list[str]] = {}
    for d in plan.decisions:
        b = items[d.item_id]
        if d.action == "set_due" and d.day:
            what = (f"due date {d.day:%a %b} {d.day.day}"
                    + (" (proposed)" if scheduling else " (suggested, not saved)"))
        elif d.action == "work_on" and d.day:
            what = ("prepare for it" if b.event else "work on it") + (
                f" at {_clock(d.time)}" if d.time else "")
            what += (" (reminder proposed)" if scheduling and d.time
                     else " (suggested day, not saved)")
        else:
            continue
        days.setdefault(d.day, []).append(f"- {b.title}: {what}. {_clean(d.reason)}")
    for day in sorted(days):
        out.append(f"**{day:%A %b} {day.day}**\n" + "\n".join(days[day]))
    for action, head in (("leave", "Left as they are"), ("exclude", "Not in this plan")):
        rows = [f"- {items[d.item_id].title}: {_clean(d.reason)}" for d in plan.decisions
                if d.action == action and not (items[d.item_id].important
                                               and not items[d.item_id].event)]
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
              "reminder": "reminder", "undated": "no date"}
    for b in sorted(brief.items.values(), key=lambda b: (not b.important, b.when or "~")):
        out.append(f"- {b.title} ({labels[b.status]}"
                   + (f", {_when(b.when)}" if b.when else "") + ")")
    return "\n".join(out)

