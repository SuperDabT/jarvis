"""Quick capture (Ctrl+N): one line of plain words -> a task, idea or reminder.

A floating box over the window. Enter sends the text to ``jarvis.capture`` on a
worker thread (its own SQLite connection, like the chat). Then:

- saved: the summary line shows what was stored ("Saved task “…” · due Thu Oct
  15"); the input clears for the next one.
- clarify: the question and its options appear; the original text stays shown
  and is sent again with the answer (an option, or typed into the same input).
- error: the reason, with Try again and Save as typed (a plain task, no model).

Esc closes it (the window gives it first claim on Esc).
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from jarvis import db
from jarvis.config import Config
from jarvis.gui.widgets import _clear, _label, repolish

# capture_fn(cfg, text, answers) and plain_fn(cfg, text) -> capture.CaptureResult
CaptureFn = Callable[[Config, str, tuple], Any]
PlainFn = Callable[[Config, str], Any]

PLACEHOLDER = "Add a task, idea or reminder… e.g. “remind me to buy cat food tomorrow at 5pm”"
ANSWER_PLACEHOLDER = "Type an answer, or pick one above"
BUSY_TEXT = "Reading it…"
HINT = "enter save · esc close"
WIDTH = 640


def default_capture(cfg: Config, text: str, answers: tuple) -> Any:
    from jarvis import capture as cap

    conn = db.connect(cfg.db_path)
    try:
        with cap.make_client(cfg) as client:
            return cap.capture(conn, text, client, answers=answers)
    finally:
        conn.close()


def default_plain(cfg: Config, text: str) -> Any:
    from jarvis import capture as cap

    conn = db.connect(cfg.db_path)
    try:
        return cap.capture_plain(conn, text)
    finally:
        conn.close()


class _Bridge(QObject):
    done = Signal(int, object)   # request id, CaptureResult
    failed = Signal(int, str)


def _run(fn: Callable[[], Any], rid: int, bridge: _Bridge) -> None:
    try:
        result = fn()
    except Exception as e:  # noqa: BLE001 - shown in the box, never crashes the window
        try:
            bridge.failed.emit(rid, f"{type(e).__name__}: {e}")
        except RuntimeError:
            pass
    else:
        try:
            bridge.done.emit(rid, result)
        except RuntimeError:  # the window closed meanwhile
            pass


class CaptureOverlay(QFrame):
    captured = Signal(object)    # a saved CaptureResult
    closed = Signal()

    def __init__(self, cfg: Config, capture_fn: CaptureFn | None = None,
                 plain_fn: PlainFn | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("capture")
        self.cfg = cfg
        self.capture_fn = capture_fn or default_capture
        self.plain_fn = plain_fn or default_plain
        self.state = "idle"          # idle / busy / clarify / error / saved
        self.pending = None          # the CaptureResult that asked a question
        self.text = ""               # what was typed (kept across a clarification)
        self.last = None             # the last CaptureResult
        self._rid = 0
        self._thread: threading.Thread | None = None
        self._bridge = _Bridge(self)
        self._bridge.done.connect(self._on_done)
        self._bridge.failed.connect(self._on_failed)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)
        head = QHBoxLayout()
        head.addWidget(_label("Quick capture", "captureHead"))
        head.addStretch(1)
        head.addWidget(_label(HINT, "captureHint"))
        lay.addLayout(head)
        self.original = _label("", "captureOriginal")
        self.original.setWordWrap(True)
        self.original.hide()
        lay.addWidget(self.original)
        self.question = _label("", "captureQuestion")
        self.question.setWordWrap(True)
        self.question.hide()
        lay.addWidget(self.question)
        self.options_box = QWidget()
        self.options_row = QHBoxLayout(self.options_box)
        self.options_row.setContentsMargins(0, 0, 0, 0)
        self.options_row.setSpacing(6)
        self.options_box.hide()
        lay.addWidget(self.options_box)
        self.input = QLineEdit()
        self.input.setObjectName("captureEdit")
        self.input.setPlaceholderText(PLACEHOLDER)
        self.input.returnPressed.connect(self.submit)
        lay.addWidget(self.input)
        self.status = _label("", "captureStatus")
        self.status.setWordWrap(True)
        self.status.hide()
        lay.addWidget(self.status)
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.retry_btn = QPushButton("Try again")
        self.retry_btn.clicked.connect(self.retry)
        self.plain_btn = QPushButton("Save as typed")
        self.plain_btn.clicked.connect(self.save_plain)
        for b in (self.retry_btn, self.plain_btn):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.hide()
            actions.addWidget(b)
        lay.addLayout(actions)
        self.option_buttons: list[QPushButton] = []
        self.hide()

    # ------------------------------------------------------------ open / close

    def open(self) -> None:
        self.show()
        self.raise_()
        self.input.setFocus()
        self.input.selectAll()

    def close_overlay(self) -> None:
        self.hide()
        self.closed.emit()

    @property
    def busy(self) -> bool:
        return self.state == "busy"

    # ------------------------------------------------------------ actions

    def submit(self) -> None:
        typed = self.input.text().strip()
        if self.busy or not typed:
            return
        if self.state == "clarify" and self.pending is not None:
            self.answer(typed)
            return
        self.text = typed
        self._start(lambda: self.capture_fn(self.cfg, typed, ()))

    def answer(self, reply: str) -> None:
        """Answer the open question (an option's text or typed words)."""
        if self.busy or self.pending is None:
            return
        answers = self.pending.answered(reply)
        text = self.text
        self._start(lambda: self.capture_fn(self.cfg, text, answers))

    def retry(self) -> None:
        if self.busy or not self.text:
            return
        answers = self.last.answers if self.last is not None else ()
        text = self.text
        self._start(lambda: self.capture_fn(self.cfg, text, answers))

    def save_plain(self) -> None:
        if self.busy or not self.text:
            return
        text = self.text
        self._start(lambda: self.plain_fn(self.cfg, text))

    def wait(self, timeout: float = 10.0) -> None:
        """Block until the running capture is shown (tests)."""
        from PySide6.QtWidgets import QApplication

        if self._thread is not None:
            self._thread.join(timeout)
        for _ in range(100):
            QApplication.instance().processEvents()
            if not self.busy:
                break

    # ------------------------------------------------------------ internals

    def _start(self, fn: Callable[[], Any]) -> None:
        self._rid += 1
        self._set_state("busy")
        self._show_status(BUSY_TEXT, "busy")
        self._thread = threading.Thread(target=_run, args=(fn, self._rid, self._bridge),
                                        name="jarvis-capture", daemon=True)
        self._thread.start()

    def _set_state(self, state: str) -> None:
        self.state = state
        busy = state == "busy"
        self.input.setReadOnly(busy)
        for b in (*self.option_buttons, self.retry_btn, self.plain_btn):
            b.setEnabled(not busy)
        self.retry_btn.setVisible(state == "error")
        self.plain_btn.setVisible(state == "error")
        clarify = state == "clarify" or (busy and self.pending is not None)
        self.question.setVisible(clarify)
        self.options_box.setVisible(clarify and bool(self.option_buttons))
        self.original.setVisible(clarify or state == "error" or (busy and self.pending is not None))
        self.input.setPlaceholderText(ANSWER_PLACEHOLDER if clarify else PLACEHOLDER)
        self.adjustSize()

    def _show_status(self, text: str, level: str) -> None:
        self.status.setText(text)
        self.status.setProperty("level", level)
        repolish(self.status)
        self.status.setVisible(bool(text))

    def _on_done(self, rid: int, result: Any) -> None:
        if rid != self._rid:
            return
        from jarvis import capture as cap

        self.last = result
        self.original.setText(f"“{self.text}”")
        if result.status == "saved":
            self.pending = None
            self._options(())
            self.input.clear()
            self._set_state("saved")
            line = cap.summary(result)
            notes = getattr(result.interpretation, "notes", ()) or ()
            self._show_status(line + (f"  ({'; '.join(notes)})" if notes else ""), "ok")
            self.captured.emit(result)
        elif result.status == "clarify":
            self.pending = result
            self.question.setText(result.clarification.question)
            self._options(result.clarification.options)
            self.input.clear()
            self._set_state("clarify")
            self._show_status("", "idle")
            self.input.setFocus()
        else:
            self.pending = None
            self._options(())
            self._set_state("error")
            self._show_status(cap.summary(result), "error")

    def _on_failed(self, rid: int, message: str) -> None:
        if rid != self._rid:
            return
        self.pending = None
        self._options(())
        self.original.setText(f"“{self.text}”")
        self._set_state("error")
        self._show_status(f"Not saved: {message}", "error")

    def _options(self, options: tuple) -> None:
        _clear(self.options_row)
        self.option_buttons = []
        for label, value in options:
            b = QPushButton(label)
            b.setObjectName("chip")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, v=value: self.answer(v))
            self.options_row.addWidget(b)
            self.option_buttons.append(b)
        self.options_row.addStretch(1)
