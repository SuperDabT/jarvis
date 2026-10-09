"""Ask Jarvis: grounded questions and answers over the synced project data.

Objectivity works the same way as the briefing: Python gathers the data, the
model only phrases an answer, and code checks the answer afterwards.

1. ``build_context`` turns the database into a compact text block (about 6k
   tokens at most). Every item starts with a reference in brackets ("[#31]",
   "[commit:02fb2d2]", "[milestone:Core demo]", "[card:12]",
   "[check:rail_budget:5V]", "[fact:activity]"). Only real rows go in; all counts
   and day differences are computed here, so the model never has to.
2. ``ask`` streams the model's reply. The model must end with a
   ``SOURCES: ref, ref`` line naming the references it used.
3. Post-validation never drops the answer but makes problems visible:
   unknown refs in SOURCES are dropped, an issue number or commit sha that is not
   in the database gets a note, and numbers or dates that appear nowhere in the
   context (or in the question) are listed in ``Answer.unverified_numbers``
   with a note.

Streaming and the SOURCES line: ``on_token`` receives display text as it
arrives. A line that starts with "SOURCES:" is held back (only the start of a
line is buffered, and only while it could still become "SOURCES:"), so the
user never sees it. When ``ask`` returns, the GUI should replace the streamed
text with ``answer.display_text`` (the cleaned answer plus notes) and show
``answer.sources`` as links: that final text is the authoritative one.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from jarvis.briefing import (
    DATE_RE,
    ISO_DATE_RE,
    NUMBER_RE,
    NUMBER_WORD_RE,
    NUMBER_WORDS,
    SHA_RE,
    _fmt_day,
    _norm_date,
    _norm_num,
)
from jarvis.config import Config
from jarvis.db import now_iso
from jarvis.facts import ISSUE_REF_RE, Fact, NextStep, compute_facts, first_line, tokens
from jarvis.ollama import DEFAULT_EMBED_MODEL, OllamaClient, OllamaError

CHARS_PER_TOKEN = 4
CITED_SOURCES_MAX = 6
CONTEXT_TOKEN_BUDGET = 6000
BODY_CHARS = 300          # issue body excerpt for relevant issues
MAX_BODIES = 6            # at most this many issue bodies per question
WHY_CHARS = 220           # card / electrical "why" excerpt
RECENT_CLOSED_DAYS = 30
MAX_COMMITS = 40
HISTORY_TURNS = 6         # question/answer pairs kept for multi-turn
# qwen3-embedding:0.6b cosine between a question and "title + body"; only used to
# pull in extra issue bodies the keywords missed (see README "Ask Jarvis").
EMBED_MIN_SIMILARITY = 0.62
MAX_EMBED_EXTRA = 3

# Question words that say nothing about which issue is meant.
_QUESTION_NOISE = frozenset(tokens(
    "issue issues done left work worked working last week weeks day days month project "
    "status ok okay fine tell show list still open closed close finish finished need "
    "needs remaining remain happen happened about which recent recently anything thing "
    "things going progress lately yet already been"
))

SYSTEM_PROMPT = """\
You are Jarvis, a local assistant. The owner of an embedded project asks you questions \
about their own project. You answer from the CONTEXT block in their message: their synced \
GitHub data (issues, milestones, commits), facts computed by a program, and electrical \
check results computed from their hardware.yaml. Every CONTEXT item starts with its \
reference in square brackets, like [#31] or [commit:02fb2d2].

Rules:
- Use ONLY the CONTEXT. No outside knowledge, no guesses, no assumptions about work that \
is not recorded there. If the CONTEXT does not contain the answer, say so plainly in one \
sentence and name the data that would answer it (an issue, a commit, a hardware.yaml \
value). Questions unrelated to the project (weather, news, general trivia) get one \
sentence saying Jarvis only knows this project's data.
- Be concise: at most 6 sentences, or a short list of at most 8 items. No preamble.
- Neutral tone: no praise, no encouragement, no hype, no exclamation marks.
- An issue is done only if it is closed. Items marked (uncertain) are heuristic guesses: \
say "may", for example "#28 is open but may already be done: commit 02fb2d2 looks like \
the same work". Never state a guess as fact.
- Do no arithmetic: do not add, subtract or count anything yourself. Quote numbers, \
counts and dates exactly as the CONTEXT gives them. Every count you need is already in \
the CONTEXT (the "counts:" part of each milestone line, the section totals): quote counts \
from the CONTEXT; never compute them. Do not write "plus N more", "N others" or "the \
remaining N" unless that exact count is in the CONTEXT; name the items instead.
- For a milestone question, start with its due date, days left and closed count exactly \
as the milestone line gives them.
- Refer to issues as #N with a few words of the title, and to commits by their \
7-character sha. Never write the square-bracket references in the answer itself; they \
belong only on the SOURCES line.
- For "recently" or "in the last N days" questions, use the "N days ago" labels in the \
CONTEXT to pick items, and the precomputed counts. Summarize the work by theme in plain \
words, one item per theme with its dates and one or two example shas, for example \
"Started the track.c tracker (Sep 30 to Oct 5, e.g. e9256c6)". Do not list every commit.
- For "which issues are about X": list the issues tagged "keyword hit" whose title \
or body is about X (the tag is a hint for you; do not copy it). Any other issue you think is related gets "may be related" and the \
reason from its title.
- For questions about what to do, work on, focus on, plan or prioritize: answer from \
SUGGESTED NEXT STEPS, in its order, starting with step 1; summarize steps rather than \
copying them, keep their hedges ("may already be done"), and say it is Jarvis's \
suggestion from the data, not a schedule. Do not invent tasks, dates or time estimates.
- Electrical results come from hardware.yaml values (mostly datasheet or seller figures, \
not measurements); say so when you answer about them.
- End with one last line: "SOURCES: " then the references you used, without brackets, \
separated by commas, for example: SOURCES: #31, commit:02fb2d2, milestone:Core demo, \
check:rail_budget:5V, fact:activity
"""

# Appended to SYSTEM_PROMPT for brevity="voice": the answer is read aloud.
VOICE_RULES = """
This answer will be spoken aloud by a text-to-speech voice. These rules override the \
length rule above:
- Answer in 1 to 3 short spoken sentences. No lists, no bullet points, no headings, no \
markdown, no tables, no references in square brackets, no parentheses if you can avoid them.
- Never read out a list of issue numbers. Give the count exactly as the CONTEXT states \
it (for example the milestone's "counts:"), then name at most 2 issues, each by number \
and a few words of its title. For SUGGESTED NEXT STEPS, cover only the first 2 or 3 \
steps, with at most 2 issue numbers in the whole answer.
- Keep hedges for uncertain items ("may already be done").
- Still end with the SOURCES line as described above; it is not spoken.
"""

UNREACHABLE = ("The local model isn't reachable (Ollama at {url}). "
               "Try again once it's running.")


# ---------------------------------------------------------------- data types


@dataclass(frozen=True)
class Source:
    ref: str
    label: str
    url: str | None = None


@dataclass
class Context:
    text: str
    refs: dict[str, Source]               # every ref present in ``text``
    issue_numbers: set[int]               # every issue in the database
    commit_shas: list[str]                # every commit sha in the database
    body_issues: list[int] = field(default_factory=list)
    omitted: dict[str, int] = field(default_factory=dict)  # section -> items cut for budget
    ranking: str = "keywords"             # or "keywords+embeddings"
    window_days: int | None = None        # time window the question asked about, if any
    fact_evidence: dict[str, list[str]] = field(default_factory=dict)
    # stable_prefix mode: text[:stable_chars] is the part that doesn't depend on the
    # question or the clock (byte-identical between questions, so Ollama can reuse
    # its KV cache for it); 0 = no split.
    stable_chars: int = 0
    # SUGGESTED NEXT STEPS (facts.NextStep) and every ref they point at (issues,
    # commits, milestones, cards, checks) plus "fact:next_steps".
    next_steps: list[NextStep] = field(default_factory=list)
    next_step_refs: set[str] = field(default_factory=set)
    today: date | None = None
    last_commit: tuple[str, date] | None = None   # (7-char sha, local day) of the newest

    @property
    def stable_text(self) -> str:
        return self.text[:self.stable_chars]

    @property
    def question_text(self) -> str:
        return self.text[self.stable_chars:].lstrip("\n")

    @property
    def tokens(self) -> int:
        return math.ceil(len(self.text) / CHARS_PER_TOKEN)


@dataclass
class Answer:
    text: str                              # the model's answer, SOURCES line removed
    sources: list[Source]
    unverified_numbers: list[str]
    notes: list[str]
    model: str | None
    duration: float                        # seconds, whole ask() call
    first_token_s: float | None = None     # seconds until the first streamed chunk
    error: str | None = None               # set when the model failed (text explains)
    raw: str = ""                          # the full model reply, unedited
    dropped_refs: list[str] = field(default_factory=list)
    context_tokens: int = 0
    # Only the refs the answer itself cited (SOURCES line) or named inline (#N, a sha),
    # without the fact-evidence expansion that ``sources`` adds; at most
    # CITED_SOURCES_MAX, ``cited_more_count`` says how many more there were. Empty
    # when the answer text is empty. (``sources`` is unchanged, for compatibility.)
    cited_sources: list[Source] = field(default_factory=list)
    cited_more_count: int = 0
    # Issue numbers / shas the answer named that aren't in the synced data.
    invalid_refs: list[str] = field(default_factory=list)
    # Statements about commit dates that contradict the synced commits.
    claim_errors: list[str] = field(default_factory=list)
    # Escalation (see jarvis.escalate): which tier produced this answer, whether a
    # fast answer was judged weak and replaced, why, and how long the model reasoned.
    tier: str = "fast"                     # "fast" | "deep" | "fallback"
    escalated: bool = False
    escalation_reasons: list[str] = field(default_factory=list)
    thinking_s: float | None = None

    @property
    def display_text(self) -> str:
        return "\n\n".join([self.text, *self.notes]) if self.notes else self.text


# ---------------------------------------------------------------- helpers


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _one_line(text: str | None, limit: int) -> str:
    s = re.sub(r"\s+", " ", text or "").strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _labels(row: sqlite3.Row) -> list[str]:
    try:
        out = json.loads(row["labels"] or "[]")
    except ValueError:
        return []
    return [str(x) for x in out] if isinstance(out, list) else []


def _issue_url(repo: str, row: sqlite3.Row) -> str:
    return row["html_url"] or f"https://github.com/{repo}/issues/{row['number']}"


def plural_issues(n: int) -> str:
    return f"{n} issue" if n == 1 else f"{n} issues"


def mentioned_issues(text: str) -> list[int]:
    """Issue numbers written as #N or "issue N" in ``text``, in order."""
    nums = [int(x) for x in ISSUE_REF_RE.findall(text)]
    nums += [int(x) for x in re.findall(r"\bissue\s+(\d+)\b", text, re.I)]
    return list(dict.fromkeys(nums))


def question_terms(text: str) -> set[str]:
    return set(tokens(text)) - _QUESTION_NOISE


_WORD_NUM = {"one": 1, "a": 1, "an": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "seven": 7, "eight": 8, "nine": 9, "ten": 10, "couple": 2, "few": 3}
_UNIT_DAYS = {"day": 1, "week": 7, "month": 30}


def time_window(question: str) -> int | None:
    """Days of history a question asks about ("last two weeks" -> 14), else None."""
    q = question.lower()
    m = re.search(r"\b(?:last|past|previous)\s+(?:(\d+|one|a|an|two|three|four|five|six|"
                  r"seven|eight|nine|ten|couple(?: of)?|few)\s+)?(day|week|month)s?\b", q)
    if m:
        n = m.group(1)
        num = 1 if n is None else int(n) if n.isdigit() else _WORD_NUM[n.split()[0]]
        return num * _UNIT_DAYS[m.group(2)]
    for pat, days in ((r"\bfortnight\b", 14), (r"\bthis week\b", 7), (r"\bthis month\b", 30),
                      (r"\byesterday\b", 1), (r"\btoday\b", 0),
                      (r"\b(recently|lately|recent)\b", 14)):
        if re.search(pat, q):
            return days
    return None


# ---------------------------------------------------------------- relevance


def body_excerpt(body: str | None, terms: Sequence[str], limit: int = BODY_CHARS) -> str:
    """The body's start, or (if a matched term only appears later) start + the match."""
    text = re.sub(r"\s+", " ", body or "").strip()
    if len(text) <= limit:
        return text
    want = set(terms)
    for m in re.finditer(r"[A-Za-z0-9]+", text):
        if m.start() < limit - 20:
            if set(tokens(m.group(0))) & want:
                break  # already inside the plain excerpt
            continue
        if set(tokens(m.group(0))) & want:
            head = text[: limit // 2].rstrip()
            a = max(m.start() - limit // 4, limit // 2)
            return f"{head} … {_one_line(text[a:], limit // 2)}"
    return _one_line(text, limit)



def keyword_scores(question: str, issues: Sequence[sqlite3.Row],
                   milestone_titles: dict[int, str]) -> dict[int, tuple[float, list[str]]]:
    """issue number -> (score, matched terms) for issues sharing a term with the question.

    A term in the title, labels or milestone name counts 2, one only in the body 1.
    """
    q = question_terms(question)
    out: dict[int, tuple[float, list[str]]] = {}
    if not q:
        return out
    for i in issues:
        head = " ".join([i["title"] or "", *_labels(i),
                         milestone_titles.get(i["milestone_number"], "")])
        head_hits = q & set(tokens(head))
        body_hits = (q & set(tokens(i["body"] or ""))) - head_hits
        score = 2 * len(head_hits) + len(body_hits)
        if score:
            out[i["number"]] = (score, sorted(head_hits | body_hits))
    return out


_EMBED_CACHE: dict[tuple[str, str], list[float]] = {}


def _cosine(a: list[float], b: list[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b, strict=False)) / (na * nb) if na and nb else 0.0


def embedding_scores(embedder: Any, question: str, issues: Sequence[sqlite3.Row],
                     model: str = DEFAULT_EMBED_MODEL) -> dict[int, float] | None:
    """issue number -> cosine(question, title + body); None if embedding is unavailable.

    Issue vectors are cached in memory by text hash, so only new or edited issues
    are embedded again within one process (the window).
    """
    texts = {i["number"]: f"{i['title'] or ''}\n{(i['body'] or '')[:1000]}" for i in issues}
    keys = {n: (model, hashlib.sha1(t.encode()).hexdigest()) for n, t in texts.items()}
    todo = [n for n in texts if keys[n] not in _EMBED_CACHE]
    try:
        vecs = embedder.embed([question] + [texts[n] for n in todo], model=model)
    except (OllamaError, AttributeError, TypeError):
        return None
    qvec, rest = vecs[0], vecs[1:]
    for n, v in zip(todo, rest, strict=True):
        _EMBED_CACHE[keys[n]] = v
    return {n: _cosine(qvec, _EMBED_CACHE[keys[n]]) for n in texts}


# ---------------------------------------------------------------- context


@dataclass
class _Section:
    title: str
    lines: list[str]
    # Lines kept no matter the budget (facts, milestones); the rest are trimmed from the end.
    required: bool = False


def _electrical(conn: sqlite3.Connection, cfg: Config,
                refs: dict[str, Source]) -> tuple[list[str], str | None]:
    """Lines about rails and electrical findings, plus the project description if known.

    Runs the pure checks on hardware.yaml (repo copy, else cfg.hardware_path), the
    same input ``pipeline.load_checks`` uses. Falls back to the stored
    ``electrical_findings`` rows if the file is missing or invalid.
    """
    from jarvis.checks import run_all
    from jarvis.hardware import HardwareError, find_hardware, load_hardware

    lines: list[str] = []
    project = None
    findings: list[tuple[str, str, str, str]] = []  # check_id, status, title, why
    try:
        src = find_hardware(conn, cfg.hardware_path)
    except OSError:
        src = None
    if src is None:
        lines.append("No hardware.yaml found (repo or local), so no live electrical checks.")
    else:
        try:
            hw = load_hardware(src.text)
        except HardwareError as e:
            lines.append(f"hardware.yaml ({src.label}) is invalid: {_one_line(str(e), 160)}")
        else:
            project = (hw.project_description or hw.project_name or "").strip() or None
            lines.append(f"Computed by Python from {src.label} (values are mostly datasheet "
                         "or seller figures, not measurements).")
            for r in hw.rails.values():
                cur = f", rated {r.max_current_a:g} A" if r.max_current_a is not None else ""
                tol = f", tolerance {r.tolerance_pct:g}%" if r.tolerance_pct is not None else ""
                loads = ", ".join(p.name for p in hw.parts_on(r.name)) or "none"
                lines.append(f"Rail {r.name}: {r.nominal_v:g} V{cur}{tol}; parts: {loads}")
            findings = [(f.check_id, f.status, f.title, f.why) for f in run_all(hw)]
    if not findings:
        for row in conn.execute(
                "SELECT check_id, status, title, why FROM electrical_findings ORDER BY check_id"):
            findings.append((row["check_id"], row["status"], row["title"], row["why"]))
        if findings:
            lines.append("Last stored check results:")
    for check_id, status, title, why in findings:
        ref = f"check:{check_id}"
        refs[ref] = Source(ref, title or check_id, None)
        lines.append(f"[{ref}] {(status or 'unknown').upper()}: {title}. "
                     f"{_one_line(why, WHY_CHARS)}")
    return lines, project


def build_context(conn: sqlite3.Connection, cfg: Config, question: str,
                  now: datetime | None = None, *, history: Sequence[tuple[str, str]] = (),
                  embedder: Any | None = None,
                  budget_tokens: int = CONTEXT_TOKEN_BUDGET,
                  stable_prefix: bool = False) -> Context:
    """A compact, reference-tagged text block of real project data for one question.

    Facts and milestones are always included; open issue titles next; the rest
    (issue bodies, electrical, commits, closed issues, cards) is trimmed to fit
    ``budget_tokens`` (estimated at 4 characters per token). Issue bodies are only
    included for issues the question names (#N) or shares keywords with, plus up to
    3 more ranked by embeddings when ``embedder`` (an OllamaClient) works.

    ``stable_prefix=True`` (voice) orders the context for prompt caching: first
    everything that only depends on the synced data and today's date (project,
    facts, milestones, open issues in a fixed order without question hints,
    electrical, commits, closed issues, cards), trimmed to the budget independently
    of the question; then the volatile part (local time and sync time, issues asked
    about, the issues relevant to this question with their bodies and keyword hints,
    a time-window view). ``Context.stable_chars`` marks the split.
    """
    now = (now or datetime.now(UTC)).astimezone()
    tz = now.tzinfo
    today = now.date()
    repo = cfg.repo
    refs: dict[str, Source] = {}

    def local_day(ts: str | None) -> date | None:
        dt = _parse_ts(ts)
        return dt.astimezone(tz).date() if dt else None

    def day_str(d: date | None) -> str:
        if d is None:
            return "?"
        ago = (today - d).days
        rel = ("today" if ago == 0 else "yesterday" if ago == 1 else f"{ago} days ago"
               if ago > 1 else f"in {-ago} days")
        return f"{_fmt_day(d)} ({d.strftime('%a')}, {rel})"

    issues = conn.execute("SELECT * FROM issues ORDER BY number").fetchall()
    by_num = {i["number"]: i for i in issues}
    milestones = conn.execute(
        "SELECT * FROM milestones ORDER BY state DESC, due_on IS NULL, due_on, number").fetchall()
    ms_title = {m["number"]: m["title"] for m in milestones}
    commits = [c for c in conn.execute("SELECT * FROM commits ORDER BY committed_at DESC, sha")
               if _parse_ts(c["committed_at"])]
    last_commit = ((commits[0]["sha"][:7], local_day(commits[0]["committed_at"]))
                   if commits else None)
    open_issues = [i for i in issues if i["state"] == "open"]

    # --- relevance
    recent_user = " ".join(c for r, c in list(history)[-4:] if r == "user")
    asked = list(dict.fromkeys(mentioned_issues(question) + mentioned_issues(recent_user)))
    window = time_window(question)
    kw = keyword_scores(question, open_issues, ms_title)
    ranking = "keywords"
    body_for: list[int] = [n for n in asked if n in by_num]
    for n, _ in sorted(kw.items(), key=lambda kv: (-kv[1][0], kv[0])):
        if n not in body_for:
            body_for.append(n)
    if embedder is not None and open_issues and question_terms(question):
        sims = embedding_scores(embedder, question, open_issues)
        if sims is not None:
            ranking = "keywords+embeddings"
            extra = [n for n, s in sorted(sims.items(), key=lambda kv: -kv[1])
                     if s >= EMBED_MIN_SIMILARITY and n not in body_for][:MAX_EMBED_EXTRA]
            body_for += extra
    body_for = body_for[:MAX_BODIES]

    # --- header
    last_sync = conn.execute("SELECT MAX(ran_at) FROM sync_log").fetchone()[0]
    sync_dt = _parse_ts(last_sync)
    sync_txt = (f"{_fmt_day(sync_dt.astimezone(tz).date())} {sync_dt.astimezone(tz):%H:%M}"
                if sync_dt else "never")
    elec_lines, project = _electrical(conn, cfg, refs)
    if stable_prefix:
        header = [
            f"Project: {project or 'an embedded project'} GitHub repo: {repo}.",
            f"Today: {now:%A} {_fmt_day(today)}, {today.year} ({today.isoformat()}).",
            "All counts and day differences in the CONTEXT were computed by Jarvis from "
            "the synced data.",
        ]
        now_lines = [f"Local time: {now:%H:%M}. GitHub data last synced: {sync_txt}."]
    else:
        header = [
            f"Project: {project or 'an embedded project'} GitHub repo: {repo}.",
            f"Today: {now:%A} {_fmt_day(today)}, {today.year} ({today.isoformat()}), "
            f"local time {now:%H:%M}.",
            f"GitHub data last synced: {sync_txt}. All counts and day differences below "
            "were computed by Jarvis from the synced data.",
        ]
        now_lines = []

    # --- facts
    try:
        facts: list[Fact] = compute_facts(conn, now)
    except Exception as e:  # noqa: BLE001 - facts are useful, not essential
        facts = []
        header.append(f"(Facts unavailable: {_one_line(str(e), 120)})")
    maybe_nums = {int(f.data["issue"]) for f in facts
                  if f.kind == "maybe_done" and "issue" in f.data}
    fact_lines = []
    fact_evidence: dict[str, list[str]] = {}
    next_lines: list[str] = []
    steps: list[NextStep] = []
    step_refs: set[str] = set()
    for f in facts:
        ref = f"fact:{f.id}"
        refs[ref] = Source(ref, _one_line(f.text, 90), None)
        fact_evidence[ref] = list(f.evidence)
        if f.kind == "next_steps":
            steps = [NextStep(**d) for d in f.data["steps"]]
            step_refs = {ref, *f.evidence}
            next_lines.append(f"[{ref}] Ranked by Jarvis from open issues, milestones, "
                              "electrical checks and commit dates:")
            next_lines += [f"{st.rank}. {'(uncertain) ' if not st.certain else ''}{st.text} "
                           f"Why: {st.reason}." for st in steps]
            continue
        fact_lines.append(f"[{ref}] {'(uncertain) ' if not f.certain else ''}{f.text}")

    # --- milestones
    ms_lines = []
    for m in milestones:
        ref = f"milestone:{m['title']}"
        refs[ref] = Source(ref, f"Milestone {m['title']}", m["html_url"])
        closed, opened = m["closed_issues"] or 0, m["open_issues"] or 0
        due_dt = _parse_ts(m["due_on"])
        if due_dt:
            due = due_dt.astimezone(UTC).date()
            left = (due - today).days
            rel = (f"in {left} days" if left > 1 else "tomorrow" if left == 1 else "today"
                   if left == 0 else f"overdue by {-left} days")
            due_txt = f"due {_fmt_day(due)}, {due.year} ({rel})"
        else:
            due_txt = "no due date"
        nums_open = [i["number"] for i in open_issues if i["milestone_number"] == m["number"]]
        nums_closed = [i["number"] for i in issues
                       if i["milestone_number"] == m["number"] and i["state"] == "closed"]
        line = (f"[{ref}] {m['title']}: {m['state']}, {due_txt}, {closed} of {closed + opened} "
                f"issues closed, {opened} open")
        if nums_open:
            line += f"; open: {', '.join(f'#{n}' for n in nums_open)}"
        if nums_closed:
            line += f"; closed: {', '.join(f'#{n}' for n in nums_closed)}"
        # Counts the model must quote rather than compute (it otherwise invents
        # "plus 6 more"-style totals).
        maybe = [n for n in nums_open if n in maybe_nums]
        maybe_txt = f" ({', '.join(f'#{n}' for n in maybe)})" if maybe else ""
        line += (f"; counts: {len(nums_open)} open, {len(nums_closed)} closed, "
                 f"{len(nums_open) + len(nums_closed)} total in the synced data, "
                 f"{len(maybe)} open but may be done{maybe_txt}, "
                 f"{len(nums_open) - len(maybe)} open and not marked may-be-done")
        ms_lines.append(line)

    # --- open issues (relevant first, then by milestone due order, then number)
    ms_rank = {m["number"]: k for k, m in enumerate(milestones)}

    def issue_line(i: sqlite3.Row, with_body: bool, hint: bool = True) -> str:
        n = i["number"]
        ref = f"#{n}"
        refs[ref] = Source(ref, f"#{n} {i['title']}", _issue_url(repo, i))
        bits = [f"[{ref}] {i['title']}", i["state"]]
        if i["milestone_number"] is not None:
            bits.append(f"milestone {ms_title.get(i['milestone_number'], '?')}")
        else:
            bits.append("no milestone")
        if _labels(i):
            bits.append("labels: " + ", ".join(_labels(i)))
        if i["state"] == "closed" and i["closed_at"]:
            bits.append(f"closed {day_str(local_day(i['closed_at']))}")
        else:
            bits.append(f"updated {day_str(local_day(i['updated_at']))}")
        if hint and n in kw:
            bits.append("keyword hit: " + ", ".join(kw[n][1]))  # a hint for the model
        line = " | ".join(bits)
        if with_body:
            body = body_excerpt(i["body"], kw[n][1] if n in kw else [])
            line += f"\n    body: {body or '(empty)'}"
        return line

    ordered_open = sorted(open_issues, key=lambda i: (
        0 if i["number"] in body_for else 1 if i["number"] in kw else 2,
        body_for.index(i["number"]) if i["number"] in body_for else 0,
        ms_rank.get(i["milestone_number"], len(ms_rank)), i["number"]))
    if stable_prefix:
        # fixed order, no hints or bodies: the same lines for every question
        stable_open = sorted(open_issues, key=lambda i: (
            ms_rank.get(i["milestone_number"], len(ms_rank)), i["number"]))
        open_lines = [issue_line(i, False, hint=False) for i in stable_open]
        relevant_lines = [issue_line(i, i["number"] in body_for) for i in ordered_open
                          if i["number"] in body_for or i["number"] in kw]
    else:
        open_lines = [issue_line(i, i["number"] in body_for) for i in ordered_open]
        relevant_lines = []

    asked_lines = []
    for n in asked:
        if n not in by_num:
            asked_lines.append(f"#{n}: there is no issue #{n} in the synced data.")
        elif by_num[n]["state"] != "open":
            asked_lines.append(issue_line(by_num[n], True))

    # --- recently closed
    cutoff_closed = today - timedelta(days=RECENT_CLOSED_DAYS)
    closed_recent = sorted(
        (i for i in issues if i["state"] == "closed" and i["number"] not in asked
         and (local_day(i["closed_at"]) or date.min) >= cutoff_closed),
        key=lambda i: (i["closed_at"], i["number"]), reverse=True)

    def count_since(days: int, dates: list[date | None]) -> int:
        cut = today - timedelta(days=days)
        return sum(1 for d in dates if d and d >= cut)

    closed_dates = [local_day(i["closed_at"]) for i in issues if i["state"] == "closed"]
    closed_lines = [f"Issues closed: {count_since(7, closed_dates)} in the last 7 days, "
                    f"{count_since(14, closed_dates)} in the last 14 days, "
                    f"{count_since(30, closed_dates)} in the last 30 days."]
    closed_lines += [issue_line(i, i["number"] in body_for) for i in closed_recent]

    # --- commits (only the asked-about window when the question names one)
    commit_dates = [local_day(c["committed_at"]) for c in commits]
    commit_lines = [f"Commits: {count_since(7, commit_dates)} in the last 7 days, "
                    f"{count_since(14, commit_dates)} in the last 14 days, "
                    f"{count_since(30, commit_dates)} in the last 30 days; "
                    f"{len(commits)} synced in total."]
    shown_commits = commits[:MAX_COMMITS]
    commits_title = "RECENT COMMITS (newest first)"
    closed_title = f"ISSUES CLOSED IN THE LAST {RECENT_CLOSED_DAYS} DAYS"

    def commit_line(c: sqlite3.Row) -> str:
        sha = c["sha"][:7]
        ref = f"commit:{sha}"
        line1 = first_line(c["message"])
        refs[ref] = Source(ref, f"{sha} {_one_line(line1, 70)}",
                           c["html_url"] or f"https://github.com/{repo}/commit/{c['sha']}")
        return f"[{ref}] {day_str(local_day(c['committed_at']))}: {_one_line(line1, 140)}"

    stable_views: dict[str, tuple[str, list[str]]] = {}
    if stable_prefix:
        # the default views, independent of the question (a time window is added to
        # the question part instead of replacing them)
        all_closed = sorted((i for i in issues if i["state"] == "closed"
                             and (local_day(i["closed_at"]) or date.min) >= cutoff_closed),
                            key=lambda i: (i["closed_at"], i["number"]), reverse=True)
        stable_views = {
            "commits": (commits_title, commit_lines
                        + [commit_line(c) for c in commits[:MAX_COMMITS]]),
            "closed": (closed_title, closed_lines[:1]
                       + [issue_line(i, False, hint=False) for i in all_closed]),
        }
    if window is not None:
        since = today - timedelta(days=window)
        shown_commits = [c for c, d in zip(commits, commit_dates, strict=True)
                         if d and d >= since][:MAX_COMMITS]
        span = f"since {_fmt_day(since)}, {since.year}"
        commits_title = (f"COMMITS IN THE ASKED PERIOD, THE LAST {window} DAYS ({span}; "
                         f"{len(shown_commits)} commits, newest first)")
        closed_recent = [i for i in closed_recent if local_day(i["closed_at"]) >= since]
        closed_title = (f"ISSUES CLOSED IN THE ASKED PERIOD, THE LAST {window} DAYS ({span}; "
                        f"{len(closed_recent)} issues)")
        closed_lines = closed_lines[:1] + [issue_line(i, False) for i in closed_recent]
        if not shown_commits:
            commit_lines.append(f"(no commits since {_fmt_day(since)})")
    commit_lines += [commit_line(c) for c in shown_commits]

    # --- cards
    card_lines = []
    for row in conn.execute("SELECT * FROM cards WHERE status = 'active' ORDER BY type, id"):
        ref = f"card:{row['id']}"
        refs[ref] = Source(ref, row["title"] or ref, None)
        src = f" (about {row['source_ref']})" if row["source_ref"] else ""
        card_lines.append(f"[{ref}] {row['type']} card{src}: {row['title']}. "
                          f"{_one_line(row['why'], WHY_CHARS)}")

    no_ms = sum(1 for i in open_issues if i["milestone_number"] is None)
    open_title = (f"OPEN ISSUES ({plural_issues(len(open_issues))} open in total, "
                  f"{no_ms} of them with no milestone)")

    # --- assemble within the budget
    if stable_prefix:
        windowed = {"commits": (commits_title, commit_lines),
                    "closed": (closed_title, closed_lines)} if window is not None else {}
        return _assemble_stable(
            budget_tokens, header, fact_lines, ms_lines, open_title, open_lines, elec_lines,
            stable_views, windowed, card_lines, now_lines, asked_lines, relevant_lines, refs,
            dict(issue_numbers=set(by_num),
                 commit_shas=[c["sha"] for c in conn.execute("SELECT sha FROM commits")],
                 ranking=ranking, fact_evidence=fact_evidence, window_days=window,
                 next_steps=steps, next_step_refs=step_refs, today=today,
                 last_commit=last_commit),
            body_for, next_lines)
    sections = {
        "header": _Section("PROJECT", header, required=True),
        "facts": _Section("FACTS (computed by Jarvis)", fact_lines or ["(none)"], required=True),
        "milestones": _Section("MILESTONES", ms_lines or ["(none)"], required=True),
        "next": _Section(NEXT_STEPS_TITLE, next_lines, required=True),
        "asked": _Section("ISSUES ASKED ABOUT", asked_lines),
        "open": _Section(open_title, open_lines or ["(none)"]),
        "electrical": _Section("ELECTRICAL (hardware.yaml checks)", elec_lines),
        "commits": _Section(commits_title, commit_lines),
        "closed": _Section(closed_title, closed_lines),
        "cards": _Section("ACTIVE SUGGESTION CARDS", card_lines),
    }
    # Fill order: what must be there first; display order is the dict order above.
    fill = ["header", "facts", "milestones", "next", "asked", "open", "electrical", "commits",
            "closed", "cards"]
    budget = budget_tokens * CHARS_PER_TOKEN
    used = 0
    kept: dict[str, list[str]] = {}
    omitted: dict[str, int] = {}
    for key in fill:
        sec = sections[key]
        if not sec.lines:
            continue
        cost = len(sec.title) + 2
        out: list[str] = []
        for k, line in enumerate(sec.lines):
            more = len(sec.lines) - k
            reserve = 40 if more > 1 else 0  # room for "(... N more not shown)"
            if not sec.required and used + cost + len(line) + 1 + reserve > budget:
                omitted[key] = more
                break
            out.append(line)
            cost += len(line) + 1
        if not out:
            continue
        if key in omitted:
            out.append(f"(... {omitted[key]} more not shown)")
            cost += 30
        kept[key] = out
        used += cost

    text = "\n\n".join(f"{sections[k].title}:\n" + "\n".join(kept[k])
                       for k in sections if k in kept)
    present = set(re.findall(r"\[([^\[\]\n]+)\]", text))
    refs = {r: s for r, s in refs.items() if r in present}
    return Context(
        text=text, refs=refs, issue_numbers=set(by_num),
        commit_shas=[c["sha"] for c in conn.execute("SELECT sha FROM commits")],
        body_issues=[n for n in body_for if f"#{n}" in refs], omitted=omitted,
        ranking=ranking, fact_evidence=fact_evidence, window_days=window,
        next_steps=steps, next_step_refs=step_refs, today=today, last_commit=last_commit,
    )


def _fill(sections: dict[str, _Section], keys: Sequence[str], budget: int, used: int,
          kept: dict[str, list[str]], omitted: dict[str, int]) -> int:
    """Keep each section's lines in order while they fit ``budget`` characters
    (required sections always fit); returns the characters used."""
    for key in keys:
        sec = sections[key]
        if not sec.lines:
            continue
        cost = len(sec.title) + 2
        out: list[str] = []
        for k, line in enumerate(sec.lines):
            more = len(sec.lines) - k
            reserve = 40 if more > 1 else 0
            if not sec.required and used + cost + len(line) + 1 + reserve > budget:
                omitted[key] = more
                break
            out.append(line)
            cost += len(line) + 1
        if not out:
            continue
        if key in omitted:
            out.append(f"(... {omitted[key]} more not shown)")
            cost += 30
        kept[key] = out
        used += cost
    return used


NEXT_STEPS_TITLE = ("SUGGESTED NEXT STEPS (computed by Jarvis from issues, milestones and "
                    "checks — not a schedule)")

# stable_prefix mode: the stable part gets the whole budget (once warm it costs
# nothing: it is cached), and the question part may add up to this many tokens.
QUESTION_PART_TOKENS = 1000


def _assemble_stable(budget_tokens: int, header: list[str], fact_lines: list[str],
                     ms_lines: list[str], open_title: str, open_lines: list[str],
                     elec_lines: list[str], views: dict[str, tuple[str, list[str]]],
                     windowed: dict[str, tuple[str, list[str]]], card_lines: list[str],
                     now_lines: list[str], asked_lines: list[str], relevant_lines: list[str],
                     refs: dict[str, Source], extra: dict[str, Any],
                     body_for: list[int], next_lines: list[str]) -> Context:
    """build_context(stable_prefix=True): the stable sections fill ``budget_tokens``
    on their own (so their trimming never depends on the question); the question
    part comes after, within QUESTION_PART_TOKENS more."""
    sections = {
        "header": _Section("PROJECT", header, required=True),
        "facts": _Section("FACTS (computed by Jarvis)", fact_lines or ["(none)"],
                          required=True),
        "milestones": _Section("MILESTONES", ms_lines or ["(none)"], required=True),
        "next": _Section(NEXT_STEPS_TITLE, next_lines, required=True),
        "open": _Section(open_title, open_lines or ["(none)"]),
        "electrical": _Section("ELECTRICAL (hardware.yaml checks)", elec_lines),
        "commits": _Section(*views["commits"]),
        "closed": _Section(*views["closed"]),
        "cards": _Section("ACTIVE SUGGESTION CARDS", card_lines),
        "now": _Section("NOW", now_lines, required=True),
        "asked": _Section("ISSUES ASKED ABOUT", asked_lines),
        "relevant": _Section("OPEN ISSUES RELEVANT TO THIS QUESTION (details)",
                             relevant_lines),
    }
    # A question about a time window gets its own commits / closed views in the
    # question part; the default views stay in the stable part.
    for key, (title, lines) in windowed.items():
        sections[f"window_{key}"] = _Section(title, lines)
    stable = ["header", "facts", "milestones", "next", "open", "electrical", "commits", "closed",
              "cards"]
    volatile = ["now", "asked", "relevant", *(f"window_{k}" for k in windowed)]
    budget = budget_tokens * CHARS_PER_TOKEN
    kept: dict[str, list[str]] = {}
    omitted: dict[str, int] = {}
    used = _fill(sections, stable, budget, 0, kept, omitted)
    _fill(sections, volatile, used + QUESTION_PART_TOKENS * CHARS_PER_TOKEN, used, kept,
          omitted)

    def join(keys: Sequence[str]) -> str:
        return "\n\n".join(f"{sections[k].title}:\n" + "\n".join(kept[k])
                            for k in keys if k in kept)

    stable_text, question_text = join(stable), join(volatile)
    text = stable_text + "\n\n" + question_text if question_text else stable_text
    present = set(re.findall(r"\[([^\[\]\n]+)\]", text))
    refs = {r: s for r, s in refs.items() if r in present}
    return Context(text=text, refs=refs, omitted=omitted,
                   body_issues=[n for n in body_for if f"#{n}" in refs],
                   stable_chars=len(stable_text), **extra)


# ---------------------------------------------------------------- streaming display


_THINK_RE = re.compile(r"<think>.*?(?:</think>|$)", re.S)


class ThinkFilter:
    """Drop ``<think>...</think>`` blocks from streamed text (thinking models leaking).

    Text that could still be the start of a "<think>" tag is held back briefly.
    """

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self._buf = ""
        self._inside = False

    def feed(self, chunk: str) -> str:
        self._buf += chunk
        out: list[str] = []
        while self._buf:
            if self._inside:
                k = self._buf.find(self.CLOSE)
                if k < 0:
                    # Keep only a possible partial closing tag.
                    keep = max((n for n in range(1, len(self.CLOSE))
                                if self._buf.endswith(self.CLOSE[:n])), default=0)
                    self._buf = self._buf[len(self._buf) - keep:] if keep else ""
                    break
                self._buf = self._buf[k + len(self.CLOSE):].lstrip("\n")
                self._inside = False
                continue
            k = self._buf.find(self.OPEN)
            if k >= 0:
                out.append(self._buf[:k])
                self._buf = self._buf[k + len(self.OPEN):]
                self._inside = True
                continue
            keep = max((n for n in range(1, len(self.OPEN))
                        if self._buf.endswith(self.OPEN[:n])), default=0)
            out.append(self._buf[: len(self._buf) - keep])
            self._buf = self._buf[len(self._buf) - keep:]
            break
        return "".join(out)

    def flush(self) -> str:
        rest = "" if self._inside else self._buf
        self._buf = ""
        return rest


def strip_think(text: str) -> str:
    """``text`` without ``<think>...</think>`` blocks (an unclosed one runs to the end)."""
    return _THINK_RE.sub("", text).lstrip("\n") if "<think>" in text else text


class SourcesLineFilter:
    """Pass streamed text through, withholding a final "SOURCES:" line.

    Only the start of each line is buffered, and only while it could still turn
    into "SOURCES:" (ignoring leading spaces and markdown * _ > `), so normal text
    streams with no visible delay. Everything from a SOURCES line on is suppressed.
    """

    MARKS = ("SOURCES:", "SOURCE:")
    _DECOR = re.compile(r"[*_>`]")

    def __init__(self) -> None:
        self._line = ""
        self._free = False      # the current line is known not to be SOURCES
        self.suppressed = False

    def feed(self, chunk: str) -> str:
        out: list[str] = []
        for ch in chunk:
            if self.suppressed:
                break
            if self._free:
                out.append(ch)
                if ch == "\n":
                    self._free = False
                continue
            self._line += ch
            if ch == "\n":
                out.append(self._line)
                self._line = ""
                continue
            key = self._DECOR.sub("", self._line).lstrip().upper()
            if not key:
                continue
            if any(key.startswith(m) for m in self.MARKS):
                self.suppressed = True
                self._line = ""
            elif not any(m.startswith(key) for m in self.MARKS):
                out.append(self._line)
                self._line = ""
                self._free = True
        return "".join(out)

    def flush(self) -> str:
        rest, self._line = ("" if self.suppressed else self._line), ""
        return rest


# ---------------------------------------------------------------- post-validation

_SOURCES_RE = re.compile(r"[*_`]*\bSOURCES?\b[*_`]*\s*:[*_`]*", re.I)


# The model sometimes copies the context's relevance tag into its answer.
_TAG_ECHO_RE = re.compile(r"[ \t]*\((?:keyword hit|matches question)[^()\n]*\)", re.I)


def split_sources(raw: str) -> tuple[str, list[str]]:
    """(answer without the SOURCES line, raw ref strings) from a model reply."""
    matches = list(_SOURCES_RE.finditer(raw))
    if not matches:
        return raw.strip(), []
    m = matches[-1]
    tail = raw[m.end():]
    if len(tail) > 600:  # a "sources" word mid-answer, not the closing line
        return raw.strip(), []
    body = raw[: m.start()].rstrip(" \t*_-—:\n")
    items = [x.strip(" \t*_`-[]().\"'") for x in re.split(r"[,;\n]+", tail)]
    return body.strip(), [x for x in items if x]


def resolve_ref(item: str, ctx: Context) -> str | None:
    """Map one SOURCES entry to a context ref, or None if it is not in the context."""
    s = item.strip()
    if s in ctx.refs:
        return s
    low = {r.lower(): r for r in ctx.refs}
    if s.lower() in low:
        return low[s.lower()]
    m = re.fullmatch(r"(?:issue[:\s]*)?#?(\d+)", s, re.I)
    if m and f"#{m.group(1)}" in ctx.refs:
        return f"#{m.group(1)}"
    m = re.fullmatch(r"(?:commit[:\s]*)?([0-9a-f]{7,40})", s, re.I)
    if m:
        sha = m.group(1).lower()
        for r in ctx.refs:
            if r.startswith("commit:") and (sha.startswith(r[7:]) or r[7:].startswith(sha)):
                return r
    m = re.fullmatch(r"milestone[:\s]+(.+)", s, re.I)
    if m and f"milestone:{m.group(1).strip()}".lower() in low:
        return low[f"milestone:{m.group(1).strip()}".lower()]
    for prefix in ("fact:", "check:", "card:"):
        if f"{prefix}{s}".lower() in low:
            return low[f"{prefix}{s}".lower()]
    return None


def _allowed(text: str) -> tuple[set[str], set[str]]:
    """(numbers, normalized month-day dates) appearing in ``text``."""
    dates = {_norm_date(m.group(1), m.group(2)) for m in DATE_RE.finditer(text)}
    for m in ISO_DATE_RE.finditer(text):
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        dates.add(_norm_date(_fmt_day(d), str(d.day)))
    nums = {_norm_num(n) for n in NUMBER_RE.findall(text)}
    nums |= {NUMBER_WORDS[w.lower()] for w in NUMBER_WORD_RE.findall(text)}
    return nums, dates


def find_unverified_numbers(answer: str, allowed_text: str) -> list[str]:
    """Numbers and dates in ``answer`` that appear nowhere in ``allowed_text``.

    Issue references (#N) and commit shas are checked separately and skipped here.
    """
    nums, dates = _allowed(allowed_text)
    out: list[str] = []
    rest = ISSUE_REF_RE.sub(" ", answer)
    for m in DATE_RE.finditer(rest):
        if _norm_date(m.group(1), m.group(2)) not in dates:
            out.append(m.group(0))
    rest = DATE_RE.sub(" ", rest)
    for m in ISO_DATE_RE.finditer(rest):
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            ok = _norm_date(_fmt_day(d), str(d.day)) in dates
        except ValueError:
            ok = False
        if not ok:
            out.append(m.group(0))
    rest = ISO_DATE_RE.sub(" ", rest)
    rest = SHA_RE.sub(" ", rest)
    for m in NUMBER_RE.finditer(rest):
        if _norm_num(m.group(0)) not in nums:
            out.append(m.group(0))
    for m in NUMBER_WORD_RE.finditer(rest):
        if NUMBER_WORDS[m.group(1).lower()] not in nums:
            out.append(m.group(0))
    return list(dict.fromkeys(out))


_MONTH_NUM = {m: k + 1 for k, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
_CLAIM_DATE = (r"(?:on\s+|since\s+|after\s+)?(?:(?:mon|tues|wednes|thurs|fri|satur|sun)day,?\s+)?"
               r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}"
               r"(?:st|nd|rd|th)?|today|yesterday)")
_NO_COMMITS_SINCE_RE = re.compile(
    r"\bno\s+(?:new\s+)?commits?\b[^.\n]{0,40}?\b(?:since|after)\s+" + _CLAIM_DATE, re.I)
_LAST_COMMIT_RE = re.compile(
    r"\b(?:last|latest|most recent|newest)\s+commit\b[^.\n]{0,40}?\b(?:was|is|on|from|"
    r"dated|made|pushed)\s+" + _CLAIM_DATE, re.I)


def _claim_day(text: str, today: date) -> date | None:
    t = text.lower().strip()
    if t == "today":
        return today
    if t == "yesterday":
        return today - timedelta(days=1)
    m = DATE_RE.match(t)
    if not m:
        return None
    try:
        d = date(today.year, _MONTH_NUM[m.group(1)[:3].lower()], int(m.group(2)))
    except ValueError:
        return None
    return d.replace(year=d.year - 1) if d > today + timedelta(days=1) else d


def commit_date_claim_errors(body: str, ctx: Context) -> list[str]:
    """Claims about commit dates that contradict the newest synced commit.

    "no commits since D" is wrong when the last commit is after D, and misleading
    when it is more than a day before D (the quiet stretch began earlier);
    "the last commit was on D" is wrong unless D is the last commit's local day.
    Returns the offending phrases (empty when nothing is checkable).
    """
    if not ctx.last_commit or ctx.today is None:
        return []
    last = ctx.last_commit[1]
    out: list[str] = []
    for m in _NO_COMMITS_SINCE_RE.finditer(body):
        d = _claim_day(m.group(1), ctx.today)
        if d is not None and (last > d or last < d - timedelta(days=1)):
            out.append(m.group(0).strip())
    for m in _LAST_COMMIT_RE.finditer(body):
        d = _claim_day(m.group(1), ctx.today)
        if d is not None and d != last:
            out.append(m.group(0).strip())
    return list(dict.fromkeys(out))


def _says_missing(text: str, n: int) -> bool:
    """True if the answer itself already says issue #n does not exist."""
    return bool(re.search(rf"\bno(?:t an?)?\s+issue\s+#{n}\b", text, re.I) or re.search(
        rf"#{n}\b[^.\n]*\b(?:does not|doesn't|isn't|is not|not)\b[^.\n]*"
        r"\b(?:exist|synced|repo|an issue)", text, re.I))


def validate_answer(raw: str, ctx: Context, question: str,
                    history: Sequence[tuple[str, str]] = ()) -> Answer:
    """Clean and check a model reply against the context (model/timing left empty)."""
    body, items = split_sources(raw)
    body = _TAG_ECHO_RE.sub("", body)
    sources: list[str] = []
    dropped: list[str] = []
    for it in items:
        r = resolve_ref(it, ctx)
        if r is None:
            dropped.append(it)
        elif r not in sources:
            sources.append(r)
    # Refs mentioned inline but missing from SOURCES are still sources.
    for n in mentioned_issues(body):
        if f"#{n}" in ctx.refs and f"#{n}" not in sources:
            sources.append(f"#{n}")
    for m in SHA_RE.finditer(body):
        r = resolve_ref(m.group(0), ctx)
        if r and r not in sources:
            sources.append(r)
    cited = list(sources) if body.strip() else []
    # A cited fact brings its evidence (issues, commits, milestones) along as links.
    for r in list(sources):
        for ev in ctx.fact_evidence.get(r, []):
            if ev in ctx.refs and ev not in sources:
                sources.append(ev)

    notes: list[str] = []
    bad_issues = [n for n in mentioned_issues(body)
                  if n not in ctx.issue_numbers and not _says_missing(body, n)]
    if bad_issues:
        listed = ", ".join(f"#{n}" for n in bad_issues)
        verb = "isn't an issue" if len(bad_issues) == 1 else "aren't issues"
        notes.append(f"Note: {listed} {verb} in this repo.")
    no_iso = ISO_DATE_RE.sub(" ", ISSUE_REF_RE.sub(" ", body))
    bad_shas = [m.group(0) for m in SHA_RE.finditer(no_iso)
                if not any(s.lower().startswith(m.group(0).lower()) for s in ctx.commit_shas)]
    if bad_shas:
        notes.append(f"Note: commit {', '.join(dict.fromkeys(bad_shas))} isn't among the "
                     "synced commits.")
    claims = commit_date_claim_errors(body, ctx)
    if claims and ctx.last_commit:
        sha, day = ctx.last_commit
        notes.append(f"Note: the last synced commit is {sha}, on {_fmt_day(day)}.")
    allowed_text = "\n".join([ctx.text, question, *(c for r, c in history if r == "user")])
    unverified = find_unverified_numbers(body, allowed_text)
    if unverified:
        notes.append(f"Unverified numbers: {', '.join(unverified)} (not in Jarvis's data).")
    return Answer(text=body, sources=[ctx.refs[r] for r in sources], unverified_numbers=unverified,
                  notes=notes, model=None, duration=0.0, raw=raw, dropped_refs=dropped,
                  context_tokens=ctx.tokens,
                  cited_sources=[ctx.refs[r] for r in cited[:CITED_SOURCES_MAX]],
                  cited_more_count=max(0, len(cited) - CITED_SOURCES_MAX),
                  invalid_refs=[f"#{n}" for n in bad_issues] + list(dict.fromkeys(bad_shas)),
                  claim_errors=claims)


# ---------------------------------------------------------------- history + persistence

_CHAT_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT CHECK (role IN ('user', 'assistant')),
    content TEXT,
    sources_json TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS chat_messages_session ON chat_messages (session_id, id);
"""


def _ensure_table(conn: sqlite3.Connection) -> None:
    """Same DDL as db.SCHEMA; a guard for connections opened by older code."""
    conn.executescript(_CHAT_SCHEMA)


def trim_history(history: Sequence[tuple[str, str]],
                 turns: int = HISTORY_TURNS) -> list[tuple[str, str]]:
    """The last ``turns`` question/answer pairs, starting with a user message."""
    msgs = [(r, c) for r, c in history if r in ("user", "assistant") and (c or "").strip()]
    msgs = msgs[-2 * turns:]
    while msgs and msgs[0][0] != "user":
        msgs.pop(0)
    return msgs


def save_message(conn: sqlite3.Connection, session_id: str, role: str, content: str,
                 meta: dict[str, Any] | None = None) -> int:
    _ensure_table(conn)
    cur = conn.execute(
        "INSERT INTO chat_messages (session_id, role, content, sources_json, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (session_id, role, content, json.dumps(meta) if meta is not None else None, now_iso()))
    conn.commit()
    return cur.lastrowid


def load_history(conn: sqlite3.Connection, session_id: str,
                 turns: int = HISTORY_TURNS) -> list[tuple[str, str]]:
    """(role, content) of a session's last ``turns`` exchanges, oldest first."""
    _ensure_table(conn)
    rows = conn.execute(
        "SELECT role, content FROM chat_messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
        (session_id, 4 * turns)).fetchall()
    return trim_history([(r["role"], r["content"]) for r in reversed(rows)], turns)


# ---------------------------------------------------------------- ask


Brevity = Literal["text", "voice"]


def build_messages(ctx: Context, question: str, history: Sequence[tuple[str, str]],
                   brevity: Brevity = "text") -> list[dict[str, str]]:
    system = SYSTEM_PROMPT + VOICE_RULES if brevity == "voice" else SYSTEM_PROMPT
    if ctx.stable_chars:
        # Stable data in the system message, before the history: the prompt prefix
        # stays byte-identical across questions and turns (Ollama reuses its cache).
        system += ("\nCONTEXT, part 1 (project data; part 2 for this question is in the "
                   f"user's message):\n{ctx.stable_text}\n\nEND OF CONTEXT PART 1.\n")
        user = (f"CONTEXT, part 2:\n{ctx.question_text}\n\nEND OF CONTEXT.\n\n"
                f"QUESTION: {question}")
    else:
        user = f"CONTEXT:\n{ctx.text}\n\nEND OF CONTEXT.\n\nQUESTION: {question}"
    msgs = [{"role": "system", "content": system}]
    msgs += [{"role": r, "content": c} for r, c in trim_history(history)]
    msgs.append({"role": "user", "content": user})
    return msgs


def ask(conn: sqlite3.Connection, cfg: Config, question: str,
        history: Sequence[tuple[str, str]], client: OllamaClient | None,
        on_token: Callable[[str], None] | None = None, now: datetime | None = None, *,
        session_id: str | None = None, use_embeddings: bool = False,
        brevity: Brevity = "text", budget_tokens: int = CONTEXT_TOKEN_BUDGET,
        stable_prefix: bool = False, think: bool | None = None,
        on_thinking: Callable[[str], None] | None = None,
        max_thinking_s: float | None = None,
        ctx_out: list[Context] | None = None) -> Answer:
    """Answer one question from the synced data. Never raises for model failures.

    ``history`` is the earlier (role, content) messages of this conversation (only
    the last 6 turns are sent). ``on_token`` gets display text as it streams, with
    the SOURCES line held back; replace it with ``answer.display_text`` when this
    returns. With ``session_id`` the question and answer are stored in
    chat_messages. ``client`` None -> a client for cfg.ollama_url / cfg.model.
    ``brevity="voice"`` adds VOICE_RULES (1-3 short spoken sentences, no lists or
    markdown); grounding and validation are unchanged. ``<think>`` blocks a thinking
    model may emit are removed from the stream and the answer. ``budget_tokens``
    caps the context size (smaller = faster first token, less detail).
    ``stable_prefix`` puts the question-independent context first, in the system
    message (see build_context), so a warm model reuses its prompt cache.
    ``think=True`` asks the model for its native reasoning (OllamaClient.chat_stream);
    the reasoning never reaches ``on_token`` or the answer, ``on_thinking`` gets its
    chunks and ``answer.thinking_s`` how long it took; ``max_thinking_s`` caps the
    reasoning (the answer then has ``error`` set to the budget message). ``ctx_out`` (a list) receives
    the Context used, for callers that check the answer further (jarvis.escalate).
    """
    t0 = time.monotonic()
    question = question.strip()
    history = trim_history(history)
    own = client is None
    if client is None:
        client = OllamaClient(cfg.ollama_url, cfg.model)
    model = getattr(client, "model", cfg.model)
    try:
        if session_id:
            save_message(conn, session_id, "user", question)
        ctx = build_context(conn, cfg, question, now, history=history,
                            embedder=client if use_embeddings else None,
                            budget_tokens=budget_tokens, stable_prefix=stable_prefix)
        if ctx_out is not None:
            ctx_out.append(ctx)
        messages = build_messages(ctx, question, history, brevity)
        stream_kw: dict[str, Any] = {}
        if think is not None:
            stream_kw["think"] = think
        if on_thinking is not None:
            stream_kw["on_thinking"] = on_thinking
        if max_thinking_s is not None:
            stream_kw["max_thinking_s"] = max_thinking_s
        think_filter = ThinkFilter()
        filt = SourcesLineFilter()
        chunks: list[str] = []
        first: float | None = None
        error: str | None = None
        try:
            for chunk in client.chat_stream(messages, **stream_kw):
                if first is None:
                    first = time.monotonic() - t0
                chunks.append(chunk)
                if on_token:
                    shown = filt.feed(think_filter.feed(chunk))
                    if shown:
                        on_token(shown)
            if on_token and (tail := filt.feed(think_filter.flush()) + filt.flush()):
                on_token(tail)
        except OllamaError as e:
            error = str(e)
        raw = strip_think("".join(chunks))
        thinking_s = getattr(client, "last_thinking_s", None) if think else None
        if error is not None and not raw.strip():
            msg = (UNREACHABLE.format(url=cfg.ollama_url) if "cannot reach" in error
                   else f"The local model failed: {error}")
            return Answer(text=msg, sources=[], unverified_numbers=[], notes=[], model=model,
                          duration=time.monotonic() - t0, first_token_s=first, error=error,
                          context_tokens=ctx.tokens, thinking_s=thinking_s)
        ans = validate_answer(raw, ctx, question, history)
        if error is not None:
            ans.notes.append(f"Note: the answer was cut off ({error}).")
            ans.error = error
        ans.model = model
        ans.thinking_s = thinking_s
        ans.first_token_s = first
        ans.duration = time.monotonic() - t0
        if session_id and error is None:
            save_message(conn, session_id, "assistant", ans.text, {
                "sources": [asdict(s) for s in ans.sources], "notes": ans.notes,
                "unverified_numbers": ans.unverified_numbers, "model": model,
                "duration": round(ans.duration, 2)})
        return ans
    finally:
        if own:
            client.close()


# ---------------------------------------------------------------- dev entry


def _main(argv: list[str] | None = None) -> int:
    """Dev only: ``python -m jarvis.chat "question"`` (uses $JARVIS_DB etc.)."""
    import argparse
    import sys

    from jarvis import db

    p = argparse.ArgumentParser(prog="python -m jarvis.chat")
    p.add_argument("question")
    p.add_argument("--session", help="store Q/A under this session id (default: not stored)")
    p.add_argument("--embed", action="store_true",
                   help="also rank issue bodies with qwen3-embedding (swaps models in Ollama)")
    p.add_argument("--show-context", action="store_true")
    a = p.parse_args(argv)
    cfg = Config.from_env()
    conn = db.connect(cfg.db_path)
    history = load_history(conn, a.session) if a.session else []
    if a.show_context:
        ctx = build_context(conn, cfg, a.question, history=history)
        print(ctx.text, f"\n--- ~{ctx.tokens} tokens, omitted={ctx.omitted}\n", file=sys.stderr)
    with OllamaClient(cfg.ollama_url, cfg.model) as client:
        ans = ask(conn, cfg, a.question, history, client,
                  on_token=lambda t: print(t, end="", flush=True),
                  session_id=a.session, use_embeddings=a.embed)
    print("\n")
    for n in ans.notes:
        print(n)
    for s in ans.sources:
        print(f"  [{s.ref}] {s.label}" + (f"  {s.url}" if s.url else ""))
    if ans.dropped_refs:
        print(f"  (dropped refs: {', '.join(ans.dropped_refs)})")
    ft = f"{ans.first_token_s:.1f}s" if ans.first_token_s is not None else "-"
    print(f"-- {ans.model}, first token {ft}, total {ans.duration:.1f}s, "
          f"context ~{ans.context_tokens} tokens")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
