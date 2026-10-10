"""Evidence for a routed question, and the answers code can give by itself (no Qt).

``intent.route`` decides what a message asks for; this module fetches the
records that answer it and, for straightforward lookups, writes the answer
without a model:

- ``agenda``: the user's items dated in the timeframe (plus overdue ones when
  it starts today) and GitHub milestones due in it. Undated items and the
  project backlog are counted, never listed as due.
- ``capture``: a reply pointing to the capture form (the user confirms there).
- ``recall`` with nothing found: says so, and what Jarvis keeps.

Everything else goes to the model with ``build_routed_context``: only the
sections the route needs, each line tagged with its reference ([item:12],
[chat:40], [#31], ...), so answers stay checkable (``chat.validate_answer``).

Memory is what Jarvis already stores: past chat messages (the user's words
are labelled as theirs, Jarvis's earlier answers as answers, not facts) and the
task list, searched by the question's words, newest first among equals.
Nothing is written here.
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

from jarvis import chat, planning
from jarvis import items as it
from jarvis.chat import Answer, Context, Source
from jarvis.config import Config
from jarvis.facts import tokens
from jarvis.intent import Route

UNDATED_LISTED = 8          # undated items named in a model context
DATED_AHEAD_DAYS = planning.HORIZON_DAYS   # dated items shown beyond the timeframe
MEMORY_HITS = 6
MEMORY_EXCERPT = 240
_RECALL_NOISE = frozenset(tokens(
    "what did decide decided say said tell told ask asked mention mentioned choose chose "
    "pick picked agree agreed settle settled remember recall last week weeks month earlier "
    "before previously time talk talked discuss discussed about we you jarvis conclude "
    "concluded note noted"))

ASSISTANT_PROMPT = """\
You are Jarvis, the user's local personal assistant. The CONTEXT holds what Jarvis \
retrieved for this question: possibly YOUR ITEMS (the user's own task list from Jarvis's \
database), MEMORY (earlier conversations and items, with dates) and project data (synced \
GitHub issues, milestones, commits, computed facts, hardware checks). Every CONTEXT line \
starts with its reference in square brackets.

Rules:
- Answer the question first, in plain words. Be concise: at most 6 sentences or a short \
list of at most 8 items. No preamble, no praise, no exclamation marks.
- Use only the CONTEXT for anything about the user, their tasks, their project or their \
past conversations. If it doesn't contain the answer, say so in one sentence and say what \
is missing. Never claim to remember, or to have checked, anything that isn't in the CONTEXT.
- Keep the user's own items and the project backlog apart. Only an item's own date makes it \
due on a day. Open GitHub issues, suggested next steps and undated items are backlog, not \
obligations for a day: never present them as things the user has to do today.
- Never decide for the user: don't prioritize, schedule, reschedule or change anything. When \
asked what to work on, lay out the relevant options with the facts that matter (due dates, \
what is overdue, what a step unblocks, hedges such as "may already be done") and leave the \
choice to the user. Mark your own suggestions as suggestions.
- MEMORY: "you said" lines are the user's own words; "Jarvis answered" lines are earlier \
answers, not facts the user confirmed. When they disagree, the newest wins; say it changed.
- Quote dates and counts exactly as the CONTEXT gives them; do no arithmetic. An issue is \
done only if it is closed; items marked (uncertain) are guesses, say "may".
- Refer to issues as #N, commits by their 7-character sha, items by their title. Never write \
the square-bracket references in the answer itself.
- End with one last line: "SOURCES: " then the references you used, without brackets, \
separated by commas, for example: SOURCES: item:12, #31, chat:40. Write "SOURCES: none" if \
you used none.
"""

GENERAL_PROMPT = """\
You are Jarvis, the user's local assistant, running on a local model without internet \
access. This question doesn't need the user's stored data. Answer from general knowledge, \
concisely (at most 6 sentences), neutral in tone. If it needs live information (weather, \
news, prices, scores, the time somewhere), say you have no internet access. Don't claim \
anything about the user's tasks, project or past conversations. End with the line: \
SOURCES: none
"""



# ---------------------------------------------------------------- agenda


@dataclass(frozen=True)
class Entry:
    ref: str                 # "item:12" or "milestone:Core demo"
    title: str
    day: date | None
    time: str                # "5:00 PM" or ""
    kind: str                # task / idea / reminder / milestone
    project: str | None = None
    late_days: int = 0
    url: str | None = None

    @property
    def source(self) -> Source:
        return Source(self.ref, self.title, self.url)


@dataclass
class Agenda:
    start: date
    end: date
    span: str
    today: date
    overdue: list[Entry] = field(default_factory=list)
    due: list[Entry] = field(default_factory=list)
    milestones: list[Entry] = field(default_factory=list)
    later: Entry | None = None          # the next dated item after the timeframe
    undated: int = 0                    # open items with no date
    project: str | None = None


def _local(now: datetime | None) -> datetime:
    """``now`` in its own zone when it has one (the system's otherwise)."""
    if now is not None and now.tzinfo is not None:
        return now
    return it.local_now(now)


def _clock(when: str) -> str:
    if len(when) < 16:
        return ""
    h, m = int(when[11:13]), int(when[14:16])
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def _entry(i: it.Item, projects: dict[int, it.Project], today: date) -> Entry:
    d = date.fromisoformat(i.when[:10]) if i.when else None
    p = projects.get(i.project_id) if i.project_id is not None else None
    late = (today - d).days if d and d < today else 0
    return Entry(f"item:{i.id}", i.title, d, _clock(i.when or ""), i.kind,
                 p.name if p else None, late)


def build_agenda(conn: sqlite3.Connection, start: date, end: date, span: str,
                 now: datetime, project: str | None = None) -> Agenda:
    """The user's items dated from ``start`` to ``end`` (overdue ones too when the
    timeframe starts today), milestones due in it, and what is left out (counted)."""
    local = _local(now)
    today = local.date()
    ag = Agenda(start, end, span, today, project=project)
    projects = {p.id: p for p in it.list_projects(conn, include_archived=True)}
    chosen = next((p for p in projects.values() if p.slug == project), None)
    pid = chosen.id if chosen else None
    open_items = [i for i in it.open_items(conn) if pid is None or i.project_id == pid]
    if start <= today:
        ag.overdue = [_entry(i, projects, today)
                      for i in it.overdue_items(conn, local, local.tzinfo)
                      if pid is None or i.project_id == pid]
    late_ids = {e.ref for e in ag.overdue}
    for i in open_items:
        if not i.when:
            ag.undated += 1
            continue
        d = date.fromisoformat(i.when[:10])
        e = _entry(i, projects, today)
        if e.ref in late_ids:
            continue
        if start <= d <= end:
            ag.due.append(e)
        elif d > end and (ag.later is None or (d, e.time) < (ag.later.day, ag.later.time)):
            ag.later = e
    ag.due.sort(key=lambda e: (e.day, e.time == "", _time_key(e.time)))
    if chosen is None or chosen.github_repo:      # milestones belong to the GitHub project
        try:
            for r in conn.execute("SELECT title, due_on, html_url FROM milestones "
                                  "WHERE state = 'open' AND due_on IS NOT NULL"):
                d = date.fromisoformat(r["due_on"][:10])
                if start <= d <= end:
                    ag.milestones.append(Entry(f"milestone:{r['title']}", r["title"], d, "",
                                               "milestone", url=r["html_url"]))
        except (sqlite3.Error, ValueError):
            pass
    return ag


def _time_key(t: str) -> tuple[int, int]:
    if not t:
        return (0, 0)
    h, rest = t.split(":")
    m, ampm = rest.split()
    return ((int(h) % 12) + (12 if ampm == "PM" else 0), int(m))


def _day(d: date, today: date) -> str:
    delta = (d - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    return f"{d:%a %b} {d.day}"


def _line(e: Entry, ag: Agenda, with_day: bool) -> str:
    bits = []
    if with_day and e.day:
        bits.append(_day(e.day, ag.today))
    if e.time:
        bits.append(e.time)
    if e.kind in ("reminder", "idea"):
        bits.append(e.kind)
    if e.project:
        bits.append(e.project)
    return f"- {e.title}" + (f" ({', '.join(bits)})" if bits else "")


def agenda_answer(ag: Agenda) -> Answer:
    """The agenda in plain words: what is due (and overdue), nothing promoted from
    the backlog; what was left out is counted, not listed."""
    span = ag.span
    single = ag.start == ag.end
    lines: list[str] = []
    sources: list[Source] = []
    scope = f" for {ag.project}" if ag.project else ""
    if ag.due:
        lines.append(f"Due {span}{scope}:")
        lines += [_line(e, ag, not single) for e in ag.due]
        sources += [e.source for e in ag.due]
    if ag.overdue:
        lines.append("Overdue (from before today):" if ag.due else "Overdue:")
        lines += [f"- {e.title} (was due {_day(e.day, ag.today)}, "
                  f"{e.late_days} day{'s' if e.late_days != 1 else ''} ago)"
                  for e in ag.overdue if e.day]
        sources += [e.source for e in ag.overdue]
    if not ag.due:
        lines.insert(0, f"Nothing is explicitly due {span}{scope}."
                     if not ag.overdue else f"Nothing new is due {span}{scope}.")
    if ag.milestones:
        names = ", ".join(f"{m.title} ({_day(m.day, ag.today)})" for m in ag.milestones)
        lines.append(f"On GitHub, milestone{'s' if len(ag.milestones) > 1 else ''} due "
                     f"{span}: {names}.")
        sources += [m.source for m in ag.milestones]
    rest = []
    if ag.undated:
        rest.append(f"{ag.undated} open item{'s have' if ag.undated != 1 else ' has'} no date")
    if ag.later and ag.later.day:
        rest.append(f"the next dated item is {ag.later.title} ({_day(ag.later.day, ag.today)})")
        sources.append(ag.later.source)
    if rest:
        tail = "; ".join(rest)
        lines.append(f"{tail[0].upper()}{tail[1:]}." + (
            " Open project issues aren't scheduled for any day." if ag.due or ag.overdue
            else ""))
    return Answer(text="\n".join(lines), sources=sources, unverified_numbers=[], notes=[],
                  model=None, duration=0.0, tier="direct",
                  cited_sources=sources[:chat.CITED_SOURCES_MAX],
                  cited_more_count=max(0, len(sources) - chat.CITED_SOURCES_MAX))


def capture_answer(text: str) -> Answer:
    """Capture goes through the capture form, where the user checks and saves it."""
    what = f"“{text}”" if text else "that"
    return Answer(text=f"I can add {what} to your list. Check the details in Capture and "
                       "press Enter to save it; nothing is saved until you do.",
                  sources=[], unverified_numbers=[], notes=[], model=None, duration=0.0,
                  tier="direct", action={"kind": "capture", "text": text})


# ---------------------------------------------------------------- memory


@dataclass(frozen=True)
class Memory:
    ref: str                 # "chat:40" or "item:12"
    day: date | None
    who: str                 # "you said" / "Jarvis answered" / "item"
    text: str


def _terms(question: str) -> set[str]:
    return chat.question_terms(question) - _RECALL_NOISE


def search_memory(conn: sqlite3.Connection, question: str, now: datetime,
                  limit: int = MEMORY_HITS) -> list[Memory]:
    """Past chat messages and items sharing the question's words, best first.
    The question itself (just saved by the chat) is not a memory."""
    terms = _terms(question)
    if not terms:
        return []
    days = chat.time_window(question)
    since = (now - timedelta(days=days * 2 if days else 3650)).astimezone(UTC).isoformat()
    scored: list[tuple[int, str, Memory]] = []
    q = question.strip()
    try:
        for r in conn.execute("SELECT id, role, content, created_at FROM chat_messages "
                              "WHERE created_at >= ? ORDER BY id DESC LIMIT 2000", (since,)):
            content = r["content"] or ""
            if content.strip() == q:
                continue
            hit = len(terms & set(tokens(content)))
            if hit:
                d = chat._parse_ts(r["created_at"])
                who = "you said" if r["role"] == "user" else "Jarvis answered"
                scored.append((hit, r["created_at"] or "", Memory(
                    f"chat:{r['id']}", d.astimezone(now.tzinfo).date() if d else None, who,
                    chat._one_line(content, MEMORY_EXCERPT))))
        for i in it.list_items(conn, status="all"):
            hit = len(terms & set(tokens(f"{i.title} {i.body or ''}")))
            if hit:
                d = chat._parse_ts(i.created_at)
                state = "done" if i.done else "open"
                scored.append((hit, i.created_at, Memory(
                    f"item:{i.id}", d.astimezone(now.tzinfo).date() if d else None,
                    f"item ({state})",
                    chat._one_line(f"{i.title}. {i.body or ''}".strip(" ."), MEMORY_EXCERPT))))
    except (sqlite3.Error, it.ItemError):
        return []
    best = sorted(scored, key=lambda s: (-s[0], _neg(s[1])))[:limit]
    return [m for _, _, m in best]


def _neg(ts: str) -> str:
    """Sort key for newest-first among equal scores."""
    return "".join(chr(0x10FFFF - ord(c)) for c in ts)


def recall_gap_answer(question: str) -> Answer:
    terms = sorted(_terms(question))
    what = f" about {' '.join(terms[:4])}" if terms else ""
    return Answer(text=f"I couldn't find anything{what} in earlier conversations or your "
                       "items. Jarvis keeps its chat history and your task list, and has no "
                       "other notes, so I can't tell you more than that.",
                  sources=[], unverified_numbers=[], notes=[], model=None, duration=0.0,
                  tier="direct")


# ---------------------------------------------------------------- model context


def _items_block(conn: sqlite3.Connection, now: datetime, route: Route,
                 refs: dict[str, Source]) -> list[str]:
    today = _local(now).date()
    start = route.start or today
    end = max(route.end or today, today + timedelta(days=DATED_AHEAD_DAYS))
    ag = build_agenda(conn, start, end, route.span or "soon", now, project=route.project)
    out = ["YOUR ITEMS (the user's own task list in Jarvis; only an item's own date puts it "
           "on a day):"]

    def fmt(e: Entry, prefix: str) -> str:
        refs[e.ref] = e.source
        bits = [prefix] if prefix else []
        if e.time:
            bits.append(e.time)
        bits.append(e.kind)
        if e.project:
            bits.append(f"project {e.project}")
        return f"[{e.ref}] {e.title} ({', '.join(bits)})"

    for e in ag.overdue:
        out.append(fmt(e, f"overdue: was due {_day(e.day, today)}, {e.late_days} days ago"))
    for e in ag.due:
        out.append(fmt(e, f"due {_day(e.day, today)}"))
    if not ag.overdue and not ag.due:
        out.append(f"(nothing dated between today and {_day(end, today)})")
    projects = {p.id: p for p in it.list_projects(conn, include_archived=True)}
    undated = [i for i in it.someday_items(conn, project=route.project)]
    for i in undated[:UNDATED_LISTED]:
        out.append(fmt(_entry(i, projects, today), "no date"))
    if len(undated) > UNDATED_LISTED:
        out.append(f"(and {len(undated) - UNDATED_LISTED} more undated items)")
    return out


def _memory_block(conn: sqlite3.Connection, question: str, now: datetime,
                  refs: dict[str, Source]) -> list[str]:
    hits = search_memory(conn, question, now)
    out = ["MEMORY (earlier conversations and items that share the question's words; "
           "newest first among equals):"]
    for m in hits:
        refs[m.ref] = Source(m.ref, f"{m.who}: {m.text[:60]}")
        when = chat._fmt_day(m.day) if m.day else "undated"
        out.append(f"[{m.ref}] {when}, {m.who}: {m.text}")
    return out


def build_routed_context(conn: sqlite3.Connection, cfg: Config, question: str,
                         now: datetime | None, route: Route, *,
                         history: Sequence[tuple[str, str]] = (), embedder: Any = None,
                         budget_tokens: int = chat.CONTEXT_TOKEN_BUDGET,
                         stable_prefix: bool = False, focus: Sequence[str] = ()) -> Context:
    """The context for a routed model answer: only the sections ``route`` needs.
    Project data (when needed) is chat.build_context's; the user's items and the
    memory block follow it, in the question-dependent part."""
    now = _local(now)
    extra_refs: dict[str, Source] = {}
    extra: list[str] = []
    if route.intent == "project" and not route.project:
        route = replace(route, project=_repo_project(conn, cfg.repo))
    if route.personal or route.project:
        extra += _items_block(conn, now, route, extra_refs)
    if route.intent == "recall":
        extra += ["", *_memory_block(conn, question, now, extra_refs)]
    if route.project_data:
        budget = max(1500, budget_tokens - sum(len(x) for x in extra) // chat.CHARS_PER_TOKEN)
        ctx = chat.build_context(conn, cfg, question, now, history=history, embedder=embedder,
                                 budget_tokens=budget, stable_prefix=stable_prefix, focus=focus)
    else:
        today = now.date()
        head = [f"Today: {now:%A} {chat._fmt_day(today)}, {today.year} ({today.isoformat()}), "
                f"local time {now:%H:%M}."]
        issues = {r[0] for r in conn.execute("SELECT number FROM issues")}
        shas = [r[0] for r in conn.execute("SELECT sha FROM commits")]
        ctx = Context(text="\n".join(head), refs={}, issue_numbers=issues, commit_shas=shas,
                      today=today)
    if extra:
        ctx.text = ctx.text.rstrip() + "\n\n" + "\n".join(extra)
        ctx.refs.update(extra_refs)
    return ctx


def _repo_project(conn: sqlite3.Connection, repo: str) -> str | None:
    """The slug of the Jarvis project that mirrors the GitHub repo, if any."""
    try:
        r = conn.execute("SELECT slug FROM projects WHERE github_repo = ? COLLATE NOCASE",
                         (repo,)).fetchone()
    except sqlite3.Error:
        return None
    return r["slug"] if r else None


def system_prompt(route: Route | None) -> str | None:
    """The system prompt for a routed model answer (None: chat's project prompt).
    Routes that use the project data also get chat's project-specific rules."""
    if route is None:
        return None
    if route.intent == "general":
        return GENERAL_PROMPT
    if route.project_data:
        head, end = ASSISTANT_PROMPT.rsplit("- End with one last line", 1)
        return (head + chat.PROJECT_RULES_A + chat.PROJECT_RULES_B
                + "- End with one last line" + end)
    return ASSISTANT_PROMPT


def item_ref(ref: str) -> int | None:
    m = re.fullmatch(r"item:(\d+)", ref)
    return int(m[1]) if m else None
