"""Voice turns in the chat panel: the pure state machine, the mic icon, settings.

One voice turn goes idle -> listening -> transcribing -> thinking -> speaking ->
idle. ``VoiceTurn`` only knows the states and which events move between them,
so the flow is testable without Qt; ``barge_in_action`` says what the mic
button / Ctrl+Space ("mic") or Esc ("esc") should do in each state. The chat
panel (``chat_panel.ChatPanel``) runs the workers and the UI around it.
"""
from __future__ import annotations

import os
import threading
from typing import Any

from PySide6.QtCore import QPointF, QRectF, QSettings, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPaintEvent, QPen
from PySide6.QtWidgets import QAbstractButton, QWidget

from jarvis.gui.theme import LIGHT, Tokens

IDLE, LISTENING, TRANSCRIBING, THINKING, SPEAKING = (
    "idle", "listening", "transcribing", "thinking", "speaking")
STATES = (IDLE, LISTENING, TRANSCRIBING, THINKING, SPEAKING)

# (state, event) -> next state
TRANSITIONS: dict[tuple[str, str], str] = {
    (IDLE, "start"): LISTENING,
    (LISTENING, "recorded"): TRANSCRIBING,
    (LISTENING, "no_speech"): IDLE,
    (TRANSCRIBING, "no_speech"): IDLE,          # empty transcript
    (TRANSCRIBING, "transcript"): THINKING,
    (THINKING, "speech_started"): SPEAKING,
    (SPEAKING, "speech_paused"): THINKING,      # speech caught up with the model
    (THINKING, "done"): IDLE,
    (SPEAKING, "done"): IDLE,
    **{(s, "error"): IDLE for s in STATES if s != IDLE},
    **{(s, "cancel"): IDLE for s in STATES if s != IDLE},
}

CAPTIONS = {
    LISTENING: "Listening… click the mic or press Ctrl+Space to stop",
    TRANSCRIBING: "Transcribing…",
    THINKING: "Thinking…",
    SPEAKING: "Speaking · Esc or the mic to interrupt",
}
NO_SPEECH = "Didn't catch that."
MIC_TOOLTIP = "Talk to Jarvis (Ctrl+Space)"
AUTO_LISTEN_KEY = "voice/auto_listen"
AUTO_LISTEN_LABEL = "Keep listening after replies"


class InvalidTransition(ValueError):
    pass


class VoiceTurn:
    """Where the current voice turn is. ``muted`` means speech was cancelled for
    this turn (the text answer still finishes); ``answer_done`` that the model
    has returned."""

    def __init__(self) -> None:
        self.state = IDLE
        self.trail: list[str] = [IDLE]   # every state entered (tests, debugging)
        self.muted = False
        self.answer_done = False

    def can(self, event: str) -> bool:
        return (self.state, event) in TRANSITIONS

    def fire(self, event: str) -> str:
        nxt = TRANSITIONS.get((self.state, event))
        if nxt is None:
            raise InvalidTransition(f"{event!r} is not valid while {self.state}")
        self.state = nxt
        self.trail.append(nxt)
        if nxt == IDLE:
            self.muted = False
            self.answer_done = False
        return nxt

    @property
    def active(self) -> bool:
        """Is there voice activity that the mic / Esc would act on?"""
        if self.state in (LISTENING, TRANSCRIBING):
            return True
        return self.state in (THINKING, SPEAKING) and not self.muted


def barge_in_action(state: str, key: str, muted: bool = False) -> str | None:
    """What a mic press (``key="mic"``, also Ctrl+Space) or Esc does now.

    "start" listening; "stop_recording" (submit what was said); "cancel" the
    turn; "mute" the speech still to come (the text answer keeps streaming);
    "stop_speech" (barge-in while speaking); None = nothing to do.
    """
    if state == IDLE:
        return "start" if key == "mic" else None
    if state == LISTENING:
        return "stop_recording" if key == "mic" else "cancel"
    if state == TRANSCRIBING:
        return "cancel"
    if muted:
        return None
    if state == THINKING:
        return "mute"
    if state == SPEAKING:
        return "stop_speech"
    return None


class TurnSpeaker:
    """The panel's Speaker as seen by one turn's ``voice_answer``: once muted,
    nothing more is said. The lock makes "check muted, then say" atomic, so a
    sentence can't slip in after the GUI muted and stopped the speaker."""

    def __init__(self, speaker: Any) -> None:
        self._speaker = speaker
        self._lock = threading.Lock()
        self._muted = False

    def say(self, text: str) -> None:
        with self._lock:
            if not self._muted:
                self._speaker.say(text)

    def mute(self) -> None:
        with self._lock:
            self._muted = True

    @property
    def muted(self) -> bool:
        return self._muted

    @property
    def speaking(self) -> bool:
        return not self._muted and bool(getattr(self._speaker, "speaking", False))


# ---------------------------------------------------------------- settings


def ui_settings() -> QSettings:
    """Per-user UI settings (QSettings org "jarvis", app "ui"). Tests point
    ``JARVIS_UI_SETTINGS`` at a scratch ini file so they never touch the real one."""
    path = os.environ.get("JARVIS_UI_SETTINGS")
    if path:
        return QSettings(path, QSettings.Format.IniFormat)
    return QSettings("jarvis", "ui")


def read_bool(settings: QSettings, key: str, default: bool = False) -> bool:
    v = settings.value(key, default)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


# ---------------------------------------------------------------- mic icon


def paint_mic(p: QPainter, box: QRectF, color: QColor, stroke: float = 1.5) -> None:
    """A line-art microphone (capsule, cradle, stem, foot) filling ``box``."""
    s = min(box.width(), box.height())
    cx = box.center().x()
    top = box.center().y() - s / 2
    pen = QPen(color, stroke)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    cw, ch = s * 0.36, s * 0.56
    p.drawRoundedRect(QRectF(cx - cw / 2, top + s * 0.06, cw, ch), cw / 2, cw / 2)
    r = s * 0.31
    cy = top + s * 0.44
    path = QPainterPath(QPointF(cx - r, cy))
    path.arcTo(QRectF(cx - r, cy - r, 2 * r, 2 * r), 180, 180)
    p.drawPath(path)
    p.drawLine(QPointF(cx, cy + r), QPointF(cx, top + s * 0.92))
    p.drawLine(QPointF(cx - s * 0.17, top + s * 0.92), QPointF(cx + s * 0.17, top + s * 0.92))


class MicButton(QAbstractButton):
    """Round mic button next to Send. ``active`` (listening) fills it red."""

    SIZE = 28
    ICON = 16

    def __init__(self, tokens: Tokens | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("mic")
        self.tokens = tokens or LIGHT
        self._active = False
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(MIC_TOOLTIP)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return QSize(self.SIZE, self.SIZE)

    @property
    def active(self) -> bool:
        return self._active

    def set_active(self, on: bool) -> None:
        self._active = bool(on)
        self.setProperty("active", "true" if on else "false")
        self.update()

    def set_tokens(self, tokens: Tokens) -> None:
        self.tokens = tokens
        self.update()

    def paintEvent(self, e: QPaintEvent) -> None:  # noqa: N802 - Qt API
        t = self.tokens
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        hover = self.underMouse() and self.isEnabled()
        if self._active:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(t.red))
            p.drawEllipse(r)
            icon = QColor("#ffffff")
        else:
            if hover or self.isDown():
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(t.fill2 if self.isDown() else t.hover))
                p.drawEllipse(r)
            icon = QColor(t.faint if not self.isEnabled() else (t.text if hover else t.muted))
        box = QRectF(0, 0, self.ICON, self.ICON)
        box.moveCenter(r.center())
        paint_mic(p, box, icon, 1.6)
        p.end()


class MicGlyph(QWidget):
    """The small mic in a spoken question's bubble."""

    def __init__(self, tokens: Tokens | None = None, size: int = 12,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.tokens = tokens or LIGHT
        self.setFixedSize(size, size)
        self.setToolTip("Spoken")

    def set_tokens(self, tokens: Tokens) -> None:
        self.tokens = tokens
        self.update()

    def paintEvent(self, e: QPaintEvent) -> None:  # noqa: N802 - Qt API
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        paint_mic(p, QRectF(self.rect()), QColor(self.tokens.muted), 1.2)
        p.end()


__all__ = ["VoiceTurn", "InvalidTransition", "TRANSITIONS", "STATES", "barge_in_action",
           "TurnSpeaker", "MicButton", "MicGlyph", "paint_mic", "ui_settings", "read_bool",
           "CAPTIONS", "NO_SPEECH", "MIC_TOOLTIP", "AUTO_LISTEN_KEY", "AUTO_LISTEN_LABEL",
           "IDLE", "LISTENING", "TRANSCRIBING", "THINKING", "SPEAKING"]
