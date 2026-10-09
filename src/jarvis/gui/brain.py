"""The project brain: a living graph of the repo, plus the small painted widgets
of the cockpit columns (pulse bars, milestone bars, rail bars).

``BrainGraph`` draws ``brain_data.Graph`` with the stable radial layout from
``brain_data.layout``. Nodes drift a little, live ones (activity in the last
48 h) glow mint, new ones grow in. Hover a node to inspect it, click to lock
the selection, scroll to zoom, drag empty space to pan. The frame timer runs
only while the widget is visible and motion is on.
"""
from __future__ import annotations

import math
import time

from PySide6.QtCore import Property, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QRadialGradient,
    QWheelEvent,
)
from PySide6.QtWidgets import QWidget

from jarvis.gui import motion, theme
from jarvis.gui.brain_data import Graph, Node, _hash01, layout
from jarvis.gui.data import MilestoneBar, RailBudget
from jarvis.gui.theme import Tokens

FPS = 30
HIT_PX = 10
RADIUS = {"core": 7.5, "hub": 5.0, "milestone": 5.5, "issue": 3.6, "commit": 2.4, "card": 4.2,
          "check": 3.2, "part": 3.0, "paper": 2.2}
LABELLED = ("core", "hub", "milestone")


def qc(hex_: str, alpha: float = 1.0) -> QColor:
    c = QColor(hex_)
    c.setAlphaF(max(0.0, min(1.0, alpha)))
    return c


class BrainGraph(QWidget):
    hovered = Signal(object)   # node id or None
    locked = Signal(object)    # node id or None (click)

    def __init__(self, tokens: Tokens, parent: QWidget | None = None):
        super().__init__(parent)
        self.tokens = tokens
        self.graph = Graph((), ())
        self.pos: dict[str, tuple[float, float]] = {}
        self.hidden_kinds: set[str] = set()
        self.hover_id: str | None = None
        self.lock_id: str | None = None
        self.highlight: set[str] = set()
        self._born: dict[str, float] = {}     # node id -> time it appeared (grow-in)
        self._progress = 1.0                  # first-show reveal, 0 -> 1
        self.zoom = 1.0
        self.pan = QPointF(0, 0)
        self._drag: QPointF | None = None
        self._dragged = False
        self._t0 = time.monotonic()
        self.setMouseTracking(True)
        self.setMinimumSize(360, 320)
        self._timer = QTimer(self, interval=round(1000 / FPS))
        self._timer.timeout.connect(self.update)
        self._hl_timer = QTimer(self, singleShot=True, interval=12_000)
        self._hl_timer.timeout.connect(lambda: self.set_highlight(set()))

    # ------------------------------------------------------------ data

    def _get_progress(self) -> float:
        return self._progress

    def _set_progress(self, v: float) -> None:
        self._progress = float(v)
        self.update()

    progress = Property(float, _get_progress, _set_progress)

    def set_data(self, graph: Graph, animate: bool = False) -> None:
        first = not self.graph.nodes
        old = {n.id for n in self.graph.nodes}
        self.graph = graph
        self.pos = layout(graph)
        now = time.monotonic()
        if not first:
            for n in graph.nodes:
                if n.id not in old:
                    self._born[n.id] = now
        if self.hover_id and not graph.node(self.hover_id):
            self.hover_id = None
        if self.lock_id and not graph.node(self.lock_id):
            self.lock_id = None
            self.locked.emit(None)
        if first and animate and motion.enabled():
            motion.grow(self, b"progress", 1400, easing="out")
        else:
            self._progress = 1.0
        self.update()

    def set_tokens(self, tokens: Tokens) -> None:
        self.tokens = tokens
        self.update()

    def set_hidden_kinds(self, kinds: set[str]) -> None:
        self.hidden_kinds = set(kinds)
        if self.hover_id and self._kind(self.hover_id) in self.hidden_kinds:
            self.hover_id = None
        self.update()

    def set_highlight(self, ids: set[str]) -> None:
        """Light up these nodes (the sources of the latest answer) for a while."""
        self.highlight = {i for i in ids if self.graph.node(i)}
        if self.highlight:
            self._hl_timer.start()
        self.update()

    def lock(self, node_id: str | None) -> None:
        self.lock_id = node_id if node_id and self.graph.node(node_id) else None
        self.update()
        self.locked.emit(self.lock_id)

    def _kind(self, node_id: str) -> str:
        n = self.graph.node(node_id)
        return n.kind if n else ""

    def visible(self, n: Node) -> bool:
        return n.kind not in self.hidden_kinds

    # ------------------------------------------------------------ geometry

    def set_inset_left(self, px: float) -> None:
        """Keep the graph clear of an overlay this wide on the left (the HUD);
        at most a third of the width, so a narrow window still shows it whole."""
        self.inset_left = max(0.0, min(px, self.width() / 3))
        self.update()

    def _area(self) -> QRectF:
        x = getattr(self, "inset_left", 0.0)
        return QRectF(x, 0, self.width() - x, self.height())

    def _scale(self) -> tuple[float, float]:
        """(x, y) scale: the disc stretched to fill the area, at most 1.45 : 1."""
        a = self._area()
        w, h = a.width(), a.height()
        sx = 0.46 * min(w, h * 1.45)
        sy = 0.46 * min(h, w * 1.45)
        return sx * self.zoom, sy * self.zoom

    def _center(self) -> QPointF:
        a = self._area()
        return QPointF(a.center().x() + self.pan.x(), a.center().y() + self.pan.y())

    def _t(self) -> float:
        return time.monotonic() - self._t0 if self._timer.isActive() else 0.0

    def screen_pos(self, node_id: str) -> QPointF | None:
        p = self.pos.get(node_id)
        if p is None:
            return None
        (sx, sy), c = self._scale(), self._center()
        x, y = p
        t = self._t()
        if t and node_id != "jarvis":  # a slow drift, different per node
            ph = 6.283 * _hash01(node_id, "ph")
            amp = 0.006 + 0.006 * _hash01(node_id, "amp")
            x += amp * math.sin(t * 0.35 + ph)
            y += amp * math.cos(t * 0.29 + ph * 1.3)
        reveal = self._reveal(node_id)
        return QPointF(c.x() + x * sx * reveal, c.y() + y * sy * reveal)

    def _reveal(self, node_id: str) -> float:
        p = self._progress
        if p >= 1.0:
            return 1.0
        x, y = self.pos.get(node_id, (0, 0))
        d = min(1.0, math.hypot(x, y))
        return motion.ease_out(motion.clamp01((p - 0.35 * d) / 0.65))

    def node_at(self, x: float, y: float) -> str | None:
        """The visible node under (x, y), nearest first, within HIT_PX."""
        best, best_d = None, HIT_PX + 0.0
        for n in self.graph.nodes:
            if not self.visible(n):
                continue
            sp = self.screen_pos(n.id)
            if sp is None:
                continue
            d = math.hypot(sp.x() - x, sp.y() - y) - RADIUS.get(n.kind, 3)
            if d < best_d:
                best, best_d = n.id, d
        return best

    # ------------------------------------------------------------ input

    def mouseMoveEvent(self, e: QMouseEvent) -> None:  # noqa: N802 - Qt API
        p = e.position()
        if self._drag is not None:
            delta = p - self._drag
            if abs(delta.x()) + abs(delta.y()) > 2:
                self._dragged = True
            self.pan += delta
            self._drag = p
            self.update()
            return
        hit = self.node_at(p.x(), p.y())
        self.setCursor(Qt.CursorShape.PointingHandCursor if hit else Qt.CursorShape.ArrowCursor)
        if hit != self.hover_id:
            self.hover_id = hit
            self.hovered.emit(hit)
            self.update()

    def leaveEvent(self, e) -> None:  # noqa: N802 - Qt API
        if self.hover_id is not None:
            self.hover_id = None
            self.hovered.emit(None)
            self.update()
        super().leaveEvent(e)

    def mousePressEvent(self, e: QMouseEvent) -> None:  # noqa: N802 - Qt API
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.position()
            self._dragged = False

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:  # noqa: N802 - Qt API
        if e.button() != Qt.MouseButton.LeftButton:
            return
        dragged, self._drag = self._dragged, None
        if dragged:
            return
        hit = self.node_at(e.position().x(), e.position().y())
        self.lock(None if hit == self.lock_id else hit)

    def mouseDoubleClickEvent(self, e: QMouseEvent) -> None:  # noqa: N802 - Qt API
        self.reset_view()

    def wheelEvent(self, e: QWheelEvent) -> None:  # noqa: N802 - Qt API
        step = 1.12 if e.angleDelta().y() > 0 else 1 / 1.12
        new = max(0.6, min(4.0, self.zoom * step))
        # zoom around the cursor
        c = self._center()
        p = e.position()
        k = new / self.zoom
        self.pan += (p - c) * (1 - k)
        self.zoom = new
        self.update()

    def reset_view(self) -> None:
        self.zoom, self.pan = 1.0, QPointF(0, 0)
        self.update()

    # ------------------------------------------------------------ timer

    def _sync_timer(self) -> None:
        w = self.window()
        run = motion.enabled() and self.isVisible() and not (w and w.isMinimized())
        if run and not self._timer.isActive():
            self._timer.start()
        elif not run:
            self._timer.stop()

    @property
    def running(self) -> bool:
        return self._timer.isActive()

    def showEvent(self, e) -> None:  # noqa: N802 - Qt API
        super().showEvent(e)
        self._sync_timer()

    def hideEvent(self, e) -> None:  # noqa: N802 - Qt API
        super().hideEvent(e)
        self._timer.stop()

    # ------------------------------------------------------------ paint

    def focus_set(self) -> set[str]:
        """The selected node and its neighbours (drawn bright)."""
        sel = self.lock_id or self.hover_id
        if not sel:
            return set()
        return {sel} | {n.id for _, n in self.graph.neighbours(sel)}

    def paintEvent(self, e: QPaintEvent) -> None:  # noqa: N802 - Qt API
        t = self.tokens
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = self._center()
        sx, sy = self._scale()
        # faint rings: the brain's "orbits"
        p.setBrush(Qt.BrushStyle.NoBrush)
        for r in (0.3, 0.62, 0.95):
            p.setPen(QPen(qc(t.ink, 0.035), 1))
            p.drawEllipse(c, r * sx, r * sy)

        nodes = {n.id: n for n in self.graph.nodes if self.visible(n)}
        pts = {nid: self.screen_pos(nid) for nid in nodes}
        focus = self.focus_set()
        sel = self.lock_id or self.hover_id
        now = time.monotonic()
        tt = self._t()

        # edges
        for ed in self.graph.edges:
            a, b = pts.get(ed.a), pts.get(ed.b)
            if a is None or b is None:
                continue
            on = bool(sel) and (ed.a == sel or ed.b == sel)
            hl = ed.a in self.highlight or ed.b in self.highlight
            if on:
                col, w = qc(t.ink, 0.75), 1.2
            elif hl:
                col, w = qc(t.signal, 0.55), 1.1
            elif ed.kind in ("mentions", "maybe"):
                col, w = qc(t.signal, 0.32 if not sel else 0.12), 1.0
            elif ed.kind == "history":
                col, w = qc(t.ink, 0.06 if not sel else 0.025), 0.8
            else:
                col, w = qc(t.ink, 0.075 if not sel else 0.03), 0.8
            pen = QPen(col, w)
            if ed.dashed:
                pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawLine(a, b)

        # nodes
        lf = theme.mono_font(10)
        p.setFont(lf)
        for nid, n in nodes.items():
            pt = pts[nid]
            if pt is None:
                continue
            r = RADIUS.get(n.kind, 3.0)
            born = self._born.get(nid)
            if born is not None:
                k = (now - born) / 1.2
                if k >= 1 or not motion.enabled():
                    self._born.pop(nid, None)
                else:
                    r *= 0.4 + 0.6 * motion.ease_out(k)
            dim = bool(sel) and nid not in focus
            fill, alpha = self._node_color(n)
            if dim:
                alpha *= 0.3
            if n.live or nid in self.highlight or n.kind == "core":
                pulse = 0.5 + 0.5 * math.sin(tt * 2.2 + 6.28 * _hash01(nid, "p")) if tt else 0.6
                glow = QRadialGradient(pt, r * 4.2)
                gc = t.ink if n.kind == "core" else t.signal
                glow.setColorAt(0, qc(gc, (0.30 + 0.18 * pulse) * (0.4 if dim else 1)))
                glow.setColorAt(1, qc(gc, 0))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(glow)
                p.drawEllipse(pt, r * 4.2, r * 4.2)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qc(fill, alpha))
            if n.kind == "card":  # a ring: something to act on
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(qc(fill, alpha), 1.6))
            p.drawEllipse(pt, r, r)
            if nid == sel:
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(qc(t.ink, 0.9), 1.2))
                p.drawEllipse(pt, r + 4, r + 4)
            show_label = (n.kind in LABELLED or nid == sel
                          or (nid in focus and n.kind in ("issue", "milestone", "part"))
                          or (n.kind == "issue" and n.live))
            if show_label and self._progress > 0.8:
                p.setPen(qc(t.text if n.kind in ("core", "milestone") or nid in focus
                            else t.muted, 0.35 if dim else 0.95))
                p.drawText(QPointF(pt.x() + r + 5, pt.y() + 4), n.label)
        p.end()

    def _node_color(self, n: Node) -> tuple[str, float]:
        t = self.tokens
        if n.live:
            return t.signal, 1.0
        if n.kind == "core":
            return t.accent_hover, 1.0
        if n.kind in ("hub", "milestone"):
            return t.ink, 0.95
        if n.kind == "issue":
            return (t.ink, 0.85) if n.status == "open" else (t.ink, 0.3)
        if n.kind == "check":
            if n.status == "fail":
                return t.red, 1.0
            return (t.signal, 0.55) if n.status == "pass" else (t.ink, 0.6)
        if n.kind == "card":
            return t.signal, 0.9
        if n.kind == "paper":
            return (t.signal, 0.8) if n.status == "relevant" else (t.ink, 0.38)
        if n.kind == "commit":
            return t.ink, 0.5
        return t.ink, 0.6


# ---------------------------------------------------------------- column widgets


class PulseBars(QWidget):
    """Activity per day as a dense bar sparkline (mint), newest on the right."""

    def __init__(self, tokens: Tokens, parent: QWidget | None = None):
        super().__init__(parent)
        self.tokens = tokens
        self.counts: tuple[int, ...] = ()
        self._progress = 1.0
        self.setFixedHeight(46)

    def _get_progress(self) -> float:
        return self._progress

    def _set_progress(self, v: float) -> None:
        self._progress = float(v)
        self.update()

    progress = Property(float, _get_progress, _set_progress)

    def set_data(self, counts: tuple[int, ...], animate: bool = False) -> None:
        self.counts = tuple(counts)
        if animate and motion.enabled():
            motion.grow(self, b"progress", motion.SLOW)
        else:
            self._progress = 1.0
        self.update()

    def paintEvent(self, e: QPaintEvent) -> None:  # noqa: N802 - Qt API
        t = self.tokens
        p = QPainter(self)
        n = len(self.counts)
        if not n:
            p.end()
            return
        w, h = self.width(), self.height()
        step = w / n
        top = max(self.counts) or 1
        p.setPen(Qt.PenStyle.NoPen)
        for i, v in enumerate(self.counts):
            k = motion.stagger(self._progress, i, n)
            if v:
                bh = max(2.0, (h - 2) * (0.25 + 0.75 * v / top)) * k
                p.setBrush(qc(t.signal, 0.55 + 0.45 * v / top))
            else:
                bh = 1.0
                p.setBrush(qc(t.ink, 0.12))
            p.drawRect(QRectF(i * step, h - bh, max(1.0, step - 1), bh))
        p.end()


class MilestoneMini(QWidget):
    """One milestone as a thin bar: closed (mint), may be done (hatched), open."""

    def __init__(self, bar: MilestoneBar, tokens: Tokens, parent: QWidget | None = None):
        super().__init__(parent)
        self.bar = bar
        self.tokens = tokens
        self.setFixedHeight(6)
        self.setToolTip(f"{bar.title}: {bar.label}")

    def paintEvent(self, e: QPaintEvent) -> None:  # noqa: N802 - Qt API
        t, b = self.tokens, self.bar
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qc(t.ink, 0.1))
        p.drawRoundedRect(QRectF(0, 0, w, h), 3, 3)
        if b.total:
            cw = w * b.closed / b.total
            mw = w * b.maybe / b.total
            p.setBrush(qc(t.signal))
            p.drawRoundedRect(QRectF(0, 0, cw, h), 3, 3)
            if mw:
                p.setBrush(QBrush(qc(t.ink, 0.6), Qt.BrushStyle.BDiagPattern))
                p.drawRect(QRectF(cw, 0, mw, h))
        p.end()


class RailMini(QWidget):
    """A rail's worst-case current against its rating (or unknown rating)."""

    def __init__(self, budget: RailBudget, tokens: Tokens, parent: QWidget | None = None):
        super().__init__(parent)
        self.budget = budget
        self.tokens = tokens
        self.setFixedHeight(6)

    def paintEvent(self, e: QPaintEvent) -> None:  # noqa: N802 - Qt API
        t, b = self.tokens, self.budget
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qc(t.ink, 0.1))
        p.drawRoundedRect(QRectF(0, 0, w, h), 3, 3)
        load = b.worst_case_a
        if b.rating_a:
            frac = min(1.0, load / b.rating_a)
            col = t.red if load > b.rating_a else t.signal
            p.setBrush(qc(col))
            p.drawRoundedRect(QRectF(0, 0, w * frac, h), 3, 3)
        else:
            # rating unknown: a dashed outline, no claim about how full the rail is
            p.setPen(QPen(qc(t.ink, 0.45), 1, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 3, 3)
        p.end()


__all__ = ["BrainGraph", "PulseBars", "MilestoneMini", "RailMini", "RADIUS"]
