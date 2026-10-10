"""The "Ask Jarvis" side panel: a chat over the synced project data.

The engine is ``jarvis.chat.ask`` (grounded context, streamed reply, validated
sources); this module is only its UI. A question runs on a background thread
with its own SQLite connection; streamed tokens come back through a queued Qt
signal. One question at a time: Send is disabled while Jarvis answers.

Thinking mode (``jarvis.escalate``): typed and spoken questions go through
``answer_with_escalation``. In "auto" a weak first answer is replaced by the
reasoning model's; the THINK toggle (Ctrl+T, remembered) goes straight to it.
Its phase events come back like the tokens: the first answer fades out, the
reply shows "Thinking deeper — <why>" with the orb in its "deep" state, and the
meta line says which tier answered and how long the model thought.

Voice (mic button / Ctrl+Space): a ``voice_turn.VoiceTurn`` walks listening ->
transcribing -> thinking -> speaking. Recording, speech-to-text and the spoken
answer run on worker threads (``jarvis.voice``); every callback comes back as a
queued signal, so the orb and the bubbles are only touched on the GUI thread.
A voice turn joins the same history and session as typed questions, and only
one turn (typed or spoken) runs at a time.
"""
from __future__ import annotations

import inspect
import math
import re
import shutil
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QDesktopServices,
    QKeyEvent,
    QKeySequence,
    QShortcut,
    QTextCursor,
    QTextOption,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QMenu,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
    QWidgetItem,
)

from jarvis import db
from jarvis.config import Config
from jarvis.gui import motion, theme
from jarvis.gui import voice_turn as vt
from jarvis.gui.orb import COMPACT, Orb
from jarvis.gui.theme import LIGHT, Tokens
from jarvis.gui.widgets import _clear, _label, repolish, set_font

PANEL_WIDTH = 380
SUGGESTED = (
    "What's left for Core demo?",
    "Is my 5V rail OK?",
    "What did I work on this week?",
)
THINKING_SHOW_SECONDS_AFTER = 3
THINKING_ORB = 20  # px, the orb next to "thinking…" in a reply
STREAM_CARET = " ▍"  # after the streamed text until the answer is complete
THINK_KEY = "chat/think_mode"
THINK_TOOLTIP = ("Thinking mode (Ctrl+T, typed questions only): answer with the reasoning "
                 "model straight away. Off: a quick answer first, the reasoning model only "
                 "when it is weak.")
DEEP_REASONS = {
    "think_mode": "thinking mode",
    "planning": "planning question",
    "escalated": "first answer was weak",
}
NO_ANSWER = "I couldn't put an answer together from the synced data. Try rephrasing the question."

# ask_fn(conn, cfg, question, history, on_token, session_id[, mode=, on_phase=]) -> chat.Answer
# (``mode`` and ``on_phase`` are passed only to functions that accept them)
AskFn = Callable[..., Any]


def default_ask(conn: sqlite3.Connection, cfg: Config, question: str, history: list,
                on_token: Callable[[str], None], session_id: str, *, mode: str = "auto",
                on_phase: Callable[[str, dict], None] | None = None) -> Any:
    """Typed chat: gemma without reasoning first, with reasoning when that is weak
    (``mode="think"``: with reasoning straight away)."""
    from jarvis.escalate import answer_with_escalation

    return answer_with_escalation(conn, cfg, question, history, mode=mode, fast_client=None,
                                  deep_client=None, on_token=on_token, on_phase=on_phase,
                                  session_id=session_id)


def accepted_kw(fn: Callable[..., Any], **kw: Any) -> dict[str, Any]:
    """The keyword arguments of ``kw`` that ``fn`` takes (all of them for **kwargs)."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return {}
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return kw
    return {k: v for k, v in kw.items() if k in params}


# Voice seams, injectable for tests (nothing real is recorded or played there).
# recorder_factory(on_level) -> object with record() -> wav bytes | None, stop()
RecorderFactory = Callable[[Callable[[float], None]], Any]
# transcribe_fn(wav, cfg) -> text
TranscribeFn = Callable[[bytes, Config], str]
# voice_answer_fn(conn, cfg, question, history, speaker, on_token, session_id) -> chat.Answer
VoiceAnswerFn = Callable[..., Any]
# speaker_factory(cfg, on_level, on_state, on_error) -> object with say(), stop(),
# close() and a ``speaking`` property (voice.Speaker fits)
SpeakerFactory = Callable[..., Any]

AUTO_LISTEN_DELAY_MS = 450   # pause between Jarvis finishing and listening again
UNSTAGE_DELAY_MS = 700       # the orb lingers on its stage after a turn
UNSTAGE_ERROR_MS = 1600      # ... longer after an error, while it flashes


def default_recorder(on_level: Callable[[float], None]) -> Any:
    from jarvis.voice import Recorder

    return Recorder(on_level=on_level)


def default_transcribe(wav: bytes, cfg: Config) -> str:
    from jarvis.voice import transcribe

    return transcribe(wav, cfg)


def default_voice_answer(conn: sqlite3.Connection, cfg: Config, question: str, history: list,
                         speaker: Any, on_token: Callable[[str], None], session_id: str, *,
                         mode: str = "auto",
                         on_phase: Callable[[str, dict], None] | None = None) -> Any:
    """Voice: qwen first; planning questions and weak answers go to gemma, never
    with reasoning (too slow to wait for when spoken)."""
    from jarvis.voice import voice_answer

    return voice_answer(conn, cfg, question, history, None, speaker, on_token=on_token,
                        session_id=session_id, mode=mode, on_phase=on_phase,
                        deep_think=False)


def default_speaker(cfg: Config, on_level: Callable[[float], None],
                    on_state: Callable[[str], None], on_error: Callable[[str], None]) -> Any:
    from jarvis.voice import Speaker

    return Speaker(cfg, on_level=on_level, on_state=on_state, on_error=on_error)


def mic_available() -> bool:
    return shutil.which("pw-record") is not None


def markdown_html(text: str) -> str:
    """The answer's Markdown as HTML with real paragraph spacing (QLabel's own
    Markdown mode drops it)."""
    from PySide6.QtGui import QTextDocument

    doc = QTextDocument()
    doc.setMarkdown(text)
    html = doc.toHtml()
    return re.sub(r"margin-bottom:\d+px", "margin-bottom:8px", html)


def new_session_id() -> str:
    return f"window-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


def thought_text(seconds: float | None, cut: bool = False) -> str:
    """"thought for 12 s" (the reasoning's duration), "" when it didn't reason."""
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return ""
    n = f"{seconds:.0f} s" if seconds >= 10 else f"{seconds:.1f} s"
    return f"thought for {n}" + (", cut short" if cut else "")


def chip_text(ref: str, label: str = "") -> str:
    """"#31" stays, "commit:02fb2d2" -> "02fb2d2", "milestone:Core demo" -> "Core demo"."""
    if ref.startswith("#"):
        return ref
    kind, _, rest = ref.partition(":")
    if not rest:
        return ref
    if kind == "commit":
        return rest[:7]
    if kind == "card":
        return f"card {rest}"
    if kind in ("fact", "check"):
        # "fact:maybe_done:28" -> "maybe done #28", "check:rail_budget:5V" -> "rail budget 5V"
        name, _, arg = rest.partition(":")
        name = name.replace("_", " ")
        if not arg:
            return name
        return f"{name} #{arg}" if arg.isdigit() else f"{name} {arg}"
    return rest


CHIP_MAX = PANEL_WIDTH - 80   # px of chip text: a chip never outgrows the reply


def elide_chip(button: QPushButton, text: str) -> str:
    """A long source title ends in "…" (the tooltip has all of it)."""
    return button.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, CHIP_MAX)


# ---------------------------------------------------------------- flow layout


class FlowLayout(QLayout):
    """Left-to-right items that wrap (for source chips)."""

    def __init__(self, parent: QWidget | None = None, spacing: int = 6):
        super().__init__(parent)
        self._items: list[QWidgetItem] = []
        self._sp = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:  # noqa: N802 - Qt API
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, i: int):  # noqa: N802 - Qt API
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i: int):  # noqa: N802 - Qt API
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):  # noqa: N802 - Qt API
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt API
        return True

    def heightForWidth(self, w: int) -> int:  # noqa: N802 - Qt API
        return self._do(QRect(0, 0, w, 0), True)

    def setGeometry(self, r: QRect) -> None:  # noqa: N802 - Qt API
        super().setGeometry(r)
        self._do(r, False)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802 - Qt API
        s = QSize()
        for it in self._items:
            s = s.expandedTo(it.minimumSize())
        return s

    def _do(self, r: QRect, test: bool) -> int:
        x, y, line = r.x(), r.y(), 0
        for it in self._items:
            hint = it.sizeHint()
            nx = x + hint.width() + self._sp
            if nx - self._sp > r.right() and line > 0:
                x, y = r.x(), y + line + self._sp
                nx, line = x + hint.width() + self._sp, 0
            if not test:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x, line = nx, max(line, hint.height())
        return y + line - r.y()


# ---------------------------------------------------------------- message text


class MessageText(QTextBrowser):
    """Selectable message text that always fits the panel's width.

    A QLabel can't break a word, so one long path or identifier made a reply as
    wide as that word and pushed it past the window's edge; this wraps at word
    boundaries and, when a word is longer than the line, inside it (copying
    still gives the text as written). Its height follows the laid-out document,
    and its minimum width is small, so it never widens the conversation.

    Streaming appends to the document (``add_text``): Qt lays out only the
    paragraph that changed, so each token costs the same however long the reply
    is. Re-setting a label's whole text per token cost time proportional to the
    reply (26 ms a token at 5,500 characters) and starved the GUI thread.

    ``fit=True`` (the question bubble): as wide as the text when it is short,
    wrapping when it isn't."""

    MIN_W = 48

    def __init__(self, name: str, parent: QWidget | None = None, *, fit: bool = False,
                 links: bool = False):
        super().__init__(parent)
        self.setObjectName(name)
        self.fit = fit
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setLineWrapMode(QTextBrowser.LineWrapMode.WidgetWidth)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setOpenLinks(False)          # never navigate the message itself
        flags = Qt.TextInteractionFlag.TextSelectableByMouse
        if links:
            flags |= Qt.TextInteractionFlag.LinksAccessibleByMouse
            self.anchorClicked.connect(QDesktopServices.openUrl)
        self.setTextInteractionFlags(flags)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)   # Ctrl+C, never a Tab stop
        self.viewport().setAutoFillBackground(False)
        self.document().setDocumentMargin(0)
        self.setSizePolicy(QSizePolicy.Policy.Preferred if fit else QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)
        self._caret = ""
        self._h = -1
        self.document().documentLayout().documentSizeChanged.connect(self._fit_height)
        self._fit_height()

    # -- content

    def text(self) -> str:
        """The text as shown (without the streaming caret)."""
        t = self.toPlainText()
        return t[:-len(self._caret)] if self._caret and t.endswith(self._caret) else t

    def set_plain(self, text: str, caret: str = "") -> None:
        self._caret = caret
        self.setPlainText(text + caret)
        self.updateGeometry()

    def set_html(self, html: str) -> None:
        self._caret = ""
        self.setHtml(html)
        self.updateGeometry()

    def add_text(self, text: str) -> None:
        """Add streamed text before the caret; only the last paragraph is laid out again."""
        if not text:
            return
        cur = QTextCursor(self.document())
        cur.movePosition(QTextCursor.MoveOperation.End)
        if self._caret:
            cur.movePosition(QTextCursor.MoveOperation.Left, n=len(self._caret))
        cur.insertText(text)

    # -- size

    def _fit_height(self, *_: Any) -> None:
        h = math.ceil(self.document().size().height()) + 2 * self.frameWidth()
        if h != self._h:
            self._h = h
            self.setFixedHeight(max(h, 1))

    def natural_width(self) -> int:
        """The width of the longest line, unwrapped."""
        fm = self.fontMetrics()
        widest = max((fm.horizontalAdvance(line) for line in self.text().split("\n")), default=0)
        return widest + 2 * self.frameWidth() + 4

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        w = self.natural_width() if self.fit else super().sizeHint().width()
        return QSize(w, max(self._h, 1))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return QSize(self.MIN_W, max(self._h, 1))

    def wheelEvent(self, e) -> None:  # noqa: N802 - Qt API
        """The conversation scrolls, not the message (it never has anything to scroll)."""
        area = self.parentWidget()
        while area is not None and not isinstance(area, QScrollArea):
            area = area.parentWidget()
        if area is None:
            e.ignore()
            return
        QApplication.sendEvent(area.verticalScrollBar(), e)
        e.accept()


# ---------------------------------------------------------------- messages


class UserMessage(QWidget):
    """The question bubble; a spoken one (``voice=True``) carries a small mic."""

    def __init__(self, text: str, parent: QWidget | None = None, *, voice: bool = False,
                 tokens: Tokens | None = None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(40, 0, 0, 0)
        lay.addStretch(1)
        bubble = QFrame()
        bubble.setObjectName("userBubble")
        bl = QHBoxLayout(bubble)
        bl.setContentsMargins(12, 8, 12, 9)
        bl.setSpacing(7)
        self.voice = voice
        self.glyph: vt.MicGlyph | None = None
        if voice:
            self.glyph = vt.MicGlyph(tokens, 12)
            col = QVBoxLayout()
            col.setContentsMargins(0, 0, 0, 0)
            # centred on the first line of text (also when the question wraps)
            col.addSpacing(max(0, (_label("x", "userText").sizeHint().height() - 12) // 2))
            col.addWidget(self.glyph)
            col.addStretch(1)
            bl.addLayout(col)
            bl.setContentsMargins(10, 8, 12, 9)
        # Short questions keep the bubble snug; long ones wrap inside it.
        self.label = MessageText("userText", fit=True)
        self.label.set_plain(text)
        bl.addWidget(self.label)
        lay.addWidget(bubble)
        self.text = text


_PRE = '<p style="margin:0; white-space:pre">{}</p>'


def note_html(text: str) -> str:
    """A voice message as rich text. A URL and a "Start it with: <command>" hint
    each get a line of their own that never wraps (Qt breaks inside URLs and at
    hyphens otherwise), the command in monospace, so it reads as something to
    copy; copying still gives the plain text."""
    import html

    head, sep, cmd = text.partition("Start it with: ")
    head = head.strip()
    m = re.search(r"\s(https?://\S+?[.,;]?)(?:\s|$)", head)
    if m:
        before, url, after = head[:m.start()], m.group(1), head[m.end():].strip()
        out = (f'<p style="margin:0">{html.escape(before)}</p>' + _PRE.format(html.escape(url))
               + (f'<p style="margin:0">{html.escape(after)}</p>' if after else ""))
    else:
        out = f'<p style="margin:0">{html.escape(head)}</p>'
    if sep:
        out += ('<p style="margin:6px 0 0 0">Start it with:</p>'
                + _PRE.format(f"<code>{html.escape(cmd.strip())}</code>"))
    return out


class VoiceNote(QWidget):
    """A quiet line in the conversation: "Didn't catch that." or a voice error
    with the hint to fix it (selectable, so a command can be copied)."""

    def __init__(self, text: str, error: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 8, 0)
        self.error = error
        self.label = _label("", "voiceNote", wrap=True)
        self.label.setProperty("level", "error" if error else "info")
        self.label.setTextFormat(Qt.TextFormat.RichText)
        self.label.setText(note_html(text))
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        # a no-wrap URL or command line never widens the conversation (it is cut at
        # the edge instead; selecting and copying still gives all of it)
        self.label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        lay.addWidget(self.label, 1)
        self.text = text


class BotMessage(QWidget):
    """Jarvis's reply: thinking state, streamed text, then the validated answer.

    ``phase`` takes the escalation events (``jarvis.escalate``): a discarded
    first answer fades out and the indicator becomes "Thinking deeper — <why>"
    with the orb in its "deep" state."""

    def __init__(self, tokens: Tokens, parent: QWidget | None = None):
        super().__init__(parent)
        self.tokens = tokens
        self.streamed = ""
        self.answer = None
        self.started = time.monotonic()
        self.deep_reason: str | None = None   # set once the deep pass started
        self.thought_s: float | None = None
        self.thinking_cut = False
        self.fallback = False
        self._discarding = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 8, 0)
        lay.setSpacing(5)
        head = QHBoxLayout()
        head.setSpacing(6)
        self.name = _label("✦ Jarvis", "botName")
        self.name.setFont(theme.mono_font(11))
        head.addWidget(self.name)
        self.meta = _label("", "botMeta", wrap=True)   # wraps rather than widening the reply
        self.meta.setFont(theme.mono_font(10, caps=False, spacing_em=0.04))
        head.addWidget(self.meta, 1)
        lay.addLayout(head)
        # thinking indicator: a small orb in its thinking state + elapsed seconds
        self.thinking = QWidget()
        tl = QHBoxLayout(self.thinking)
        tl.setContentsMargins(0, 2, 0, 2)
        tl.setSpacing(8)
        self.thinking_orb = Orb(tokens, THINKING_ORB, "thinking")
        tl.addWidget(self.thinking_orb)
        self.thinking_text = _label("thinking…", "thinking", wrap=True)
        tl.addWidget(self.thinking_text, 1)
        lay.addWidget(self.thinking)
        self.body = MessageText("botText", links=True)
        self.body.hide()
        self._live = False                # the body holds streamed text (with the caret)
        lay.addWidget(self.body)
        self.notes = MessageText("botNote")
        self.notes.hide()
        lay.addWidget(self.notes)
        self.chips_box = QWidget()
        self.chips = FlowLayout(self.chips_box, 5)
        self.chips_box.hide()
        lay.addWidget(self.chips_box)
        self.chip_buttons: list[QPushButton] = []
        self._timer = QTimer(self, interval=1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._tick()

    @property
    def is_thinking(self) -> bool:
        return self.thinking.isVisible() if self.isVisible() else not self.thinking.isHidden()

    @property
    def deep(self) -> bool:
        return self.deep_reason is not None

    def _tick(self) -> None:
        secs = int(time.monotonic() - self.started)
        extra = f"  ·  {secs} s" if secs >= THINKING_SHOW_SECONDS_AFTER else ""
        if self.deep:
            why = DEEP_REASONS.get(self.deep_reason or "", "")
            head = "Thinking deeper" + (f" — {why}" if why else "…")
        else:
            head = "thinking…"
        self.thinking_text.setText(head + extra)

    def phase(self, name: str, info: dict) -> None:
        """An escalation event for this reply (see ``jarvis.escalate``)."""
        if name == "discard_fast":
            self._discard_streamed()
        elif name == "deep":
            self.deep_reason = str(info.get("reason") or "escalated")
            self._show_thinking()
        elif name == "answering":
            self.thought_s = info.get("thinking_s")
        elif name == "thinking_cut":
            self.thought_s = info.get("thinking_s")
            self.thinking_cut = True
        elif name == "fallback":
            self.fallback = True

    def _show_thinking(self) -> None:
        self.thinking.show()
        self.thinking_orb.set_state("deep" if self.deep else "thinking")
        if not self._timer.isActive():
            self._timer.start()
        self._tick()

    def _discard_streamed(self) -> None:
        """The first answer was weak: fade its text out, back to the indicator."""
        self.streamed = ""
        if self.body.isHidden():
            self._show_thinking()
            return
        self._discarding = True

        def gone() -> None:
            if not self._discarding:      # the deep answer already started
                return
            self._discarding = False
            self.body.setGraphicsEffect(None)
            self.body.hide()
            self.body.set_plain("")
            self._live = False
            self._show_thinking()

        motion.fade_out(self.body, motion.BASE, on_done=gone)

    def _stop_thinking(self) -> None:
        self._timer.stop()
        self.thinking.hide()  # hiding stops the orb's frame timer too
        self.thinking_orb.set_state("idle")

    def add_token(self, text: str) -> None:
        if not text:
            return
        if self._discarding:              # cut the fade short: new text is coming
            self._discarding = False
            self.body.setGraphicsEffect(None)
        if not self.thinking.isHidden():
            self._stop_thinking()
        self.body.show()
        if self.streamed and self._live:
            self.body.add_text(text)      # only the new text is laid out
        else:
            self.body.set_plain(self.streamed + text, caret=STREAM_CARET)
            self._live = True
        self.streamed += text

    def finish(self, answer: Any) -> None:
        self._stop_thinking()
        if self._discarding:
            self._discarding = False
            self.body.setGraphicsEffect(None)
        self.answer = answer
        self._live = False
        self.body.show()
        error = getattr(answer, "error", None)
        text = (getattr(answer, "text", "") or "").strip()
        if error and not getattr(answer, "sources", None) and not self.streamed.strip():
            self.body.setObjectName("botError")
            self.body.set_plain(text or f"The model failed: {error}")
            repolish(self.body)
        else:
            self.body.set_html(markdown_html(text or NO_ANSWER))
        notes = [n for n in getattr(answer, "notes", None) or [] if n]
        if notes:
            self.notes.set_plain("\n".join(notes))
            self.notes.show()
        if hasattr(answer, "cited_sources"):
            # only what the answer itself cited (chat.Answer), at most a handful
            self._set_sources(list(answer.cited_sources or []),
                              int(getattr(answer, "cited_more_count", 0) or 0))
        else:
            self._set_sources(getattr(answer, "sources", None) or [])
        self.meta.setText(" · ".join(self.meta_bits(answer)))

    def meta_bits(self, answer: Any) -> list[str]:
        """Model, tier, reasoning time and duration for the reply's meta line."""
        bits = []
        if getattr(answer, "model", None):
            bits.append(str(answer.model))
        tier = getattr(answer, "tier", None)
        if tier == "fallback" or self.fallback:
            bits.append("fallback")
        elif (tier == "deep" and getattr(answer, "escalated", False)) or self.deep:
            bits.append("deep")
        thought = getattr(answer, "thinking_s", None)
        if thought is None:
            thought = self.thought_s
        if t := thought_text(thought, self.thinking_cut):
            bits.append(t)
        dur = getattr(answer, "duration", None)
        if isinstance(dur, (int, float)) and dur > 0:
            bits.append(f"{dur:.0f} s" if dur >= 10 else f"{dur:.1f} s")
        return bits

    def fail(self, message: str) -> None:
        self._stop_thinking()
        self._live = False
        self.body.setObjectName("botError")
        self.body.set_plain(message)
        repolish(self.body)
        self.body.show()

    def _set_sources(self, sources: list, more: int = 0) -> None:
        _clear(self.chips)
        self.chip_buttons = []
        self.more_chip: QPushButton | None = None
        for s in sources:
            ref = getattr(s, "ref", str(s))
            label = getattr(s, "label", "") or ref
            url = getattr(s, "url", None)
            b = QPushButton()
            b.setObjectName("srcChip")
            b.setText(elide_chip(b, chip_text(ref, label)))
            b.setToolTip(label + (f"\n{url}" if url else ""))
            if url:
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.clicked.connect(lambda _=False, u=url: QDesktopServices.openUrl(QUrl(u)))
            self.chips.addWidget(b)
            self.chip_buttons.append(b)
        if more > 0 and sources:
            m = QPushButton(f"+{more} more")
            m.setObjectName("srcChip")
            m.setProperty("more", "true")
            m.setToolTip(f"The answer cited {more} more source{'s' if more != 1 else ''}.")
            self.chips.addWidget(m)
            self.more_chip = m
        self.chips_box.setVisible(bool(sources))


# ---------------------------------------------------------------- composer


class Composer(QFrame):
    submitted = Signal(str)

    MIN_LINES, MAX_LINES = 1, 6

    def __init__(self, parent: QWidget | None = None, tokens: Tokens | None = None):
        super().__init__(parent)
        self.setObjectName("composer")
        self.setProperty("focus", "false")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 6, 6)
        lay.setSpacing(6)
        self.edit = QPlainTextEdit()
        self.edit.setObjectName("composerEdit")
        self.edit.setPlaceholderText("Ask about the project…")
        self.edit.setTabChangesFocus(True)
        self.edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.edit.document().setDocumentMargin(2)
        self.edit.installEventFilter(self)
        self.edit.textChanged.connect(self._grow)
        lay.addWidget(self.edit, 1)
        self.mic_btn = vt.MicButton(tokens or LIGHT)
        lay.addWidget(self.mic_btn, 0, Qt.AlignmentFlag.AlignBottom)
        self.send_btn = QPushButton("↑")
        self.send_btn.setObjectName("send")
        self.send_btn.setFixedSize(28, 28)
        self.send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_btn.setToolTip("Send (Enter)")
        self.send_btn.clicked.connect(self.submit)
        lay.addWidget(self.send_btn, 0, Qt.AlignmentFlag.AlignBottom)
        self._busy = False
        self._grow()
        self._update_send()
        self.edit.textChanged.connect(self._update_send)

    def eventFilter(self, obj: QObject, e: QEvent) -> bool:  # noqa: N802 - Qt API
        if obj is self.edit:
            if e.type() == QEvent.Type.KeyPress:
                k: QKeyEvent = e
                if k.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
                        k.modifiers() & Qt.KeyboardModifier.ShiftModifier):
                    self.submit()
                    return True
            elif e.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
                self.setProperty("focus", "true" if e.type() == QEvent.Type.FocusIn else "false")
                repolish(self)
        return super().eventFilter(obj, e)

    def _grow(self) -> None:
        # A QPlainTextEdit document's height is counted in (wrapped) lines.
        lines = round(self.edit.document().size().height()) or 1
        n = max(self.MIN_LINES, min(self.MAX_LINES, lines))
        self.edit.setFixedHeight(int(n * self.edit.fontMetrics().lineSpacing() + 10))

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._update_send()

    def _update_send(self) -> None:
        self.send_btn.setEnabled(not self._busy and bool(self.edit.toPlainText().strip()))

    def submit(self) -> None:
        text = self.edit.toPlainText().strip()
        if not text or self._busy:
            return
        self.edit.clear()
        self.submitted.emit(text)


# ---------------------------------------------------------------- panel


class _Silent:
    """A speaker that says nothing (speech output failed to start)."""
    speaking = False

    def say(self, text: str) -> None:
        pass


class _ChatBridge(QObject):
    token = Signal(int, str)
    phase = Signal(int, str, object)        # escalation event name, info dict
    finished = Signal(int, object)
    failed = Signal(int, str)


class _VoiceBridge(QObject):
    """Worker-thread callbacks of a voice turn, delivered on the GUI thread.
    The int is the voice turn id (stale events are dropped)."""
    mic_level = Signal(int, float)
    recorded = Signal(int, object)          # wav bytes or None (no speech)
    transcribed = Signal(int, str)
    answered = Signal(int, object)          # chat.Answer
    failed = Signal(int, str, str)          # stage ("record", "stt", "answer"), message
    speaker_state = Signal(int, str)
    speaker_level = Signal(int, float)
    speaker_error = Signal(int, str)


def _emit(sig, *args) -> None:
    try:
        sig.emit(*args)
    except RuntimeError:  # the window closed while the model was still talking
        pass


def _phase_kw(fn: Callable[..., Any], mode: str, rid: int, bridge: _ChatBridge) -> dict:
    return accepted_kw(fn, mode=mode,
                       on_phase=lambda name, info: _emit(bridge.phase, rid, str(name),
                                                         dict(info or {})))


def _run_ask(cfg: Config, ask_fn: AskFn, question: str, history: list, session_id: str,
             rid: int, bridge: _ChatBridge, mode: str = "auto") -> None:
    conn = None
    try:
        conn = db.connect(cfg.db_path)
        answer = ask_fn(conn, cfg, question, history,
                        lambda t: _emit(bridge.token, rid, t), session_id,
                        **_phase_kw(ask_fn, mode, rid, bridge))
    except Exception as e:  # noqa: BLE001 - shown inline, never crashes the window
        _emit(bridge.failed, rid, f"Something went wrong: {type(e).__name__}: {e}")
    else:
        _emit(bridge.finished, rid, answer)
    finally:
        if conn is not None:
            conn.close()


def _run_record(recorder: Any, vid: int, bridge: _VoiceBridge) -> None:
    from jarvis.voice import VoiceError

    try:
        wav = recorder.record()
    except VoiceError as e:
        _emit(bridge.failed, vid, "record", str(e))
    except Exception as e:  # noqa: BLE001 - shown inline
        _emit(bridge.failed, vid, "record", f"Recording failed: {type(e).__name__}: {e}")
    else:
        _emit(bridge.recorded, vid, wav)


def _run_transcribe(fn: TranscribeFn, wav: bytes, cfg: Config, vid: int,
                    bridge: _VoiceBridge) -> None:
    from jarvis.voice import VoiceError

    try:
        text = fn(wav, cfg)
    except VoiceError as e:
        _emit(bridge.failed, vid, "stt", str(e))
    except Exception as e:  # noqa: BLE001
        _emit(bridge.failed, vid, "stt", f"Speech-to-text failed: {type(e).__name__}: {e}")
    else:
        _emit(bridge.transcribed, vid, str(text or ""))


def _run_voice_answer(cfg: Config, fn: VoiceAnswerFn, question: str, history: list,
                      session_id: str, speaker: Any, rid: int, vid: int,
                      chat_bridge: _ChatBridge, bridge: _VoiceBridge,
                      mode: str = "auto") -> None:
    conn = None
    try:
        conn = db.connect(cfg.db_path)
        answer = fn(conn, cfg, question, history, speaker,
                    lambda t: _emit(chat_bridge.token, rid, t), session_id,
                    **_phase_kw(fn, mode, rid, chat_bridge))
    except Exception as e:  # noqa: BLE001
        _emit(bridge.failed, vid, "answer", f"Something went wrong: {type(e).__name__}: {e}")
    else:
        _emit(bridge.answered, vid, answer)
    finally:
        if conn is not None:
            conn.close()


class ChatPanel(QFrame):
    """Right-side panel. ``ask_fn`` and the voice seams are injectable for tests."""

    answered = Signal(object)
    close_requested = Signal()
    voice_state_changed = Signal(str)

    def __init__(self, cfg: Config, tokens: Tokens, ask_fn: AskFn | None = None,
                 model_name: str | None = None, parent: QWidget | None = None, *,
                 recorder_factory: RecorderFactory | None = None,
                 transcribe_fn: TranscribeFn | None = None,
                 voice_answer_fn: VoiceAnswerFn | None = None,
                 speaker_factory: SpeakerFactory | None = None,
                 mic_ok: bool | None = None, settings: Any = None):
        super().__init__(parent)
        self.setObjectName("chat")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.cfg = cfg
        self.tokens = tokens
        self.ask_fn = ask_fn or default_ask
        self.session_id = new_session_id()
        self.history: list[tuple[str, str]] = []
        self.messages: list[QWidget] = []
        self._busy = False
        self._rid = 0
        self._current: BotMessage | None = None
        self._thread: threading.Thread | None = None
        self._bridge = _ChatBridge(self)
        self._bridge.token.connect(self._on_token)
        self._bridge.phase.connect(self._on_phase)
        self._bridge.finished.connect(self._on_finished)
        self._bridge.failed.connect(self._on_failed)
        self.setFixedWidth(PANEL_WIDTH)

        # voice
        self.recorder_factory = recorder_factory or default_recorder
        self.transcribe_fn = transcribe_fn or default_transcribe
        self.voice_answer_fn = voice_answer_fn or default_voice_answer
        self.speaker_factory = speaker_factory or default_speaker
        self.mic_ok = mic_available() if mic_ok is None else bool(mic_ok)
        self.settings = settings if settings is not None else vt.ui_settings()
        self.turn = vt.VoiceTurn()
        self._deep = False                 # the current turn reached the deep tier
        self._vid = 0                      # current voice turn id
        self._speech_turn = 0              # turn whose speaker events count (0 = none)
        self._recorder: Any = None
        self._speaker: Any = None          # one Speaker per panel, made on first use
        self._turn_speaker: vt.TurnSpeaker | None = None
        self._voice_threads: list[threading.Thread] = []
        self._voice_error = False
        self._closed = False
        self._vbridge = _VoiceBridge(self)
        vb = self._vbridge
        vb.mic_level.connect(self._on_mic_level)
        vb.recorded.connect(self._on_recorded)
        vb.transcribed.connect(self._on_transcribed)
        vb.answered.connect(self._on_voice_answer)
        vb.failed.connect(self._on_voice_failed)
        vb.speaker_state.connect(self._on_speaker_state)
        vb.speaker_level.connect(self._on_speaker_level)
        vb.speaker_error.connect(self._on_speaker_error)
        self._unstage_timer = QTimer(self, singleShot=True)
        self._unstage_timer.timeout.connect(self._unstage_if_idle)
        self._rearm_timer = QTimer(self, singleShot=True, interval=AUTO_LISTEN_DELAY_MS)
        self._rearm_timer.timeout.connect(self._rearm)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        head = QWidget()
        head.setMinimumHeight(46)  # grows if the orb is expanded
        hl = QHBoxLayout(head)
        hl.setContentsMargins(12, 0, 10, 0)
        hl.setSpacing(8)
        # Jarvis's presence: the arc-reactor orb (idle here; the voice flow drives it)
        self.orb = Orb(tokens, COMPACT, "idle")
        self.orb.setToolTip("Jarvis")
        hl.addWidget(self.orb)
        self._head_lay = hl
        # title over the model name, so the title never gets squeezed
        names = QVBoxLayout()
        names.setContentsMargins(0, 6, 0, 6)
        names.setSpacing(0)
        self.title = _label("Ask Jarvis", "chatTitle")
        self.title.setFont(theme.display_font(17, 800, 82, caps=True, spacing_em=0.01))
        self.title.setMinimumWidth(self.title.sizeHint().width())
        names.addWidget(self.title)
        self.sub = _label(model_name or cfg.model, "chatSub")
        self.sub.setFont(theme.mono_font(10, caps=False, spacing_em=0.04))
        self.sub.setToolTip("Answers are written by the local model from the synced repo data "
                            "only; numbers and references are checked afterwards.")
        names.addWidget(self.sub)
        hl.addLayout(names)
        hl.addStretch(1)
        self.think_btn = QPushButton("Think")
        self.think_btn.setObjectName("thinkToggle")
        self.think_btn.setCheckable(True)
        self.think_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.think_btn.setToolTip(THINK_TOOLTIP)
        self.think_btn.setChecked(vt.read_bool(self.settings, THINK_KEY))
        self.think_btn.toggled.connect(self.set_think_mode)
        hl.addWidget(self.think_btn)
        self._think_shortcut = QShortcut(QKeySequence("Ctrl+T"), self)
        self._think_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self._think_shortcut.activated.connect(self.think_btn.toggle)
        self.menu_btn = QPushButton("⋯")
        self.menu_btn.setObjectName("ghost")
        self.menu_btn.setToolTip("Voice options")
        self.menu_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.menu = QMenu(self.menu_btn)
        self.auto_listen_action = self.menu.addAction(vt.AUTO_LISTEN_LABEL)
        self.auto_listen_action.setCheckable(True)
        self.auto_listen_action.setChecked(vt.read_bool(self.settings, vt.AUTO_LISTEN_KEY))
        self.auto_listen_action.toggled.connect(self.set_auto_listen)
        self.menu_btn.setMenu(self.menu)
        hl.addWidget(self.menu_btn)
        self.new_btn = QPushButton("New")
        self.new_btn.setObjectName("ghost")
        self.new_btn.setToolTip("Start a new conversation")
        self.new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_btn.clicked.connect(self.new_chat)
        hl.addWidget(self.new_btn)
        close = QPushButton("✕")
        close.setObjectName("ghost")
        close.setToolTip("Hide the chat (Ctrl+J)")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self.close_requested)
        hl.addWidget(close)
        outer.addWidget(head)
        line = QFrame()
        line.setObjectName("hairline")
        line.setFixedHeight(1)
        outer.addWidget(line)

        # The orb's stage: in voice mode the header orb moves here and grows.
        self.stage = QWidget()
        stage_col = QVBoxLayout(self.stage)
        stage_col.setContentsMargins(16, 14, 16, 10)
        stage_col.setSpacing(8)
        sl = QHBoxLayout()
        sl.setContentsMargins(0, 0, 0, 0)
        sl.addStretch(1)
        sl.addStretch(1)
        stage_col.addLayout(sl)
        self._stage_lay = sl
        self.caption = _label("", "voiceCaption")
        self.caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.caption.setWordWrap(True)
        stage_col.addWidget(self.caption)
        self.stage.hide()
        self._staged = False
        outer.addWidget(self.stage)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        self.list = QVBoxLayout(holder)
        self.list.setContentsMargins(18, 18, 14, 12)
        self.list.setSpacing(18)
        self.list.addStretch(1)
        self.scroll.setWidget(holder)
        self.scroll.verticalScrollBar().rangeChanged.connect(self._stick_to_bottom)
        self._stick = True
        self.scroll.verticalScrollBar().valueChanged.connect(self._track_stick)
        outer.addWidget(self.scroll, 1)

        self.empty = self._build_empty()
        self.list.insertWidget(0, self.empty)

        foot = QVBoxLayout()
        foot.setContentsMargins(14, 8, 14, 12)
        foot.setSpacing(6)
        self.composer = Composer(tokens=tokens)
        self.composer.submitted.connect(self.ask)
        self.mic_btn = self.composer.mic_btn
        self.mic_btn.clicked.connect(self.toggle_voice)
        if not self.mic_ok:
            self.mic_btn.setEnabled(False)
            from jarvis.voice import NO_PW_RECORD

            self.mic_btn.setToolTip(NO_PW_RECORD)
        foot.addWidget(self.composer)
        foot.addWidget(_label("enter send · shift+enter new line · ctrl+space talk",
                              "composerHint"))
        outer.addLayout(foot)

    # ------------------------------------------------------------ empty state

    def _build_empty(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 18, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(set_font(_label("Ask about the project", "emptyTitle"),
                               theme.display_font(24, 800, 82, caps=True, spacing_em=-0.01)))
        hint = _label("Answers come only from the synced issues, commits, milestones and "
                      "checks, with their sources.", "emptyHint", wrap=True)
        lay.addWidget(hint)
        lay.addSpacing(8)
        self.suggestion_buttons = []
        for q in SUGGESTED:
            b = QPushButton(q)
            b.setObjectName("chip")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            b.clicked.connect(lambda _=False, text=q: self.ask(text))
            lay.addWidget(b)
            self.suggestion_buttons.append(b)
        return w

    # ------------------------------------------------------------ public

    @property
    def orb_staged(self) -> bool:
        """Whether the orb is on its stage (expanded, voice mode)."""
        return self._staged

    def set_orb_staged(self, on: bool, animate: bool = True) -> None:
        """Voice mode: move the orb from the header to a stage above the
        conversation and grow it to ``orb.EXPANDED`` px (animated); ``False``
        shrinks it and puts it back in the header."""
        on = bool(on)
        if on == self._staged:
            return
        self._staged = on
        if on:
            self._head_lay.removeWidget(self.orb)
            self._stage_lay.insertWidget(1, self.orb)
            self.stage.show()
            self.orb.show()
            self.orb.set_expanded(True, animate)
            return

        def back() -> None:
            if self._staged:  # re-staged meanwhile
                return
            self._stage_lay.removeWidget(self.orb)
            self._head_lay.insertWidget(0, self.orb)
            self.orb.show()
            self.stage.hide()

        self.orb.set_expanded(False, animate, on_done=back)

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def think_mode(self) -> bool:
        return self.think_btn.isChecked()

    @property
    def mode(self) -> str:
        """The escalation mode for the next question: "think" or "auto"."""
        return "think" if self.think_mode else "auto"

    def set_think_mode(self, on: bool) -> None:
        """THINK toggle / Ctrl+T: answer with the reasoning model straight away.
        Remembered across restarts; applies from the next question."""
        on = bool(on)
        if self.think_btn.isChecked() != on:
            self.think_btn.setChecked(on)  # re-enters via toggled
            return
        self.settings.setValue(THINK_KEY, on)
        self.settings.sync()

    def focus_input(self) -> None:
        self.composer.edit.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def new_chat(self) -> None:
        if self.turn_busy:
            return
        for m in self.messages:
            self.list.removeWidget(m)
            m.setParent(None)
            m.deleteLater()
        self.messages = []
        self.history = []
        self.session_id = new_session_id()
        self.empty.show()
        self.focus_input()

    def ask(self, question: str) -> None:
        question = (question or "").strip()
        if not question or self.turn_busy:
            return
        self.empty.hide()
        self._busy = True
        self._sync_controls()
        self._add(UserMessage(question))
        bot = BotMessage(self.tokens)
        self._current = bot
        self._add(bot)
        self._stick = True
        self._rid += 1
        self._question = question
        self._deep = False
        self._thread = threading.Thread(
            target=_run_ask, name="jarvis-chat", daemon=True,
            args=(self.cfg, self.ask_fn, question, list(self.history), self.session_id,
                  self._rid, self._bridge, self.mode))
        self._thread.start()

    def wait(self, timeout: float = 10.0) -> None:
        """Block until the current answer is shown (tests)."""
        from PySide6.QtWidgets import QApplication

        if self._thread is not None:
            self._thread.join(timeout)
        for _ in range(100):
            QApplication.instance().processEvents()
            if not self._busy:
                break

    # ------------------------------------------------------------ internals

    def _add(self, w: QWidget) -> None:
        self.messages.append(w)
        self.list.insertWidget(self.list.count() - 1, w)
        motion.fade_rise(w, 10, motion.BASE + 40)

    def _track_stick(self, value: int) -> None:
        bar = self.scroll.verticalScrollBar()
        self._stick = value >= bar.maximum() - 24

    def _stick_to_bottom(self, _min: int, maximum: int) -> None:
        if self._stick:
            self.scroll.verticalScrollBar().setValue(maximum)

    def _on_token(self, rid: int, text: str) -> None:
        if rid == self._rid and self._current is not None:
            self._current.add_token(text)

    def _on_phase(self, rid: int, name: str, info: Any) -> None:
        if rid != self._rid or self._current is None:
            return
        self._current.phase(name, info if isinstance(info, dict) else {})
        if name == "deep":
            self._deep = True
            if self.turn.state != vt.IDLE:
                self._render_voice()

    def _done(self) -> None:
        self._busy = False
        self._current = None
        self._sync_controls()

    def _on_finished(self, rid: int, answer: Any) -> None:
        if rid != self._rid or self._current is None:
            return
        self._current.finish(answer)
        if not getattr(answer, "error", None):
            self.history += [("user", self._question),
                             ("assistant", getattr(answer, "text", "") or "")]
        self._done()
        self.answered.emit(answer)

    def _on_failed(self, rid: int, message: str) -> None:
        if rid != self._rid or self._current is None:
            return
        self._current.fail(message)
        self._done()

    # ------------------------------------------------------------ voice: public

    @property
    def voice_state(self) -> str:
        return self.turn.state

    @property
    def voice_active(self) -> bool:
        """Listening, transcribing, or a turn whose speech hasn't been cancelled."""
        return self.turn.active

    @property
    def turn_busy(self) -> bool:
        """A typed answer is streaming or a voice turn is under way."""
        return self._busy or self.turn.state != vt.IDLE

    @property
    def auto_listen(self) -> bool:
        return self.auto_listen_action.isChecked()

    def set_auto_listen(self, on: bool) -> None:
        on = bool(on)
        if self.auto_listen_action.isChecked() != on:
            self.auto_listen_action.setChecked(on)  # re-enters via toggled
            return
        self.settings.setValue(vt.AUTO_LISTEN_KEY, on)
        self.settings.sync()

    def toggle_voice(self) -> None:
        """Mic button / Ctrl+Space: start listening, or act on the current turn
        (stop recording, cancel, or barge in on the speech)."""
        self._voice_key("mic")

    def stop_voice(self) -> bool:
        """Esc: stop whatever voice is doing. False when there was nothing to stop
        (so Esc can do its usual job)."""
        if not self.turn.active:
            return False
        self._voice_key("esc")
        return True

    def start_listening(self) -> bool:
        if (self._closed or not self.mic_ok or self._busy
                or self.turn.state != vt.IDLE):
            return False
        self._rearm_timer.stop()
        self._unstage_timer.stop()
        self._vid += 1
        vid = self._vid
        self._voice_error = False
        self._fire("start")
        self.set_orb_staged(True)
        self.orb.set_level(0.0)
        bridge = self._vbridge
        try:
            self._recorder = self.recorder_factory(
                lambda level: _emit(bridge.mic_level, vid, float(level)))
        except Exception as e:  # noqa: BLE001
            self._on_voice_failed(vid, "record", str(e))
            return False
        self._spawn(_run_record, "jarvis-voice-rec", self._recorder, vid, bridge)
        return True

    def shutdown(self) -> None:
        """Stop the mic and the speaker and wait for the voice workers (window close)."""
        if self._closed:
            return
        self._closed = True
        self._vid += 1                     # every pending voice event is now stale
        self._speech_turn = 0
        self._rearm_timer.stop()
        self._unstage_timer.stop()
        rec, self._recorder = self._recorder, None
        if rec is not None:
            try:
                rec.stop()
            except Exception:  # noqa: BLE001
                pass
        if self._turn_speaker is not None:
            self._turn_speaker.mute()
        speaker, self._speaker = self._speaker, None
        if speaker is not None:
            try:
                speaker.close()
            except Exception:  # noqa: BLE001
                pass
        deadline = time.monotonic() + 3.0
        for t in self._voice_threads:
            t.join(max(0.0, deadline - time.monotonic()))
        self._voice_threads = [t for t in self._voice_threads if t.is_alive()]

    def voice_threads(self) -> list[threading.Thread]:
        """Voice worker threads still running (tests)."""
        return [t for t in self._voice_threads if t.is_alive()]

    def closeEvent(self, e) -> None:  # noqa: N802 - Qt API
        self.shutdown()
        super().closeEvent(e)

    # ------------------------------------------------------------ voice: internals

    def _spawn(self, target: Callable[..., None], name: str, *args: Any) -> None:
        self._voice_threads = [t for t in self._voice_threads if t.is_alive()]
        t = threading.Thread(target=target, name=name, daemon=True, args=args)
        self._voice_threads.append(t)
        t.start()

    def _fire(self, event: str) -> None:
        state = self.turn.fire(event)
        self._render_voice()
        self.voice_state_changed.emit(state)

    def _render_voice(self) -> None:
        """Orb, caption and controls for the current voice state."""
        state = self.turn.state
        if state != vt.IDLE and not self.turn.muted:
            deep = self._deep and state == vt.THINKING
            self.orb.set_state({vt.LISTENING: "listening", vt.SPEAKING: "speaking"}
                               .get(state, "deep" if deep else "thinking"))
            self.caption.setText(vt.DEEP_CAPTION if deep else vt.CAPTIONS.get(state, ""))
            self.caption.show()
        else:
            self.caption.hide()
            self.orb.set_level(0.0)
            if self.orb.state != "error":
                self.orb.set_state("idle")
        self._sync_controls()

    def _sync_controls(self) -> None:
        busy = self.turn_busy
        self.composer.set_busy(busy)
        self.new_btn.setEnabled(not busy)
        self.mic_btn.set_active(self.turn.state == vt.LISTENING)
        self.mic_btn.setEnabled(self.mic_ok and not self._closed and not self._busy
                                and (self.turn.state == vt.IDLE or self.turn.active))
        if self.mic_ok:
            self.mic_btn.setToolTip("Stop listening (Ctrl+Space)"
                                    if self.turn.state == vt.LISTENING else
                                    "Stop speaking (Esc)" if self.turn.active
                                    and self.turn.state != vt.TRANSCRIBING else vt.MIC_TOOLTIP)

    def _voice_key(self, key: str) -> None:
        action = vt.barge_in_action(self.turn.state, key, self.turn.muted)
        if action == "start":
            self.start_listening()
        elif action == "stop_recording":
            if self._recorder is not None:
                self._recorder.stop()      # record() returns what was said
        elif action == "cancel":
            self._cancel_voice()
        elif action == "mute":
            self._mute_speech()
        elif action == "stop_speech":
            self._mute_speech()
            if self.turn.answer_done:
                self._end_turn(natural=False)

    def _cancel_voice(self) -> None:
        """Drop the turn before a question was asked (listening / transcribing)."""
        self._vid += 1
        rec, self._recorder = self._recorder, None
        if rec is not None:
            rec.stop()
        self._fire("cancel")
        self._schedule_unstage()

    def _mute_speech(self) -> None:
        """No more speech this turn; the text answer still finishes."""
        self.turn.muted = True
        self._speech_turn = 0
        if self._turn_speaker is not None:
            self._turn_speaker.mute()
        if self._speaker is not None:
            self._speaker.stop()
        if self.turn.state == vt.SPEAKING:
            self.turn.fire("speech_paused")
        self._render_voice()
        self._schedule_unstage()

    def _ensure_speaker(self) -> Any:
        if self._speaker is None:
            bridge = self._vbridge
            self._speaker = self.speaker_factory(
                self.cfg,
                lambda level: _emit(bridge.speaker_level, self._speech_turn, float(level)),
                lambda state: _emit(bridge.speaker_state, self._speech_turn, str(state)),
                lambda msg: _emit(bridge.speaker_error, self._speech_turn, str(msg)))
        return self._speaker

    def _note(self, text: str, error: bool = False) -> None:
        self._add(VoiceNote(text, error))
        self._stick = True

    def _schedule_unstage(self, ms: int = UNSTAGE_DELAY_MS) -> None:
        self._unstage_timer.start(ms)

    def _unstage_if_idle(self) -> None:
        if self.turn.state == vt.IDLE or self.turn.muted:
            self.set_orb_staged(False)

    def _end_turn(self, natural: bool) -> None:
        """The voice turn is over (answer shown, speech done or stopped)."""
        self._speech_turn = 0
        self._turn_speaker = None
        if self.turn.state != vt.IDLE:
            self._fire("done")
        if (natural and not self._voice_error and self.auto_listen and self.mic_ok
                and not self._closed):
            self._rearm_timer.start()      # stay staged: the next question follows
        else:
            self._schedule_unstage(UNSTAGE_ERROR_MS if self._voice_error
                                   else UNSTAGE_DELAY_MS)

    def _rearm(self) -> None:
        if not self.start_listening():
            self._schedule_unstage()

    def _voice_error_ui(self, message: str) -> None:
        self._voice_error = True
        self._note(message, error=True)
        self.orb.set_state("error")        # flashes, then settles to idle by itself

    # -- worker events (GUI thread)

    def _on_mic_level(self, vid: int, level: float) -> None:
        if vid == self._vid and self.turn.state == vt.LISTENING:
            self.orb.set_level(level)

    def _on_recorded(self, vid: int, wav: Any) -> None:
        if vid != self._vid or self.turn.state != vt.LISTENING:
            return
        self._recorder = None
        if not wav:
            self._fire("no_speech")
            self._note(vt.NO_SPEECH)
            self._schedule_unstage()
            return
        self._fire("recorded")
        self._spawn(_run_transcribe, "jarvis-voice-stt", self.transcribe_fn, wav, self.cfg,
                    vid, self._vbridge)

    def _on_transcribed(self, vid: int, text: str) -> None:
        if vid != self._vid or self.turn.state != vt.TRANSCRIBING:
            return
        text = (text or "").strip()
        if not text:
            self._fire("no_speech")
            self._note(vt.NO_SPEECH)
            self._schedule_unstage()
            return
        self._fire("transcript")
        self.empty.hide()
        self._add(UserMessage(text, voice=True, tokens=self.tokens))
        bot = BotMessage(self.tokens)
        self._current = bot
        self._add(bot)
        self._stick = True
        self._rid += 1
        self._question = text
        self._deep = False
        try:
            speaker = self._ensure_speaker()
        except Exception as e:  # noqa: BLE001 - no speech, the text answer still comes
            speaker = None
            self._voice_error_ui(f"Speech output is unavailable: {e}")
        self._turn_speaker = vt.TurnSpeaker(speaker if speaker is not None else _Silent())
        if speaker is None:
            self._turn_speaker.mute()
            self.turn.muted = True
            self._render_voice()
        else:
            self._speech_turn = vid
        self._spawn(_run_voice_answer, "jarvis-voice-answer", self.cfg, self.voice_answer_fn,
                    text, list(self.history), self.session_id, self._turn_speaker, self._rid,
                    vid, self._bridge, self._vbridge, "auto")  # Think is typed-only

    def _on_voice_answer(self, vid: int, answer: Any) -> None:
        if vid != self._vid or self.turn.state not in (vt.THINKING, vt.SPEAKING):
            return
        bot = self._current
        self._current = None
        if bot is not None:
            bot.finish(answer)
        if not getattr(answer, "error", None):
            self.history += [("user", self._question),
                             ("assistant", getattr(answer, "text", "") or "")]
        self.turn.answer_done = True
        self.answered.emit(answer)
        speaking = self._turn_speaker is not None and self._turn_speaker.speaking
        if self.turn.muted or not speaking:
            self._end_turn(natural=not self.turn.muted and not getattr(answer, "error", None))
        else:
            self._sync_controls()          # wait for the speaker to go idle

    def _on_voice_failed(self, vid: int, stage: str, message: str) -> None:
        if vid != self._vid or self.turn.state == vt.IDLE:
            return
        self._recorder = None
        if stage == "answer":
            bot, self._current = self._current, None
            if bot is not None:
                bot.fail(message)
            if self._turn_speaker is not None:
                self._turn_speaker.mute()
            if self._speaker is not None:
                self._speaker.stop()
            self._speech_turn = 0
            self._turn_speaker = None
            self._voice_error = True
            self.orb.set_state("error")
            self._fire("error")
        else:
            self._fire("error")
            self._voice_error_ui(message)
        self._schedule_unstage(UNSTAGE_ERROR_MS)

    def _on_speaker_state(self, sid: int, state: str) -> None:
        if not sid or sid != self._speech_turn or self.turn.muted:
            return
        if state == "speaking" and self.turn.state == vt.THINKING:
            self._fire("speech_started")
        elif state == "idle":
            if self.turn.answer_done:
                self._end_turn(natural=True)
            elif self.turn.state == vt.SPEAKING:
                self._fire("speech_paused")   # waiting for the model's next sentence

    def _on_speaker_level(self, sid: int, level: float) -> None:
        if sid and sid == self._speech_turn and self.turn.state == vt.SPEAKING:
            self.orb.set_level(level)

    def _on_speaker_error(self, sid: int, message: str) -> None:
        if not sid or sid != self._speech_turn:
            return
        self._mute_speech()
        self._voice_error_ui(message)
        if self.turn.answer_done:
            self._end_turn(natural=False)

    def set_tokens(self, tokens: Tokens) -> None:
        self.tokens = tokens
        self.orb.set_tokens(tokens)
        self.mic_btn.set_tokens(tokens)
        for m in self.messages:
            if isinstance(m, BotMessage):
                m.tokens = tokens
                m.thinking_orb.set_tokens(tokens)
            elif isinstance(m, UserMessage) and m.glyph is not None:
                m.glyph.set_tokens(tokens)


__all__ = ["ChatPanel", "BotMessage", "UserMessage", "VoiceNote", "SUGGESTED", "chip_text", "QLabel"]
