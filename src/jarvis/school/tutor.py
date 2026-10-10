"""The tutor: a prompt per course module, a streamed reply, and its blocks.

A reply is Markdown for the student plus fenced blocks for the app:

    ```check
    {"q": "Is p → q true when p is false?", "a": "Yes: a false premise ..."}
    ```

``check`` is a check question (shown as a card, its answer hidden until asked);
``stage`` (next stage of School) asks for a visual. Blocks never show as text:
``split_blocks`` takes them out of a finished reply and ``streaming_text``
hides them (and a block still being written) while the reply streams.

Conversations live in ``chat_messages`` under ``school:<course>:<id>`` session
ids, so recall finds them and no new table is needed.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from jarvis import chat
from jarvis.school.modules import Module

KINDS = ("stage", "check")
GENERAL_COURSE = "General"     # School's tab for studying outside a course
SESSION_PREFIX = "school:"
# The reply's length for one idea; longer when the student asks for depth.
REPLY_WORDS = 180
TRANSCRIPT_LIMIT = 80          # messages shown when a session is reopened
_BLOCK_RE = re.compile(r"```[ \t]*(stage|check)\b[ \t]*\n?(.*?)```", re.S | re.I)
_FENCE = "```"


@dataclass(frozen=True)
class Block:
    kind: str                       # "stage" | "check"
    data: dict[str, Any] | None     # the parsed JSON; None when unusable
    error: str | None = None


@dataclass(frozen=True)
class Request:
    course: str
    module: Module
    question: str
    session_id: str
    today: date
    image_b64: str | None = None    # a photo of a problem or slide (PNG/JPEG, base64)
    upcoming: str = ""              # "Midterm in 5 days" for the course, or ""


@dataclass
class Reply:
    text: str                       # what the student reads (blocks taken out)
    raw: str                        # as the model wrote it (stored)
    blocks: list[Block] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def checks(self) -> list[dict[str, str]]:
        return [b.data for b in self.blocks if b.kind == "check" and b.data]


# ---------------------------------------------------------------- sessions


def new_session_id(course: str) -> str:
    return f"{SESSION_PREFIX}{course}:{uuid.uuid4().hex[:10]}"


def is_school_session(session_id: str) -> bool:
    return session_id.startswith(SESSION_PREFIX)


def transcript(conn: sqlite3.Connection, session_id: str,
               limit: int = TRANSCRIPT_LIMIT) -> list[tuple[str, str]]:
    """(role, raw content) of a session's last ``limit`` messages, oldest first."""
    rows = conn.execute("SELECT role, content FROM chat_messages WHERE session_id = ? "
                        "ORDER BY id DESC LIMIT ?", (session_id, limit)).fetchall()
    return [(r["role"], r["content"] or "") for r in reversed(rows)]


# ---------------------------------------------------------------- blocks


def _parse(kind: str, body: str) -> Block:
    body = body.strip()
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        return Block(kind, None, f"not JSON ({e.msg})")
    if not isinstance(data, dict):
        return Block(kind, None, "not a JSON object")
    if kind == "check":
        q, a = str(data.get("q") or "").strip(), str(data.get("a") or "").strip()
        if not q:
            return Block(kind, None, "a check needs a question (q)")
        return Block(kind, {"q": q, "a": a})
    return Block(kind, data)


def split_blocks(text: str) -> tuple[str, list[Block]]:
    """The reply without its blocks, and the blocks in order. An unclosed block
    at the end (a cut-off reply) is dropped from the text too."""
    blocks: list[Block] = []

    def take(m: re.Match) -> str:
        blocks.append(_parse(m[1].lower(), m[2]))
        return ""

    visible = _BLOCK_RE.sub(take, text)
    cut = _open_block_start(visible)
    if cut is not None:
        visible = visible[:cut]
    return _tidy(unlatex(visible)), blocks


def _open_block_start(text: str) -> int | None:
    """Where a block that is still open (or a fence that may become one) starts."""
    i = text.rfind(_FENCE)
    if i < 0:
        return None
    if text.count(_FENCE) % 2 == 0:       # every fence closed: ordinary code blocks
        return None
    tag = text[i + 3:].lstrip(" \t").lower()
    word = re.match(r"[a-z]*", tag)[0]
    if any(k.startswith(word) for k in KINDS) or any(word.startswith(k) for k in KINDS):
        return i
    return None


def streaming_text(text: str) -> str:
    """What to show of a reply still streaming: finished blocks taken out, and a
    block being written (or a fence that may start one) hidden."""
    visible = _BLOCK_RE.sub("", text)
    cut = _open_block_start(visible)
    return unlatex(visible[:cut] if cut is not None else visible)


def _tidy(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ---------------------------------------------------------------- LaTeX -> Unicode
# The model writes LaTeX now and then despite the prompt; the reply is shown as
# rich text, so common math is turned into Unicode instead of showing raw TeX.

_TEX_SYMBOLS = {
    "neg": "¬", "lnot": "¬", "land": "∧", "wedge": "∧", "lor": "∨", "vee": "∨",
    "to": "→", "rightarrow": "→", "implies": "⇒", "Rightarrow": "⇒", "leftarrow": "←",
    "leftrightarrow": "↔", "iff": "⇔", "Leftrightarrow": "⇔", "forall": "∀",
    "exists": "∃", "in": "∈", "notin": "∉", "subseteq": "⊆", "subset": "⊂",
    "supseteq": "⊇", "cup": "∪", "cap": "∩", "emptyset": "∅", "varnothing": "∅",
    "setminus": "∖", "times": "×", "cdot": "·", "div": "÷", "pm": "±", "leq": "≤",
    "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠", "approx": "≈",
    "equiv": "≡", "infty": "∞", "nabla": "∇", "partial": "∂", "sum": "Σ", "prod": "Π",
    "int": "∫", "sqrt": "√", "ldots": "…", "dots": "…", "cdots": "⋯", "circ": "∘",
    "mid": "|", "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "Delta": "Δ",
    "epsilon": "ε", "theta": "θ", "lambda": "λ", "mu": "μ", "pi": "π", "sigma": "σ",
    "phi": "φ", "omega": "ω", "quad": " ", "qquad": "  ", ",": " ", ";": " ", "!": "",
    "left": "", "right": "", "lfloor": "⌊", "rfloor": "⌋", "lceil": "⌈", "rceil": "⌉",
    "{": "{", "}": "}", "%": "%", "$": "$",
}
_BLACKBOARD = {"N": "ℕ", "Z": "ℤ", "Q": "ℚ", "R": "ℝ", "C": "ℂ"}
_SUP = str.maketrans("0123456789+-=()nkix", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿᵏⁱˣ")
_SUB = str.maketrans("0123456789+-=()ijkn", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ᵢⱼₖₙ")
# $...$ that holds math: no space just inside the dollars (so "$5 and $10" stays)
_INLINE_MATH = re.compile(r"\$\$(.+?)\$\$|\$(?=\S)([^$\n]*?\S)\$(?!\d)", re.S)


def tex_to_unicode(tex: str) -> str:
    """Common LaTeX math as plain Unicode: \\frac{a}{b} -> (a)/(b), x^{2} -> x²,
    \\mathbb{Z} -> ℤ, \\leq -> ≤. Unknown commands lose their backslash."""
    s = tex
    s = re.sub(r"\\mathbb\{?([NZQRC])\}?", lambda m: _BLACKBOARD[m[1]], s)
    s = re.sub(r"\\(?:text|mathrm|textbf|mathbf|mathit|operatorname)\{([^{}]*)\}", r"\1", s)
    for _ in range(3):                                     # nested fractions
        s = re.sub(r"\\[dt]?frac\{([^{}]*)\}\{([^{}]*)\}",
                   lambda m: f"{_group(m[1])}/{_group(m[2])}", s)
    s = re.sub(r"\\sqrt\{([^{}]*)\}", lambda m: f"√{_group(m[1])}", s)
    s = re.sub(r"\\([A-Za-z]+|[,;!{}%$])",
               lambda m: _TEX_SYMBOLS.get(m[1], m[1]), s)

    def script(m: re.Match, table: dict, mark: str) -> str:
        body = m[1] if m[1] is not None else m[2]
        out = body.translate(table)
        return out if all(ch != c for ch, c in zip(out, body)) else f"{mark}({body})"

    s = re.sub(r"\^\{([^{}]*)\}|\^(\w)", lambda m: script(m, _SUP, "^"), s)
    s = re.sub(r"_\{([^{}]*)\}|_(\w)", lambda m: script(m, _SUB, "_"), s)
    return s.replace("{", "").replace("}", "")


def _group(x: str) -> str:
    x = x.strip()
    return x if re.fullmatch(r"[\w.]+", x) else f"({x})"


def unlatex(text: str) -> str:
    """A reply with its $...$ / $$...$$ math (and stray \\commands) in Unicode."""
    def inline(m: re.Match) -> str:
        return tex_to_unicode(m[1] if m[1] is not None else m[2])

    out = _INLINE_MATH.sub(inline, text)
    out = re.sub(r"\\\(|\\\)|\\\[|\\\]", "", out)
    if "\\" in out:                       # math written without dollars
        out = re.sub(r"\\(mathbb\{?[NZQRC]\}?|[dt]?frac\{[^{}]*\}\{[^{}]*\}|[A-Za-z]+)",
                     lambda m: tex_to_unicode(m[0]), out)
    return out


# ---------------------------------------------------------------- prompt


def system_prompt(req: Request) -> str:
    m = req.module
    if m.key == "general" and req.course == GENERAL_COURSE:
        who = ("You are Jarvis's tutor. The student is studying on their own, any subject "
               "they bring; ask what they're working on if it isn't clear.")
    else:
        who = f"You are Jarvis's tutor for the student's course {req.course} ({m.name})."
    lines = [
        f"{who} Today is {req.today:%A %B} {req.today.day}, {req.today.year}.",
        "Teach one idea at a time, in plain words, with a small concrete example. "
        f"Keep a reply under about {REPLY_WORDS} words unless the student asks for more. "
        "Build on what the student already said; if they are wrong, say what is right "
        "about their attempt first, then the fix.",
        "Write Markdown. Write math in plain Unicode (¬ ∧ ∨ → ↔ ∀ ∃ ∈ ⊆ ∪ ∩ ℤ ∇ ² ≤ ≥), "
        "never LaTeX: no $ signs and no backslash commands; a fraction is n(n + 1)/2.",
        "After explaining something new, end with ONE short check question as a block, "
        "with its answer, and never give that answer in the text:",
        '```check\n{"q": "the question", "a": "the answer, one or two sentences"}\n```',
        "If the student sends a photo, first restate the problem exactly as you read "
        "it, so they can correct a misreading, then teach.",
        "If you are not sure of something, say so instead of guessing.",
    ]
    if m.topics:
        lines.append("The course covers: " + "; ".join(m.topics) + ".")
    if req.upcoming:
        lines.append(f"Coming up in this course: {req.upcoming}.")
    return "\n".join(lines)


def build_messages(req: Request, history: list[tuple[str, str]]) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = [{"role": "system", "content": system_prompt(req)}]
    msgs += [{"role": role, "content": content} for role, content in history]
    user: dict[str, Any] = {"role": "user", "content": req.question}
    if req.image_b64:
        user["images"] = [req.image_b64]
    msgs.append(user)
    return msgs


# ---------------------------------------------------------------- ask


def ask(client: Any, conn: sqlite3.Connection, req: Request,
        on_token: Callable[[str], None] | None = None) -> Reply:
    """Stream the tutor's reply to ``req`` and store both messages in the session."""
    t0 = time.monotonic()
    history = chat.load_history(conn, req.session_id)
    parts: list[str] = []
    for chunk in client.chat_stream(build_messages(req, history), options={"temperature": 0.4}):
        parts.append(chunk)
        if on_token:
            on_token(chunk)
    raw = "".join(parts)
    text, blocks = split_blocks(raw)
    meta = {"school": req.course, "module": req.module.key}
    chat.save_message(conn, req.session_id, "user", req.question,
                      {**meta, "image": bool(req.image_b64)})
    chat.save_message(conn, req.session_id, "assistant", raw, meta)
    return Reply(text, raw, blocks, time.monotonic() - t0)
