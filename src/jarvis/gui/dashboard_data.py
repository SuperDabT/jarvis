"""The Projects dashboard's view model (no Qt): the active milestone, its risk,
and the issues to work on next.

Active milestone: the one the user picked (a UI preference: its number), else
the nearest-due open milestone (overdue first, then soonest, then ones without
a due date), the same order as ``facts`` uses for next steps. The milestones
themselves come from the synced ``milestones`` table (``data.Timeline``).

Risk (``assess_risk``) compares the pace the milestone needs with the pace it
has had: issues of that milestone closed in the last RISK_WINDOW_DAYS days.

- no open issues: done;
- no due date: no deadline to be at risk against (said so, not guessed);
- past its due date with open issues: overdue;
- some closed in the window: at that pace, the open issues take
  open / (closed / window) days; on track when that fits before the due date,
  at risk when it doesn't;
- none closed in the window: at risk when the due date is within
  RISK_SOON_DAYS (nothing is moving and little time is left); otherwise not
  enough data to tell.

Issues (``rank_issues``): open issues of the active milestone, ordered by what
their labels say, never by a guess: "blocker"/"blocking" first, then explicit
priority labels (P0..P4, critical/urgent, high, medium, low), then the
project's phase-N labels, then issue number; "blocked" issues last (not
workable yet).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import quote

from jarvis.gui.data import MilestoneBar, Timeline, fmt_day, plural

RISK_WINDOW_DAYS = 14
RISK_SOON_DAYS = 7
ISSUES_SHOWN = 4

_PRIORITY_RE = re.compile(
    r"^(?:priority|prio|pri)?[\s:/_-]*(p[0-4]|critical|urgent|high|medium|med|low)"
    r"(?:[\s:/_-]*(?:priority|prio))?$", re.I)
_PRIORITY_RANK = {"p0": 0, "critical": 0, "urgent": 0, "p1": 1, "high": 1, "p2": 2,
                  "medium": 2, "med": 2, "p3": 3, "low": 3, "p4": 4}
_PHASE_RE = re.compile(r"^phase[-_ ]?(\d+)$", re.I)
BLOCKER_LABELS = {"blocker", "blocking"}
BLOCKED_LABELS = {"blocked"}


# ---------------------------------------------------------------- risk


@dataclass(frozen=True)
class Risk:
    level: str   # "done" | "on_track" | "at_risk" | "overdue" | "unknown"
    label: str   # "On track", "At risk", ...
    why: str     # one short sentence, with the numbers it used

    @property
    def tone(self) -> str:
        return {"done": "good", "on_track": "good", "at_risk": "warn", "overdue": "bad"}.get(
            self.level, "muted")


def assess_risk(bar: MilestoneBar, closed_on: list[date], today: date,
                window: int = RISK_WINDOW_DAYS) -> Risk:
    """``closed_on``: the close dates of the milestone's closed issues."""
    open_ = max(0, bar.total - bar.closed)
    if bar.total and open_ == 0:
        return Risk("done", "Done", "Every issue in it is closed.")
    if bar.total == 0:
        return Risk("unknown", "No issues", "It has no issues yet, so there is nothing to "
                    "measure.")
    if bar.due is None:
        return Risk("unknown", "No deadline", "It has no due date to be at risk against.")
    days = (bar.due - today).days
    if days < 0:
        return Risk("overdue", "Overdue", f"Due {fmt_day(bar.due)}, "
                    f"{plural(open_, 'issue')} still open.")
    recent = sum(1 for d in closed_on if 0 <= (today - d).days < window)
    if recent == 0:
        if days <= RISK_SOON_DAYS:
            return Risk("at_risk", "At risk", f"No issues closed in the last {window} days, "
                        f"{open_} open, due in {plural(days, 'day')}.")
        return Risk("unknown", "Too early to tell", f"No issues closed in the last {window} "
                    "days, so there is no pace to project from.")
    need = open_ / (recent / window)
    pace = f"{recent} closed in the last {window} days"
    if need <= days:
        return Risk("on_track", "On track", f"At this pace ({pace}) the {open_} open take "
                    f"about {plural(round(need), 'day')}; due in {plural(days, 'day')}.")
    return Risk("at_risk", "At risk", f"At this pace ({pace}) the {open_} open take about "
                f"{plural(round(need), 'day')}; due in {plural(days, 'day')}.")


# ---------------------------------------------------------------- issues


@dataclass(frozen=True)
class IssueRow:
    number: int
    title: str
    tag: str          # "blocker", "P1", "high", "phase-2", "blocked" or "" (short form)
    tag_kind: str     # "blocker" | "priority" | "phase" | "blocked" | ""
    url: str | None


def _labels(raw) -> list[str]:
    try:
        labels = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(x).strip() for x in labels if str(x).strip()]


def issue_rank(labels: list[str]) -> tuple[tuple, str, str]:
    """(sort key, tag, tag kind) from an issue's labels."""
    low = [lb.lower() for lb in labels]
    blocker = any(lb in BLOCKER_LABELS for lb in low)
    blocked = any(lb in BLOCKED_LABELS for lb in low)
    prios = [(_PRIORITY_RANK[w], w.upper() if w.startswith("p") else w)
             for lb in labels if (m := _PRIORITY_RE.match(lb)) and (w := m.group(1).lower())]
    prio, prio_label = min(prios) if prios else (9, "")
    phases = [(int(m.group(1)), lb) for lb in labels if (m := _PHASE_RE.match(lb))]
    phase, phase_label = min(phases) if phases else (99, "")
    key = (1 if blocked else 0, 0 if blocker else 1, prio, phase)
    if blocked:
        return key, "blocked", "blocked"
    if blocker:
        return key, "blocker", "blocker"
    if prio_label:
        return key, prio_label, "priority"
    if phase_label:
        return key, phase_label, "phase"
    return key, "", ""


def rank_issues(issues, milestone_number: int | None) -> list[IssueRow]:
    """Open issues of the milestone (all open issues when there is none), best first."""
    rows = []
    for i in issues:
        if i["state"] != "open":
            continue
        if milestone_number is not None and i["milestone_number"] != milestone_number:
            continue
        key, tag, kind = issue_rank(_labels(i["labels"]))
        rows.append(((key, i["number"]), IssueRow(i["number"], i["title"] or f"#{i['number']}",
                                                  tag, kind, i["html_url"])))
    rows.sort(key=lambda kv: kv[0])
    return [r for _, r in rows]


# ---------------------------------------------------------------- the dashboard


@dataclass(frozen=True)
class Dashboard:
    today: date
    active: MilestoneBar | None
    others: tuple[MilestoneBar, ...]
    risk: Risk | None
    issues: tuple[IssueRow, ...]        # the first ISSUES_SHOWN
    milestone_open: int                 # open issues in the active milestone
    open_total: int                     # open issues in the repo
    view_all_url: str | None
    picked: bool = False                # the user chose the active milestone

    @property
    def days_left(self) -> int | None:
        if self.active is None or self.active.due is None:
            return None
        return (self.active.due - self.today).days


def pick_active(bars: tuple[MilestoneBar, ...], chosen: int | None) -> MilestoneBar | None:
    """The chosen milestone while it is open; else the nearest-due one."""
    by_num = {b.number: b for b in bars}
    if chosen is not None and chosen in by_num:
        return by_num[chosen]
    return bars[0] if bars else None


def build_dashboard(conn, repo: str, timeline: Timeline | None, chosen: int | None,
                    today: date) -> Dashboard:
    from jarvis.gui.data import _parse

    bars = timeline.bars if timeline else ()
    active = pick_active(bars, chosen)
    issues = conn.execute("SELECT number, title, state, labels, milestone_number, html_url, "
                          "closed_at FROM issues").fetchall()
    open_total = sum(1 for i in issues if i["state"] == "open")
    risk = None
    if active is not None:
        closed_on = []
        for i in issues:
            if i["milestone_number"] == active.number and i["state"] == "closed" \
                    and i["closed_at"]:
                try:
                    closed_on.append(_parse(i["closed_at"]).astimezone().date())
                except ValueError:
                    continue
        risk = assess_risk(active, closed_on, today)
    ranked = rank_issues(issues, active.number if active else None)
    base = f"https://github.com/{repo}"
    if active is not None:
        url = active.url or f"{base}/issues?q=" + quote(f'is:open milestone:"{active.title}"')
    else:
        url = f"{base}/issues"
    return Dashboard(today, active, tuple(b for b in bars if b is not active), risk,
                     tuple(ranked[:ISSUES_SHOWN]), len(ranked), open_total, url,
                     picked=chosen is not None and active is not None
                     and active.number == chosen)
