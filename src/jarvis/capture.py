"""Natural-language capture: free text -> a validated task, idea or reminder.

The local model (``OllamaClient.chat_json``, schema-constrained JSON) reads the
text and fills a form. Python then decides what is saved; the model never
writes. For every date, project and priority the model must also quote the
words of the text it took them from:

- A value whose quoted words are not in the text is dropped (never invented).
- Dates and times in common forms ("Thursday", "tomorrow at 5pm", "Oct 12",
  "in 3 days", "2026-10-12 17:00") are worked out here, from ``now`` in ``tz``,
  not taken from the model; other phrasings use the model's date after checks.
- A phrase that could mean two things ("next Thursday", "at 5") or a date in the
  past becomes a ``Clarification`` with concrete options instead of a guess.
- The model may also ask a question itself when the text is unclear.

The checked result goes through ``items.create_item`` like any other item.
``capture()`` returns a ``CaptureResult``: saved, clarify (answer it and call
again with ``answers``; the original text is kept), or error (model down or its
reply unusable; nothing saved). No Qt here: the window and ``jarvis add`` share it.
"""
from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

from jarvis import items
from jarvis.ollama import OllamaError

TEXT_MAX = 1000
TITLE_MAX = 200
BODY_MAX = 2000
MAX_ROUNDS = 2            # clarifications per capture before giving up
FAR_FUTURE_DAYS = 5 * 365
CAPTURE_TIMEOUT_S = 90
STATUSES = ("saved", "clarify", "error")

WEEKDAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tues": 1, "tue": 1, "wednesday": 2,
            "wed": 2, "thursday": 3, "thurs": 3, "thur": 3, "thu": 3, "friday": 4, "fri": 4,
            "saturday": 5, "sat": 5, "sunday": 6, "sun": 6}
MONTHS = {"january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4,
          "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8,
          "september": 9, "sept": 9, "sep": 9, "october": 10, "oct": 10, "november": 11,
          "nov": 11, "december": 12, "dec": 12}
NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
           "seven": 7, "eight": 8, "nine": 9, "ten": 10}
# Words a date phrase may contain besides the date itself.
FILLER = frozenset({"by", "on", "at", "before", "until", "till", "due", "the", "this", "of",
                    "coming", "around", "in", "for", "from", "now", "o'clock", "oclock"})


class CaptureError(RuntimeError):
    """The model could not be reached, or its reply could not be used."""


@dataclass(frozen=True)
class Clarification:
    question: str
    options: tuple[tuple[str, str], ...] = ()  # (label, answer text)
    field: str = "other"                       # due / remind / other


@dataclass(frozen=True)
class Answer:
    """The user's reply to a Clarification (``field`` copied from it)."""
    field: str
    question: str
    text: str


@dataclass(frozen=True)
class Interpretation:
    """A checked capture, ready for items.create_item."""
    kind: str
    title: str
    body: str | None = None
    due: str | None = None       # "YYYY-MM-DD" or "YYYY-MM-DDTHH:MM", wall clock
    remind: str | None = None    # "YYYY-MM-DDTHH:MM", wall clock
    project: str | None = None   # project slug
    priority: int = 0
    notes: tuple[str, ...] = ()  # what was dropped or adjusted, for the user


@dataclass(frozen=True)
class CaptureResult:
    text: str
    status: str                                   # one of STATUSES
    item: items.Item | None = None
    interpretation: Interpretation | None = None
    clarification: Clarification | None = None
    error: str | None = None
    answers: tuple[Answer, ...] = ()              # clarifications answered so far
    model: str | None = None
    seconds: float = 0.0

    @property
    def saved(self) -> bool:
        return self.status == "saved"

    def answered(self, text: str) -> tuple[Answer, ...]:
        """The answers to pass to ``capture`` again after replying ``text`` to
        this result's clarification."""
        c = self.clarification
        if c is None:
            raise ValueError("this capture asked nothing")
        return (*self.answers, Answer(c.field, c.question, text.strip()))


# ---------------------------------------------------------------- dates


@dataclass(frozen=True)
class Resolved:
    """A date phrase worked out here. ``recognized`` False: not a form this
    module reads (use the model's value). ``options``: it could mean either."""
    day: date | None = None
    time: str | None = None      # "HH:MM"
    recognized: bool = False
    options: tuple[tuple[str, str], ...] = ()


def _label_day(d: date, today: date) -> str:
    if d == today:
        return f"today ({d:%a %b} {d.day})"
    if d == today + timedelta(days=1):
        return f"tomorrow ({d:%a %b} {d.day})"
    return f"{d:%a %b} {d.day}"


def _label_time(hhmm: str) -> str:
    h, m = map(int, hhmm.split(":"))
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def _num(word: str) -> int | None:
    return int(word) if word.isdigit() else NUMBERS.get(word)


_ISO_DT = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})[t ](\d{2}):(\d{2})\b")
_ISO_D = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTH_NAMES = "|".join(sorted(MONTHS, key=len, reverse=True))
_MONTH_DAY = re.compile(rf"\b({_MONTH_NAMES})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b")
_DAY_MONTH = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_NAMES})\b")
_WEEKDAY = re.compile(r"\b(next\s+)?(" + "|".join(sorted(WEEKDAYS, key=len, reverse=True))
                      + r")\b")
_IN_N = re.compile(r"\bin\s+(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
                   r"(days?|weeks?)\b")
_RELATIVE = re.compile(r"\b(day after tomorrow|today|tonight|tomorrow|tmrw|tmr)\b")
_AMPM = re.compile(r"\b(\d{1,2})(?::([0-5]\d))?\s*(a\.?m\.?|p\.?m\.?)(?=\W|$)")
_H24 = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_NOON = re.compile(r"\b(noon|midday)\b")
_BARE_HOUR = re.compile(r"\bat\s+(\d{1,2})\b(?!\s*:)")


def resolve_phrase(phrase: str, today: date) -> Resolved:
    """Work out a date / time phrase ("by Thursday", "tomorrow at 5pm") relative to
    ``today``. Unknown words anywhere in it -> not recognized."""
    s = " " + re.sub(r"[,!?;]", " ", phrase.lower()) + " "
    days: list[date] = []
    times: list[str] = []
    options: list[tuple[str, str]] = []
    tonight = False

    def eat(m: re.Match) -> None:
        nonlocal s
        s = s[:m.start()] + " " + s[m.end():]

    def take(rx: re.Pattern, fn) -> None:
        while (m := rx.search(s)) is not None:
            fn(m)
            eat(m)

    def iso_dt(m):
        try:
            d = date(int(m[1]), int(m[2]), int(m[3]))
            if int(m[4]) > 23:
                raise ValueError
        except ValueError:
            days.append(date.min)  # an impossible date: not recognized
            return
        days.append(d)
        times.append(f"{m[4]}:{m[5]}")

    def iso_d(m):
        try:
            days.append(date(int(m[1]), int(m[2]), int(m[3])))
        except ValueError:
            days.append(date.min)

    def month_day(month: str, day: str):
        mo, dd = MONTHS[month], int(day)
        try:
            d = date(today.year, mo, dd)
            if d < today:
                d = date(today.year + 1, mo, dd)
        except ValueError:
            d = date.min
        days.append(d)

    def relative(m):
        nonlocal tonight
        word = m[1]
        offset = {"today": 0, "tonight": 0, "tomorrow": 1, "tmrw": 1, "tmr": 1,
                  "day after tomorrow": 2}[word]
        tonight = tonight or word == "tonight"
        days.append(today + timedelta(days=offset))

    def weekday(m):
        ahead = (WEEKDAYS[m[2]] - today.weekday()) % 7
        coming = today + timedelta(days=ahead)
        if m[1]:  # "next Thursday": the coming one, or the one after?
            later = coming + timedelta(days=7)
            if ahead == 0:
                coming = later
                later = coming + timedelta(days=7)
            options.extend([(_label_day(coming, today), coming.isoformat()),
                            (_label_day(later, today), later.isoformat())])
        else:
            days.append(coming)

    def in_n(m):
        n = _num(m[1]) or 0
        days.append(today + timedelta(days=n * (7 if m[2].startswith("week") else 1)))

    def ampm(m):
        h, mm = int(m[1]), int(m[2] or 0)
        if not 1 <= h <= 12:
            times.append("bad")
            return
        h = h % 12 + (12 if m[3].startswith("p") else 0)
        times.append(f"{h:02d}:{mm:02d}")

    take(_ISO_DT, iso_dt)
    take(_ISO_D, iso_d)
    take(_MONTH_DAY, lambda m: month_day(m[1], m[2]))
    take(_DAY_MONTH, lambda m: month_day(m[2], m[1]))
    take(_RELATIVE, relative)
    take(_WEEKDAY, weekday)
    take(_IN_N, in_n)
    take(_AMPM, ampm)
    take(_H24, lambda m: times.append(f"{int(m[1]):02d}:{m[2]}"))
    take(_NOON, lambda m: times.append("12:00"))
    bare: list[int] = []
    take(_BARE_HOUR, lambda m: bare.append(int(m[1])))

    leftover = [w for w in s.split() if w not in FILLER]
    if leftover or len(days) > 1 or len(times) + len(bare) > 1 or date.min in days \
            or "bad" in times:
        return Resolved()
    if not (days or times or bare or options):
        return Resolved()
    day = days[0] if days else None
    for h in bare:
        if 13 <= h <= 23 or h == 0:
            times.append(f"{h:02d}:00")
        elif 1 <= h <= 12 and tonight:
            times.append(f"{h % 12 + 12:02d}:00")
        elif 1 <= h <= 12:  # "at 5": morning or evening?
            base = day.isoformat() + "T" if day else ""
            when = f"{_label_day(day, today)} " if day else ""
            am, pm = f"{h:02d}:00", f"{h % 12 + 12:02d}:00"
            return Resolved(day=day, recognized=True, options=(
                (f"{when}{_label_time(am)}", f"{base}{am}"),
                (f"{when}{_label_time(pm)}", f"{base}{pm}")))
        else:
            return Resolved()
    if options:
        if times:  # "next Friday at 5pm": keep the time with each option
            options = [(f"{label} {_label_time(times[0])}", f"{value}T{times[0]}")
                       for label, value in options]
        return Resolved(recognized=True, options=tuple(options))
    return Resolved(day=day, time=times[0] if times else None, recognized=True)


# ---------------------------------------------------------------- model


SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(items.KINDS)},
        "title": {"type": "string"},
        "body": {"type": ["string", "null"]},
        "due_phrase": {"type": ["string", "null"]},
        "due_date": {"type": ["string", "null"]},
        "due_time": {"type": ["string", "null"]},
        "remind_phrase": {"type": ["string", "null"]},
        "remind_date": {"type": ["string", "null"]},
        "remind_time": {"type": ["string", "null"]},
        "project": {"type": ["string", "null"]},
        "project_phrase": {"type": ["string", "null"]},
        "priority": {"type": "integer", "minimum": 0, "maximum": 3},
        "priority_phrase": {"type": ["string", "null"]},
        "question": {"type": ["string", "null"]},
    },
    "required": ["kind", "title", "body", "due_phrase", "due_date", "due_time",
                 "remind_phrase", "remind_date", "remind_time", "project", "project_phrase",
                 "priority", "priority_phrase", "question"],
}

SYSTEM_PROMPT = """\
You turn one note the user typed into a structured item for their personal task list. \
You only fill in the form; you never add anything the note does not say.

Fields:
- kind: "reminder" if they ask to be reminded or notified ("remind me", "don't let me \
forget"), "idea" for an idea, thought or something to explore later ("idea:", "what if", \
"maybe build"), otherwise "task".
- title: a short imperative title in the user's own words, without the date, time, \
project or priority words, e.g. "Finish differential equations homework". Keep \
course names, names and numbers exactly. Capitalize the first word.
- body: extra detail from the note that is not in the title (a preference, a condition, \
a note), in their words, e.g. "Preferably before the gym." Otherwise null.
- due_phrase: the exact words of the note that say when it must be done by ("by \
Thursday", "today", "before Oct 12"), copied character for character; null if none. \
due_date (YYYY-MM-DD) and due_time (HH:MM, 24-hour) are what that phrase means, using \
the current date below; due_time null unless a time of day is given.
- remind_phrase, remind_date, remind_time: the same for when to remind them ("remind me \
tomorrow at 5pm" -> remind). A deadline is due; a moment to be told is remind. Never \
fill both from the same words.
- project: the slug of one of the projects listed below, only if the note clearly refers \
to that project by name or #slug; project_phrase: the exact words that refer to it. \
Otherwise both null. A general topic is not a project.
- priority: 0 normally; 1 to 3 only if the note says it is important or urgent \
("important", "urgent", "asap", "!"), with priority_phrase the exact words; otherwise \
priority 0 and priority_phrase null.
- question: null, unless the note is so unclear that saving it would likely be wrong \
(for example you cannot tell what to do, or a date could mean very different days). Then \
one short question to ask the user. Missing details are not unclear: leave them null.

Never invent a date, time, project or priority that the note does not state.
"""


def user_prompt(text: str, answers: Sequence[Answer], now: datetime,
                projects: Sequence[items.Project]) -> str:
    lines = [f"Now: {now:%A %Y-%m-%d %H:%M} ({now.tzname() or 'local time'})", "Projects:"]
    lines += [f"- {p.slug}: {p.name}" + (f" ({p.github_repo})" if p.github_repo else "")
              for p in projects] or ["- (none)"]
    lines += ["", "Note:", text]
    for a in answers:
        lines += ["", f"You asked: {a.question}", f"The user answered: {a.text}"]
    return "\n".join(lines)


# ---------------------------------------------------------------- validation


_WORD = re.compile(r"[a-z0-9]+")


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def _in_text(phrase: str | None, text: str) -> bool:
    return bool(phrase and phrase.strip() and _norm(phrase) in _norm(text))


def _content_words(s: str) -> list[str]:
    return [w[:5] for w in _WORD.findall(s.lower()) if len(w) >= 3]


def _from_text(value: str, text: str, share: float = 0.5) -> bool:
    """Most of ``value``'s words (first 5 letters, 3+ letter words) occur in text."""
    words = _content_words(value)
    if not words:
        return bool(_WORD.findall(value.lower())) and _norm(value) in _norm(text)
    have = set(_content_words(text))
    return sum(w in have for w in words) / len(words) >= share


def _str(raw: dict, key: str) -> str | None:
    v = raw.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise CaptureError(f"the model's {key} is not text")
    v = v.strip()
    return v or None


def _model_day(value: str | None, key: str) -> date | None:
    if value is None:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise CaptureError(f"the model's {key} {value!r} is not a date") from None


def _model_time(value: str | None, key: str) -> str | None:
    if value is None:
        return None
    try:
        return datetime.strptime(value, "%H:%M").strftime("%H:%M")
    except ValueError:
        raise CaptureError(f"the model's {key} {value!r} is not a time") from None


def _when(which: str, raw: dict, text: str, now: datetime, notes: list[str],
          answers: Sequence[Answer] = ()) -> tuple[date | None, str | None] | Clarification:
    """The (day, time) for "due" or "remind", or a question. An answer to an
    earlier question about this field settles a phrase that was unclear."""
    got = _when_from_text(which, raw, text, now, notes)
    if not isinstance(got, Clarification):
        return got
    for a in reversed([a for a in answers if a.field == which]):
        r = resolve_phrase(a.text, now.date())
        if r.recognized and not r.options:
            settled = _settle(r.day, r.time, now)
            if settled is not None:
                return settled
    return got


def _settle(day: date | None, hhmm: str | None,
            now: datetime) -> tuple[date | None, str | None] | None:
    """A worked-out (day, time), or None if it is past or too far away."""
    today = now.date()
    if day is None and hhmm is not None:  # a time alone: its next occurrence
        day = today if hhmm > now.strftime("%H:%M") else today + timedelta(days=1)
    if day is not None and (day < today or (day == today and hhmm is not None
                                             and hhmm <= now.strftime("%H:%M"))):
        return None
    if day is not None and day > today + timedelta(days=FAR_FUTURE_DAYS):
        return None
    return day, hhmm


def _when_from_text(which: str, raw: dict, text: str, now: datetime,
                    notes: list[str]) -> tuple[date | None, str | None] | Clarification:
    phrase = _str(raw, f"{which}_phrase")
    m_day = _model_day(_str(raw, f"{which}_date"), f"{which}_date")
    m_time = _model_time(_str(raw, f"{which}_time"), f"{which}_time")
    noun = "due date" if which == "due" else "reminder time"
    if phrase is None or not _in_text(phrase, text):
        if m_day or m_time:
            notes.append(f"ignored a {noun} that isn't in your text")
        return None, None
    today = now.date()
    r = resolve_phrase(phrase, today)
    if r.options:
        ask = "When is it due" if which == "due" else "When should I remind you"
        return Clarification(f"{ask}: “{phrase}”?", r.options, which)
    if r.recognized:
        day, hhmm = r.day, r.time
        if (m_day and day and m_day != day) or (m_time and hhmm and m_time != hhmm):
            notes.append(f"read “{phrase}” as {_label_day(day or today, today)}"
                         + (f" {_label_time(hhmm)}" if hhmm else ""))
    else:
        day, hhmm = m_day, m_time
        if day is None and hhmm is None:
            if not _looks_dated(phrase):  # "remind me": no time was given at all
                return None, None
            ask = "When is it due" if which == "due" else "When should I remind you"
            return Clarification(f"{ask}? I couldn't read “{phrase}”.", (), which)
    settled = _settle(day, hhmm, now)
    if settled is None:
        return Clarification(f"“{phrase}” is already past or too far away. "
                             "When did you mean?", (), which)
    return settled


_DATED = re.compile(r"\d|\b(" + "|".join([*WEEKDAYS, *MONTHS, "today", "tonight", "tomorrow",
                                          "tmrw", "tmr", "noon", "midday", "week", "month",
                                          "year", "morning", "evening", "afternoon"])
                    + r")\b")


def _looks_dated(phrase: str) -> bool:
    """Has a number or a date / time word (so it is meant as a date)."""
    return bool(_DATED.search(phrase.lower()))


def _stamp(day: date | None, hhmm: str | None) -> str | None:
    if day is None:
        return None
    return f"{day.isoformat()}T{hhmm}" if hhmm else day.isoformat()


def _project(raw: dict, text: str, projects: Sequence[items.Project],
             notes: list[str]) -> str | None:
    by_slug = {p.slug: p for p in projects}
    tags = [t.lower() for t in re.findall(r"(?<![\w&])#([A-Za-z][\w-]*)", text)]
    for tag in tags:
        if tag in by_slug:
            return tag
    for tag in tags:
        notes.append(f"no project #{tag}")
    slug = _str(raw, "project")
    if slug is None:
        return None
    phrase = _str(raw, "project_phrase")
    p = by_slug.get(slug.lower().removeprefix("#"))
    if p is None:
        notes.append(f"ignored unknown project “{slug}”")
        return None
    # its slug and name only: a repo name like "stm32-lidar-scanner" holds generic
    # words ("stm32") that would file "my STM32 projects" under it
    names = set(_content_words(f"{p.slug} {p.name}"))
    if not _in_text(phrase, text) or not names & set(_content_words(phrase or "")):
        notes.append(f"didn't file it under {p.name}: the text doesn't name it")
        return None
    return p.slug


def _priority(raw: dict, text: str, notes: list[str]) -> int:
    value = raw.get("priority", 0)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 3:
        raise CaptureError(f"the model's priority {value!r} is not 0 to 3")
    if value and not _in_text(_str(raw, "priority_phrase"), text):
        notes.append("ignored a priority that isn't in your text")
        return 0
    return value


def validate(raw: Any, text: str, now: datetime,
             projects: Sequence[items.Project],
             answers: Sequence[Answer] = ()) -> Interpretation | Clarification:
    """Check the model's form against the text. Raises CaptureError if it is
    unusable; returns a Clarification if something material is unclear."""
    if not isinstance(raw, dict):
        raise CaptureError("the model's reply is not a JSON object")
    notes: list[str] = []
    kind = raw.get("kind")
    if kind not in items.KINDS:
        raise CaptureError(f"the model's kind {kind!r} is not task, idea or reminder")
    title = _str(raw, "title")
    if title is None:
        raise CaptureError("the model gave no title")
    if len(title) > TITLE_MAX or not _from_text(title, text):
        raise CaptureError("the model's title doesn't match your text")
    question = _str(raw, "question")
    if question and not answers:
        return Clarification(question[:300])
    body = _str(raw, "body")
    if body is not None and (len(body) > BODY_MAX or not _from_text(body, text, 0.6)
                             or _norm(body) == _norm(title)):
        body = None
    if body:
        body = body[0].upper() + body[1:]
    due = _when("due", raw, text, now, notes, answers)
    if isinstance(due, Clarification):
        return due
    remind = _when("remind", raw, text, now, notes, answers)
    if isinstance(remind, Clarification):
        return remind
    due_at = _stamp(*due)
    remind_day, remind_time = remind
    if remind_day is not None and remind_time is None:
        # "remind me tomorrow": no moment to remind at, so it is due that day
        notes.append("no reminder time given")
        due_at = due_at or remind_day.isoformat()
        remind_at = None
    else:
        remind_at = _stamp(remind_day, remind_time)
    return Interpretation(kind=kind, title=title[0].upper() + title[1:], body=body,
                          due=due_at, remind=remind_at,
                          project=_project(raw, text, projects, notes),
                          priority=_priority(raw, text, notes), notes=tuple(notes))


# ---------------------------------------------------------------- capture


def make_client(cfg: Any) -> Any:
    """The configured local model (cfg.model), as for briefings and chat."""
    from jarvis.ollama import OllamaClient

    return OllamaClient(cfg.ollama_url, cfg.model, timeout=CAPTURE_TIMEOUT_S)


def _clean_text(text: Any) -> str:
    if not isinstance(text, str) or not text.strip():
        raise items.ItemError("type something to capture", "text")
    text = " ".join(text.split())
    if len(text) > TEXT_MAX:
        raise items.ItemError(f"that's longer than {TEXT_MAX} characters", "text")
    return text


def interpret(text: str, client: Any, *, answers: Sequence[Answer] = (),
              projects: Sequence[items.Project] = (), now: datetime | None = None,
              tz: tzinfo | None = None) -> Interpretation | Clarification:
    """Ask the model and check its reply (raises CaptureError). Answers to earlier
    questions count as part of the text."""
    local = items.local_now(now, tz)
    full = " ".join([text, *(a.text for a in answers)])
    try:
        raw = client.chat_json(SYSTEM_PROMPT, user_prompt(text, answers, local, projects),
                               SCHEMA)
    except OllamaError as e:
        raise CaptureError(f"the local model isn't available ({e})") from e
    return validate(raw, full, local, projects, answers)


def capture(conn: sqlite3.Connection, text: str, client: Any, *,
            answers: Sequence[Answer] = (), now: datetime | None = None,
            tz: tzinfo | None = None) -> CaptureResult:
    """Interpret ``text`` and save it. Never raises for model trouble: that is
    an "error" result with nothing saved."""
    t0 = time.monotonic()
    answers = tuple(answers)
    model = getattr(client, "model", None)

    def result(status: str, **kw: Any) -> CaptureResult:
        return CaptureResult(text=text, status=status, answers=answers, model=model,
                             seconds=round(time.monotonic() - t0, 2), **kw)

    try:
        clean = _clean_text(text)
        projects = items.list_projects(conn)
        got = interpret(clean, client, answers=answers, projects=projects, now=now, tz=tz)
    except (CaptureError, items.ItemError) as e:
        return result("error", error=str(e))
    if isinstance(got, Clarification):
        if len(answers) >= MAX_ROUNDS:
            return result("error", error="still unclear after your answers; "
                                         "try rewording it with the date spelled out")
        return result("clarify", clarification=got)
    try:
        item = items.create_item(conn, got.title, kind=got.kind, body=got.body,
                                 priority=got.priority, due=got.due, remind=got.remind,
                                 project=got.project, now=now, tz=tz)
    except items.ItemError as e:
        return result("error", error=f"couldn't save it: {e}", interpretation=got)
    return result("saved", item=item, interpretation=got)


def capture_plain(conn: sqlite3.Connection, text: str, *, now: datetime | None = None,
                  tz: tzinfo | None = None) -> CaptureResult:
    """Save the text as it is, as a task with no date (when the model is down and
    the user chooses to keep it anyway). No model involved."""
    t0 = time.monotonic()
    try:
        clean = _clean_text(text)
        if len(clean) > items.TITLE_MAX:
            raise items.ItemError(f"that's longer than {items.TITLE_MAX} characters", "text")
        item = items.create_item(conn, clean, now=now, tz=tz)
    except items.ItemError as e:
        return CaptureResult(text=text, status="error", error=str(e))
    interp = Interpretation(kind="task", title=item.title, notes=("saved as typed",))
    return CaptureResult(text=text, status="saved", item=item, interpretation=interp,
                         seconds=round(time.monotonic() - t0, 2))


# ---------------------------------------------------------------- describing


def describe_when(stamp: str | None, today: date) -> str:
    """"Thu Oct 15", "today 5:00 PM", "tomorrow"."""
    if not stamp:
        return ""
    d = date.fromisoformat(stamp[:10])
    if d == today:
        day = "today"
    elif d == today + timedelta(days=1):
        day = "tomorrow"
    else:
        day = f"{d:%a %b} {d.day}" + (f", {d.year}" if d.year != today.year else "")
    return f"{day} {_label_time(stamp[11:])}" if len(stamp) > 10 else day


def summary(result: CaptureResult, now: datetime | None = None,
            tz: tzinfo | None = None) -> str:
    """One line for the ticker / terminal: "Saved task “…” · due Thu Oct 15 · #lidar"."""
    if result.status == "error":
        return f"Not saved: {result.error}"
    if result.status == "clarify":
        return result.clarification.question
    i, today = result.item, items.local_now(now, tz).date()
    bits = [f"Saved {i.kind} “{i.title}”"]
    if i.due_at:
        bits.append(f"due {describe_when(i.due_at, today)}")
    if i.remind_at:
        bits.append(f"remind {describe_when(i.remind_at, today)}")
    if result.interpretation and result.interpretation.project:
        bits.append(f"#{result.interpretation.project}")
    if i.priority:
        bits.append("!" * i.priority)
    return " · ".join(bits)


__all__ = ["Answer", "CaptureError", "CaptureResult", "Clarification", "Interpretation",
           "Resolved", "capture", "capture_plain", "describe_when", "interpret", "make_client",
           "resolve_phrase", "summary", "validate"]
