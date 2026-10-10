"""What needs the user's attention, computed from their records (no Qt, no model).

Jarvis's race-team view: code reads the user's own open items and works out
the signals that matter, each with its reason, so the important part of a
morning summary is instant and can't get a date wrong:

- ``today``    due today, or a fixed commitment today;
- ``event``    an exam, appointment or other fixed commitment in the next week,
               with the days left before it;
- ``overdue``  past its date, with how long;
- ``squeeze``  a deadline on the day of a commitment or the day after it;
- ``cluster``  three or more deadlines within two days;
- ``soon``     due in the next three days;
- ``stale``    undated items nobody has touched for weeks (counted).

Canvas entries (``jarvis.canvas``) count like items from today on: an exam or
exam is a commitment, an assignment or quiz a deadline, labelled with its course. A
past Canvas assignment is never "overdue": the feed can't say whether it was
submitted.

``signals`` ranks them; ``daily_brief`` shows the top three (the morning
summary), ``attention_answer`` the full list, and ``study_check`` what is
coming before the user sits down to study. Everything stated is a fact from
the records; the one recommendation in ``study_check`` is labelled as one.
GitHub issues are never in here: they are backlog, not commitments.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from jarvis import canvas, chat, planning
from jarvis import items as it
from jarvis.actions import human_when
from jarvis.chat import Answer, Source

EVENT_DAYS = 7          # commitments this far ahead are worth preparing for
SOON_DAYS = 3           # deadlines this close are mentioned
CLUSTER_SPAN = 2        # days
CLUSTER_MIN = 3         # deadlines in CLUSTER_SPAN days
STALE_DAYS = 21         # undated and untouched this long
BRIEF_TOP = 3           # signals in the morning summary
FULL_TOP = 8            # signals in "what needs my attention"

# lower is more important
RANK = {"today": 0, "event": 1, "overdue": 2, "squeeze": 3, "cluster": 4, "soon": 5,
        "stale": 6}


@dataclass(frozen=True)
class Signal:
    kind: str
    text: str                        # one line: the fact and why it matters
    refs: tuple[str, ...] = ()       # "item:12"
    when: str = ""                   # sort key inside a kind (a date)


@dataclass(frozen=True)
class Rec:
    """One dated thing on the user's plate: their own item or a Canvas entry."""
    ref: str                         # "item:12" / "canvas:event-assignment-3"
    title: str
    when: str                        # stored local wall clock
    event: bool                      # a fixed commitment (exam, appointment, ...)

    @property
    def day(self) -> date:
        return date.fromisoformat(self.when[:10])


@dataclass
class Picture:
    today: date
    signals: list[Signal]
    labels: dict[str, str] = field(default_factory=dict)   # ref -> title
    events: list[Rec] = field(default_factory=list)        # next EVENT_DAYS, soonest first
    due_soon: list[Rec] = field(default_factory=list)      # dated, not events, next week
    overdue: list[Rec] = field(default_factory=list)


def _days(n: int) -> str:
    return f"{n} day{'s' if n != 1 else ''}"


def _short(d: date, today: date) -> str:
    if d == today:
        return "today"
    if d == today + timedelta(days=1):
        return "tomorrow"
    return f"{d:%a %b} {d.day}"


def _before(event_day: date, today: date) -> str:
    """The days left before a commitment, by name."""
    days = [today + timedelta(days=k) for k in range((event_day - today).days)]
    if not days:
        return "no full day left before it"
    names = ", ".join("today" if d == today else f"{d:%a}" for d in days)
    return f"{_days(len(days))} left before it ({names})"


def _records(conn: sqlite3.Connection, today: date) -> tuple[list[Rec], list[it.Item]]:
    """(dated records, undated items). Canvas entries come from today on: the feed
    can't say whether a past assignment was submitted, so none is "overdue"."""
    recs: list[Rec] = []
    undated: list[it.Item] = []
    for i in it.open_items(conn):
        stored = i.due_at or i.remind_at
        if stored:
            recs.append(Rec(f"item:{i.id}", i.title, stored, planning.is_event(i)))
        else:
            undated.append(i)
    for e in canvas.upcoming(conn, today, today + timedelta(days=EVENT_DAYS)):
        recs.append(Rec(e.ref, e.label, e.when, e.commitment))
    return recs, undated


def picture(conn: sqlite3.Connection, now: datetime) -> Picture:
    """Every signal in the user's open items and Canvas entries, most important first."""
    today = now.date()
    pic = Picture(today, [])
    out: list[Signal] = []
    recs, undated = _records(conn, today)
    dated: list[Rec] = []
    for r in recs:
        pic.labels[r.ref] = r.title
        day = r.day
        if day < today:
            pic.overdue.append(r)
            out.append(Signal("overdue", f"{r.title}: {_days((today - day).days)} overdue (was "
                                         f"due {human_when(r.when)})", (r.ref,), r.when))
            continue
        dated.append(r)
        if day == today:
            what = (f"{r.title} {human_when(r.when).split(', ')[-1]} today" if "T" in r.when
                    else f"{r.title} is due today")
            out.append(Signal("today", what, (r.ref,), r.when))
            if r.event:
                pic.events.append(r)
        elif r.event and (day - today).days <= EVENT_DAYS:
            pic.events.append(r)
            out.append(Signal("event", f"{r.title} {human_when(r.when)}: "
                                       f"{_before(day, today)}", (r.ref,), r.when))
        else:
            if (day - today).days <= EVENT_DAYS:
                pic.due_soon.append(r)
            if (day - today).days <= SOON_DAYS:
                out.append(Signal("soon", f"{r.title} is due {_short(day, today)}", (r.ref,),
                                  r.when))
    # a deadline right on or after a commitment competes with preparing for it
    for e in pic.events:
        for r in dated:
            if r.ref != e.ref and not r.event and e.day <= r.day <= e.day + timedelta(days=1):
                rel = "the same day as" if r.day == e.day else "the day after"
                out.append(Signal("squeeze", f"{r.title} is due {_short(r.day, today)}, {rel} "
                                             f"{e.title}: it needs time before then too",
                                  (r.ref, e.ref), r.when))
    # several deadlines in a couple of days
    ahead = sorted((r for r in dated if r.day <= today + timedelta(days=EVENT_DAYS)),
                   key=lambda r: (r.when[:10], r.ref))
    used: set[str] = set()
    for k, r in enumerate(ahead):
        group = [r2 for r2 in ahead[k:] if (r2.day - r.day).days < CLUSTER_SPAN
                 and r2.ref not in used]
        if len(group) >= CLUSTER_MIN:
            used |= {r2.ref for r2 in group}
            last = group[-1].day
            span = (_short(r.day, today) if last == r.day
                    else f"{_short(r.day, today)} to {_short(last, today)}")
            out.append(Signal("cluster", f"{len(group)} deadlines {span}: "
                                         + ", ".join(r2.title for r2 in group),
                              tuple(r2.ref for r2 in group), r.when[:10]))
    stale = sorted((i for i in undated
                    if (today - datetime.fromisoformat(i.updated_at).date()).days >= STALE_DAYS),
                   key=lambda i: i.updated_at)
    if stale:
        oldest = stale[0]
        weeks = (today - datetime.fromisoformat(oldest.updated_at).date()).days // 7
        out.append(Signal("stale", f"{len(stale)} undated item{'s' if len(stale) != 1 else ''} "
                                   f"untouched for {STALE_DAYS // 7}+ weeks (oldest: "
                                   f"{oldest.title}, {weeks} weeks)",
                          tuple(f"item:{i.id}" for i in stale), oldest.updated_at))
        for i in stale:
            pic.labels[f"item:{i.id}"] = i.title
    for lst in (pic.events, pic.due_soon, pic.overdue):
        lst.sort(key=lambda r: (r.when, r.ref))
    out.sort(key=lambda s: (RANK[s.kind], s.when))
    pic.signals = out
    return pic


def _answer(pic: Picture, lines: list[str], refs: list[str]) -> Answer:
    refs = list(dict.fromkeys(refs))
    sources = [Source(r, pic.labels.get(r, r)) for r in refs]
    return Answer(text="\n".join(lines), sources=sources, unverified_numbers=[], notes=[],
                  model=None, duration=0.0, tier="direct",
                  cited_sources=sources[:chat.CITED_SOURCES_MAX],
                  cited_more_count=max(0, len(sources) - chat.CITED_SOURCES_MAX))


def _listed(pic: Picture, top: int, head: str) -> tuple[list[str], list[str]]:
    shown = pic.signals[:top]
    lines = [head] + [f"{k}. {s.text}." for k, s in enumerate(shown, 1)]
    refs = [r for s in shown for r in s.refs]
    return lines, refs


def daily_brief(conn: sqlite3.Connection, now: datetime) -> Answer:
    """The morning summary: the three things that matter most today, by the records."""
    pic = picture(conn, now)
    head = f"**{now:%A %b} {now.day}**: what needs your attention"
    if not pic.signals:
        return _answer(pic, [f"**{now:%A %b} {now.day}**: nothing is due, overdue or coming "
                             "up in the next week on your list."], [])
    lines, refs = _listed(pic, BRIEF_TOP, head)
    if not any(s.kind == "today" for s in pic.signals):
        lines.append("Nothing is due today.")
    more = len(pic.signals) - BRIEF_TOP
    if more > 0:
        lines.append(f"{more} more thing{'s' if more != 1 else ''} to watch; ask “what needs "
                     "my attention” for the full list.")
    return _answer(pic, lines, refs)


def attention_answer(conn: sqlite3.Connection, now: datetime) -> Answer:
    """Every signal, most important first (capped)."""
    pic = picture(conn, now)
    if not pic.signals:
        return _answer(pic, ["Nothing needs attention: nothing is overdue, due soon or "
                             "coming up in the next week on your list."], [])
    lines, refs = _listed(pic, FULL_TOP, "What needs your attention, most important first:")
    more = len(pic.signals) - FULL_TOP
    if more > 0:
        lines.append(f"(and {more} more)")
    return _answer(pic, lines, refs)


def study_check(conn: sqlite3.Connection, now: datetime) -> Answer:
    """Before a study session: the next commitments, what is due around them and
    what is overdue, then one labelled suggestion. Jarvis doesn't know which
    tasks are study, so this goes by dates."""
    pic = picture(conn, now)
    today = pic.today
    lines = ["**Before you study** (by the dates on your list):"]
    refs: list[str] = []
    upcoming = [e for e in pic.events if e.day >= today]
    if upcoming:
        for e in upcoming[:2]:
            lines.append(f"- {e.title}, {human_when(e.when)}: {_before(e.day, today)}.")
            refs.append(e.ref)
    else:
        lines.append(f"- No exam or other fixed commitment in the next {EVENT_DAYS} days.")
    if pic.due_soon:
        lines.append("- Due in the next week: " + ", ".join(
            f"{r.title} ({_short(r.day, today)})" for r in pic.due_soon[:4])
            + (f", and {len(pic.due_soon) - 4} more" if len(pic.due_soon) > 4 else "") + ".")
        refs += [r.ref for r in pic.due_soon[:4]]
    if pic.overdue:
        lines.append("- Overdue: " + ", ".join(
            f"{r.title} ({_days((today - r.day).days)})" for r in pic.overdue[:3]) + ".")
        refs += [r.ref for r in pic.overdue[:3]]
    if upcoming:
        e = upcoming[0]
        tip = f"{e.title} is the soonest fixed commitment, so it is the natural focus"
        squeezed = [s for s in pic.signals if s.kind == "squeeze" and e.ref in s.refs]
        if squeezed:
            tip += f"; {pic.labels.get(squeezed[0].refs[0], 'one deadline')} lands right after it"
    elif pic.due_soon:
        r = pic.due_soon[0]
        tip = f"{r.title} is due soonest ({_short(r.day, today)})"
    elif pic.overdue:
        tip = f"nothing is coming up; {pic.overdue[0].title} is the oldest overdue item"
    else:
        tip = "nothing on your list is pressing by date: a good time for longer-term work"
    lines.append(f"Suggestion: {tip}.")
    return _answer(pic, lines, refs)
