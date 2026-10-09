"""Electrical findings -> cards.

Python (``jarvis.checks``) decides every status and number. This module turns
non-passing findings into cards and closes cards whose finding passes or is gone.

Fingerprint decision: ``source_ref`` is always "hardware.yaml". The fingerprint
is type + source_ref + check_id, so it is fixed before any model call and never
depends on the model's issue choice; a card cannot be duplicated because the
model linked a different issue this time. The linked issue, if any, goes into
``source_url`` and the why text instead.

The optional model pass (Gemma via Ollama) only (a) links at most one open issue
and (b) rewrites the title and why in plain words. Its output is validated like
the briefing: the issue must exist and be open, and the text may contain no
number that is not in the finding. On any failure the template text is kept.
Model wording is cached per finding content hash in ``electrical_findings``, so
an unchanged finding never re-queries the model.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from jarvis.briefing import check_numbers
from jarvis.cards import auto_resolve, fingerprint, upsert_card
from jarvis.checks import Finding, fmt
from jarvis.db import now_iso
from jarvis.facts import Fact
from jarvis.hardware import Hardware
from jarvis.ollama import OllamaClient, OllamaError

SOURCE_REF = "hardware.yaml"
CARD_TYPE = "electrical"
MAX_TITLE_WORDS = 10
MAX_WHY_SENTENCES = 2
MAX_WHY_CHARS = 400
# Bump when SYSTEM_PROMPT changes so cached wording is regenerated.
PROMPT_VERSION = "2"

# Model text must not soften a non-passing finding into a verdict.
SOFTENING_RE = re.compile(
    r"\b(safe|fine|ok|okay|no problem|not a problem|nothing to worry|no issue)\b", re.I)
ISSUE_REF_RE = re.compile(r"#\d+")

SYSTEM_PROMPT = """\
You help an engineer triage electrical check results for a hobby embedded project \
(an STM32 pan-tilt LiDAR tracker). A Python program computed each finding; its status \
and numbers are correct and final.

Two tasks:
(a) issue: choose at most one issue from the given OPEN issues that this finding is \
directly about (the same part, rail, power supply or wiring). Return its number, or 0 \
if none clearly fits. Do not link an issue only because it mentions hardware in general.
(b) Rewrite the title in at most 10 plain words, and the explanation ("why") in 1 or 2 \
short plain sentences for the engineer. Keep the most important number of the original \
title (with its unit) in your title.

Rules:
- Use only numbers that appear in the finding, copied exactly with their units. Never \
calculate, round or convert a number. Fewer numbers is fine.
- Do not write issue numbers or "#" in the title or why.
- Keep the meaning of the status. "unknown" means a value is missing from \
hardware.yaml so the margin cannot be confirmed. "warn" means close to a limit. "fail" \
means over a limit. Never call a finding safe, fine or ok.
- No exclamation marks, no filler.
"""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "issue": {"type": "integer"},
        "title": {"type": "string"},
        "why": {"type": "string"},
    },
    "required": ["issue", "title", "why"],
}


@dataclass
class Phrased:
    title: str
    why: str
    issue: int | None
    source: str  # "template" | "model" | "model (cached)"
    rejected: list[str]


# ---------------------------------------------------------------- selection


def carded_findings(findings: list[Finding]) -> list[Finding]:
    """Non-passing findings that become cards.

    A ``missing:<field>`` finding is skipped when another non-passing finding
    already names that field (e.g. the 5V budget card covers
    ``missing:rails.5V.max_current_a``); it still shows in ``jarvis checks``.
    """
    bad = [f for f in findings if f.status != "pass"]
    covered = {m for f in bad if f.check != "missing" for m in f.missing}
    return [f for f in bad if not (f.check == "missing" and f.missing
                                   and f.missing[0] in covered)]


# ---------------------------------------------------------------- template text


def _rail_line(hw: Hardware, f: Finding) -> str:
    rails = f.rails or sorted({hw.parts[p].rail for p in f.parts
                               if p in hw.parts and hw.parts[p].rail})
    lines = []
    for r in rails:
        if r not in hw.rails:
            continue
        rail = hw.rails[r]
        loads = []
        for p in hw.parts_on(r, include_planned=True):
            spec = []
            if p.v_min is not None or p.v_max is not None:
                spec.append(f"{fmt(p.v_min) if p.v_min is not None else '?'}-"
                            f"{fmt(p.v_max) if p.v_max is not None else '?'} V")
            if p.current_peak_a is not None:
                spec.append(f"{fmt(p.current_peak_a)} A peak")
            plan = ", planned" if p.planned else ""
            loads.append(f"{p.count} x {p.name} ({', '.join(spec) or 'no limits listed'}{plan})")
        lines.append(f"Parts on the {r} rail ({fmt(rail.nominal_v)} V nominal): "
                     f"{'; '.join(loads) or 'none listed'}.")
    if not lines and f.parts:
        lines.append(f"Parts involved: {', '.join(f.parts)}.")
    return " ".join(lines)


QUESTIONS = {
    "rail_budget": "Is this margin safe? What current rating should the {rail} supply have, "
                   "and what would you change on the shield?",
    "planned_parts": "Is this margin safe once the planned parts are added? What would you "
                     "change on the shield?",
    "voltage_window": "Is this margin safe for {part}? What would you change on the shield, "
                      "and what should I measure to confirm it?",
    "common_ground": "Is this wiring safe as described? What would you change on the shield, "
                     "and how do I confirm the grounds with a multimeter?",
    "logic_level": "Is this logic-level margin safe? What would you change on the shield?",
    "missing": "How do I find or measure this value, and is the design safe while it is "
               "unknown? What would you change on the shield?",
}


def claude_prompt(hw: Hardware, f: Finding, issue: sqlite3.Row | dict | None = None) -> str:
    """A prompt that works pasted into Claude with zero other context."""
    project = hw.project_description or hw.project_name or "an embedded project"
    rail = f.rails[0] if f.rails else (f.numbers.get("rail") or "")
    q = QUESTIONS.get(f.check, QUESTIONS["missing"]).format(
        rail=rail, part=f.parts[0] if f.parts else "this part")
    nums = "\n".join(f"- {k}: {_num_str(v)}" for k, v in f.numbers.items())
    excerpt = hw.excerpt(rails=f.rails, parts=f.parts, signals=f.signals)
    out = [
        f"I'm working on {project}",
        _rail_line(hw, f),
        "",
        "My project checker flagged this from my hardware.yaml. A Python script computed "
        "every number below from the file; nothing was estimated by a language model.",
        "",
        f"Check: {f.check_id} (status: {f.status.upper()})",
        f"Finding: {f.title}",
        f.why,
        "",
        "Numbers:",
        nums or "- (none)",
        "",
        "hardware.yaml excerpt (verify: true marks typical datasheet or seller figures, "
        "not measurements; null means unknown):",
        "```yaml",
        excerpt,
        "```",
    ]
    if issue is not None:
        out += ["", f"Related open issue: #{issue['number']} \"{issue['title']}\""
                    + (f" ({issue['html_url']})" if issue["html_url"] else "")]
    out += ["", f"Question: {q}"]
    return "\n".join(out) + "\n"


def _num_str(v: Any) -> str:
    if isinstance(v, float):
        return fmt(v)
    if v is None:
        return "null (unknown)"
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return str(v)


# ---------------------------------------------------------------- model pass


def _open_issues(conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    rows = conn.execute(
        "SELECT number, title, labels, html_url FROM issues WHERE state = 'open' ORDER BY number"
    ).fetchall()
    return {r["number"]: r for r in rows}


def user_prompt(f: Finding, issues: dict[int, sqlite3.Row]) -> str:
    finding = {"check_id": f.check_id, "status": f.status, "title": f.title, "why": f.why,
               "numbers": f.numbers, "parts": f.parts}
    listed = [{"number": n, "title": r["title"], "labels": json.loads(r["labels"] or "[]")}
              for n, r in issues.items()]
    return (f"Finding (JSON):\n{json.dumps(finding, ensure_ascii=False, default=str)}\n\n"
            f"Open issues (JSON):\n{json.dumps(listed, ensure_ascii=False)}\n\n"
            "Return issue, title and why.")


def _text_problem(text: str, f: Finding, max_words: int | None = None) -> str | None:
    text = text.strip()
    if not text:
        return "empty"
    if "\n" in text:
        return "multiple lines"
    if max_words and len(text.split()) > max_words:
        return f"longer than {max_words} words"
    if "!" in text:
        return "exclamation mark"
    if ISSUE_REF_RE.search(text):
        return "mentions an issue number"
    if f.status != "pass" and SOFTENING_RE.search(text):
        return "softens the status"
    # Numbers must all come from the finding (reuses the briefing's checker).
    ref = Fact(f.check_id, "electrical", 0, f"{f.title} {f.why}", f.numbers)
    return check_numbers(text, [ref])


def validate_rewrite(raw: Any, f: Finding, open_nums: set[int]
                     ) -> tuple[str | None, str | None, int | None, list[str]]:
    """(title or None, why or None, issue or None, reasons for anything rejected)."""
    reasons: list[str] = []
    if not isinstance(raw, dict):
        return None, None, None, ["reply is not an object"]
    issue = raw.get("issue")
    if isinstance(issue, str) and issue.strip().lstrip("#").isdigit():
        issue = int(issue.strip().lstrip("#"))
    if isinstance(issue, bool) or not isinstance(issue, int) or issue <= 0:
        issue = None
    elif issue not in open_nums:
        reasons.append(f"issue #{issue} is not an open issue")
        issue = None
    title = str(raw.get("title") or "").strip()
    why = " ".join(str(raw.get("why") or "").split())
    p = _text_problem(title, f, MAX_TITLE_WORDS)
    if p:
        reasons.append(f"title: {p}")
        title = None
    p = _text_problem(why, f)
    if not p:
        sentences = [s for s in re.split(r"(?<=[.?])\s+", why) if s.strip()]
        if len(sentences) > MAX_WHY_SENTENCES:
            p = f"more than {MAX_WHY_SENTENCES} sentences"
        elif len(why) > MAX_WHY_CHARS:
            p = f"longer than {MAX_WHY_CHARS} characters"
    if p:
        reasons.append(f"why: {p}")
        why = None
    return title, why, issue, reasons


def _model_key(f: Finding) -> str:
    return f"{f.content_hash()}:v{PROMPT_VERSION}"


def _cached(conn: sqlite3.Connection, f: Finding) -> sqlite3.Row | None:
    row = conn.execute("SELECT * FROM electrical_findings WHERE check_id = ?",
                       (f.check_id,)).fetchone()
    if row is not None and row["model_hash"] == _model_key(f) and row["model_result"]:
        return row
    return None


def phrase(conn: sqlite3.Connection, f: Finding, ollama: OllamaClient | None,
           issues: dict[int, sqlite3.Row]) -> Phrased:
    """Card title/why/issue: cached model text, fresh model text, or the template."""
    template = Phrased(f.title, f.why, None, "template", [])
    row = _cached(conn, f)
    if row is not None:
        issue = row["model_issue"] if row["model_issue"] in issues else None
        src = "template (model rejected, cached)" if row["model_result"] == "rejected" \
            else "model (cached)"
        return Phrased(row["model_title"] or f.title, row["model_why"] or f.why, issue, src, [])
    if ollama is None:
        return template
    try:
        raw = ollama.chat_json(SYSTEM_PROMPT, user_prompt(f, issues), SCHEMA)
    except OllamaError as e:
        template.rejected = [f"model unavailable: {e}"]
        return template  # not cached: retry next run
    title, why, issue, reasons = validate_rewrite(raw, f, set(issues))
    result = "model" if (title or why or issue) else "rejected"
    conn.execute(
        """UPDATE electrical_findings SET model_hash = ?, model_result = ?, model_title = ?,
               model_why = ?, model_issue = ?, model = ? WHERE check_id = ?""",
        (_model_key(f), result, title, why, issue, getattr(ollama, "model", None),
         f.check_id))
    conn.commit()
    return Phrased(title or f.title, why or f.why, issue,
                   "model" if result == "model" else "template", reasons)


# ---------------------------------------------------------------- storing


def _store_findings(conn: sqlite3.Connection, findings: list[Finding]) -> None:
    now = now_iso()
    ids = [f.check_id for f in findings]
    for f in findings:
        conn.execute(
            """INSERT INTO electrical_findings (check_id, finding_hash, status, title, why,
                   numbers_json, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(check_id) DO UPDATE SET finding_hash=excluded.finding_hash,
                   status=excluded.status, title=excluded.title, why=excluded.why,
                   numbers_json=excluded.numbers_json, updated_at=excluded.updated_at""",
            (f.check_id, f.content_hash(), f.status, f.title, f.why,
             json.dumps(f.numbers, default=str), now))
    marks = ",".join("?" * len(ids))
    conn.execute(f"DELETE FROM electrical_findings WHERE check_id NOT IN ({marks})"
                 if ids else "DELETE FROM electrical_findings", ids)
    conn.commit()


@dataclass
class SyncResult:
    new: int
    resolved: int
    notes: list[str]
    cards: list[tuple[Finding, Phrased, int]]  # (finding, text, card id)


def sync_cards(conn: sqlite3.Connection, hw: Hardware, findings: list[Finding],
               ollama: OllamaClient | None = None) -> SyncResult:
    """Create/refresh cards for non-passing findings; auto-resolve the rest."""
    _store_findings(conn, findings)
    issues = _open_issues(conn)
    notes: list[str] = []
    made: list[tuple[Finding, Phrased, int]] = []
    new = 0
    keep: set[str] = set()
    for f in carded_findings(findings):
        ph = phrase(conn, f, ollama, issues)
        for r in ph.rejected:
            notes.append(f"{f.check_id}: {r}")
        issue = issues.get(ph.issue) if ph.issue else None
        why = ph.why
        if issue is not None:
            why = f"{why} Related: #{issue['number']} {issue['title']}."
        fp = fingerprint(CARD_TYPE, SOURCE_REF, f.check_id)
        before = conn.execute("SELECT status FROM cards WHERE fingerprint = ?", (fp,)).fetchone()
        card_id, created = upsert_card(
            conn, type_=CARD_TYPE, title=ph.title, why=why, source_ref=SOURCE_REF,
            source_url=issue["html_url"] if issue is not None else None,
            claude_prompt=claude_prompt(hw, f, issue), check_id=f.check_id)
        if created or (before is not None and before["status"] == "auto_resolved"):
            new += 1
        keep.add(fp)
        made.append((f, ph, card_id))
    # Close active electrical cards whose finding now passes or no longer exists.
    stale = [r["fingerprint"] for r in conn.execute(
        "SELECT fingerprint FROM cards WHERE type = ? AND source_ref = ? AND status = 'active'",
        (CARD_TYPE, SOURCE_REF)) if r["fingerprint"] not in keep]
    resolved = auto_resolve(conn, stale)
    return SyncResult(new, resolved, notes, made)


def active_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Active electrical cards counted by their finding's status."""
    rows = conn.execute(
        """SELECT COALESCE(e.status, 'unknown') AS status, COUNT(*) AS n FROM cards c
           LEFT JOIN electrical_findings e ON e.check_id = c.check_id
           WHERE c.type = 'electrical' AND c.status = 'active' GROUP BY 1""").fetchall()
    return {r["status"]: r["n"] for r in rows}
