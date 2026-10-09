"""Deterministic facts for the briefing.

Python computes every number here; a language model may later only phrase these
facts. Each fact is small, carries the raw data behind it and points at its
evidence (issues, commits, milestones). Heuristic inferences are marked
``certain=False`` and worded as "may", never as settled.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

MATCH_WINDOW_DAYS = 30  # commits considered for maybe_done matching
DEADLINE_SOON_DAYS = 14  # milestone counts as "due soon" within this many days
STALE_DAYS = 14  # an open issue untouched this long in a due-soon milestone is stale
MATCH_THRESHOLD = 0.5  # share of issue title tokens that must appear in the commit line
MATCH_MIN_OVERLAP = 2  # and at least this many tokens in common

STOPWORDS = frozenset(
    """
    a an the of on to in for and or but with without by at as from into onto out up
    is are was were be been being it its it's this that these those their there then
    than when while where which who what how why each every all any some no not
    so if do does did can could should would will just also only more most less
    our we you your my me i he she they them his her
    add adds fix fixes update updates create creates make makes use uses new
    """.split()
)
# Words that carry no meaning once stemmed (e.g. "added" -> "add", "fixed" -> "fix").
GENERIC_STEMS = frozenset({"add", "fix", "updat", "creat", "mak", "us", "remov", "chang"})

FILE_RE = re.compile(
    r"\b[\w-]+\.(?:c|h|cpp|hpp|cc|py|ino|s|yaml|yml|json|toml|md|txt|ld|ioc)\b", re.I
)
ISSUE_REF_RE = re.compile(r"(?<![\w/&])#(\d+)\b")
WORD_RE = re.compile(r"[a-z0-9]+")
# A commit line starting with one of these only begins the work, so it does not
# count as a title match (a literal #N reference or a file mention still counts).
PARTIAL_MARKERS = frozenset({"start", "started", "begin", "wip", "partial", "initial",
                             "draft", "scaffold", "stub"})


@dataclass(frozen=True)
class Fact:
    id: str
    kind: str
    priority: int
    text: str
    data: dict[str, Any]
    evidence: list[str] = field(default_factory=list)
    certain: bool = True


# ---------------------------------------------------------------- text helpers


def stem(word: str) -> str:
    """Very light stemmer: strip -ing/-ed/-es/-s, then a trailing -e (words > 4 chars).

    The trailing-e step makes close/closes/closed/closing all map to "clos".
    """
    if len(word) <= 4:
        return word
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and not word.endswith("ss"):
            word = word[: -len(suffix)]
            break
    if len(word) > 4 and word.endswith("e"):
        word = word[:-1]
    return word


def tokens(text: str) -> list[str]:
    """Normalized meaningful tokens, in order, duplicates kept.

    File names (track.c) are removed first; they are matched separately.
    """
    text = FILE_RE.sub(" ", text.lower())
    out = []
    for w in WORD_RE.findall(text):
        if w in STOPWORDS or w.isdigit() or len(w) < 2:
            continue
        s = stem(w)
        if s in GENERIC_STEMS or s in STOPWORDS:
            continue
        out.append(s)
    return out


def file_names(text: str) -> set[str]:
    return {m.group(0).lower() for m in FILE_RE.finditer(text)}


def first_line(message: str | None) -> str:
    return (message or "").strip().splitlines()[0].strip() if (message or "").strip() else ""


def fmt_day(d: date) -> str:
    return f"{d.strftime('%b')} {d.day}"


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _due_date(due_on: str | None) -> date | None:
    """GitHub due_on is a UTC timestamp; the due date is its UTC date component."""
    dt = _parse_ts(due_on)
    return dt.astimezone(UTC).date() if dt else None


# ---------------------------------------------------------------- main entry


def compute_facts(
    conn: sqlite3.Connection, now: datetime | None = None, days: int = 7
) -> list[Fact]:
    now = (now or datetime.now(UTC)).astimezone()  # aware, local tz
    tz = now.tzinfo
    today = now.date()

    def local_day(ts: str | None) -> date | None:
        dt = _parse_ts(ts)
        return dt.astimezone(tz).date() if dt else None

    milestones = conn.execute(
        "SELECT * FROM milestones WHERE state = 'open' ORDER BY number"
    ).fetchall()
    issues = conn.execute("SELECT * FROM issues ORDER BY number").fetchall()
    open_issues = [i for i in issues if i["state"] == "open"]
    commits = [
        c for c in conn.execute("SELECT * FROM commits ORDER BY committed_at DESC, sha")
        if _parse_ts(c["committed_at"]) is not None
    ]

    facts: list[Fact] = []
    due_soon: set[int] = set()

    # 1, 2: milestone deadlines and progress
    for m in milestones:
        closed, opened = m["closed_issues"] or 0, m["open_issues"] or 0
        total = closed + opened
        ev = [f"milestone:{m['title']}"]
        due = _due_date(m["due_on"])
        if due is None:
            facts.append(Fact(
                id=f"milestone_progress:{m['number']}", kind="milestone_progress", priority=4,
                text=f"Milestone {m['title']} has {closed} of {total} issues closed and no due date.",
                data={"milestone": m["title"], "closed": closed, "total": total, "open": opened},
                evidence=ev,
            ))
            continue
        days_left = (due - today).days
        if days_left <= DEADLINE_SOON_DAYS:
            due_soon.add(m["number"])
        data = {"milestone": m["title"], "due": due.isoformat(), "days_left": days_left,
                "closed": closed, "total": total, "open": opened, "pace_per_day": None}
        head = f"Milestone {m['title']}"
        progress = f"{closed} of {total} issues closed"
        if days_left < 0:
            text = (f"{head} was due {fmt_day(due)} and is overdue by "
                    f"{plural(-days_left, 'day')}, with {progress}.")
        elif days_left == 0:
            text = f"{head} is due today ({fmt_day(due)}), with {progress}."
        else:
            pace = round(opened / days_left, 1)
            data["pace_per_day"] = pace
            text = (f"{head} is due {fmt_day(due)}, in {plural(days_left, 'day')}, with {progress}; "
                    f"finishing the {opened} open needs {pace} issues per day.")
        facts.append(Fact(
            id=f"milestone_deadline:{m['number']}", kind="milestone_deadline",
            priority=1 if days_left <= DEADLINE_SOON_DAYS else 3,
            text=text, data=data, evidence=ev,
        ))

    # 3: maybe_done
    # Windows are whole local calendar days, so facts only change when the date does.
    match_cutoff = today - timedelta(days=MATCH_WINDOW_DAYS)
    recent = [c for c in commits if local_day(c["committed_at"]) >= match_cutoff]
    maybe: dict[int, list[dict]] = {}
    for issue in open_issues:
        matches = match_issue(issue, recent)
        if not matches:
            continue
        maybe[issue["number"]] = matches
        n = issue["number"]
        best = matches[0]
        shown = [{**mt, "date": local_day(mt["committed_at"]).isoformat()} for mt in matches]
        for s in shown:
            s.pop("committed_at")
        data = {"issue": n, "title": issue["title"], "method": best["method"], "commits": shown}
        quoted = f'#{n} "{issue["title"]}"'
        if best["method"] == "reference":
            text = (f"{quoted} may already be done: commit {best['sha']} "
                    f"(\"{best['line']}\") references it.")
        elif best["method"] == "tokens":
            text = (f"{quoted} may already be done: commit {best['sha']} "
                    f"(\"{best['line']}\") describes similar work.")
        else:
            fname = best["file"]
            data["file"] = fname
            data["note"] = f"commits touching {fname} mention it; the file may already exist"
            count = best["file_commit_count"]
            text = (f"{quoted} may already be done: {plural(count, 'recent commit')} "
                    f"mention {fname}, so the file may already exist.")
        facts.append(Fact(
            id=f"maybe_done:{n}", kind="maybe_done", priority=2, text=text, data=data,
            evidence=[f"#{n}"] + [f"commit:{mt['sha']}" for mt in matches], certain=False,
        ))

    # 4: adjusted_progress
    for m in milestones:
        nums = sorted(i["number"] for i in open_issues
                      if i["milestone_number"] == m["number"] and i["number"] in maybe)
        if not nums:
            continue
        closed, opened = m["closed_issues"] or 0, m["open_issues"] or 0
        total = closed + opened
        adj = min(closed + len(nums), total)
        refs = ", ".join(f"#{x}" for x in nums)
        those = "that one is" if len(nums) == 1 else f"those {len(nums)} are"
        facts.append(Fact(
            id=f"adjusted_progress:{m['number']}", kind="adjusted_progress", priority=2,
            text=(f"If {those} done ({refs}), milestone {m['title']} is at "
                  f"{adj} of {total} closed instead of {closed}."),
            data={"milestone": m["title"], "closed": closed, "total": total,
                  "maybe_done": nums, "adjusted_closed": adj},
            evidence=[f"milestone:{m['title']}"] + [f"#{x}" for x in nums], certain=False,
        ))

    # 5: activity
    act_cutoff = today - timedelta(days=days)
    window = [c for c in commits if local_day(c["committed_at"]) >= act_cutoff]
    if window:
        dates = sorted(local_day(c["committed_at"]) for c in window)
        # Count each token once per commit; ties go to the token seen most recently.
        counts: Counter[str] = Counter()
        latest: dict[str, float] = {}
        for c in window:
            ts = _parse_ts(c["committed_at"]).timestamp()
            for t in set(tokens(first_line(c["message"]))):
                counts[t] += 1
                latest[t] = max(latest.get(t, ts), ts)
        theme = sorted(counts, key=lambda t: (-counts[t], -latest[t], t))[:3]
        span = (f"all on {fmt_day(dates[0])}" if dates[0] == dates[-1]
                else f"{fmt_day(dates[0])} to {fmt_day(dates[-1])}")
        theme_txt = f"; the most frequent words are {', '.join(theme)}" if theme else ""
        facts.append(Fact(
            id="activity", kind="activity", priority=3,
            text=f"There were {plural(len(window), 'commit')} in the last {days} days "
                 f"({span}){theme_txt}.",
            data={"days": days, "count": len(window), "first": dates[0].isoformat(),
                  "last": dates[-1].isoformat(), "theme": theme,
                  "theme_counts": {t: counts[t] for t in theme}},
            evidence=[f"commit:{c['sha'][:7]}" for c in window[:5]],
        ))

    # 6: quiet_gap
    if commits:
        last = commits[0]
        last_day = local_day(last["committed_at"])
        gap = (today - last_day).days
        if gap >= 2:
            facts.append(Fact(
                id="quiet_gap", kind="quiet_gap", priority=3,
                text=f"The last commit was {plural(gap, 'day')} ago, on {fmt_day(last_day)}.",
                data={"days_since": gap, "last_commit": last["sha"][:7],
                      "last_date": last_day.isoformat()},
                evidence=[f"commit:{last['sha'][:7]}"],
            ))

    # 7: recently_closed
    closed_recent = sorted(
        (i for i in issues if i["state"] == "closed" and i["closed_at"]
         and local_day(i["closed_at"]) >= act_cutoff),
        key=lambda i: (i["closed_at"], i["number"]), reverse=True,
    )
    if closed_recent:
        items = [{"issue": i["number"], "title": i["title"],
                  "date": local_day(i["closed_at"]).isoformat()} for i in closed_recent]
        listed = "; ".join(
            f"#{x['issue']} {x['title']} ({fmt_day(date.fromisoformat(x['date']))})"
            for x in items[:5])
        more = f"; and {len(items) - 5} more" if len(items) > 5 else ""
        n = len(items)
        facts.append(Fact(
            id="recently_closed", kind="recently_closed", priority=4,
            text=f"{plural(n, 'issue')} {'was' if n == 1 else 'were'} closed in the last "
                 f"{days} days: {listed}{more}.",
            data={"days": days, "count": n, "issues": items},
            evidence=[f"#{x['issue']}" for x in items],
        ))

    # 8: stale_issue
    stale_cutoff = today - timedelta(days=STALE_DAYS)
    ms_title = {m["number"]: m["title"] for m in milestones}
    for i in open_issues:
        if i["milestone_number"] not in due_soon:
            continue
        upd = local_day(i["updated_at"])
        if upd is None or upd >= stale_cutoff:
            continue
        idle = (today - upd).days
        title = ms_title[i["milestone_number"]]
        facts.append(Fact(
            id=f"stale_issue:{i['number']}", kind="stale_issue", priority=3,
            text=f"#{i['number']} {i['title']} in milestone {title} has not been updated "
                 f"in {idle} days.",
            data={"issue": i["number"], "title": i["title"], "milestone": title,
                  "days_since_update": idle},
            evidence=[f"#{i['number']}", f"milestone:{title}"],
        ))

    # 9: unassigned_work
    loose = [i for i in open_issues if i["milestone_number"] is None]
    if loose:
        labels = Counter(lb for i in loose for lb in set(json.loads(i["labels"] or "[]")))
        unlabeled = sum(1 for i in loose if not json.loads(i["labels"] or "[]"))
        parts = [f"{k} {'is' if k == 1 else 'are'} labeled {lb}"
                 for lb, k in sorted(labels.items(), key=lambda kv: (-kv[1], kv[0]))]
        if unlabeled:
            parts.append(f"{unlabeled} {'has' if unlabeled == 1 else 'have'} no label")
        n = len(loose)
        facts.append(Fact(
            id="unassigned_work", kind="unassigned_work", priority=5,
            text=f"{plural(n, 'open issue')} {'has' if n == 1 else 'have'} no milestone; "
                 f"{', '.join(parts)}.",
            data={"count": n, "labels": dict(sorted(labels.items())), "unlabeled": unlabeled,
                  "issues": [i["number"] for i in loose]},
            evidence=[f"#{i['number']}" for i in loose],
        ))

    # 10: electrical (active electrical cards, by their finding's status)
    elec = electrical_fact(conn)
    if elec is not None:
        facts.append(elec)

    # 11: research (active research cards and the issues they point at)
    res = research_fact(conn)
    if res is not None:
        facts.append(res)

    # 12: suggested next steps (ranked from the facts above)
    steps = _rank_next_steps(conn, today, tz, milestones, open_issues, maybe, commits)
    if steps:
        facts.append(next_steps_fact(steps))

    return sorted(facts, key=lambda f: (f.priority, f.id))


ELECTRICAL_STATUS_ORDER = ("fail", "warn", "unknown")


def electrical_fact(conn: sqlite3.Connection) -> Fact | None:
    """Count active electrical cards by finding status; None if there are none."""
    rows = conn.execute(
        """SELECT c.id, c.title, COALESCE(e.status, 'unknown') AS status FROM cards c
           LEFT JOIN electrical_findings e ON e.check_id = c.check_id
           WHERE c.type = 'electrical' AND c.status = 'active' ORDER BY c.id"""
    ).fetchall()
    if not rows:
        return None
    counts = Counter(r["status"] for r in rows)
    by = {k: counts[k] for k in ELECTRICAL_STATUS_ORDER if counts[k]}
    words = {"fail": "over a limit", "warn": "close to a limit",
             "unknown": "unconfirmed because a hardware.yaml value is missing"}
    parts = [f"{k} {words[s]}" for s, k in by.items()]
    n = len(rows)
    return Fact(
        id="electrical", kind="electrical", priority=2 if by.get("fail") else 3,
        text=f"{plural(n, 'electrical check card')} {'is' if n == 1 else 'are'} active: "
             f"{', '.join(parts)}.",
        data={"active": n, "by_status": by},
        evidence=["cards:electrical"],
    )


def research_fact(conn: sqlite3.Connection) -> Fact | None:
    """Count active research cards and name the issues (or libraries) they point at."""
    rows = conn.execute(
        """SELECT c.source_ref, i.number, i.title FROM cards c
           LEFT JOIN issues i ON c.source_ref = '#' || i.number
           WHERE c.type = 'research' AND c.status = 'active' ORDER BY c.id"""
    ).fetchall()
    if not rows:
        return None
    issues: dict[int, str] = {}
    libraries: list[str] = []
    for r in rows:
        if r["number"] is not None:
            issues.setdefault(r["number"], r["title"])
        elif r["source_ref"] and r["source_ref"] not in libraries:
            libraries.append(r["source_ref"])
    targets = [f"#{n} {t}" for n, t in sorted(issues.items())] + libraries
    n = len(rows)
    return Fact(
        id="research", kind="research", priority=4,
        text=f"{plural(n, 'research card')} {'is' if n == 1 else 'are'} active, for "
             f"{'; '.join(targets)}.",
        data={"active": n, "issues": sorted(issues), "libraries": libraries},
        evidence=[f"#{x}" for x in sorted(issues)],
    )


# ---------------------------------------------------------------- next steps

NEXT_STEPS_MAX = 5
NEXT_STEPS_PRIORITY = 6      # below every other fact: never crowds the briefing
MAX_VERIFY_LISTED = 3        # maybe-done issues named in the "confirm and close" step
MAX_MILESTONE_STEPS = 2      # milestone issues before blockers and the quiet gap
PHASE_RE = re.compile(r"^phase[-_ ]?(\d+)$", re.I)


@dataclass(frozen=True)
class NextStep:
    """One suggested next step: deterministic, with the reason and evidence behind it.

    ``kind``: "verify_close" (open issues that may already be done), "milestone_issue"
    (next open issue of the nearest-due milestone), "blocker" (hardware.yaml values
    that electrical checks need), "quiet_gap" (no commits for a while; neutral).
    """
    rank: int
    kind: str
    text: str
    reason: str
    evidence: list[str] = field(default_factory=list)
    certain: bool = True


def _phase(labels_json: str | None) -> int:
    """Phase number from a "phase-N" label (lowest wins); 99 when there is none."""
    try:
        labels = json.loads(labels_json or "[]")
    except ValueError:
        labels = []
    nums = [int(m.group(1)) for lb in labels if (m := PHASE_RE.match(str(lb)))]
    return min(nums) if nums else 99


def _rank_next_steps(conn: sqlite3.Connection, today: date, tz: Any, milestones: list,
                     open_issues: list, maybe: dict[int, list[dict]],
                     commits: list) -> list[NextStep]:
    """Ranked next steps (at most NEXT_STEPS_MAX), from issues, milestones and checks.

    Order: (a) confirm and close issues that may already be done (cheap wins,
    uncertain); (b) open issues of the nearest-due milestone by phase label
    (phase-1 < phase-2 < phase-3 < none), then number, skipping maybe-done ones
    (at most MAX_MILESTONE_STEPS here); (c) electrical checks blocked on missing
    hardware.yaml values; (d) a quiet gap of 2+ days without commits; then more
    milestone issues if there is room.
    """
    due_ms = sorted(((m, d) for m in milestones if (d := _due_date(m["due_on"])) is not None),
                    key=lambda md: (md[1], md[0]["number"]))
    ms_rank = {m["number"]: k for k, (m, _) in enumerate(due_ms)}

    verify: list[NextStep] = []
    if maybe:
        nums = sorted(maybe, key=lambda n: (
            ms_rank.get(next((i["milestone_number"] for i in open_issues
                              if i["number"] == n), None), len(ms_rank)), n))
        by_num = {i["number"]: i for i in open_issues}
        listed = nums[:MAX_VERIFY_LISTED]
        shas = list(dict.fromkeys(mt["sha"] for n in listed for mt in maybe[n][:1]))
        names = ", ".join(f"#{n} {by_num[n]['title']}" for n in listed)
        more = f" (and {len(nums) - len(listed)} more)" if len(nums) > len(listed) else ""
        what = ("1 issue that may already be done" if len(nums) == 1
                else f"{len(nums)} issues that may already be done")
        verify.append(NextStep(
            rank=0, kind="verify_close",
            text=f"Confirm and close {what}: {names}{more}.",
            reason=f"recent commits ({', '.join(shas)}) look like the same work; "
                   "a quick check, not certain",
            evidence=[f"#{n}" for n in nums] + [f"commit:{s}" for s in shas], certain=False))

    work: list[NextStep] = []
    if due_ms:
        m, due = due_ms[0]
        cands = sorted((i for i in open_issues if i["milestone_number"] == m["number"]
                        and i["number"] not in maybe),
                       key=lambda i: (_phase(i["labels"]), i["number"]))
        left = (due - today).days
        rel = (f"in {plural(left, 'day')}" if left > 0 else "today" if left == 0
               else f"overdue by {plural(-left, 'day')}")
        for i in cands:
            ph = _phase(i["labels"])
            ph_txt = f"phase-{ph}" if ph != 99 else "no phase label"
            work.append(NextStep(
                rank=0, kind="milestone_issue",
                text=f"Work on #{i['number']} {i['title']} (milestone {m['title']}, "
                     f"due {fmt_day(due)}, {rel}).",
                reason=f"open in the nearest-due milestone; {ph_txt}, ordered by phase "
                       "then issue number",
                evidence=[f"#{i['number']}", f"milestone:{m['title']}"]))

    blockers: list[NextStep] = []
    missing = [r["check_id"][len("missing:"):] for r in conn.execute(
        "SELECT check_id FROM electrical_findings WHERE check_id LIKE 'missing:%' "
        "AND COALESCE(status, 'unknown') != 'pass' ORDER BY check_id")]
    cards = conn.execute(
        """SELECT c.id, COALESCE(e.status, 'unknown') AS status FROM cards c
           LEFT JOIN electrical_findings e ON e.check_id = c.check_id
           WHERE c.type = 'electrical' AND c.status = 'active' ORDER BY c.id""").fetchall()
    unknown_cards = [c["id"] for c in cards if c["status"] == "unknown"]
    if missing:
        n_cards = len(unknown_cards)
        tail = (f": {plural(n_cards, 'electrical check card')} "
                f"{'stays' if n_cards == 1 else 'stay'} unconfirmed until then"
                if n_cards else "")
        blockers.append(NextStep(
            rank=0, kind="blocker",
            text=f"Fill in {plural(len(missing), 'hardware.yaml value')} "
                 f"({', '.join(missing)}){tail}.",
            reason="electrical checks can't confirm the power rails without these values",
            evidence=[f"card:{c}" for c in unknown_cards]
            + [f"check:missing:{x}" for x in missing]))
    elif cards:
        blockers.append(NextStep(
            rank=0, kind="blocker",
            text=f"Review {plural(len(cards), 'active electrical check card')}.",
            reason="electrical checks found problems or open questions",
            evidence=[f"card:{c['id']}" for c in cards]))

    quiet: list[NextStep] = []
    if commits:
        last = commits[0]
        dt = _parse_ts(last["committed_at"])
        last_day = dt.astimezone(tz).date()
        gap = (today - last_day).days
        if gap >= 2:
            quiet.append(NextStep(
                rank=0, kind="quiet_gap",
                text=f"No commits for {plural(gap, 'day')}; the last was {last['sha'][:7]} "
                     f"on {fmt_day(last_day)}.",
                reason="for context only, not a judgement",
                evidence=[f"commit:{last['sha'][:7]}"]))

    ordered = (verify + work[:MAX_MILESTONE_STEPS] + blockers + quiet
               + work[MAX_MILESTONE_STEPS:])[:NEXT_STEPS_MAX]
    return [NextStep(k + 1, s.kind, s.text, s.reason, s.evidence, s.certain)
            for k, s in enumerate(ordered)]


def next_steps_fact(steps: list[NextStep]) -> Fact:
    listed = " ".join(f"{s.rank}. {s.text}" for s in steps)
    evidence = list(dict.fromkeys(e for s in steps for e in s.evidence))
    return Fact(
        id="next_steps", kind="next_steps", priority=NEXT_STEPS_PRIORITY,
        text=f"Suggested next steps (computed by Jarvis, not a schedule): {listed}",
        data={"steps": [asdict(s) for s in steps]}, evidence=evidence,
        certain=all(s.certain for s in steps))


def compute_next_steps(conn: sqlite3.Connection, now: datetime | None = None) -> list[NextStep]:
    """The ranked suggested next steps (empty when there is nothing to suggest)."""
    for f in compute_facts(conn, now):
        if f.kind == "next_steps":
            return [NextStep(**d) for d in f.data["steps"]]
    return []


# ---------------------------------------------------------------- matching


def match_issue(issue: sqlite3.Row | dict, commits: list) -> list[dict]:
    """Best commits (max 2) that may have completed ``issue``; [] if none.

    Methods, strongest first: "reference" (message contains #N), "tokens"
    (>= 50% of the issue title's tokens and >= 2 tokens appear in the commit's
    first line), "file" (the title names a file the commit's first line names).
    Commits older than the issue are ignored.
    """
    n = issue["number"]
    title = issue["title"] or ""
    created = _parse_ts(issue["created_at"]) if "created_at" in issue.keys() else None  # noqa: SIM118 (sqlite3.Row)
    itoks = set(tokens(title))
    ifiles = file_names(title)
    cands: list[dict] = []
    file_hits: dict[str, int] = Counter()
    for c in commits:
        when = _parse_ts(c["committed_at"])
        if created and when < created:
            continue
        line = first_line(c["message"])
        base = {"sha": c["sha"][:7], "line": line, "committed_at": c["committed_at"]}
        refs = {int(x) for x in ISSUE_REF_RE.findall(c["message"] or "")}
        if n in refs:
            cands.append({**base, "method": "reference", "score": 1.0})
            continue
        words = WORD_RE.findall(line.lower())
        partial = bool(words) and words[0] in PARTIAL_MARKERS
        if itoks and not partial:
            overlap = itoks & set(tokens(line))
            score = round(len(overlap) / len(itoks), 2)
            if score >= MATCH_THRESHOLD and len(overlap) >= MATCH_MIN_OVERLAP:
                cands.append({**base, "method": "tokens", "score": score,
                              "overlap": sorted(overlap)})
                continue
        shared = sorted(ifiles & file_names(line))
        if shared:
            for f in shared:
                file_hits[f] += 1
            cands.append({**base, "method": "file", "score": 0.3, "file": shared[0]})
    rank = {"reference": 0, "tokens": 1, "file": 2}
    cands.sort(key=lambda d: (rank[d["method"]], -d["score"], _neg_ts(d["committed_at"]),
                              d["sha"]))
    best = cands[:2]
    for d in best:
        if d["method"] == "file":
            d["file_commit_count"] = file_hits[d["file"]]
    return best


def _neg_ts(ts: str) -> float:
    return -_parse_ts(ts).timestamp()


# ---------------------------------------------------------------- hashing


def facts_hash(facts: list[Fact]) -> str:
    """sha256 over a canonical JSON of the facts; independent of input order."""
    canon = sorted((asdict(f) for f in facts), key=lambda d: d["id"])
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
