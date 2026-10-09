"""The "Ask Jarvis" side panel: a chat over the synced project data.

The engine is ``jarvis.chat.ask`` (grounded context, streamed reply, validated
sources); this module is only its UI. A question runs on a background thread
with its own SQLite connection; streamed tokens come back through a queued Qt
signal. One question at a time: Send is disabled while Jarvis answers.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QKeyEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
    QWidgetItem,
)

from jarvis import db
from jarvis.config import Config
from jarvis.gui import motion
from jarvis.gui.orb import COMPACT, Orb
from jarvis.gui.theme import Tokens
from jarvis.gui.widgets import _clear, _label, repolish

PANEL_WIDTH = 380
SUGGESTED = (
    "What's left for Core demo?",
    "Is my 5V rail OK?",
    "What did I work on this week?",
)
THINKING_SHOW_SECONDS_AFTER = 3
THINKING_ORB = 20  # px, the orb next to "thinking…" in a reply

# ask_fn(conn, cfg, question, history, on_token, session_id) -> chat.Answer
AskFn = Callable[[sqlite3.Connection, Config, str, list, Callable[[str], None], str], Any]


def default_ask(conn: sqlite3.Connection, cfg: Config, question: str, history: list,
                on_token: Callable[[str], None], session_id: str) -> Any:
    from jarvis.chat import ask

    return ask(conn, cfg, question, history, None, on_token=on_token, session_id=session_id)


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
    return rest


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


# ---------------------------------------------------------------- messages


class UserMessage(QWidget):
    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(40, 0, 0, 0)
        lay.addStretch(1)
        bubble = QFrame()
        bubble.setObjectName("userBubble")
        bl = QVBoxLayout(bubble)
        bl.setContentsMargins(12, 8, 12, 9)
        self.label = _label(text, "userText")
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        # Short questions stay on one line; long ones wrap inside the bubble.
        if self.label.fontMetrics().horizontalAdvance(text) > PANEL_WIDTH - 110:
            self.label.setWordWrap(True)
            self.label.setMinimumWidth(PANEL_WIDTH - 130)
        bl.addWidget(self.label)
        lay.addWidget(bubble)
        self.text = text


class BotMessage(QWidget):
    """Jarvis's reply: thinking state, streamed text, then the validated answer."""

    def __init__(self, tokens: Tokens, parent: QWidget | None = None):
        super().__init__(parent)
        self.tokens = tokens
        self.streamed = ""
        self.answer = None
        self.started = time.monotonic()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 8, 0)
        lay.setSpacing(5)
        head = QHBoxLayout()
        head.setSpacing(6)
        self.name = _label("✦ Jarvis", "botName")
        head.addWidget(self.name)
        self.meta = _label("", "botMeta")
        head.addWidget(self.meta)
        head.addStretch(1)
        lay.addLayout(head)
        # thinking indicator: a small orb in its thinking state + elapsed seconds
        self.thinking = QWidget()
        tl = QHBoxLayout(self.thinking)
        tl.setContentsMargins(0, 2, 0, 2)
        tl.setSpacing(8)
        self.thinking_orb = Orb(tokens, THINKING_ORB, "thinking")
        tl.addWidget(self.thinking_orb)
        self.thinking_text = _label("thinking…", "thinking")
        tl.addWidget(self.thinking_text)
        tl.addStretch(1)
        lay.addWidget(self.thinking)
        self.body = _label("", "botText", wrap=True)
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                          | Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.body.setOpenExternalLinks(True)
        self.body.hide()
        lay.addWidget(self.body)
        self.notes = _label("", "botNote", wrap=True)
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

    def _tick(self) -> None:
        secs = int(time.monotonic() - self.started)
        extra = f"  ·  {secs} s" if secs >= THINKING_SHOW_SECONDS_AFTER else ""
        self.thinking_text.setText("thinking…" + extra)

    def _stop_thinking(self) -> None:
        self._timer.stop()
        self.thinking.hide()  # hiding stops the orb's frame timer too
        self.thinking_orb.set_state("idle")

    def add_token(self, text: str) -> None:
        if not text:
            return
        if not self.thinking.isHidden():
            self._stop_thinking()
        self.body.show()
        self.streamed += text
        self.body.setTextFormat(Qt.TextFormat.PlainText)
        self.body.setText(self.streamed + " ▍")

    def finish(self, answer: Any) -> None:
        self._stop_thinking()
        self.answer = answer
        self.body.show()
        error = getattr(answer, "error", None)
        text = (getattr(answer, "text", "") or "").strip()
        if error and not getattr(answer, "sources", None) and not self.streamed.strip():
            self.body.setObjectName("botError")
            self.body.setTextFormat(Qt.TextFormat.PlainText)
            self.body.setText(text or f"The model failed: {error}")
            repolish(self.body)
        else:
            self.body.setTextFormat(Qt.TextFormat.RichText)
            self.body.setText(markdown_html(text or "(no answer)"))
        notes = [n for n in getattr(answer, "notes", None) or [] if n]
        if notes:
            self.notes.setText("\n".join(notes))
            self.notes.show()
        self._set_sources(getattr(answer, "sources", None) or [])
        bits = []
        if getattr(answer, "model", None):
            bits.append(str(answer.model))
        dur = getattr(answer, "duration", None)
        if isinstance(dur, (int, float)) and dur > 0:
            bits.append(f"{dur:.0f} s" if dur >= 10 else f"{dur:.1f} s")
        self.meta.setText(" · ".join(bits))

    def fail(self, message: str) -> None:
        self._stop_thinking()
        self.body.setObjectName("botError")
        self.body.setTextFormat(Qt.TextFormat.PlainText)
        self.body.setText(message)
        repolish(self.body)
        self.body.show()

    def _set_sources(self, sources: list) -> None:
        _clear(self.chips)
        self.chip_buttons = []
        for s in sources:
            ref = getattr(s, "ref", str(s))
            label = getattr(s, "label", "") or ref
            url = getattr(s, "url", None)
            b = QPushButton(chip_text(ref, label))
            b.setObjectName("srcChip")
            b.setToolTip(label + (f"\n{url}" if url else ""))
            if url:
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.clicked.connect(lambda _=False, u=url: QDesktopServices.openUrl(QUrl(u)))
            self.chips.addWidget(b)
            self.chip_buttons.append(b)
        self.chips_box.setVisible(bool(sources))


# ---------------------------------------------------------------- composer


class Composer(QFrame):
    submitted = Signal(str)

    MIN_LINES, MAX_LINES = 1, 6

    def __init__(self, parent: QWidget | None = None):
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


class _ChatBridge(QObject):
    token = Signal(int, str)
    finished = Signal(int, object)
    failed = Signal(int, str)


def _emit(sig, *args) -> None:
    try:
        sig.emit(*args)
    except RuntimeError:  # the window closed while the model was still talking
        pass


def _run_ask(cfg: Config, ask_fn: AskFn, question: str, history: list, session_id: str,
             rid: int, bridge: _ChatBridge) -> None:
    conn = None
    try:
        conn = db.connect(cfg.db_path)
        answer = ask_fn(conn, cfg, question, history,
                        lambda t: _emit(bridge.token, rid, t), session_id)
    except Exception as e:  # noqa: BLE001 - shown inline, never crashes the window
        _emit(bridge.failed, rid, f"Something went wrong: {type(e).__name__}: {e}")
    else:
        _emit(bridge.finished, rid, answer)
    finally:
        if conn is not None:
            conn.close()


class ChatPanel(QFrame):
    """Right-side panel. ``ask_fn`` is injectable for tests."""

    answered = Signal(object)
    close_requested = Signal()

    def __init__(self, cfg: Config, tokens: Tokens, ask_fn: AskFn | None = None,
                 model_name: str | None = None, parent: QWidget | None = None):
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
        self._bridge.finished.connect(self._on_finished)
        self._bridge.failed.connect(self._on_failed)
        self.setFixedWidth(PANEL_WIDTH)

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
        hl.addWidget(_label("Ask Jarvis", "chatTitle"))
        self.sub = _label(model_name or cfg.model, "chatSub")
        self.sub.setToolTip("Answers are written by the local model from the synced repo data "
                            "only; numbers and references are checked afterwards.")
        hl.addWidget(self.sub)
        hl.addStretch(1)
        self.new_btn = QPushButton("New chat")
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
        sl = QHBoxLayout(self.stage)
        sl.setContentsMargins(0, 14, 0, 6)
        sl.addStretch(1)
        sl.addStretch(1)
        self._stage_lay = sl
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
        self.composer = Composer()
        self.composer.submitted.connect(self.ask)
        foot.addWidget(self.composer)
        foot.addWidget(_label("Enter to send · Shift+Enter for a new line", "composerHint"))
        outer.addLayout(foot)

    # ------------------------------------------------------------ empty state

    def _build_empty(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 18, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(_label("Ask about the project", "emptyTitle"))
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

    def focus_input(self) -> None:
        self.composer.edit.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def new_chat(self) -> None:
        if self._busy:
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
        if not question or self._busy:
            return
        self.empty.hide()
        self._busy = True
        self.composer.set_busy(True)
        self.new_btn.setEnabled(False)
        self._add(UserMessage(question))
        bot = BotMessage(self.tokens)
        self._current = bot
        self._add(bot)
        self._stick = True
        self._rid += 1
        self._question = question
        self._thread = threading.Thread(
            target=_run_ask, name="jarvis-chat", daemon=True,
            args=(self.cfg, self.ask_fn, question, list(self.history), self.session_id,
                  self._rid, self._bridge))
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

    def _done(self) -> None:
        self._busy = False
        self._current = None
        self.composer.set_busy(False)
        self.new_btn.setEnabled(True)

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

    def set_tokens(self, tokens: Tokens) -> None:
        self.tokens = tokens
        self.orb.set_tokens(tokens)
        for m in self.messages:
            if isinstance(m, BotMessage):
                m.tokens = tokens
                m.thinking_orb.set_tokens(tokens)


__all__ = ["ChatPanel", "BotMessage", "UserMessage", "SUGGESTED", "chip_text", "QLabel"]
