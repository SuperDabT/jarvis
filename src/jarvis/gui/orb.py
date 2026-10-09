"""The orb: Jarvis's presence in the chat panel, drawn as the site's radar.

A QPainter widget: a soft radial glow, an outer ring of rotating arc segments
(the sweep), a ring of "coil" segments, counter-rotating inner arcs and a warm
core. It is drawn in the ink of the two-colour pair (cream rings on green in
dark mode, green rings on cream in light mode); while it is live (listening,
speaking) the sweep arcs take on the signal colour, the site's colour for live
data. An error is a brief signal flash. It has
a ``state`` (idle / listening / thinking / speaking / error) and an input
``level`` (0..1, mic or speech loudness), smoothed with a fast attack and a
slower decay.

What each state looks like is decided by pure functions (``params_for``,
``smooth_level``, ``error_warmth``) so it can be tested without painting.

Cost: the frame timer runs only while the orb is visible, its window isn't
minimised, and motion is on. Active states tick at ~60 fps; idle ticks slowly
(``IDLE_FPS``). With motion off the orb is a static picture of its state.
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, QTimer, Property, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QConicalGradient,
    QPainter,
    QPaintEvent,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import QSizePolicy, QWidget
from shiboken6 import isValid

from jarvis.gui import motion
from jarvis.gui.theme import Tokens

STATES = ("idle", "listening", "thinking", "speaking", "error")
ACTIVE_FPS = 60
IDLE_FPS = 12
ERROR_MS = 900  # the signal flash, then back to idle
COMPACT = 30  # px, in the chat header
EXPANDED = 160  # px, voice mode
ATTACK_S = 0.04  # level rises this fast (time constant)
DECAY_S = 0.22  # ... and falls this slowly


@dataclass(frozen=True)
class OrbParams:
    spin_outer: float  # deg/s, outer arc segments
    spin_inner: float  # deg/s, inner arcs (negative = counter-rotating)
    glow: float  # 0..1, halo strength
    core: float  # core radius as a fraction of the orb radius
    rings: tuple[float, float, float]  # radii (outer arcs, coils, inner arcs), fractions
    warm: float  # 0..1, mix towards the signal colour (error flash)
    fps: int  # frame rate this state needs
    trail: float = 0.35  # 0..1, how long the arcs' fading tails are (speed cue)
    live: float = 0.0  # 0..1, mix of the signal colour into the sweep (listening, speaking)


BASE_RINGS = (0.80, 0.62, 0.45)
BASE_CORE = 0.25


def error_warmth(elapsed_ms: float) -> float:
    """Signal amount of the error flash: up fast, then fading out by ``ERROR_MS``."""
    if elapsed_ms < 0 or elapsed_ms >= ERROR_MS:
        return 0.0
    x = elapsed_ms / ERROR_MS
    return min(1.0, x / 0.12) if x < 0.12 else (1 - (x - 0.12) / 0.88) ** 1.5


def params_for(state: str, level: float = 0.0, t: float = 0.0,
               error_elapsed_ms: float = 0.0) -> OrbParams:
    """The look of ``state`` at input ``level`` (0..1) and time ``t`` (seconds)."""
    lv = motion.clamp01(level)
    o, c, i = BASE_RINGS
    if state == "listening":
        # the rings breathe outwards with the mic level
        return OrbParams(28.0, -18.0, 0.42 + 0.45 * lv, BASE_CORE + 0.05 * lv,
                         (o + 0.07 * lv, c + 0.05 * lv, i + 0.035 * lv), 0.0, ACTIVE_FPS,
                         trail=0.5, live=1.0)
    if state == "thinking":
        shimmer = 0.06 * math.sin(t * 5.0)
        return OrbParams(230.0, -320.0, 0.55 + shimmer, BASE_CORE + 0.015 * math.sin(t * 7.0),
                         BASE_RINGS, 0.0, ACTIVE_FPS, trail=1.0)
    if state == "speaking":
        # the glow and core pulse with the voice
        return OrbParams(36.0, -24.0, 0.40 + 0.60 * lv, BASE_CORE + 0.09 * lv,
                         (o + 0.02 * lv, c, i + 0.02 * lv), 0.0, ACTIVE_FPS, live=1.0)
    if state == "error":
        w = error_warmth(error_elapsed_ms)
        return OrbParams(12.0, -8.0, 0.35 + 0.35 * w, BASE_CORE, BASE_RINGS, w, ACTIVE_FPS)
    # idle: slow, dim
    return OrbParams(10.0, -6.0, 0.32, BASE_CORE, BASE_RINGS, 0.0, IDLE_FPS)


def smooth_level(current: float, target: float, dt: float, attack: float = ATTACK_S,
                 decay: float = DECAY_S) -> float:
    """One step of attack/decay smoothing (exponential, frame-rate independent)."""
    target = motion.clamp01(target)
    tau = attack if target > current else decay
    if dt <= 0:
        return current
    if tau <= 0:
        return target
    k = 1 - math.exp(-dt / tau)
    return current + (target - current) * k


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    t = motion.clamp01(t)
    return QColor.fromRgbF(a.redF() + (b.redF() - a.redF()) * t,
                           a.greenF() + (b.greenF() - a.greenF()) * t,
                           a.blueF() + (b.blueF() - a.blueF()) * t, 1.0)


def _alpha(c: QColor, a: float) -> QColor:
    out = QColor(c)
    out.setAlphaF(motion.clamp01(a))
    return out


class Orb(QWidget):
    """The orb. API for the voice flow: ``set_state``, ``set_level``, ``set_size``,
    ``expanded`` / ``set_expanded``; ``state_changed`` reports state changes."""

    state_changed = Signal(str)

    def __init__(self, tokens: Tokens, size: int = COMPACT, state: str = "idle",
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.tokens = tokens
        self._state = "idle"
        self._target = 0.0  # level as reported
        self._level = 0.0  # smoothed level
        self._t = 0.0  # animation clock (s)
        self._a_outer = 0.0
        self._a_inner = 0.0
        self._last = time.monotonic()
        self._error_at: float | None = None
        self._size = size
        self._expanded = False
        self._compact = size
        self._size_anim = None
        self._watched: QObject | None = None
        self.setFixedSize(size, size)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._tick)
        self._error_timer = QTimer(self, singleShot=True, interval=ERROR_MS)
        self._error_timer.timeout.connect(self._error_over)
        self.set_state(state)

    # ------------------------------------------------------------ API

    @property
    def state(self) -> str:
        return self._state

    @property
    def level(self) -> float:
        """The smoothed level the orb is drawing."""
        return self._level

    @property
    def running(self) -> bool:
        """Whether the frame timer is active (tests, CPU checks)."""
        return self._timer.isActive()

    def set_tokens(self, tokens: Tokens) -> None:
        self.tokens = tokens
        self.update()

    def set_state(self, state: str) -> None:
        if state not in STATES:
            raise ValueError(f"unknown orb state {state!r}; one of {', '.join(STATES)}")
        if state == "error":
            self._error_at = time.monotonic()
            self._error_timer.start()
        elif self._state == "error":
            self._error_timer.stop()
        if state in ("idle", "thinking"):
            self._target = 0.0
        changed = state != self._state
        self._state = state
        if not motion.enabled():
            self._level = self._target
        self._sync_timer()
        self.update()
        if changed:
            self.state_changed.emit(state)

    def set_level(self, level: float) -> None:
        """Input loudness 0..1 (mic while listening, voice while speaking)."""
        self._target = motion.clamp01(float(level))
        if not motion.enabled():
            self._level = self._target
            self.update()

    def set_size(self, px: int, animate: bool = True,
                 on_done: Callable[[], None] | None = None) -> None:
        """Resize the orb (square), animated with the shared easing."""
        px = int(px)
        if self._size_anim is not None and isValid(self._size_anim):
            self._size_anim.stop()
        self._size_anim = None
        if animate and motion.enabled() and self.isVisible() and px != self._size:
            self._size_anim = motion.animate(self, b"orbSize", self._size, px, motion.SLOW,
                                             "out", on_done=on_done)
        else:
            self._set_orb_size(px)
            if on_done is not None:
                on_done()

    @property
    def expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, on: bool, animate: bool = True,
                     on_done: Callable[[], None] | None = None) -> None:
        """Grow to ``EXPANDED`` px (voice mode) or back to the compact size."""
        self._expanded = bool(on)
        self.set_size(EXPANDED if on else self._compact, animate, on_done)

    def params(self) -> OrbParams:
        elapsed = (time.monotonic() - self._error_at) * 1000 if self._error_at else ERROR_MS
        return params_for(self._state, self._level, self._t, elapsed)

    # Qt property used by the size animation
    def _get_orb_size(self) -> int:
        return self._size

    def _set_orb_size(self, px: int) -> None:
        self._size = int(px)
        self.setFixedSize(self._size, self._size)
        self.update()

    orbSize = Property(int, _get_orb_size, _set_orb_size)  # noqa: N815 - Qt property name

    # ------------------------------------------------------------ timer

    def _visible_on_screen(self) -> bool:
        if not self.isVisible():
            return False
        w = self.window()
        return not (w is not None and w.isMinimized())

    def _should_run(self) -> bool:
        return motion.enabled() and self._visible_on_screen()

    def _sync_timer(self) -> None:
        if self._should_run():
            fps = self.params().fps
            interval = max(1, round(1000 / fps))
            if self._timer.interval() != interval:
                self._timer.setInterval(interval)
            if not self._timer.isActive():
                self._last = time.monotonic()
                self._timer.start()
        elif self._timer.isActive():
            self._timer.stop()

    def _tick(self) -> None:
        if not self._should_run():
            self._timer.stop()
            return
        now = time.monotonic()
        dt = min(0.1, max(0.0, now - self._last))
        self._last = now
        self._t += dt
        self._level = smooth_level(self._level, self._target, dt)
        p = self.params()
        self._a_outer = (self._a_outer + p.spin_outer * dt) % 360
        self._a_inner = (self._a_inner + p.spin_inner * dt) % 360
        want = max(1, round(1000 / p.fps))
        if self._timer.interval() != want:
            self._timer.setInterval(want)
        self.update()

    def _error_over(self) -> None:
        if self._state == "error":
            self.set_state("idle")

    def advance(self, seconds: float) -> None:
        """Step the animation clock by hand (tests and offscreen frames)."""
        dt = max(0.0, float(seconds))
        self._t += dt
        self._level = smooth_level(self._level, self._target, dt)
        p = self.params()
        self._a_outer = (self._a_outer + p.spin_outer * dt) % 360
        self._a_inner = (self._a_inner + p.spin_inner * dt) % 360
        self.update()

    def _watch_window(self) -> None:
        w = self.window()
        if w is self or w is self._watched:
            return
        if self._watched is not None:
            try:
                self._watched.removeEventFilter(self)
            except RuntimeError:
                pass
        self._watched = w
        w.installEventFilter(self)

    def eventFilter(self, obj: QObject, e: QEvent) -> bool:  # noqa: N802 - Qt API
        if obj is self._watched and e.type() in (QEvent.Type.WindowStateChange,
                                                 QEvent.Type.Show, QEvent.Type.Hide):
            QTimer.singleShot(0, self._sync_timer)
        return False

    def showEvent(self, e) -> None:  # noqa: N802 - Qt API
        super().showEvent(e)
        self._watch_window()
        self._sync_timer()

    def hideEvent(self, e) -> None:  # noqa: N802 - Qt API
        super().hideEvent(e)
        self._timer.stop()

    # ------------------------------------------------------------ paint

    def paintEvent(self, e: QPaintEvent) -> None:  # noqa: N802 - Qt API
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            self.paint(p, QRectF(self.rect()))
        finally:
            p.end()

    def paint(self, p: QPainter, rect: QRectF) -> None:
        t = self.tokens
        prm = self.params()
        dark = t.name == "dark"
        signal = QColor(t.signal)
        ring = _mix(QColor(t.orb_ring), signal, prm.warm)
        core = _mix(QColor(t.orb_core), signal, prm.warm)
        # live: the sweep arcs (and a little of the halo) turn to the signal colour
        sweep = _mix(ring, signal, 0.9 * prm.live)
        halo_c = _mix(ring, signal, 0.35 * prm.live)
        c = rect.center()
        R = min(rect.width(), rect.height()) / 2
        small = R < 22
        lw = max(1.0, R * 0.045)

        # 1. halo
        halo = QRadialGradient(c, R)
        g = prm.glow
        halo.setColorAt(0.0, _alpha(core, (0.55 if dark else 0.30) * g))
        halo.setColorAt(0.42, _alpha(halo_c, (0.26 if dark else 0.14) * g))
        halo.setColorAt(0.75, _alpha(halo_c, (0.09 if dark else 0.05) * g))
        halo.setColorAt(1.0, _alpha(halo_c, 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(halo)
        p.drawEllipse(c, R, R)

        r_out, r_coil, r_in = (R * f for f in prm.rings)

        def arc(radius: float, start_deg: float, span_deg: float, pen: QPen) -> None:
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            box = QRectF(c.x() - radius, c.y() - radius, 2 * radius, 2 * radius)
            p.drawArc(box, round(-start_deg * 16), round(-span_deg * 16))

        def pen(color: QColor, width: float, cap=Qt.PenCapStyle.FlatCap) -> QPen:
            pn = QPen(color, width)
            pn.setCapStyle(cap)
            return pn

        # 2. outer hairline + faint track
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(pen(_alpha(ring, 0.20 if dark else 0.22), max(0.8, lw * 0.35)))
        p.drawEllipse(c, r_out + lw * 2.4, r_out + lw * 2.4)
        p.setPen(pen(_alpha(ring, 0.13 if dark else 0.10), lw * 1.5))
        p.drawEllipse(c, r_out, r_out)

        def comet(radius: float, head: float, span: float, color: QColor, alpha: float,
                  width: float, direction: float) -> None:
            """An arc whose tail fades out behind its head (direction +1: moving clockwise).

            Angles here run clockwise from 3 o'clock; Qt's conical gradient runs
            counter-clockwise, hence the mirrored stops for each direction."""
            tail = span * (0.35 + 0.65 * prm.trail)
            solid = (span - tail) / 360
            full, none = _alpha(color, alpha), _alpha(color, 0.0)
            grad = QConicalGradient(c, -head)
            if direction > 0:  # the tail lies counter-clockwise of the head
                grad.setColorAt(0.0, full)
                grad.setColorAt(solid, full)
                grad.setColorAt(span / 360, none)
                grad.setColorAt(1.0, full)
                start = head - span
            else:  # the tail lies clockwise of the head
                grad.setColorAt(0.0, full)
                grad.setColorAt(1 - span / 360, none)
                grad.setColorAt(1 - solid, full)
                grad.setColorAt(1.0, full)
                start = head
            pn = QPen(QBrush(grad), width)
            pn.setCapStyle(Qt.PenCapStyle.FlatCap)
            arc(radius, start, span, pn)

        # 3. outer rotating arc segments (unequal lengths: a "scanner" look)
        a0 = self._a_outer
        w_out = lw * (1.7 if small else 1.35)
        segs = ((0, 100, 0.95), (180, 70, 0.8)) if small else \
            ((0, 96, 0.95), (128, 54, 0.75), (214, 26, 0.6), (262, 64, 0.85))
        for start, span, alpha in segs:
            comet(r_out, a0 + start + span, span, sweep, alpha, w_out, 1)

        # 4. coil ring: ten segments, slowly turning with the outer ring
        if not small:
            n = 10
            ac = a0 * 0.25
            coil = _mix(ring, core, 0.25 if dark else 0.1)
            for k in range(n):
                arc(r_coil, ac + k * 360 / n + 5, 360 / n - 10,
                    pen(_alpha(coil, (0.32 if dark else 0.26) + 0.24 * g), lw * 2.2))
            p.setPen(pen(_alpha(core if dark else ring, 0.45 if dark else 0.35), max(0.8, lw * 0.35)))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(c, r_coil - lw * 1.7, r_coil - lw * 1.7)

        # 5. inner counter-rotating arcs
        ai = self._a_inner
        w_in = lw * (1.3 if small else 1.0)
        for start, span in ((0, 120), (180, 120)):
            comet(r_in, ai + start, span, core, 0.95, w_in, -1)
        if not small:
            for start, span in ((70, 34), (250, 34)):
                arc(r_in - lw * 2.2, -ai * 0.6 + start, span,
                    pen(_alpha(core, 0.40), max(0.8, lw * 0.5)))

        # 6. core: a bright centre fading into the core colour, then a soft bloom
        rc = R * prm.core
        bloom = QRadialGradient(c, rc * 1.9)
        bloom.setColorAt(0.0, _alpha(core, 0.55 * (0.6 + 0.4 * g)))
        bloom.setColorAt(1.0, _alpha(core, 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bloom)
        p.drawEllipse(c, rc * 1.9, rc * 1.9)
        disc = QRadialGradient(c, rc)
        hot = QColor("#fffaf0")  # warm cream-white, never pure white
        disc.setColorAt(0.0, hot)
        disc.setColorAt(0.35 if dark else 0.25, _mix(core, hot, 0.55 if dark else 0.35))
        disc.setColorAt(0.85, core)
        disc.setColorAt(1.0, _alpha(core, 0.85))
        p.setBrush(disc)
        p.drawEllipse(c, rc, rc)
        # a crisp ring around the core
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(pen(_alpha(_mix(core, hot, 0.3), 0.85), max(0.8, lw * 0.5)))
        p.drawEllipse(c, rc + lw * 0.9, rc + lw * 0.9)


__all__ = ["Orb", "OrbParams", "STATES", "params_for", "smooth_level", "error_warmth",
           "COMPACT", "EXPANDED", "IDLE_FPS", "ACTIVE_FPS"]
