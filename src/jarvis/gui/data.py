"""View models for the window, loaded from SQLite. No Qt here, so it is unit-testable.

The window only reads: the newest stored briefing, the active cards, and the
evidence behind each briefing sentence (from ``facts.compute_facts``). Writes go
through ``cards.mark_done`` / ``cards.set_feedback`` in the window itself.
"""
from __future__ import annotations

import json
import sqlite3
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from jarvis.cards import Card, list_cards

GITHUB = "https://github.com"
MAX_LINKS_PER_SENTENCE = 6
NEW_CARD_HOURS = 24

# Display order and labels for card groups. Unknown types are appended after these.
CARD_TYPES: tuple[tuple[str, str], ...] = (
    ("electrical", "Electrical"),
    ("research", "Research"),
)


@dataclass(frozen=True)
class EvidenceLink:
    label: str  # "#28", "a1b2c3d", "Milestone Beta"
    url: str


@dataclass(frozen=True)
class SentenceView:
    text: str
    uncertain: bool
    fact_ids: tuple[str, ...]
    links: tuple[EvidenceLink, ...] = ()


@dataclass(frozen=True)
class BriefingView:
    headline: str
    sentences: tuple[SentenceView, ...]
    source: str  # "model" | "template"
    model: str | None
    created_at: str  # ISO 8601 UTC

    @property
    def source_label(self) -> str:
        return (self.model or "model") if self.source == "model" else "template"


@dataclass(frozen=True)
class CardView:
    card: Card
    is_new: bool = False

    @property
    def id(self) -> int:
        return self.card.id


@dataclass(frozen=True)
class CardGroup:
    type: str
    label: str
    cards: tuple[CardView, ...]


@dataclass(frozen=True)
class WatchView:
    """The daemon heartbeat line in the header ("watching · checked 2 min ago")."""

    watching: bool
    text: str
    tooltip: str = ""


@dataclass(frozen=True)
class WindowData:
    repo: str
    briefing: BriefingView | None
    groups: tuple[CardGroup, ...]
    last_sync: str | None  # ISO 8601 UTC of the newest sync_log row
    errors: tuple[str, ...] = field(default_factory=tuple)
    watch: WatchView | None = None
    # Dashboard view models (see the "dashboard" section below).
    kpis: tuple[Kpi, ...] = ()
    timeline: Timeline | None = None
    grid: ContributionGrid | None = None
    phases: PhaseBars | None = None
    power: tuple[RailPower, ...] = ()
    power_note: str | None = None  # why there is no power section (no/invalid hardware.yaml)
    rows: tuple[SuggestionRow, ...] = ()

    @property
    def card_count(self) -> int:
        return sum(len(g.cards) for g in self.groups)

    @property
    def updated_at(self) -> str | None:
        """Newest of last sync and briefing time: when the shown data was last checked."""
        stamps = [s for s in (self.last_sync, self.briefing and self.briefing.created_at) if s]
        return max(stamps, key=_parse) if stamps else None


# ---------------------------------------------------------------- time


def _parse(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def local_stamp(iso: str | None, now: datetime | None = None) -> str:
    """"18:42" today, "Oct 7 18:42" otherwise, in local time. Empty for None."""
    if not iso:
        return ""
    try:
        local = _parse(iso).astimezone()
    except ValueError:
        return iso
    today = (now or datetime.now(UTC)).astimezone().date()
    hm = local.strftime("%H:%M")
    return hm if local.date() == today else f"{local.strftime('%b')} {local.day} {hm}"


# ---------------------------------------------------------------- evidence


def evidence_link(item: str, repo: str, conn: sqlite3.Connection | None = None) -> EvidenceLink | None:
    """Turn one fact evidence string ("#12", "commit:abc1234", "milestone:Beta") into a link."""
    base = f"{GITHUB}/{repo}"
    if item.startswith("#") and item[1:].isdigit():
        return EvidenceLink(item, f"{base}/issues/{item[1:]}")
    if item.startswith("commit:"):
        sha = item.split(":", 1)[1].strip()
        if not sha:
            return None
        return EvidenceLink(sha[:7], f"{base}/commit/{sha}")
    if item.startswith("milestone:"):
        title = item.split(":", 1)[1].strip()
        url = None
        if conn is not None:
            try:
                row = conn.execute(
                    "SELECT html_url FROM milestones WHERE title = ? ORDER BY number DESC LIMIT 1",
                    (title,),
                ).fetchone()
                url = row[0] if row else None
            except sqlite3.Error:
                url = None
        return EvidenceLink(f"{title} milestone", url or f"{base}/milestones")
    return None


def _evidence_by_fact(conn: sqlite3.Connection) -> dict[str, list[str]]:
    from jarvis.facts import compute_facts  # local: facts is heavier and optional here

    return {f.id: list(f.evidence) for f in compute_facts(conn)}


def sentence_links(
    fact_ids: list[str], evidence: dict[str, list[str]], repo: str,
    conn: sqlite3.Connection | None = None,
) -> tuple[EvidenceLink, ...]:
    """Deduplicated links for the facts a sentence cites, issues and commits first."""
    seen: dict[str, EvidenceLink] = {}
    for fid in fact_ids:
        for item in evidence.get(fid, []):
            link = evidence_link(item, repo, conn)
            if link and link.url not in seen:
                seen[link.url] = link
    order = {"issues": 0, "commit": 1}
    links = sorted(seen.values(),
                   key=lambda lk: next((v for k, v in order.items() if f"/{k}/" in lk.url), 2))
    return tuple(links[:MAX_LINKS_PER_SENTENCE])


# ---------------------------------------------------------------- loaders


def latest_briefing_row(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM briefings ORDER BY id DESC LIMIT 1").fetchone()


def load_briefing(
    conn: sqlite3.Connection, repo: str, evidence: dict[str, list[str]] | None = None
) -> BriefingView | None:
    """The newest stored briefing, with evidence links per sentence.

    ``evidence`` maps fact id -> evidence strings; computed from the DB when None.
    Facts that no longer exist (the data moved on since the briefing) simply give
    no links.
    """
    row = latest_briefing_row(conn)
    if row is None:
        return None
    if evidence is None:
        try:
            evidence = _evidence_by_fact(conn)
        except Exception:  # noqa: BLE001 - links are optional; never block the briefing
            evidence = {}
    try:
        raw = json.loads(row["sentences_json"] or "[]")
    except json.JSONDecodeError:
        raw = []
    sentences = []
    for s in raw if isinstance(raw, list) else []:
        if not isinstance(s, dict) or not s.get("text"):
            continue
        ids = [str(i) for i in s.get("fact_ids") or []]
        sentences.append(SentenceView(
            text=str(s["text"]), uncertain=bool(s.get("uncertain")), fact_ids=tuple(ids),
            links=sentence_links(ids, evidence, repo, conn),
        ))
    return BriefingView(
        headline=row["headline"] or "", sentences=tuple(sentences), source=row["source"],
        model=row["model"], created_at=row["created_at"],
    )


def load_card_groups(conn: sqlite3.Connection, now: datetime | None = None) -> tuple[CardGroup, ...]:
    """Active cards grouped by type, known types first, empty groups left out."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=NEW_CARD_HOURS)
    by_type: dict[str, list[CardView]] = {}
    for c in list_cards(conn, "active"):
        try:
            is_new = _parse(c.created_at) >= cutoff
        except (TypeError, ValueError):
            is_new = False
        by_type.setdefault(c.type, []).append(CardView(c, is_new))
    labels = dict(CARD_TYPES)
    order = [t for t, _ in CARD_TYPES] + sorted(t for t in by_type if t not in labels)
    return tuple(
        CardGroup(t, labels.get(t, t.capitalize()), tuple(by_type[t]))
        for t in order if by_type.get(t)
    )


def last_sync(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(ran_at) FROM sync_log").fetchone()
    return row[0] if row else None


def ago(delta: timedelta) -> str:
    s = max(0, int(delta.total_seconds()))
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


def watch_status(row: sqlite3.Row | dict | None, now: datetime | None = None) -> WatchView | None:
    """Heartbeat of ``jarvis watch`` from the daemon_state row.

    Watching = a daemon is registered (pid set) and its last tick is newer than
    twice its interval. None when no daemon ever ticked (nothing is shown).
    """
    if row is None or not row["last_tick_at"]:
        return None
    now = now or datetime.now(UTC)
    try:
        age = now - _parse(row["last_tick_at"])
    except ValueError:
        return None
    interval = row["interval_s"] or 0
    fresh = row["pid"] is not None and interval > 0 and age.total_seconds() < 2 * interval
    tips = [f"Last background check: {local_stamp(row['last_tick_at'], now)}"]
    if row["last_change_at"]:
        tips.append(f"Last change found: {local_stamp(row['last_change_at'], now)}")
    if fresh and row["next_tick_at"]:
        tips.append(f"Next check: {local_stamp(row['next_tick_at'], now)}")
    if row["last_error"]:
        tips.append(f"Last error: {row['last_error']}")
    keys = row.keys() if hasattr(row, "keys") else ()
    if "last_research_at" in keys and row["last_research_at"]:
        line = f"Research: last {local_stamp(row['last_research_at'], now)}"
        if fresh and "next_research_at" in keys and row["next_research_at"]:
            line += f", next {local_stamp(row['next_research_at'], now)}"
        tips.append(line)
    if fresh:
        return WatchView(True, f"watching · checked {ago(age)}", "\n".join(tips))
    tips.insert(0, "jarvis watch is not running")
    return WatchView(False, "not watching", "\n".join(tips))


def load_window_data(conn: sqlite3.Connection, repo: str,
                     now: datetime | None = None, *,
                     hardware_path: str | Path | None = None) -> WindowData:
    """Everything the window shows. Each part fails on its own: a broken section
    becomes an error line, never a blank window.

    ``hardware_path``: the local hardware.yaml (``Config.hardware_path``); the
    synced repo copy wins when it exists, like ``pipeline.load_checks``.
    """
    now = now or datetime.now(UTC)
    errors: list[str] = []
    try:
        from jarvis.facts import compute_facts

        facts = compute_facts(conn, now=now)
    except Exception:  # noqa: BLE001 - facts feed links and charts; never block the window
        facts = []
    evidence = {f.id: list(f.evidence) for f in facts}
    try:
        briefing = load_briefing(conn, repo, evidence=evidence)
    except sqlite3.Error as e:
        briefing = None
        errors.append(f"Could not read the briefing: {e}")
    try:
        groups = load_card_groups(conn, now)
    except sqlite3.Error as e:
        groups = ()
        errors.append(f"Could not read suggestions: {e}")
    try:
        synced = last_sync(conn)
    except sqlite3.Error:
        synced = None
    try:
        from jarvis.db import get_daemon_state

        watch = watch_status(get_daemon_state(conn), now)
    except sqlite3.Error:
        watch = None

    today = now.astimezone().date()
    kpis = build_kpis(facts, today)
    try:
        timeline = load_timeline(conn, facts, today)
        grid = build_grid(commit_days(conn), today)
        phases = build_phase_bars(conn.execute("SELECT * FROM issues").fetchall())
        status_by_check = finding_statuses(conn)
    except sqlite3.Error as e:
        timeline, grid, phases, status_by_check = None, None, None, {}
        errors.append(f"Could not read the project data: {e}")
    power, power_note = load_power(conn, hardware_path)
    rows = build_rows(groups, status_by_check)
    return WindowData(repo, briefing, groups, synced, tuple(errors), watch,
                      kpis=kpis, timeline=timeline, grid=grid, phases=phases,
                      power=power, power_note=power_note, rows=rows)


# ---------------------------------------------------------------- refresh outcome


def refresh_message(result, now: datetime | None = None) -> tuple[str, str]:
    """(level, text) for the status line after a refresh. level: "ok" or "warn".

    ``result`` is a ``pipeline.RefreshResult`` (duck-typed: .sync, .briefing,
    .cards_new, .cards_resolved, .notes).
    """
    problems: list[str] = []
    failed = [name for name, outcome in (getattr(result, "sync", None) or {}).items()
              if str(outcome).startswith("error")]
    if failed:
        problems.append(f"GitHub sync failed for {', '.join(failed)}; showing what was cached")
    reason = getattr(getattr(result, "briefing", None), "fallback_reason", None) or ""
    if reason.startswith("model unavailable"):
        problems.append("Ollama unavailable, showing template briefing")
    elif reason.startswith("model output rejected"):
        problems.append("Model reply did not pass the checks, showing template briefing")
    notes = [str(n) for n in (getattr(result, "notes", None) or []) if n]

    stamp = local_stamp((now or datetime.now(UTC)).isoformat())
    parts = [f"Refreshed at {stamp}"]
    new = getattr(result, "cards_new", 0) or 0
    resolved = getattr(result, "cards_resolved", 0) or 0
    if new:
        parts.append(f"{new} new suggestion{'s' if new != 1 else ''}")
    if resolved:
        parts.append(f"{resolved} resolved on its own" if resolved == 1
                     else f"{resolved} resolved on their own")
    if problems:
        return "warn", " · ".join(problems + notes)
    return "ok", " · ".join(parts + notes)


def describe_error(e: BaseException) -> str:
    """A short, human sentence for an exception raised by a refresh."""
    name = type(e).__name__
    module = type(e).__module__ or ""
    if module.startswith("httpx") and ("Connect" in name or "Timeout" in name
                                       or "Network" in name):
        return "Could not reach GitHub (offline?). Showing cached data."
    if name == "GitHubError":
        return f"{e}. Showing cached data."
    if isinstance(e, sqlite3.OperationalError):
        return f"Database busy or unreadable: {e}"
    text = str(e).strip() or name
    if len(text) > 160:
        text = text[:157] + "..."
    return f"Refresh failed: {text}"


# ================================================================ dashboard
#
# Pure view models for the dashboard charts. Every number is computed here (or
# by facts.py / checks.py) from the database and hardware.yaml; the model never
# supplies one. Each builder takes plain data, so it is testable without Qt.


def fmt_day(d: date) -> str:
    return f"{d.strftime('%b')} {d.day}"


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def fmt_num(x: float) -> str:
    """1.5500001 -> "1.55", 2.0 -> "2"."""
    return f"{round(x, 3):g}"


# ---------------------------------------------------------------- KPI tiles


@dataclass(frozen=True)
class Kpi:
    key: str  # "due" | "closed" | "pace" | "last_commit"
    label: str
    value: str
    sub: str
    tone: str = "default"  # default | warn | bad | good | muted
    tooltip: str = ""


def _nearest_deadline(facts) -> Any | None:
    deadlines = [f for f in facts if f.kind == "milestone_deadline"]
    return min(deadlines, key=lambda f: (f.data["days_left"], f.data["milestone"]),
               default=None)


def build_kpis(facts, today: date) -> tuple[Kpi, ...]:
    """Four headline numbers from the briefing facts (``facts.compute_facts``).

    The nearest due milestone (overdue ones first) drives the first three tiles;
    the last tile is the time since the last commit.
    """
    out: list[Kpi] = []
    dl = _nearest_deadline(facts)
    progress = [f for f in facts if f.kind == "milestone_progress"]
    if dl is not None:
        d = dl.data
        m, days, due = d["milestone"], d["days_left"], date.fromisoformat(d["due"])
        number = dl.id.split(":", 1)[1]
        if days < 0:
            out.append(Kpi("due", f"{m} due", "overdue",
                           f"by {plural(-days, 'day')} · {fmt_day(due)}", "bad"))
        elif days == 0:
            out.append(Kpi("due", f"{m} due", "today", fmt_day(due), "warn"))
        else:
            out.append(Kpi("due", f"{m} due", plural(days, "day"), fmt_day(due),
                           "warn" if days <= 3 else "default"))
        closed, total, opened = d["closed"], d["total"], d["open"]
        adj = next((f for f in facts if f.id == f"adjusted_progress:{number}"), None)
        maybe = (adj.data["adjusted_closed"] - closed) if adj else 0
        if maybe > 0:
            nums = ", ".join(f"#{n}" for n in adj.data["maybe_done"])
            sub, tip = (f"{maybe} more may be done",
                        f"Unconfirmed: commits resemble {nums}. Check them before closing.")
        else:
            pct = round(closed / total * 100) if total else 0
            sub, tip = (f"{pct}% closed", "")
        out.append(Kpi("closed", f"{m} closed", f"{closed} / {total}", sub,
                       "good" if total and closed == total else "default", tip))
        pace = d.get("pace_per_day")
        if opened == 0:
            out.append(Kpi("pace", "Pace needed", "done", "all issues closed", "good"))
        elif pace is not None:
            out.append(Kpi("pace", "Pace needed", f"{fmt_num(pace)}/day", "to close on time",
                           "default",
                           f"{plural(opened, 'open issue')} over {plural(days, 'day')}"))
        else:
            out.append(Kpi("pace", "Still open", str(opened),
                           "past the due date" if days < 0 else "due today", "bad"))
    elif progress:
        out.append(Kpi("due", "Next due date", "none", "no milestone has a due date", "muted"))
        p = min(progress, key=lambda f: f.data["milestone"])
        out.append(Kpi("closed", f"{p.data['milestone']} closed",
                       f"{p.data['closed']} / {p.data['total']}", "no due date"))
        out.append(Kpi("pace", "Pace needed", "–", "no due date to pace against", "muted"))
    else:
        out.append(Kpi("due", "Next due date", "none", "no open milestones", "muted"))
        out.append(Kpi("closed", "Milestones", "–", "nothing to track", "muted"))
        out.append(Kpi("pace", "Pace needed", "–", "no due date to pace against", "muted"))

    gap = next((f for f in facts if f.kind == "quiet_gap"), None)
    act = next((f for f in facts if f.kind == "activity"), None)
    if gap is not None:
        last = date.fromisoformat(gap.data["last_date"])
        out.append(Kpi("last_commit", "Since last commit", plural(gap.data["days_since"], "day"),
                       f"{fmt_day(last)} · {gap.data['last_commit']}",
                       "warn" if gap.data["days_since"] >= 7 else "default"))
    elif act is not None:
        last = date.fromisoformat(act.data["last"])
        n = (today - last).days
        value = "today" if n <= 0 else "yesterday" if n == 1 else plural(n, "day")
        out.append(Kpi("last_commit", "Last commit", value,
                       f"{plural(act.data['count'], 'commit')} in {act.data['days']} days"))
    else:
        out.append(Kpi("last_commit", "Last commit", "none", "no commits synced", "muted"))
    return tuple(out)


# ---------------------------------------------------------------- milestone timeline


@dataclass(frozen=True)
class MilestoneBar:
    number: int
    title: str
    due: date | None
    closed: int
    total: int
    maybe: int = 0  # open issues that may already be done (heuristic)
    url: str | None = None

    @property
    def label(self) -> str:
        maybe = f" (+{self.maybe} maybe)" if self.maybe else ""
        when = fmt_day(self.due) if self.due else "no due date"
        return f"{when} · {self.closed}/{self.total}{maybe}"


@dataclass(frozen=True)
class Timeline:
    today: date
    start: date
    end: date
    bars: tuple[MilestoneBar, ...]

    def frac(self, d: date) -> float:
        span = max(1, (self.end - self.start).days)
        return (d - self.start).days / span

    @property
    def ticks(self) -> tuple[date, ...]:
        """Mondays (every week, or every 2/4 weeks on long spans) inside the range."""
        span = (self.end - self.start).days
        step = 7 if span <= 63 else 14 if span <= 126 else 28
        d = self.start + timedelta(days=(7 - self.start.weekday()) % 7)
        out = []
        while d <= self.end:
            out.append(d)
            d += timedelta(days=step)
        return tuple(out)


def build_timeline(milestones, maybe_by_number: dict[int, int], today: date) -> Timeline:
    """``milestones``: open milestone rows (dict-like: number, title, due_on,
    open_issues, closed_issues, html_url). Due dates first (soonest first), then
    the ones without one."""
    bars = []
    for m in milestones:
        closed, opened = m["closed_issues"] or 0, m["open_issues"] or 0
        due = None
        if m["due_on"]:
            try:
                due = _parse(m["due_on"]).astimezone(UTC).date()  # like facts._due_date
            except ValueError:
                due = None
        total = closed + opened
        maybe = min(maybe_by_number.get(m["number"], 0), max(0, total - closed))
        bars.append(MilestoneBar(m["number"], m["title"] or f"Milestone {m['number']}", due,
                                 closed, total, maybe, m["html_url"]))
    bars.sort(key=lambda b: (b.due is None, b.due or date.max, b.number))
    dues = [b.due for b in bars if b.due]
    start = today - timedelta(days=7)
    if dues and min(dues) < start + timedelta(days=2):
        start = min(dues) - timedelta(days=3)
    last = max([*dues, today + timedelta(days=14)])
    margin = max(4, math.ceil((last - start).days * 0.08))
    return Timeline(today, start, last + timedelta(days=margin), tuple(bars))


def maybe_counts(facts) -> dict[int, int]:
    """milestone number -> open issues that may be done (adjusted_progress facts)."""
    out = {}
    for f in facts:
        if f.kind == "adjusted_progress":
            n = int(f.id.split(":", 1)[1])
            out[n] = f.data["adjusted_closed"] - f.data["closed"]
    return out


def load_timeline(conn: sqlite3.Connection, facts, today: date) -> Timeline:
    rows = conn.execute("SELECT * FROM milestones WHERE state = 'open' ORDER BY number").fetchall()
    return build_timeline(rows, maybe_counts(facts), today)


# ---------------------------------------------------------------- contribution grid

GRID_WEEKS = 26


def grid_level(n: int) -> int:
    """GitHub-like buckets: 0, 1, 2-3, 4-5, 6+ commits -> level 0..4."""
    return 0 if n <= 0 else 1 if n == 1 else 2 if n <= 3 else 3 if n <= 5 else 4


@dataclass(frozen=True)
class GridDay:
    day: date
    count: int
    level: int
    col: int  # week index, 0 = oldest
    row: int  # 0 = Monday .. 6 = Sunday


@dataclass(frozen=True)
class ContributionGrid:
    today: date
    start: date  # Monday of the first column
    weeks: int
    days: tuple[GridDay, ...]  # only days up to today
    months: tuple[tuple[int, str], ...]  # (column, "Oct")
    total: int
    active_days: int
    longest_streak: int
    current_streak: int

    @property
    def caption(self) -> str:
        months = round(self.weeks / 4.345)
        head = f"{plural(self.total, 'commit')} in the last {months} months"
        if not self.total:
            return head
        bits = [head, f"active on {plural(self.active_days, 'day')}"]
        if self.longest_streak > 1:
            bits.append(f"longest streak {plural(self.longest_streak, 'day')}")
        return " · ".join(bits)

    def tooltip(self, d: GridDay) -> str:
        what = "No commits" if d.count == 0 else plural(d.count, "commit")
        return f"{what} on {d.day.strftime('%a')}, {fmt_day(d.day)}"


def commit_days(conn: sqlite3.Connection) -> list[date]:
    """Local calendar date of every stored commit."""
    out = []
    for (ts,) in conn.execute("SELECT committed_at FROM commits WHERE committed_at IS NOT NULL"):
        try:
            out.append(_parse(ts).astimezone().date())
        except ValueError:
            continue
    return out


def build_grid(days: list[date], today: date, weeks: int = GRID_WEEKS) -> ContributionGrid:
    """Commits per local day for ``weeks`` columns ending with today's week."""
    start = today - timedelta(days=today.weekday()) - timedelta(weeks=weeks - 1)
    counts: dict[date, int] = {}
    for d in days:
        if start <= d <= today:
            counts[d] = counts.get(d, 0) + 1
    cells = []
    d = start
    while d <= today:
        i = (d - start).days
        n = counts.get(d, 0)
        cells.append(GridDay(d, n, grid_level(n), i // 7, i % 7))
        d += timedelta(days=1)
    # Month labels over the first column whose Monday is in a new month; the first
    # column only gets one when the next label is at least 3 columns away.
    changes = [c for c in range(1, weeks)
               if (start + timedelta(weeks=c)).month != (start + timedelta(weeks=c - 1)).month]
    months = [(c, (start + timedelta(weeks=c)).strftime("%b")) for c in changes]
    if not changes or changes[0] >= 3:
        months.insert(0, (0, start.strftime("%b")))
    longest = run = 0
    for c in cells:
        run = run + 1 if c.count else 0
        longest = max(longest, run)
    current = 0
    for c in reversed(cells):
        if c.count:
            current += 1
        elif c.day == today and current == 0:
            continue  # nothing yet today does not break yesterday's streak
        else:
            break
    return ContributionGrid(today, start, weeks, tuple(cells), tuple(months),
                            sum(counts.values()), len(counts), longest, current)


# ---------------------------------------------------------------- issues by phase

_PHASE_RE = re.compile(r"^phase[-_ ]?(\d+)$", re.I)


@dataclass(frozen=True)
class PhaseBar:
    label: str
    open: int
    closed: int
    open_issues: tuple[int, ...] = ()

    @property
    def total(self) -> int:
        return self.open + self.closed


@dataclass(frozen=True)
class PhaseBars:
    bars: tuple[PhaseBar, ...]

    @property
    def total_open(self) -> int:
        return sum(b.open for b in self.bars)

    @property
    def max_total(self) -> int:
        return max((b.total for b in self.bars), default=0)


def issue_category(labels: list[str]) -> str:
    phases = sorted((lb for lb in labels if _PHASE_RE.match(lb)),
                    key=lambda lb: int(_PHASE_RE.match(lb).group(1)))
    if phases:
        return phases[0]
    if "someday" in labels:
        return "someday"
    return "other labels" if labels else "unlabeled"


def build_phase_bars(issues) -> PhaseBars:
    """Open and closed issues per phase label (phase-1, phase-2, ...), then
    other labels, someday and unlabeled. Categories with no issues are left out."""
    acc: dict[str, list] = {}
    for i in issues:
        try:
            labels = [str(x) for x in json.loads(i["labels"] or "[]")]
        except (json.JSONDecodeError, TypeError):
            labels = []
        cat = issue_category(labels)
        slot = acc.setdefault(cat, [0, 0, []])
        if i["state"] == "open":
            slot[0] += 1
            slot[2].append(i["number"])
        else:
            slot[1] += 1

    def order(cat: str) -> tuple:
        m = _PHASE_RE.match(cat)
        if m:
            return (0, int(m.group(1)))
        return ({"other labels": 1, "someday": 2, "unlabeled": 3}.get(cat, 1), 0)

    bars = tuple(PhaseBar(cat, o, c, tuple(sorted(nums)))
                 for cat, (o, c, nums) in sorted(acc.items(), key=lambda kv: order(kv[0])))
    return PhaseBars(bars)


# ---------------------------------------------------------------- power

BUDGET_TARGET_PCT = 80.0  # same as checks.BUDGET_WARN_PCT
EXAMPLE_TOL_PCT = 5.0  # same as checks.EXAMPLE_TOL_PCT


@dataclass(frozen=True)
class LoadSegment:
    part: str
    count: int
    peak_a: float | None  # per unit
    amps: float | None  # count x peak; None when the peak is not listed
    planned: bool = False
    state: str = "peak"  # "stall" for motors/servos

    @property
    def label(self) -> str:
        n = f" ×{self.count}" if self.count > 1 else ""
        amps = f"{fmt_num(self.amps)} A" if self.amps is not None else "? A"
        planned = " (planned)" if self.planned else ""
        return f"{self.part}{n} {self.state} {amps}{planned}"


@dataclass(frozen=True)
class RailBudget:
    rail: str
    segments: tuple[LoadSegment, ...]
    worst_case_a: float  # wired (not planned) loads with a listed peak
    planned_a: float
    rating_a: float | None
    need_80_a: float  # rating needed to stay at or under 80 %
    status: str | None  # rail_budget finding status
    missing: tuple[str, ...] = ()  # parts without a peak current

    @property
    def load_pct(self) -> float | None:
        if not self.rating_a:
            return None
        return round(self.worst_case_a / self.rating_a * 100, 1)

    @property
    def scale_max(self) -> float:
        top = max(self.worst_case_a + self.planned_a, self.need_80_a, self.rating_a or 0)
        return top * 1.08 if top > 0 else 1.0

    @property
    def caption(self) -> str:
        wired = [s for s in self.segments if not s.planned and s.amps is not None]
        if not wired and self.planned_a:
            planned = " + ".join(s.label for s in self.segments if s.planned and s.amps is not None)
            pct = (f", {fmt_num(round(self.planned_a / self.rating_a * 100, 1))}% of the "
                   f"{fmt_num(self.rating_a)} A rating" if self.rating_a else
                   ", and the rail's rating isn't in hardware.yaml")
            return f"Only planned parts so far: {planned}{pct}."
        parts = " + ".join(s.label for s in wired)
        head = f"Worst case {fmt_num(self.worst_case_a)} A" + (f" ({parts})" if parts else "")
        if self.missing:
            head += f"; no peak current listed for {', '.join(self.missing)}"
        if self.rating_a is None:
            tail = (f"The {self.rail} supply rating isn't in hardware.yaml: it needs "
                    f"≥ {fmt_num(self.need_80_a)} A to stay under {fmt_num(BUDGET_TARGET_PCT)}% load.")
        else:
            tail = (f"That is {fmt_num(self.load_pct)}% of the {fmt_num(self.rating_a)} A rating"
                    + (" — over it." if self.load_pct > 100 else
                       f", above the {fmt_num(BUDGET_TARGET_PCT)}% target." if self.load_pct >
                       BUDGET_TARGET_PCT else "."))
        return f"{head}. {tail}"


@dataclass(frozen=True)
class PartRange:
    part: str
    v_min: float | None
    v_max: float | None
    planned: bool = False
    status: str | None = None  # voltage_window finding status
    low_gap: float | None = None  # V the band goes below v_min (> 0 = violation)
    high_gap: float | None = None  # V the band goes above v_max (> 0 = violation)


@dataclass(frozen=True)
class VoltageWindow:
    rail: str
    nominal_v: float
    tolerance_pct: float | None
    band_lo: float
    band_hi: float
    example: bool  # True: tolerance unknown, the band is the ±5 % example
    parts: tuple[PartRange, ...]
    axis_lo: float
    axis_hi: float

    @property
    def ticks(self) -> tuple[float, ...]:
        span = self.axis_hi - self.axis_lo
        step = 0.25 if span <= 1.6 else 0.5 if span <= 4 else 1.0
        v = math.ceil(self.axis_lo / step - 1e-9) * step
        out = []
        while v <= self.axis_hi + 1e-9:
            out.append(round(v, 3))
            v += step
        return tuple(out)

    @property
    def caption(self) -> str:
        tol = fmt_num(self.tolerance_pct if self.tolerance_pct is not None else EXAMPLE_TOL_PCT)
        band = f"{fmt_num(self.band_lo)}–{fmt_num(self.band_hi)} V"
        low = [f"{p.part}'s {fmt_num(p.v_min)} V min" for p in self.parts
               if p.low_gap is not None and p.low_gap > 0]
        high = [f"{p.part}'s {fmt_num(p.v_max)} V max" for p in self.parts
                if p.high_gap is not None and p.high_gap > 0]
        if self.example:
            head = (f"Supply tolerance isn't in hardware.yaml, so the band is an example: "
                    f"a ±{tol}% supply ({band})")
            dips, exceeds = "would dip below", "exceed" if low else "would exceed"
        else:
            head = f"The {self.rail} supply is {fmt_num(self.nominal_v)} V ± {tol}% ({band})"
            dips, exceeds = "which dips below", "exceeds" if low else "which exceeds"
        bits = []
        if low:
            bits.append(f"{dips} {' and '.join(low)}")
        if high:
            bits.append(f"{exceeds} {' and '.join(high)}")
        if bits:
            return f"{head} {' and '.join(bits)}."
        return f"{head} stays inside every part's range."


@dataclass(frozen=True)
class RailPower:
    rail: str
    nominal_v: float
    budget: RailBudget | None
    window: VoltageWindow | None


def _is_motor(p) -> bool:
    blob = f"{p.name} {p.source} {p.notes}".lower()
    return "stall" in blob or "servo" in blob or "motor" in blob


def _ceil2(x: float) -> float:
    return math.ceil(round(x * 100, 6)) / 100


def build_power(hw, findings) -> tuple[RailPower, ...]:
    """Per rail with at least one listed part: its current budget and voltage window.

    ``hw`` is a ``hardware.Hardware``; ``findings`` the ``checks.run_all`` output
    (statuses come from there). Planned parts are drawn, marked planned, but do
    not count toward the worst case (as in the checks).
    """
    status = {f.check_id: f.status for f in findings}
    out = []
    for rail in hw.rails.values():
        parts = hw.parts_on(rail.name, include_planned=True)
        if not parts:
            continue
        segs = []
        for p in parts:
            amps = round(p.count * p.current_peak_a, 3) if p.current_peak_a is not None else None
            segs.append(LoadSegment(p.name, p.count, p.current_peak_a, amps, p.planned,
                                    "stall" if _is_motor(p) else "peak"))
        wired = round(sum(s.amps for s in segs if s.amps is not None and not s.planned), 3)
        planned = round(sum(s.amps for s in segs if s.amps is not None and s.planned), 3)
        budget = RailBudget(
            rail.name, tuple(segs), wired, planned, rail.max_current_a,
            _ceil2(wired / (BUDGET_TARGET_PCT / 100)) if wired else 0.0,
            status.get(f"rail_budget:{rail.name}"),
            tuple(s.part for s in segs if s.amps is None and not s.planned))

        nom, tol = rail.nominal_v, rail.tolerance_pct
        example = tol is None
        t = EXAMPLE_TOL_PCT if example else tol
        lo, hi = round(nom * (1 - t / 100), 3), round(nom * (1 + t / 100), 3)
        ranges = []
        for p in parts:
            if p.v_min is None and p.v_max is None:
                continue
            ranges.append(PartRange(
                p.name, p.v_min, p.v_max, p.planned, status.get(f"voltage_window:{p.name}"),
                round(p.v_min - lo, 3) if p.v_min is not None else None,
                round(hi - p.v_max, 3) if p.v_max is not None else None))
        window = None
        if ranges:
            vals = [lo, hi, nom] + [v for r in ranges for v in (r.v_min, r.v_max) if v is not None]
            a_lo = math.floor((min(vals) - 0.2) * 4) / 4
            a_hi = math.ceil((max(vals) + 0.2) * 4) / 4
            window = VoltageWindow(rail.name, nom, tol, lo, hi, example, tuple(ranges),
                                   max(0.0, a_lo), a_hi)
        out.append(RailPower(rail.name, nom, budget, window))
    # Rails with wired loads first, then rails with only planned parts.
    out.sort(key=lambda r: (not (r.budget and r.budget.worst_case_a > 0), r.rail))
    return tuple(out)


def load_power(conn: sqlite3.Connection, hardware_path: str | Path | None
               ) -> tuple[tuple[RailPower, ...], str | None]:
    """(rails, note). Same lookup as ``pipeline.load_checks`` (repo copy first)."""
    from jarvis.checks import run_all
    from jarvis.hardware import HardwareError, find_hardware, load_hardware

    try:
        src = find_hardware(conn, Path(hardware_path) if hardware_path else None)
    except (sqlite3.Error, OSError) as e:
        return (), f"Could not read hardware.yaml: {e}"
    if src is None:
        return (), "No hardware.yaml yet, so there is nothing to chart. See docs/hardware.example.yaml."
    try:
        hw = load_hardware(src.text)
    except HardwareError as e:
        return (), f"hardware.yaml is invalid: {e}"
    return build_power(hw, run_all(hw)), None


# ---------------------------------------------------------------- suggestions table

STATUS_TAGS = {  # finding status -> (tag text, tag role; see theme.TAG_NAMES)
    "fail": ("fail", "fail"),
    "warn": ("warning", "warning"),
    "unknown": ("needs value", "needs"),
    "pass": ("passing", "ok"),
}
TYPE_TAGS = {
    "electrical": ("Electrical", "electrical"),
    "research": ("Research", "research"),
}


@dataclass(frozen=True)
class SuggestionRow:
    card: Card
    type_label: str
    type_color: str
    status_label: str
    status_color: str
    is_new: bool = False

    @property
    def id(self) -> int:
        return self.card.id


def finding_statuses(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["check_id"]: r["status"]
            for r in conn.execute("SELECT check_id, status FROM electrical_findings")}


def build_rows(groups: tuple[CardGroup, ...], status_by_check: dict[str, str]
               ) -> tuple[SuggestionRow, ...]:
    """Active cards as table rows: failing checks first, then warnings, missing
    values, and research; newest first within each."""
    rank = {"fail": 0, "warn": 1, "unknown": 2}
    rows = []
    for g in groups:
        for v in g.cards:
            c = v.card
            st = status_by_check.get(c.check_id or "") if c.type == "electrical" else None
            if st in STATUS_TAGS:
                label, color = STATUS_TAGS[st]
            elif v.is_new:
                label, color = "new", "new"
            else:
                label, color = "open", "open"
            tl, tc = TYPE_TAGS.get(c.type, (c.type.capitalize(), "open"))
            rows.append((rank.get(st, 3), 0 if c.type == "electrical" else 1, c.created_at or "",
                         SuggestionRow(c, tl, tc, label, color, v.is_new)))
    rows.sort(key=lambda r: (r[0], r[1], _neg(r[2]), r[3].card.id))
    return tuple(r[3] for r in rows)


def _neg(iso: str) -> float:
    try:
        return -_parse(iso).timestamp()
    except ValueError:
        return 0.0
