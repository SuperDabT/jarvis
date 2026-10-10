"""One place for motion: durations, easing, and small animation helpers.

Every helper sets the start state immediately and starts its animation on the
next event-loop turn (so animations created while the window is being built
begin when it is actually shown). When motion is off (``--no-animations`` or
``JARVIS_REDUCE_MOTION=1``) every helper jumps to the end state synchronously
and returns ``None``; no timer is ever started.

Animations are only for change: callers run them on first show and when a
section's view model changed, never on a no-op reload.
"""
from __future__ import annotations

import math
import os
import re
import weakref
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QObject,
    QParallelAnimationGroup,
    QPauseAnimation,
    QPoint,
    QPropertyAnimation,
    QSequentialAnimationGroup,
    QTimer,
    QVariantAnimation,
)
from PySide6.QtWidgets import QGraphicsOpacityEffect, QLabel, QWidget
from shiboken6 import isValid

FAST, BASE, SLOW = 120, 220, 600  # ms
HOVER = 140   # ms, hover / press feedback on rows
EXPAND = 200  # ms, a panel section opening or closing
SELECT = 180  # ms, a selection settling in (graph ring, inspector content)
STAGGER = 60  # ms between items of a list
RISE = 8  # px a block rises while fading in

_override: bool | None = None
_live: set[QAbstractAnimation] = set()
_created = 0


# ---------------------------------------------------------------- switch


def _env_reduced() -> bool:
    return os.environ.get("JARVIS_REDUCE_MOTION", "").strip().lower() in ("1", "true", "yes",
                                                                          "on")


def enabled() -> bool:
    """Whether animations run. ``set_enabled`` wins over the environment."""
    if _override is not None:
        return _override
    return not _env_reduced()


def set_enabled(on: bool | None) -> None:
    """Force motion on/off; ``None`` goes back to the environment default."""
    global _override
    _override = on


# ---------------------------------------------------------------- easing (pure)


def curve(kind: str = "out") -> QEasingCurve:
    """"out" OutCubic (default), "pop" OutBack (small pops), "inout", "linear"."""
    types = {
        "out": QEasingCurve.Type.OutCubic,
        "pop": QEasingCurve.Type.OutBack,
        "inout": QEasingCurve.Type.InOutCubic,
        "in": QEasingCurve.Type.InCubic,
        "linear": QEasingCurve.Type.Linear,
    }
    return QEasingCurve(types.get(kind, QEasingCurve.Type.OutCubic))


def clamp01(x: float) -> float:
    return 0.0 if x <= 0 else 1.0 if x >= 1 else x


def ease_out(x: float) -> float:
    """Cubic ease-out on 0..1 (for sub-steps computed inside paint code)."""
    x = clamp01(x)
    return 1 - (1 - x) ** 3


def ease_in_out(x: float) -> float:
    x = clamp01(x)
    return 4 * x ** 3 if x < 0.5 else 1 - (-2 * x + 2) ** 3 / 2


def phase(p: float, start: float, end: float) -> float:
    """Where ``p`` (0..1) is inside the window [start, end], as 0..1."""
    if end <= start:
        return 1.0 if p >= end else 0.0
    return clamp01((p - start) / (end - start))


def stagger(p: float, i: int, n: int, span: float = 0.55) -> float:
    """Item ``i`` of ``n``'s own 0..1 progress when items start one after another.

    Each item animates over ``span`` of the whole run; their starts are spread
    over the rest, so the last one ends exactly at ``p == 1``.
    """
    if n <= 1:
        return clamp01(p / span) if span > 0 else clamp01(p)
    start = (1 - span) * i / (n - 1)
    return phase(p, start, start + span)


def pulse(x: float) -> float:
    """0 -> 1 -> 0 over x in 0..1 (a single soft pulse)."""
    return math.sin(math.pi * clamp01(x))


# ---------------------------------------------------------------- numbers (pure)

_NUM_RE = re.compile(r"^(\s*)(\d+(?:\.\d+)?)(.*)$", re.S)


def split_number(value: str) -> tuple[str, float, int, str] | None:
    """("  ", 10.0, 0, " days") for "  10 days"; ``None`` when it doesn't start with a number.

    The third item is the number of decimals to keep while counting.
    """
    m = _NUM_RE.match(value or "")
    if not m:
        return None
    num = m.group(2)
    decimals = len(num.split(".", 1)[1]) if "." in num else 0
    return m.group(1), float(num), decimals, m.group(3)


def number_formatter(value: str) -> Callable[[float], str] | None:
    """A formatter that prints a counted number in ``value``'s own format, or None."""
    parts = split_number(value)
    if parts is None:
        return None
    lead, _, decimals, rest = parts

    def fmt(v: float) -> str:
        return f"{lead}{v:.{decimals}f}{rest}"

    return fmt


# ---------------------------------------------------------------- registry


def _weak(obj: QObject) -> Callable[[], Any]:
    """A weak handle: returns ``obj`` while it lives, else None.

    Animation slots hold their widgets weakly: if a slot held the last reference
    to a Python-owned widget, deleting the animation (its child) would delete the
    widget from inside the child's destructor.
    """
    ref = weakref.ref(obj)

    def get() -> Any:
        o = ref()
        return o if o is not None and isValid(o) else None

    return get


def _register(anim: QAbstractAnimation, on_done: Callable[[], None] | None) -> None:
    global _created
    _created += 1
    _live.add(anim)
    # The slot must not hold the animation itself: a Python slot that keeps its
    # own sender alive crashes shiboken when the sender is deleted.
    ref = weakref.ref(anim)

    def done() -> None:
        a = ref()
        if a is not None:
            _live.discard(a)
        if on_done is not None:
            on_done()
        # Free it after its signal handlers have returned.
        QTimer.singleShot(0, lambda: _delete(ref))

    anim.finished.connect(done)


def track(anim: QAbstractAnimation) -> None:
    """Register a widget's own reusable animation (e.g. the peek drawer's) so
    ``seek_all`` / ``finish_all`` reach it too. Call right after ``start()``."""
    _live.add(anim)
    if not getattr(anim, "_motion_tracked", False):
        anim._motion_tracked = True
        ref = weakref.ref(anim)
        anim.finished.connect(lambda: (a := ref()) is not None and _live.discard(a))


def _delete(ref: weakref.ref) -> None:
    a = ref()
    if a is not None and isValid(a):
        a.deleteLater()


def _launch(anim: QAbstractAnimation) -> None:
    """Start on the next event-loop turn (after the window has been shown)."""

    def go() -> None:
        if isValid(anim) and anim in _live and anim.state() == QAbstractAnimation.State.Stopped:
            anim.start(QAbstractAnimation.DeletionPolicy.KeepWhenStopped)

    QTimer.singleShot(0, go)


def created_count() -> int:
    """How many animations were created so far (tests: "did anything animate?")."""
    return _created


def live() -> list[QAbstractAnimation]:
    """Animations created and not finished yet."""
    return [a for a in list(_live) if isValid(a)]


def seek_all(ms: int) -> None:
    """Put every live animation at ``ms`` from its start (offscreen frame capture)."""
    for a in live():
        a.setCurrentTime(min(ms, a.totalDuration()))


def finish_all() -> None:
    """Jump every live animation to its end state (runs their completion steps)."""
    for _ in range(10):  # completing one can start another (e.g. a chained step)
        anims = live()
        if not anims:
            return
        for a in anims:
            if not isValid(a):
                continue
            if a.state() == QAbstractAnimation.State.Stopped:
                a.start(QAbstractAnimation.DeletionPolicy.KeepWhenStopped)
            a.setCurrentTime(a.totalDuration())


def _with_delay(anim: QAbstractAnimation, delay: int, parent: QObject) -> QAbstractAnimation:
    if delay <= 0:
        return anim
    seq = QSequentialAnimationGroup(parent)
    seq.addAnimation(QPauseAnimation(delay))
    seq.addAnimation(anim)
    return seq


def _run(anim: QAbstractAnimation, delay: int, parent: QObject,
         on_done: Callable[[], None] | None) -> QAbstractAnimation:
    top = _with_delay(anim, delay, parent)
    _register(top, on_done)
    _launch(top)
    return top


# ---------------------------------------------------------------- helpers


def animate(target: QObject, prop: bytes, start: Any, end: Any, duration: int = BASE,
            easing: str = "out", delay: int = 0,
            on_done: Callable[[], None] | None = None) -> QAbstractAnimation | None:
    """Animate a Qt property from ``start`` to ``end``; with motion off set ``end`` now."""
    if not enabled():
        target.setProperty(prop.decode(), end)
        if on_done is not None:
            on_done()
        return None
    target.setProperty(prop.decode(), start)
    a = QPropertyAnimation(target, prop, target)
    a.setStartValue(start)
    a.setEndValue(end)
    a.setDuration(duration)
    a.setEasingCurve(curve(easing))
    return _run(a, delay, target, on_done)


def grow(target: QObject, prop: bytes = b"progress", duration: int = SLOW, delay: int = 0,
         start: float = 0.0, end: float = 1.0, easing: str = "linear",
         on_done: Callable[[], None] | None = None) -> QAbstractAnimation | None:
    """A 0 -> 1 ``progress``-style property (charts apply their own easing per mark)."""
    return animate(target, prop, float(start), float(end), duration, easing, delay, on_done)


def _opacity_effect(w: QWidget) -> QGraphicsOpacityEffect:
    eff = w.graphicsEffect()
    if not isinstance(eff, QGraphicsOpacityEffect):
        eff = QGraphicsOpacityEffect(w)
        w.setGraphicsEffect(eff)
    return eff


def _drop_effect(w: QWidget | None) -> None:
    if w is not None and isValid(w) and isinstance(w.graphicsEffect(), QGraphicsOpacityEffect):
        w.setGraphicsEffect(None)  # render directly again (effects cost a pixmap per paint)


def _fade_anim(w: QWidget, start: float, end: float, duration: int,
               easing: str) -> QPropertyAnimation:
    eff = _opacity_effect(w)
    eff.setOpacity(start)
    a = QPropertyAnimation(eff, b"opacity", w)
    a.setStartValue(start)
    a.setEndValue(end)
    a.setDuration(duration)
    a.setEasingCurve(curve(easing))
    return a


def fade_in(w: QWidget, duration: int = BASE, delay: int = 0, easing: str = "out",
            on_done: Callable[[], None] | None = None) -> QAbstractAnimation | None:
    if not enabled():
        if on_done is not None:
            on_done()
        return None
    a = _fade_anim(w, 0.0, 1.0, duration, easing)

    wk = _weak(w)

    def done() -> None:
        _drop_effect(wk())
        if on_done is not None:
            on_done()

    return _run(a, delay, w, done)


def fade_out(w: QWidget, duration: int = FAST, delay: int = 0, easing: str = "out",
             on_done: Callable[[], None] | None = None) -> QAbstractAnimation | None:
    if not enabled():
        if on_done is not None:
            on_done()
        return None
    return _run(_fade_anim(w, 1.0, 0.0, duration, easing), delay, w, on_done)


class _Offset:
    """Moves a laid-out widget by an offset without fighting its layout: if the
    layout repositions the widget mid-animation, that becomes the new base."""

    def __init__(self, w: QWidget):
        self.w = _weak(w)
        self.base: QPoint | None = None
        self.last: QPoint | None = None

    def step(self, dx: float, dy: float) -> None:
        w = self.w()
        if w is None:
            return
        pos = w.pos()
        if self.last is None or pos != self.last:
            self.base = QPoint(pos)
        target = self.base + QPoint(round(dx), round(dy))
        self.last = target
        if target != pos:
            w.move(target)


def _slide_anim(w: QWidget, dx: float, dy: float, duration: int, easing: str,
                outwards: bool = False) -> QVariantAnimation:
    off = _Offset(w)
    a = QVariantAnimation(w)
    a.setStartValue(0.0 if outwards else 1.0)
    a.setEndValue(1.0 if outwards else 0.0)
    a.setDuration(duration)
    a.setEasingCurve(curve(easing))
    a.valueChanged.connect(lambda v: off.step(dx * v, dy * v))
    return a


def slide(w: QWidget, dy: float = RISE, dx: float = 0.0, duration: int = BASE, delay: int = 0,
          easing: str = "out",
          on_done: Callable[[], None] | None = None) -> QAbstractAnimation | None:
    """Slide ``w`` from (dx, dy) away into its layout position."""
    if not enabled():
        if on_done is not None:
            on_done()
        return None
    return _run(_slide_anim(w, dx, dy, duration, easing), delay, w, on_done)


def fade_rise(w: QWidget, dy: float = RISE, duration: int = BASE, delay: int = 0,
              on_done: Callable[[], None] | None = None) -> QAbstractAnimation | None:
    """Fade in while rising ``dy`` px into place (briefing sentences, chat messages)."""
    if not enabled():
        if on_done is not None:
            on_done()
        return None
    group = QParallelAnimationGroup(w)
    group.addAnimation(_fade_anim(w, 0.0, 1.0, duration, "out"))
    group.addAnimation(_slide_anim(w, 0, dy, duration, "out"))

    wk = _weak(w)

    def done() -> None:
        _drop_effect(wk())
        if on_done is not None:
            on_done()

    return _run(group, delay, w, done)


def count_up(label: QLabel, start: float, end: float,
             fmt: str | Callable[[float], str] = "{:.0f}", duration: int = SLOW,
             delay: int = 0, final: str | None = None,
             on_done: Callable[[], None] | None = None) -> QAbstractAnimation | None:
    """Count ``label`` from ``start`` to ``end``; it ends on ``final`` (or ``fmt(end)``)."""
    f = fmt if callable(fmt) else fmt.format
    final_text = final if final is not None else f(end)
    if not enabled():
        label.setText(final_text)
        if on_done is not None:
            on_done()
        return None
    label.setText(f(start))
    a = QVariantAnimation(label)
    a.setStartValue(float(start))
    a.setEndValue(float(end))
    a.setDuration(duration)
    a.setEasingCurve(curve("out"))
    lk = _weak(label)
    a.valueChanged.connect(lambda v: (lb := lk()) is not None and lb.setText(f(v)))

    def done() -> None:
        lb = lk()
        if lb is not None:
            lb.setText(final_text)
        if on_done is not None:
            on_done()

    return _run(a, delay, label, done)


def count_up_text(label: QLabel, value: str, duration: int = SLOW,
                  delay: int = 0) -> QAbstractAnimation | None:
    """Count the leading number of ``value`` up from 0, keeping its format
    ("0.9/day", "10 days", "3 / 9"); text without a leading number fades in."""
    parts = split_number(value)
    fmt = number_formatter(value)
    if parts is None or fmt is None or parts[1] == 0:
        label.setText(value)
        return fade_in(label, BASE, delay)
    return count_up(label, 0.0, parts[1], fmt, duration, delay, final=value)


def collapse(w: QWidget, dx: float = 28.0, duration: int = BASE,
             on_done: Callable[[], None] | None = None) -> QAbstractAnimation | None:
    """Slide ``w`` out to the right, fade it and collapse its height to 0, then ``on_done``
    (which should actually remove it)."""
    if not enabled():
        if on_done is not None:
            on_done()
        return None
    h = max(1, w.height())
    w.setMaximumHeight(h)
    w.setMinimumHeight(0)
    out = QParallelAnimationGroup(w)
    out.addAnimation(_fade_anim(w, 1.0, 0.0, FAST + 40, "out"))
    out.addAnimation(_slide_anim(w, dx, 0, FAST + 40, "in", outwards=True))
    shrink = QPropertyAnimation(w, b"maximumHeight", w)
    shrink.setStartValue(h)
    shrink.setEndValue(0)
    shrink.setDuration(duration)
    shrink.setEasingCurve(curve("inout"))
    seq = QSequentialAnimationGroup(w)
    seq.addAnimation(out)
    seq.addAnimation(shrink)
    return _run(seq, 0, w, on_done)


def reveal(w: QWidget, show: bool, duration: int = EXPAND,
           on_done: Callable[[], None] | None = None) -> QAbstractAnimation | None:
    """Open (``show``) or close a section by animating its height, then leave it at
    its natural size (open) or hidden (closed). Motion off: shown or hidden now."""
    def settle() -> None:
        if isValid(w):
            w.setMaximumHeight(QWIDGETSIZE_MAX)
            w.setVisible(show)
        if on_done is not None:
            on_done()

    if not enabled():
        settle()
        return None
    target = max(1, w.sizeHint().height()) if show else 0
    start = 0 if show else max(1, w.height())
    w.setMaximumHeight(start)
    w.setVisible(True)
    a = QPropertyAnimation(w, b"maximumHeight", w)
    a.setStartValue(start)
    a.setEndValue(target)
    a.setDuration(duration)
    a.setEasingCurve(curve("out" if show else "inout"))
    return _run(a, 0, w, settle)


QWIDGETSIZE_MAX = (1 << 24) - 1


__all__ = [
    "BASE", "EXPAND", "FAST", "HOVER", "SELECT", "SLOW", "STAGGER", "RISE",
    "animate", "clamp01", "collapse", "count_up", "count_up_text", "created_count", "curve",
    "ease_in_out", "ease_out", "enabled", "fade_in", "fade_out", "fade_rise", "finish_all",
    "grow", "live", "number_formatter", "phase", "pulse", "reveal", "seek_all", "set_enabled",
    "slide",
    "split_number", "stagger",
]
