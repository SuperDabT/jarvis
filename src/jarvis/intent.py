"""What a chat message is asking for, and which data answers it (no Qt).

``route`` turns a question into a ``Route``: the intent, the timeframe it asks
about, and so which sources the answer may use. Clear, common requests are
recognised by their sentence structure (a few patterns per intent, not a
keyword per topic); anything else goes to the model's classifier when a client
is given (``classify_with_model``), and to "mixed" (the user's items plus the
project data) when it isn't or it fails.

Intents and their sources:

- ``agenda``   what is due / on the user's plate for a timeframe: the user's
               own items (Jarvis's task list) and GitHub milestones due in that
               timeframe. Answered by code, no model (``assistant``).
- ``attention`` the morning summary ("good morning", "daily summary"), "what
               needs my attention", and the check before studying ("I'm about
               to study"): computed from the user's items (``attention``), no
               model. ``Route.span`` says which: brief / attention / study.
- ``capture``  "remind me to ...", "add a task ...": handed to the existing
               capture flow, which the user confirms. Nothing is saved here.
- ``planning`` "what should I work on", "help me organize my weekend",
               "schedule these": open-ended goals. With a tool-calling model
               these go to the assistant loop (jarvis.agent), which picks
               its own tools; otherwise one answer from the user's open items
               and the project's suggested next steps, presented as options.
- ``project``  questions about the engineering project (issues, commits,
               milestones, hardware): the synced GitHub data.
- ``recall``   "what did I decide / say ...": past conversations and items.
- ``general``  general knowledge or chat: no stored data at all.
- ``mixed``    unclear: a compact view of the user's items and the project.

An open GitHub issue or project task is never a commitment for a day: only an
item's own due or reminder date puts it on a day.
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

Intent = Literal["agenda", "attention", "capture", "planning", "project", "recall", "general", "mixed"]
INTENTS: tuple[str, ...] = ("agenda", "capture", "planning", "project", "recall", "general",
                            "mixed")
PERSONAL_INTENTS = frozenset({"agenda", "attention", "planning", "recall", "mixed"})
PROJECT_INTENTS = frozenset({"planning", "project", "mixed"})

# ---------------------------------------------------------------- patterns

# An imperative to store something, at the start of the message.
_CAPTURE_RE = re.compile(
    r"^\s*(?:please\s+|can\s+you\s+|could\s+you\s+|jarvis,?\s+)?"
    r"(?:remind\s+me\b|add\s+(?:a\s+|an\s+)?(?:task|reminder|to-?do|item|note|idea)\b"
    r"|add\s+.+\s+to\s+my\s+(?:list|tasks|to-?dos?)\b|capture\b|note\s+(?:down|that)\b"
    r"|make\s+a\s+note\b|put\s+.+\s+on\s+my\s+(?:list|calendar)\b"
    r"|(?:to-?do|task|reminder)\s*:)", re.I)
# Asking about one's own commitments ("need to do", "on my plate", "my tasks", "due").
_AGENDA_RE = re.compile("|".join([
    r"\b(?:need|have|got|supposed)\s+to\s+(?:do|get\s+done|finish|handle)\b",
    r"\bon\s+my\s+(?:plate|list|agenda|schedule|calendar)\b",
    r"\bmy\s+(?:tasks?|to-?dos?|agenda|schedule|day|week|weekend|appointments?|"
    r"deadlines?|reminders?|commitments?|plans?|errands)\b",
    r"\b(?:what'?s|what\s+is|anything|is\s+anything|what)\s+(?:due|planned|scheduled)\b",
    r"\bwhat'?s\s+(?:on|happening)\s+(?:today|tomorrow|this|next|on)\b",
    r"\bdo\s+i\s+have\s+(?:anything|any|to|stuff|plans|something)\b",
    r"\bam\s+i\s+(?:busy|free)\b",
    r"\b(?:have|got)\s+(?:due|coming\s+up|scheduled|planned)\b",
    r"\bdue\s+(?:today|tonight|tomorrow|this|next|on)\b",
    r"\b(?:upcoming|coming\s+up)\b",
    r"\b(?:deadlines?|appointments?)\b",
]), re.I)
# The morning summary.
_BRIEF_RE = re.compile("|".join([
    r"^\s*(?:good\s+)?morning\b", r"\b(?:morning|daily)\s+(?:brief(?:ing)?|summary|update|"
    r"rundown|overview)\b", r"\bbrief\s+me\b", r"\bcatch\s+me\s+up\b",
    r"\bsummar(?:y|i[sz]e)\s+(?:of\s+)?(?:my|the)\s+day\b",
    r"\bwhat(?:'s|\s+is|\s+does)\s+(?:my|the)\s+day\s+look(?:ing)?\s+like\b",
    r"\bhow\s+(?:does|is)\s+(?:my|the)\s+day\s+look(?:ing)?\b",
]), re.I)
# What needs attention / what might be slipping.
_ATTENTION_RE = re.compile("|".join([
    r"\bneeds?\s+(?:my\s+)?attention\b", r"\bam\s+i\s+(?:forgetting|missing)\b",
    r"\bwhat\s+am\s+i\s+(?:forgetting|missing)\b",
    r"\banything\s+i(?:'m|\s+am)?\s+(?:forgetting|missing|should\s+(?:know|worry))\b",
    r"\bwhat\s+should\s+i\s+(?:know|worry\s+about)\b", r"\b(?:at\s+risk|slipping)\b",
]), re.I)
# About to sit down and study.
_STUDY_RE = re.compile("|".join([
    r"\b(?:about|going|want|ready|planning|starting)\s+to\s+(?:start\s+)?(?:study|revise)\b",
    r"\bbefore\s+(?:i\s+)?(?:study|start\s+studying|studying|revis(?:e|ing))\b",
    r"\bwhat\s+should\s+i\s+(?:study|revise)\b",
    r"\b(?:start(?:ing)?|begin(?:ning)?)\s+(?:to\s+)?(?:study|studying)\b",
    r"\bstudy\s+(?:session|time)\b", r"\bsit(?:ting)?\s+down\s+to\s+study\b",
]), re.I)
# Choosing what to work on: options, not a lookup.
_PLANNING_RE = re.compile("|".join([
    r"\b(?:should|could|can)\s+i\s+(?:work\s+on|focus\s+on|do|start|tackle|pick)\b",
    r"\bwhat\s+(?:to|next\s+to)\s+(?:work\s+on|do|focus\s+on|tackle)\b",
    r"\bwhat\s+(?:do|would)\s+you\s+(?:suggest|recommend)\b",
    r"\bprioriti[sz]", r"\bwhere\s+(?:do|should|can)\s+i\s+start\b",
    r"\bwhat'?s\s+next\b", r"\bwhat\s+is\s+next\b", r"\bnext\s+steps?\b",
    r"\bfocus\s+on\b", r"\bwhat'?s\s+the\s+plan\b",
    r"\bwhat(?:'s|\s+is|\s+are)\s+(?:our|the)\s+(?:tasks?|plan|job)\b",
]), re.I)
# A goal to work on rather than a lookup: asking for help organizing, planning or
# scheduling (the tool-using assistant takes these).
_GOAL_RE = re.compile("|".join([
    r"^\s*(?:please\s+|can\s+you\s+|could\s+you\s+|jarvis,?\s+)?(?:help\s+me\b|"
    r"organi[sz]e\b|plan\b|schedule\b|reschedule\b|arrange\b|sort\s+out\b|"
    r"figure\s+out\b|work\s+out\b|make\s+(?:me\s+)?a\s+(?:plan|schedule)\b)",
    r"\bhelp\s+me\s+(?:to\s+)?(?:plan|organi[sz]e|schedule|figure|prioriti[sz]e|decide|"
    r"fit|balance|sort|get\s+through|manage)\b",
    r"\b(?:organi[sz]e|plan|schedule)\s+(?:my|the|these|this|those|it|them)\b",
]), re.I)
# Asking about something said or decided before.
_RECALL_RE = re.compile("|".join([
    r"\b(?:did|have)\s+(?:i|we)\s+(?:ever\s+)?(?:decide|say|said|mention|tell|told|ask|"
    r"choose|chose|pick|agree|settle|note|write)\b",
    r"\bwhat\s+did\s+(?:i|we)\s+(?:decide|say|conclude|agree|choose|pick|settle|note|"
    r"talk|discuss|ask)\b",
    r"\b(?:i|we)\s+(?:decided|talked\s+about|discussed|said|mentioned)\b",
    r"\bdo\s+you\s+remember\b", r"\bremind\s+me\s+what\b",
    r"\b(?:last\s+time|earlier|before|previously)\s+(?:i|we|you)\b",
]), re.I)
# Anything that names the engineering side of things.
_PROJECT_RE = re.compile(
    r"#\d+|\b(?:issues?|commits?|milestones?|repo(?:sitory)?|github|pull\s+requests?|prs?|"
    r"firmware|hardware|pcb|schematic|board|sensor|servo|rail|uart|i2c|spi|oled|lidar|"
    r"code|bug|build|demo|electrical|wiring|checks?|cards?|suggestions?|briefing|branch|"
    r"worked\s+on|work\s+on\s+the|commit\s+history|project)\b"
    r"|\bwhat\s+did\s+(?:i|we)\s+(?:work\s+on|do|change|fix|ship|commit|push)\b", re.I)
# Questions no stored data can answer.
_GENERAL_RE = re.compile(
    r"\b(?:weather|forecast|news|headlines?|stock|bitcoin|sports?|score|recipe|joke|"
    r"movie|film|song|capital\s+of|president|prime\s+minister|translate|define|"
    r"definition|meaning\s+of|how\s+(?:do|does|to)\s+(?:a|an|you|i)\b|what\s+is\s+an?\b|"
    r"explain\b|who\s+(?:is|was)\b|why\s+(?:is|does|do)\b|hello|hi\b|hey\b|thanks?|"
    r"thank\s+you)", re.I)
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


@dataclass(frozen=True)
class Route:
    intent: str
    start: date | None = None      # agenda timeframe, inclusive
    end: date | None = None
    span: str = ""                 # "today", "tomorrow", "this week", "Friday Oct 16"
    project: str | None = None     # a Jarvis project slug the question names
    capture_text: str = ""
    by: str = "rules"              # "rules" | "model" | "fallback"

    @property
    def personal(self) -> bool:
        return self.intent in PERSONAL_INTENTS

    @property
    def project_data(self) -> bool:
        return self.intent in PROJECT_INTENTS


def timeframe(question: str, today: date) -> tuple[date, date, str] | None:
    """The days a question asks about, or None when it names none."""
    q = question.lower()
    if re.search(r"\btomorrow\b", q):
        d = today + timedelta(days=1)
        return d, d, "tomorrow"
    if re.search(r"\bthis\s+weekend\b|\bthe\s+weekend\b|\bweekend\b", q):
        sat = today + timedelta(days=(5 - today.weekday()) % 7)
        return max(sat, today), sat + timedelta(days=1), "this weekend"
    if re.search(r"\bnext\s+week\b", q):
        mon = today + timedelta(days=7 - today.weekday())
        return mon, mon + timedelta(days=6), "next week"
    if re.search(r"\bthis\s+week\b|\bthe\s+week\b|\brest\s+of\s+the\s+week\b", q):
        return today, today + timedelta(days=6 - today.weekday()), "this week"
    m = re.search(r"\b(?:next|coming|following)\s+(\d+|few|couple(?:\s+of)?)\s+days\b", q)
    if m:
        n = int(m[1]) if m[1].isdigit() else 3
        return today, today + timedelta(days=max(1, n) - 1), f"the next {max(1, n)} days"
    for k, name in enumerate(_WEEKDAYS):
        if re.search(rf"\b(?:on\s+|this\s+|next\s+)?{name}\b", q):
            d = today + timedelta(days=(k - today.weekday()) % 7)
            return d, d, f"{d:%A} {d:%b} {d.day}"
    if re.search(r"\b(?:today|tonight|this\s+(?:morning|afternoon|evening)|now)\b", q):
        return today, today, "today"
    return None


def _project_slug(conn: sqlite3.Connection | None, question: str) -> str | None:
    """A Jarvis project the question names (by slug or name), or None."""
    if conn is None:
        return None
    q = question.lower()
    try:
        rows = conn.execute("SELECT slug, name FROM projects WHERE status != 'archived'")
        for r in rows:
            for word in (r["slug"], r["name"]):
                if word and re.search(rf"\b{re.escape(word.lower())}\b", q):
                    return r["slug"]
    except sqlite3.Error:
        return None
    return None


def capture_text(question: str) -> str:
    """The thing to capture: the message without its "remind me to" / "add" lead-in."""
    t = re.sub(r"^\s*(?:please\s+|can\s+you\s+|could\s+you\s+|jarvis,?\s+)?", "", question,
               flags=re.I)
    t = re.sub(r"^(?:remind\s+me\s+(?:to\s+)?|add\s+(?:a\s+|an\s+)?(?:task|reminder|to-?do|"
               r"item|note|idea)\s*(?:to\s+|:\s*)?|capture\s*:?\s*|note\s+(?:down|that)\s+|"
               r"make\s+a\s+note\s+(?:to\s+|that\s+)?|(?:to-?do|task|reminder)\s*:\s*)", "", t,
               flags=re.I)
    return t.strip().rstrip("?.!").strip()


def route_by_rules(question: str, today: date,
                   conn: sqlite3.Connection | None = None) -> Route | None:
    """The route for a clear request, or None when the rules can't tell."""
    q = question.strip()
    slug = _project_slug(conn, q)
    names_project = bool(slug) or bool(_PROJECT_RE.search(q))
    if _CAPTURE_RE.search(q) and not _RECALL_RE.search(q):
        return Route("capture", capture_text=capture_text(q))
    if not names_project and not _GOAL_RE.search(q):
        if _STUDY_RE.search(q):
            return Route("attention", span="study")
        if _BRIEF_RE.search(q):
            return Route("attention", span="brief")
        if _ATTENTION_RE.search(q):
            return Route("attention", span="attention")
    if _RECALL_RE.search(q) and not re.search(r"\b(?:work(?:ed)?\s+on|commit|change)", q, re.I):
        return Route("recall", project=slug)
    tf = timeframe(q, today)
    if _GOAL_RE.search(q):
        return Route("planning", *(tf or (None, None, "")), project=slug)
    if _AGENDA_RE.search(q) and not _PLANNING_RE.search(q):
        if names_project and not re.search(r"\bmy\s+(?:tasks?|to-?dos?|day|agenda)\b", q, re.I):
            return Route("project", project=slug)
        start, end, span = tf or (today, today, "today")
        return Route("agenda", start, end, span, project=slug)
    if _PLANNING_RE.search(q):
        return Route("planning", *(tf or (None, None, "")), project=slug)
    if names_project:
        return Route("project", project=slug)
    if _GENERAL_RE.search(q):
        return Route("general")
    return None


CLASSIFY_SYSTEM = """\
Classify a message to a personal assistant that knows the user's task list and one \
engineering project (GitHub issues, commits, milestones, hardware). Reply with JSON.
intent is one of:
- agenda: what the user has due, scheduled or planned for some time
- capture: the user explicitly asks to add something to their list or set a reminder (a statement of facts, such as when they are free, is not a capture)
- planning: the user wants help choosing what to work on
- project: a question about the engineering project, its code, issues or hardware
- recall: what the user said, asked or decided in an earlier conversation
- general: general knowledge, definitions, small talk; nothing stored is needed
- mixed: none of these clearly
"""
CLASSIFY_SCHEMA = {"type": "object", "properties": {"intent": {"type": "string",
                                                               "enum": list(INTENTS)}},
                   "required": ["intent"]}


def classify_with_model(client: Any, question: str) -> str | None:
    """The model's intent for an unclear message (schema-constrained), or None."""
    if client is None or not hasattr(client, "chat_json"):
        return None
    try:
        got = client.chat_json(CLASSIFY_SYSTEM, question, CLASSIFY_SCHEMA)
    except Exception:  # noqa: BLE001 - any failure means "the rules' fallback"
        return None
    intent = (got or {}).get("intent") if isinstance(got, dict) else None
    return intent if intent in INTENTS else None


def route(question: str, now: datetime, conn: sqlite3.Connection | None = None,
          client: Any | None = None, history: Sequence[tuple[str, str]] = ()) -> Route:
    """The route for ``question``: the rules; then, when Jarvis's last message was a
    question, an answer to it (planning continues); then the model; then "mixed"."""
    today = now.date()
    r = route_by_rules(question, today, conn)
    if r is not None:
        return r
    last = next((c for role, c in reversed(list(history)) if role == "assistant"), "")
    if last.rstrip().endswith("?"):
        return Route("planning", project=_project_slug(conn, question), by="followup")
    slug = _project_slug(conn, question)
    intent = classify_with_model(client, question)
    if intent is None:
        return Route("mixed", project=slug, by="fallback")
    if intent == "agenda":
        start, end, span = timeframe(question, today) or (today, today, "today")
        return Route("agenda", start, end, span, project=slug, by="model")
    if intent == "capture":
        return Route("capture", capture_text=capture_text(question), by="model")
    return Route(intent, project=slug, by="model")
