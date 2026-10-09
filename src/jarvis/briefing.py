"""The briefing: a few model-phrased sentences over deterministic facts.

Python computes the facts (``jarvis.facts``). The model only phrases them, and
objectivity is enforced here in code, not trusted to the model: every sentence
must cite known fact ids, may only use numbers, dates, issue numbers and commit
shas that appear in the facts it cites, must hedge when it cites an uncertain
fact, and must avoid a list of loaded words. Anything that fails is dropped;
if too little survives, a plain template version of the facts is used instead.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Literal

from jarvis import db
from jarvis.facts import Fact, compute_facts, facts_hash
from jarvis.ollama import OllamaClient, OllamaError

MIN_SENTENCES = 2
MAX_SENTENCES = 6
MAX_HEADLINE_WORDS = 14  # prompt asks for ~12; allow a little slack
TEMPLATE_MAX_PRIORITY = 3

# Words or patterns that make a sentence an opinion, a judgement or advice.
# The briefing states facts; advice comes later via cards.
BANNED_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"!", "exclamation mark"),
    (r"\bgreat\b", "praise: great"),
    (r"\bawesome\b", "praise: awesome"),
    (r"\bexcellent\b", "praise: excellent"),
    (r"\bimpressive\b", "praise: impressive"),
    (r"\bunfortunately\b", "judgement: unfortunately"),
    (r"\bsadly\b", "judgement: sadly"),
    (r"\bbehind schedule\b", "judgement: behind schedule"),
    (r"\bfalling behind\b", "judgement: falling behind"),
    (r"\bon track\b", "judgement: on track"),
    (r"\burgent(ly)?\b", "urgency: urgent"),
    (r"\bcritical\b", "urgency: critical"),
    (r"\basap\b", "urgency: asap"),
    (r"\bmust\b", "advice: must"),
    (r"\bshould\b", "advice: should"),
    (r"\b(tomorrow|yesterday|next week|last week|this weekend)\b", "relative date not in facts"),
)
_BANNED_RE = [(re.compile(p, re.I), why) for p, why in BANNED_PATTERNS]

HEDGE_RE = re.compile(
    r"\b(may|might|looks like|look like|appears?|possibly|likely|could|seems?)\b", re.I
)

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
# "Oct 18", "October 18th", "Sept 5". "May" only counts as a month before a day number.
DATE_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b",
    re.I,
)
ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
# Commit shas: 7-40 hex chars with at least one digit and one letter.
SHA_RE = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b", re.I)
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
NUMBER_WORDS = {
    "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
    "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12",
    "thirteen": "13", "fourteen": "14", "fifteen": "15", "twenty": "20",
}
NUMBER_WORD_RE = re.compile(r"\b(" + "|".join(NUMBER_WORDS) + r")\b", re.I)

SYSTEM_PROMPT = """\
You write a short status briefing for the engineer who owns this project. \
You receive a list of facts computed by a program. Your only job is to phrase them \
in plain, natural English.

Rules:
- Use only the given facts. Do not add numbers, dates, names, causes, advice or claims \
that are not in the facts. Do not combine facts into a new claim they do not make.
- Each sentence and the headline lists the ids of the facts it uses in its fact_ids \
field. Only use ids from the list. The text itself must never contain a fact id, \
not even in brackets: the reader never sees ids.
- Copy numbers, dates (like "Oct 18"), issue numbers (like #28) and commit ids exactly \
as they appear in the facts you cite. Write numbers as digits. Do not do arithmetic.
- Facts with "certain": false are guesses. A sentence that uses one must contain the \
word "may" (or "might", "looks like"), for example "#28 may already be done". Never \
state a guess as done, and never turn it into "if ... would" without "may".
- Neutral tone: no praise, no blame, no urgency, no advice, no exclamation marks. \
Do not use the words should, must, great, unfortunately, behind or urgent.
- Short sentences, written for a person. Refer to issues as "#N" with a few words \
of their title.
- Group related facts into one sentence, for example a deadline together with the \
issues that may already be done for that milestone and the progress they would give. \
A progress-if-done fact names exactly which issues it counts; put only those issues \
with it. Other maybe-done issues get their own sentence.
- Lead with what matters most (lower priority number first). Leave out low-priority \
facts if there is no room.
- Write 3 to 6 sentences, plus a headline of at most 12 words that also cites fact ids.

Example. Facts:
[{"id":"deadline:4","priority":1,"certain":true,"text":"Milestone Beta is due Mar 3, \
in 5 days, with 2 of 6 issues closed."},
 {"id":"maybe:12","priority":2,"certain":false,"text":"#12 \\"Add login page\\" may already \
be done: commit a1b2c3d (\\"Login page\\") describes similar work."},
 {"id":"adjusted:4","priority":2,"certain":false,"text":"If that one is done (#12), \
milestone Beta is at 3 of 6 closed instead of 2."}]
Good output:
{"headline":{"text":"Beta is due Mar 3, 2 of 6 issues closed",\
"fact_ids":["deadline:4"]},
 "sentences":[{"text":"Beta is due Mar 3, in 5 days, and 2 of its 6 issues are closed.",\
"fact_ids":["deadline:4"]},
 {"text":"#12, the login page, may already be done, since commit a1b2c3d looks like the \
same work; that may put Beta at 3 of 6.","fact_ids":["maybe:12","adjusted:4"]}]}
"""

BRIEFING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "fact_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["text", "fact_ids"],
        },
        "sentences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "fact_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text", "fact_ids"],
            },
        },
    },
    "required": ["headline", "sentences"],
}


@dataclass
class Sentence:
    text: str
    fact_ids: list[str]
    uncertain: bool = False


@dataclass
class Briefing:
    headline: str
    sentences: list[Sentence]
    source: Literal["model", "template"]
    facts_hash: str
    created_at: str  # ISO 8601, UTC
    model: str | None = None
    fact_count: int = 0
    # Not persisted: what validation removed this run, and why.
    dropped: list[tuple[str, str]] = field(default_factory=list)
    from_cache: bool = False
    fallback_reason: str | None = None  # why a template was used, if it was


# ---------------------------------------------------------------- validation


def _fmt_day(d: date) -> str:
    return f"{d.strftime('%b')} {d.day}"


def _norm_date(month: str, day: str) -> str:
    return f"{month[:3].lower()} {int(day)}"


def _flatten(value: Any) -> list[str]:
    """Every scalar inside ``value`` (and dict keys), stringified."""
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in [str(k), *_flatten(v)]]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _flatten(v)]
    return [] if value is None else [str(value)]


@dataclass
class _Allowed:
    numbers: set[str]
    dates: set[str]
    shas: set[str]


def _allowed_tokens(facts: list[Fact]) -> _Allowed:
    blob = " ".join(
        s for f in facts for s in [f.text, *_flatten(f.data), *f.evidence]
    )
    dates = {_norm_date(m.group(1), m.group(2)) for m in DATE_RE.finditer(blob)}
    for m in ISO_DATE_RE.finditer(blob):
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        dates.add(_norm_date(_fmt_day(d), str(d.day)))
    shas = {s.lower() for s in SHA_RE.findall(blob)}
    numbers = {_norm_num(n) for n in NUMBER_RE.findall(blob)}
    return _Allowed(numbers=numbers, dates=dates, shas=shas)


def _norm_num(n: str) -> str:
    return n.rstrip("0").rstrip(".") if "." in n else str(int(n))


def _sha_ok(sha: str, allowed: set[str]) -> bool:
    sha = sha.lower()
    return any(a.startswith(sha) or sha.startswith(a) for a in allowed)


def check_numbers(text: str, cited: list[Fact]) -> str | None:
    """Reason string if ``text`` uses a number/date/sha not in the cited facts."""
    allowed = _allowed_tokens(cited)
    rest = text
    for m in DATE_RE.finditer(text):
        d = _norm_date(m.group(1), m.group(2))
        if d not in allowed.dates:
            return f"date not in cited facts: {m.group(0)}"
    rest = DATE_RE.sub(" ", rest)
    for m in SHA_RE.finditer(rest):
        if not _sha_ok(m.group(0), allowed.shas):
            return f"commit id not in cited facts: {m.group(0)}"
    rest = SHA_RE.sub(" ", rest)
    for m in NUMBER_RE.finditer(rest):
        if _norm_num(m.group(0)) not in allowed.numbers:
            return f"number not in cited facts: {m.group(0)}"
    for m in NUMBER_WORD_RE.finditer(rest):
        if NUMBER_WORDS[m.group(1).lower()] not in allowed.numbers:
            return f"number not in cited facts: {m.group(0)}"
    return None


_TRAILING_PAREN_RE = re.compile(
    r"\s*(?:\(([^()]*)\)|\{([^{}]*)\}|\[([^\[\]]*)\])\s*([.]?)\s*$"
)


def strip_cited_ids(text: str, by_id: dict[str, Fact]) -> str:
    """Remove a trailing "(fact_id, ...)", "{...}" or "[...]" the model copies into the text.

    Only a parenthetical made entirely of known fact ids is removed; anything else
    is left for the validators to judge.
    """
    m = _TRAILING_PAREN_RE.search(text)
    if m:
        inner = next(g for g in m.groups()[:3] if g is not None)
        parts = [p.strip() for p in inner.split(",")]
        if parts and all(p in by_id for p in parts):
            text = text[: m.start()].rstrip()
            if m.group(4) and text and text[-1] not in ".?":
                text += m.group(4)
    return text


def check_item(
    text: str, fact_ids: Any, by_id: dict[str, Fact]
) -> tuple[Sentence | None, str | None]:
    """Validate one sentence or headline. Returns (sentence, None) or (None, reason)."""
    text = strip_cited_ids((text or "").strip(), by_id)
    if not text:
        return None, "empty text"
    if not isinstance(fact_ids, list) or not fact_ids:
        return None, "cites no facts"
    ids = [str(i) for i in fact_ids]
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        return None, f"unknown fact id: {', '.join(unknown)}"
    ids = list(dict.fromkeys(ids))
    cited = [by_id[i] for i in ids]
    for rx, why in _BANNED_RE:
        if rx.search(text):
            return None, f"banned wording ({why})"
    reason = check_numbers(text, cited)
    if reason:
        return None, reason
    uncertain = any(not f.certain for f in cited)
    if uncertain and not HEDGE_RE.search(text):
        return None, "cites an uncertain fact without hedging"
    return Sentence(text=text, fact_ids=ids, uncertain=uncertain), None


def validate(
    raw: dict[str, Any], facts: list[Fact]
) -> tuple[Sentence | None, list[Sentence], list[tuple[str, str]]]:
    """Apply every objectivity rule to a model reply.

    Returns (headline or None if it failed, kept sentences, dropped (text, reason)).
    """
    by_id = {f.id: f for f in facts}
    dropped: list[tuple[str, str]] = []

    head_raw = raw.get("headline") if isinstance(raw, dict) else None
    headline: Sentence | None = None
    if isinstance(head_raw, dict):
        headline, why = check_item(str(head_raw.get("text", "")), head_raw.get("fact_ids"), by_id)
        if headline and len(headline.text.split()) > MAX_HEADLINE_WORDS:
            headline, why = None, f"headline longer than {MAX_HEADLINE_WORDS} words"
        if headline is None:
            dropped.append((f"[headline] {head_raw.get('text', '')}", why or "invalid"))
    else:
        dropped.append(("[headline]", "missing"))

    kept: list[Sentence] = []
    items = raw.get("sentences") if isinstance(raw, dict) else None
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            dropped.append((str(item), "not an object"))
            continue
        s, why = check_item(str(item.get("text", "")), item.get("fact_ids"), by_id)
        if s is None:
            dropped.append((str(item.get("text", "")), why or "invalid"))
        elif len(kept) >= MAX_SENTENCES:
            dropped.append((s.text, f"more than {MAX_SENTENCES} sentences"))
        else:
            kept.append(s)
    return headline, kept, dropped


# ---------------------------------------------------------------- building


def facts_payload(facts: list[Fact]) -> str:
    return json.dumps(
        [{"id": f.id, "priority": f.priority, "certain": f.certain, "text": f.text,
          "data": f.data} for f in facts],
        separators=(",", ":"), ensure_ascii=False, default=str,
    )


def user_prompt(facts: list[Fact], today: date) -> str:
    return (f"Today is {_fmt_day(today)}. Facts (JSON):\n{facts_payload(facts)}\n\n"
            "Write the briefing.")


def _template_headline(facts: list[Fact]) -> str:
    if not facts:
        return "No project data yet"
    text = facts[0].text.split(";")[0].rstrip(".")
    if len(text.split()) > 12:
        text = text.split(",")[0]
    return text if len(text.split()) <= 12 else "Project status"


def template_briefing(
    facts: list[Fact], fhash: str, created_at: str, reason: str | None = None
) -> Briefing:
    chosen = [f for f in facts if f.priority <= TEMPLATE_MAX_PRIORITY][:MAX_SENTENCES]
    return Briefing(
        headline=_template_headline(facts),
        sentences=[Sentence(f.text, [f.id], not f.certain) for f in chosen],
        source="template", facts_hash=fhash, created_at=created_at,
        fact_count=len(facts), fallback_reason=reason,
    )


def _from_row(row: sqlite3.Row, fact_count: int) -> Briefing:
    return Briefing(
        headline=row["headline"],
        sentences=[Sentence(s["text"], list(s["fact_ids"]), bool(s["uncertain"]))
                   for s in json.loads(row["sentences_json"] or "[]")],
        source=row["source"], facts_hash=row["facts_hash"], created_at=row["created_at"],
        model=row["model"], fact_count=fact_count, from_cache=True,
    )


def _store(conn: sqlite3.Connection, b: Briefing) -> None:
    db.insert_briefing(
        conn, b.facts_hash, b.headline,
        json.dumps([{"text": s.text, "fact_ids": s.fact_ids, "uncertain": s.uncertain}
                    for s in b.sentences], ensure_ascii=False),
        b.source, b.created_at, b.model,
    )


def build_briefing(
    conn: sqlite3.Connection,
    client: OllamaClient | None,
    now: datetime | None = None,
    force: bool = False,
) -> Briefing:
    """Facts -> (cached | model-phrased and validated | template) briefing.

    Cache: the newest stored briefing with the same facts hash is reused unless
    ``force``. A cached *template* is only reused when no client is given, so a
    briefing made while Ollama was down is upgraded once the model is back.
    """
    now = now or datetime.now(UTC)
    facts = compute_facts(conn, now=now)
    fhash = facts_hash(facts)
    created = now.astimezone(UTC).isoformat(timespec="seconds")

    if not force:
        row = db.latest_briefing(conn, fhash)
        if row is not None and (row["source"] == "model" or client is None):
            return _from_row(row, len(facts))

    if client is None:
        b = template_briefing(facts, fhash, created, "model off")
    elif not facts:
        b = template_briefing(facts, fhash, created, "no facts")
    else:
        try:
            raw = client.chat_json(SYSTEM_PROMPT, user_prompt(facts, now.astimezone().date()),
                                   BRIEFING_SCHEMA)
        except OllamaError as e:
            b = template_briefing(facts, fhash, created, f"model unavailable: {e}")
        else:
            headline, kept, dropped = validate(raw, facts)
            if headline is None or len(kept) < MIN_SENTENCES:
                why = "headline rejected" if headline is None else (
                    f"only {len(kept)} sentence(s) passed validation")
                b = template_briefing(facts, fhash, created, f"model output rejected: {why}")
            else:
                b = Briefing(headline=headline.text, sentences=kept, source="model",
                             facts_hash=fhash, created_at=created, model=client.model,
                             fact_count=len(facts))
            b.dropped = dropped
    _store(conn, b)
    return b


# ---------------------------------------------------------------- rendering


def _local_stamp(iso: str, now: datetime | None = None) -> str:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    local = dt.astimezone()
    today = (now or datetime.now(UTC)).astimezone().date()
    hm = local.strftime("%H:%M")
    return hm if local.date() == today else f"{_fmt_day(local.date())} {hm}"


def render_briefing(b: Briefing, width: int = 78, now: datetime | None = None) -> str:
    import textwrap

    out = [b.headline, ""]
    for s in b.sentences:
        mark = "~ " if s.uncertain else "- "
        out.append(textwrap.fill(s.text, width=width, initial_indent=mark,
                                 subsequent_indent="  "))
    if not b.sentences:
        out.append("Nothing to report. Run `jarvis sync` to fetch repo data.")
    out.append("")
    if b.source == "model":
        src = f"source: {b.model or 'model'}"
    elif b.fallback_reason == "model off":
        src = "source: template (model off)"
    elif (b.fallback_reason or "").startswith("model output rejected"):
        src = "source: template (model output rejected)"
    else:
        src = "source: template (model unavailable)"
    parts = [src, f"based on {b.fact_count} facts", f"updated {_local_stamp(b.created_at, now)}"]
    if any(s.uncertain for s in b.sentences):
        parts.append("~ unconfirmed")
    out.append(" · ".join(parts))
    return "\n".join(out) + "\n"
