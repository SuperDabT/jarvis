"""Which open issues get researched, and with which arXiv queries.

Triage is explainable, in this order (first match wins):

1. ``skip_issues`` / ``include_issues`` in research.yaml.
2. Issues mapped to a library in ``libraries``: covered by that library's releases.
3. Chore patterns in the title (docs, demo, review, UI, drivers, test plumbing,
   planning...): a fixed, readable list below.
4. The model (if reachable) may mark an issue "not research-worthy" with a reason;
   its answer is cached per issue text.

Queries: the issue title, cleaned of filler and code identifiers, ANDed with a
domain group ("LiDAR" | "laser scanner" | "rangefinder") unless the title already
names the sensor; plus up to two model-proposed queries (validated: 2-8 plain
words). A ``queries`` override in research.yaml replaces both.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from jarvis.db import now_iso
from jarvis.ollama import OllamaError
from jarvis.research.settings import ResearchSettings
from jarvis.research.sources import Query

SOMEDAY_LABEL = "someday"
BODY_CHARS = 500
MAX_MODEL_QUERIES = 2
MAX_TITLE_QUERY_WORDS = 5
PROMPT_VERSION = "1"

BASE_CATEGORIES = ("cs.RO", "eess.SP", "eess.SY", "cs.CV")
ML_WORDS_RE = re.compile(r"\b(classifier|classification|tinyml|learning|neural|ml|cnn)\b", re.I)

CHORE_RULES: tuple[tuple[str, str], ...] = (
    (r"\breadme\b|\bdemo video\b|\bdocument(ation)?\b", "documentation or demo"),
    (r"\bcode review\b|\bclean-?ups?\b|\brefactor\w*", "code housekeeping"),
    (r"\breturns? a status\b|\berror codes?\b|\bwrong comment\b", "code housekeeping"),
    (r"\bunit tests?\b", "test plumbing"),
    (r"\bplan\b|\bchecklist\b|\broadmap\b", "planning issue"),
    (r"\bplot\b|\bdisplay\b|\bgui\b|\bdashboard\b", "user interface"),
    (r"\bdriver\b|\bpcb\b|\bshield\b|\bwiring\b", "hardware bring-up"),
    (r"\bcommand interface\b", "I/O plumbing"),
)
_CHORE_RE = [(re.compile(p, re.I), why) for p, why in CHORE_RULES]

# Words that say what to do with the code, not what the problem is.
FILLER = frozenset(
    """a an the of on in for and or but with without by at as from into to up down
    is are was were be it its this that each when while than then after before
    create add own replace library measure watch show make use implement support
    migrate investigate move keep set get new caused run runs closes""".split())
_IDENT_RE = re.compile(r"`[^`]*`|\b\w+_\w+\b|\b[\w-]+\.(?:c|h|py|cpp|yaml)\b")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9+\-]*")
_QUERY_OK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 +\-/]*$")


@dataclass
class IssuePlan:
    number: int
    title: str
    body: str
    labels: tuple[str, ...]
    url: str | None
    skip: str | None = None  # reason, when skipped
    queries: list[Query] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def someday(self) -> bool:
        return SOMEDAY_LABEL in self.labels

    def text(self) -> str:
        """Issue text used for embeddings and the judge."""
        return f"{self.title}\n{excerpt(self.body)}".strip()


def excerpt(body: str | None, n: int = BODY_CHARS) -> str:
    body = " ".join((body or "").split())
    if len(body) <= n:
        return body
    cut = body[:n].rsplit(" ", 1)[0]
    return cut + " ..."


def open_issues(conn: sqlite3.Connection) -> list[IssuePlan]:
    rows = conn.execute(
        "SELECT number, title, body, labels, html_url FROM issues WHERE state = 'open' "
        "ORDER BY number").fetchall()
    out = []
    for r in rows:
        try:
            labels = tuple(json.loads(r["labels"] or "[]"))
        except json.JSONDecodeError:
            labels = ()
        out.append(IssuePlan(r["number"], r["title"] or "", r["body"] or "", labels,
                             r["html_url"]))
    return out


def chore_reason(title: str) -> str | None:
    for rx, why in _CHORE_RE:
        if rx.search(title):
            return why
    return None


def rule_skip(issue: IssuePlan, s: ResearchSettings) -> str | None:
    """Deterministic skip reason, or None if the issue may be researched."""
    if issue.number in s.skip_issues:
        return "skipped in research.yaml"
    if issue.number in s.include_issues:
        return None
    libs = [repo for repo, n in s.libraries.items() if n == issue.number]
    if libs:
        return f"covered by {', '.join(libs)} releases"
    why = chore_reason(issue.title)
    return f"not research: {why}" if why else None


def categories_for(text: str) -> tuple[str, ...]:
    return BASE_CATEGORIES + (("cs.LG",) if ML_WORDS_RE.search(text) else ())


def title_query(issue: IssuePlan, s: ResearchSettings) -> Query | None:
    """The title's meaningful words (max 5), anchored to the sensor domain."""
    text = _IDENT_RE.sub(" ", issue.title)
    words: list[str] = []
    for w in _WORD_RE.findall(text):
        if w.lower() in FILLER or len(w) < 2 or w.lower() in (x.lower() for x in words):
            continue
        words.append(w)
    words = words[:MAX_TITLE_QUERY_WORDS]
    if not words:
        return None
    title_l = issue.title.lower()
    has_domain = any(t.lower() in title_l for t in s.domain_terms)
    return Query(" ".join(words), () if has_domain else tuple(s.domain_terms),
                 categories_for(issue.title), "title")


def validate_query(q: Any) -> str | None:
    """A cleaned model query, or None if it is not 2-8 plain words."""
    if not isinstance(q, str):
        return None
    q = " ".join(q.replace('"', " ").replace("'", " ").split()).strip(" .,;:")
    if not q or not _QUERY_OK_RE.match(q) or "#" in q:
        return None
    if not 2 <= len(q.split()) <= 8:
        return None
    return q


# ---------------------------------------------------------------- model triage

TRIAGE_SYSTEM = """\
You help an engineer find published research (arXiv papers) for open issues in a hobby \
embedded project. Project: {project}

For the given issue decide:
- research_worthy: true only if published research on algorithms, estimation, \
filtering, control, detection, tracking, scanning strategy or tiny on-device ML could \
plausibly offer a better approach for this issue. False for chores: documentation, \
demos, refactoring, UI or plotting, device drivers, wiring, test plumbing, planning, \
or a simple procedure/bookkeeping change the engineer already knows how to do.
- reason: one short sentence.
- queries: if research_worthy, 1 or 2 arXiv search queries of 3 to 6 plain words each. \
Use the technical terms a relevant paper's abstract would contain, and include the \
sensing context when it matters (for example "2D LiDAR", "laser rangefinder", \
"pan-tilt"). No quotes, no issue numbers, no project or product names. Empty list \
if not research_worthy.
"""

TRIAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "research_worthy": {"type": "boolean"},
        "reason": {"type": "string"},
        "queries": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["research_worthy", "reason", "queries"],
}


@dataclass
class Triage:
    worthy: bool
    reason: str
    queries: list[str]
    source: str  # "model" | "model (cached)" | "none"


def _triage_key(issue: IssuePlan, model: str, s: ResearchSettings) -> str:
    blob = f"v{PROMPT_VERSION}\n{model}\n{s.project}\n{issue.title}\n{excerpt(issue.body)}"
    return hashlib.sha256(blob.encode()).hexdigest()


def model_triage(conn: sqlite3.Connection, issue: IssuePlan, ollama: Any | None,
                 s: ResearchSettings, write: bool = True) -> Triage:
    """Cached model triage. Model errors give source "none" and are not cached."""
    if ollama is None:
        return Triage(True, "", [], "none")
    key = _triage_key(issue, getattr(ollama, "model", ""), s)
    row = conn.execute("SELECT * FROM research_queries WHERE issue_number = ?",
                       (issue.number,)).fetchone()
    if row is not None and row["key"] == key:
        return Triage(bool(row["worthy"]), row["reason"] or "",
                      json.loads(row["queries_json"] or "[]"), "model (cached)")
    user = (f"Issue #{issue.number}: {issue.title}\n"
            f"Labels: {', '.join(issue.labels) or 'none'}\n"
            f"Body: {excerpt(issue.body) or '(empty)'}")
    try:
        raw = ollama.chat_json(TRIAGE_SYSTEM.format(project=s.project), user, TRIAGE_SCHEMA)
    except OllamaError as e:
        return Triage(True, f"model unavailable: {e}", [], "none")
    worthy = raw.get("research_worthy")
    worthy = worthy if isinstance(worthy, bool) else True
    reason = " ".join(str(raw.get("reason") or "").split())[:200]
    qs: list[str] = []
    proposed = raw.get("queries")
    for q in proposed if isinstance(proposed, list) else []:
        v = validate_query(q)
        if v and v.lower() not in (x.lower() for x in qs):
            qs.append(v)
    qs = qs[:MAX_MODEL_QUERIES]
    if write:
        conn.execute(
            """INSERT INTO research_queries (issue_number, key, worthy, reason, queries_json,
                   model, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(issue_number) DO UPDATE SET key=excluded.key,
                   worthy=excluded.worthy, reason=excluded.reason,
                   queries_json=excluded.queries_json, model=excluded.model,
                   created_at=excluded.created_at""",
            (issue.number, key, int(worthy), reason, json.dumps(qs),
             getattr(ollama, "model", None), now_iso()))
        conn.commit()
    return Triage(worthy, reason, qs, "model")


def plan_issues(conn: sqlite3.Connection, s: ResearchSettings, ollama: Any | None,
                only: int | None = None, write: bool = True) -> list[IssuePlan]:
    """Open issues with a skip reason or their queries. Someday issues come last."""
    plans = [p for p in open_issues(conn) if only is None or p.number == only]
    for p in plans:
        p.skip = rule_skip(p, s)
        if p.skip:
            continue
        if p.number in s.queries:
            p.queries = [Query(q, (), categories_for(p.title + " " + q), "config")
                         for q in s.queries[p.number]]
            continue
        t = model_triage(conn, p, ollama, s, write=write)
        if t.source == "none" and t.reason:
            p.notes.append(t.reason)
        if not t.worthy and p.number not in s.include_issues:
            p.skip = f"not research (model): {t.reason or 'no reason given'}"
            continue
        tq = title_query(p, s)
        if tq is not None:
            p.queries.append(tq)
        for q in t.queries:
            p.queries.append(Query(q, (), categories_for(p.title + " " + q), "model"))
        if not p.queries:
            p.skip = "no usable query"
    plans.sort(key=lambda p: (p.someday, p.number))
    return plans
