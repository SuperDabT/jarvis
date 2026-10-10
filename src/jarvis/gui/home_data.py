"""View model for the Home workspace. No Qt, no model calls.

Home answers "what needs me today": overdue and today's items, what is due in
the next two weeks (items and project milestones), the inbox (open items with
no date and no project: captures not yet sorted), each active project's open
work and its next item by date (the order the user set: date, then their own
priority; Jarvis never ranks), the GitHub project's nearest-due milestone and
computed next steps (facts.compute_next_steps, never the model), and a small
overview (done in the last 7 days, open, overdue). Each section is read on its
own; one that fails becomes an error line and an empty section, never a blank
page. Nothing here writes.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any

from jarvis import attention, canvas, planning
from jarvis import items as it
from jarvis.gui.dashboard_data import assess_risk
from jarvis.gui.data import MilestoneBar
from jarvis.gui.tasks_data import TaskRowView, day_label, row_view

UPCOMING_DAYS = 14        # items due within this many days
FOCUS_MAX = 3             # signals in Today's Focus
UPCOMING_MAX = 4          # the right column's Upcoming
RECENT_MAX = 5            # Recent Activity rows
RECENT_DAYS = 7
SCHEDULE_DAYS = 7         # Today's Schedule can step this many days ahead
TONES = {"overdue": "red", "today": "amber", "event": "amber", "squeeze": "amber",
         "cluster": "blue", "soon": "blue", "stale": "grey"}
# Short, attributed, public domain; one a day (``quote_of``).
QUOTES = (
    ("Well begun is half done.", "Aristotle"),
    ("It does not matter how slowly you go as long as you do not stop.", "Confucius"),
    ("Do what you can, with what you have, where you are.", "Theodore Roosevelt"),
    ("The secret of getting ahead is getting started.", "Mark Twain"),
    ("Little by little, one travels far.", "J. R. R. Tolkien"),
    ("Simplicity is the ultimate sophistication.", "Leonardo da Vinci"),
    ("Energy and persistence conquer all things.", "Benjamin Franklin"),
    ("Well done is better than well said.", "Benjamin Franklin"),
    ("Nothing will work unless you do.", "Maya Angelou"),
    ("He who has a why to live can bear almost any how.", "Friedrich Nietzsche"),
    ("The journey of a thousand miles begins with one step.", "Lao Tzu"),
    ("What we think, we become.", "Buddha"),
)
MILESTONE_DAYS = 60       # open milestones due within this many days
DEADLINES_MAX = 6
NEXT_STEPS_MAX = 3
ACTIVITY_MAX = 5
INBOX_MAX = 6
DONE_WINDOW_DAYS = 7
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
class ProjectSummary:
    id: int
    slug: str
    name: str
    status: str                       # active / paused
    open: int                         # open items in Jarvis
    done: int
    next_title: str | None            # the first open item by date, then priority
    next_when: str                    # "tomorrow", "Thu Oct 15", "" (undated)
    repo: str | None = None
    milestone: str | None = None      # the GitHub project's nearest-due open milestone
    milestone_due: str = ""           # "due Oct 19"
    milestone_closed: int = 0
    milestone_total: int = 0
    risk: str = ""                    # the milestone's "On track" / "At risk" ..., else ""
    risk_tone: str = "muted"          # good / warn / bad / muted
    last_activity: str = ""           # "2h ago": the newest change to its items

    @property
    def progress(self) -> float:
        """Done share: the milestone's issues for the GitHub project, else its items."""
        if self.milestone_total:
            return self.milestone_closed / self.milestone_total
        total = self.open + self.done
        return self.done / total if total else 0.0


@dataclass(frozen=True)
class Overview:
    done_week: int                    # completed in the last DONE_WINDOW_DAYS days
    open: int
    overdue: int


@dataclass(frozen=True)
class Focus:
    """One thing that needs attention (an attention.Signal, compact)."""
    head: str                         # "Lab report", "4 things due Tue Oct 13"
    note: str                         # "2 days overdue"
    kind: str                         # the signal kind
    tone: str                         # red / amber / blue / grey
    item_id: int | None = None        # when it is one of the user's items
    url: str | None = None            # a Canvas entry


@dataclass(frozen=True)
class Glance:
    overdue: int
    due_week: int                     # dated items and Canvas entries in the next 7 days
    at_risk: int                      # projects whose milestone is at risk or overdue
    done_week: int


@dataclass(frozen=True)
class Recommendation:
    text: str                         # attention.suggestion, capitalised
    ask: str                          # what "Plan it" sends to the chat


@dataclass(frozen=True)
class Stat:
    label: str
    value: str | None                 # None: not tracked (no data source)
    delta: str = ""                   # "+3 vs last week"
    tone: str = "muted"               # good / bad / muted


@dataclass(frozen=True)
class WeekBar:
    day: date
    label: str                        # "Mon"
    study: int                        # Canvas entries due that day
    work: int                         # items in a project
    personal: int                     # items with no project
    today: bool = False


@dataclass(frozen=True)
class Activity:
    ago: str                          # "2h ago"
    text: str                         # "Completed: Lab report"
    kind: str                         # done / added / commit / canvas
    when: str = ""                    # ISO, for ordering


@dataclass(frozen=True)
class Slot:
    """A timed thing on a day (Today's Schedule)."""
    time: str                         # "9:00 AM"; "" = any time; "Late" / a day if late
    title: str
    category: str                     # Study / Work / Personal / Exam
    item_id: int | None = None
    url: str | None = None
    late: bool = False                # overdue (shown on today's page)


@dataclass(frozen=True)
class Quote:
    text: str
    author: str


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
    inbox: tuple[TaskRowView, ...] = ()    # the first INBOX_MAX
    inbox_total: int = 0
    projects: tuple[ProjectSummary, ...] = ()
    overview: Overview | None = None
    name: str = ""                                    # for the greeting; "" = none set
    focus: tuple[Focus, ...] = ()
    glance: Glance | None = None
    top_priority: Focus | None = None
    top_why: str = ""                                 # the full sentence behind it
    recommendation: Recommendation | None = None
    stats: tuple[Stat, ...] = ()
    week: tuple[WeekBar, ...] = ()
    recent: tuple[Activity, ...] = ()
    schedule: tuple[tuple[date, tuple[Slot, ...]], ...] = ()   # today and the next days
    upcoming: tuple[Deadline, ...] = ()               # dated, after today
    deadlines_total: int = 0
    quote: Quote | None = None

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


def salutation(now: datetime, name: str = "") -> str:
    """"Good morning, Tal" (or "Good morning" with no name)."""
    return greeting(now).rstrip(".") + (f", {name}" if name else "")


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
               project_name: str = "", repo: str = "", name: str = "") -> HomeData:
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
    try:
        for c in canvas.upcoming(conn, today, today + timedelta(days=UPCOMING_DAYS)):
            deadlines.append(Deadline(c.day, day_label(c.day, today), _clock(c.when[11:16]),
                                      c.title, "exam" if c.commitment else "canvas",
                                      project=c.course_short, url=c.url))
    except sqlite3.Error as e:
        errors.append(f"Could not read Canvas: {e}")
    deadlines.sort(key=lambda x: (x.day, x.time or "99", x.kind != "milestone", x.title))

    inbox: list[TaskRowView] = []
    inbox_total = 0
    summaries: list[ProjectSummary] = []
    overview = None
    try:
        projects = {p.id: p for p in it.list_projects(conn, include_archived=True)}
        loose = [i for i in it.someday_items(conn) if i.project_id is None]
        inbox_total = len(loose)
        inbox = [row_view(i, "someday", local, projects) for i in loose[:INBOX_MAX]]
        summaries = _projects(conn, projects, local, today, repo)
        week_ago = (local - timedelta(days=DONE_WINDOW_DAYS)).astimezone(UTC).isoformat(
            timespec="seconds")                      # completed_at is UTC, like this
        done_week = conn.execute(
            "SELECT count(*) FROM items WHERE status = 'done' AND deleted_at IS NULL "
            "AND completed_at >= ?", (week_ago,)).fetchone()[0]
        open_n = conn.execute("SELECT count(*) FROM items WHERE status = 'open' "
                              "AND deleted_at IS NULL").fetchone()[0]
        overview = Overview(done_week, open_n, len(overdue))
    except (sqlite3.Error, it.ItemError) as e:
        errors.append(f"Could not read the inbox and projects: {e}")

    extra: dict[str, Any] = {}
    try:
        extra.update(_attention(conn, local, summaries, overview))
    except (sqlite3.Error, it.ItemError, ValueError) as e:
        errors.append(f"Could not work out what needs attention: {e}")
    try:
        extra["stats"] = _stats(conn, local, overview)
        extra["week"] = _week(conn, today)
        extra["recent"] = _recent(conn, local, brain)
        extra["schedule"] = _schedule(conn, local)
    except (sqlite3.Error, it.ItemError, ValueError) as e:
        errors.append(f"Could not read this week: {e}")
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
        errors=tuple(errors), inbox=tuple(inbox), inbox_total=inbox_total,
        projects=tuple(summaries), overview=overview,
        name=name, deadlines_total=len(deadlines),
        upcoming=tuple(d for d in deadlines if d.day > today)[:UPCOMING_MAX],
        quote=quote_of(today), **extra)


def _projects(conn: sqlite3.Connection, projects: dict[int, it.Project], local: datetime,
              today: date, repo: str) -> list[ProjectSummary]:
    """Active and paused projects: their open/done counts and next open item;
    the one mirroring ``repo`` also gets its nearest-due open milestone."""
    counts: dict[tuple[int, str], int] = {
        (r[0], r[1]): r[2] for r in conn.execute(
            "SELECT project_id, status, count(*) FROM items WHERE deleted_at IS NULL "
            "AND project_id IS NOT NULL GROUP BY project_id, status")}
    out = []
    for p in projects.values():
        if p.status == "archived":
            continue
        nxt = next(iter(it.open_items(conn, project=p.id)), None)
        when = ""
        if nxt is not None and nxt.when:
            when = day_label(date.fromisoformat(nxt.when[:10]), today)
        ms: dict[str, Any] = {}
        if repo and p.github_repo and p.github_repo.lower() == repo.lower():
            r = conn.execute(
                "SELECT number, title, due_on, open_issues, closed_issues FROM milestones "
                "WHERE state = 'open' ORDER BY due_on IS NULL, due_on, number LIMIT 1"
            ).fetchone()
            if r is not None:
                closed, opened = r["closed_issues"] or 0, r["open_issues"] or 0
                d = _milestone_day(r["due_on"])
                ms = {"milestone": r["title"], "milestone_closed": closed,
                      "milestone_total": closed + opened,
                      "milestone_due": f"due {day_label(d, today)}" if d else "no due date"}
                closed_on = []
                for c in conn.execute("SELECT closed_at FROM issues WHERE milestone_number = ? "
                                      "AND state = 'closed' AND closed_at IS NOT NULL",
                                      (r["number"],)):
                    try:
                        closed_on.append(datetime.fromisoformat(
                            c[0].replace("Z", "+00:00")).astimezone(local.tzinfo).date())
                    except ValueError:
                        continue
                risk = assess_risk(MilestoneBar(r["number"], r["title"], d, closed,
                                                closed + opened), closed_on, today)
                ms.update(risk=risk.label, risk_tone=risk.tone)
        touched = conn.execute("SELECT max(updated_at) FROM items WHERE project_id = ? "
                               "AND deleted_at IS NULL", (p.id,)).fetchone()[0]
        if touched:
            ms["last_activity"] = _ago(datetime.fromisoformat(touched), local)
        out.append(ProjectSummary(p.id, p.slug, p.name, p.status,
                                  counts.get((p.id, "open"), 0), counts.get((p.id, "done"), 0),
                                  nxt.title if nxt else None, when, p.github_repo, **ms))
    out.sort(key=lambda s: (s.status != "active", s.name.lower()))
    return out


def _clock(hhmm: str) -> str:
    if not hhmm:
        return ""
    h, m = map(int, hhmm.split(":"))
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def quote_of(today: date) -> Quote:
    """The quote for a day: the same all day, a different one tomorrow."""
    text, author = QUOTES[today.toordinal() % len(QUOTES)]
    return Quote(text, author)


def _ago(when: datetime, now: datetime) -> str:
    secs = max(0, int((now - when).total_seconds()))
    if secs < 3600:
        return f"{max(1, secs // 60)}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    days = secs // 86400
    return "yesterday" if days == 1 else f"{days}d ago"


def _item_id(ref: str) -> int | None:
    return int(ref[5:]) if ref.startswith("item:") and ref[5:].isdigit() else None


def _why(sig: attention.Signal) -> str:
    """The reason line under the top priority: its note (and what a group holds)."""
    note = sig.note[:1].upper() + sig.note[1:] if sig.note else sig.text
    if sig.kind == "cluster" and sig.details:
        return ", ".join(sig.details[:4]) + (f" and {len(sig.details) - 4} more"
                                             if len(sig.details) > 4 else "")
    return note + "."


def _focus(sig: attention.Signal, urls: dict[str, str | None]) -> Focus:
    ref = sig.refs[0] if sig.refs else ""
    return Focus(sig.head or sig.text, sig.note, sig.kind, TONES.get(sig.kind, "grey"),
                 _item_id(ref), urls.get(ref))


def _attention(conn: sqlite3.Connection, local: datetime, summaries: list[ProjectSummary],
               overview: Overview | None) -> dict[str, Any]:
    """Today's Focus, the glance counts, the top priority and the recommendation,
    all from attention.picture (the same ranking as the morning summary)."""
    pic = attention.picture(conn, local)
    today = local.date()
    urls = {c.ref: c.url for c in canvas.upcoming(conn, today,
                                                  today + timedelta(days=UPCOMING_DAYS))}
    focus = tuple(_focus(sig, urls) for sig in pic.signals[:FOCUS_MAX])
    # overdue as the Today list counts it (a time already past today is overdue)
    glance = Glance(overview.overdue if overview else len(pic.overdue),
                    len(pic.events) + len(pic.due_soon),
                    sum(1 for p in summaries if p.risk_tone in ("warn", "bad")),
                    overview.done_week if overview else 0)
    top = pic.signals[0] if pic.signals else None
    tip = attention.suggestion(pic)
    ask = ("I'm about to study, what should I focus on?" if pic.events or pic.due_soon
           else "Help me plan this week")
    return {"focus": focus, "glance": glance,
            "top_priority": _focus(top, urls) if top else None,
            "top_why": (_why(top) if top else ""),
            "recommendation": Recommendation(tip[:1].upper() + tip[1:] + ".", ask)}


def _stats(conn: sqlite3.Connection, local: datetime,
           overview: Overview | None) -> tuple[Stat, ...]:
    """Real counts first; what Jarvis has no source for says so (never a number)."""
    monday = local.date() - timedelta(days=local.weekday())
    start = datetime.combine(monday, datetime.min.time(), local.tzinfo)
    prev = start - timedelta(days=7)
    elapsed = local - start

    def done_between(a: datetime, b: datetime) -> int:
        return conn.execute(
            "SELECT count(*) FROM items WHERE status = 'done' AND deleted_at IS NULL "
            "AND completed_at >= ? AND completed_at <= ?",
            (a.astimezone(UTC).isoformat(timespec="seconds"),
             b.astimezone(UTC).isoformat(timespec="seconds"))).fetchone()[0]

    this_week = done_between(start, local)
    last_week = done_between(prev, prev + elapsed)        # the same stretch last week
    diff = this_week - last_week
    delta = f"{diff:+d} vs last week" if diff else "same as last week"
    ov = overview or Overview(0, 0, 0)
    return (Stat("Tasks completed", str(this_week), delta,
                 "good" if diff > 0 else "bad" if diff < 0 else "muted"),
            Stat("Open items", str(ov.open)),
            Stat("Overdue", str(ov.overdue), tone="bad" if ov.overdue else "muted"),
            Stat("Focus time", None), Stat("Sleep", None), Stat("Training", None))


def _week(conn: sqlite3.Connection, today: date) -> tuple[WeekBar, ...]:
    """What is due each day of this week (Mon to Sun): Canvas = Study, items in a
    project = Work, the rest = Personal."""
    monday = today - timedelta(days=today.weekday())
    days = [monday + timedelta(days=k) for k in range(7)]
    counts = {d: [0, 0, 0] for d in days}
    for r in conn.execute(
            "SELECT coalesce(due_at, remind_at) AS w, project_id FROM items "
            "WHERE deleted_at IS NULL AND coalesce(due_at, remind_at) IS NOT NULL "
            "AND substr(coalesce(due_at, remind_at), 1, 10) BETWEEN ? AND ?",
            (days[0].isoformat(), days[-1].isoformat())):
        counts[date.fromisoformat(r["w"][:10])][1 if r["project_id"] else 2] += 1
    for c in canvas.upcoming(conn, days[0], days[-1]):
        counts[c.day][0] += 1
    return tuple(WeekBar(d, f"{d:%a}", *counts[d], today=d == today) for d in days)


def _recent(conn: sqlite3.Connection, local: datetime, brain: Any) -> tuple[Activity, ...]:
    """Items added or completed, Canvas entries marked done and commits, newest first."""
    since = (local - timedelta(days=RECENT_DAYS)).astimezone(UTC).isoformat(timespec="seconds")
    out: list[tuple[datetime, str, str]] = []
    for r in conn.execute("SELECT title, created_at, completed_at FROM items WHERE "
                          "deleted_at IS NULL AND (created_at >= ? OR completed_at >= ?)",
                          (since, since)):
        if r["completed_at"] and r["completed_at"] >= since:
            out.append((datetime.fromisoformat(r["completed_at"]), f"Completed: {r['title']}",
                        "done"))
        if r["created_at"] >= since:
            out.append((datetime.fromisoformat(r["created_at"]), f"Added: {r['title']}",
                        "added"))
    if canvas.has_table(conn):
        for r in conn.execute("SELECT title, done_at FROM canvas_events WHERE done_at >= ?",
                              (since,)):
            out.append((datetime.fromisoformat(r["done_at"]), f"Marked done: {r['title']}",
                        "canvas"))
    for st in getattr(brain, "stream", None) or ():
        if getattr(st, "kind", None) == "commit":
            try:
                when = datetime.fromisoformat(st.when)
            except (TypeError, ValueError):
                continue
            if when.astimezone(UTC).isoformat(timespec="seconds") >= since:
                out.append((when, f"Commit: {st.text}", "commit"))
    out.sort(key=lambda x: x[0], reverse=True)
    return tuple(Activity(_ago(w, local), text, kind, w.isoformat())
                 for w, text, kind in out[:RECENT_MAX])


def _schedule(conn: sqlite3.Connection, local: datetime) -> tuple[tuple[date, tuple[Slot, ...]], ...]:
    """Each day's items and Canvas entries, today and the next days: overdue first
    (today only), then the timed ones by time, then the rest ("any time"). Times
    only as recorded: Jarvis has no calendar and never invents slots."""
    today = local.date()
    days = [today + timedelta(days=k) for k in range(SCHEDULE_DAYS)]
    slots: dict[date, list[tuple[tuple, Slot]]] = {d: [] for d in days}
    clock = local.strftime("%Y-%m-%dT%H:%M")
    for i in it.open_items(conn):
        when = i.due_at or i.remind_at
        if not when:
            continue
        d = date.fromisoformat(when[:10])
        cat = ("Exam" if planning._EVENT_RE.search(i.title)
               else "Work" if i.project_id else "Personal")
        late = d < today or ("T" in when and when < clock and d == today)
        if late:
            label = "Late" if d == today else day_label(d, today)
            slots[today].append(((0, when), Slot(label, i.title, cat, item_id=i.id,
                                                 late=True)))
        elif d in slots:
            timed = "T" in when
            slots[d].append(((1 if timed else 2, when), Slot(
                _clock(when[11:16]) if timed else "", i.title, cat, item_id=i.id)))
    for c in canvas.upcoming(conn, days[0], days[-1]):
        timed = "T" in c.when
        if timed and c.when < clock:
            continue                         # a course event earlier today is over
        slots[c.day].append(((1 if timed else 2, c.when), Slot(
            _clock(c.when[11:16]) if timed else "", c.label,
            "Exam" if c.commitment else "Study", url=c.url)))
    return tuple((d, tuple(sl for _, sl in sorted(slots[d], key=lambda x: x[0])))
                 for d in days)
