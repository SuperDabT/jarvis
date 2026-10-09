"""Dashboard charts, painted with QPainter (no chart library).

Each chart takes one pure view model from ``gui/data.py`` (``set_data``) and the
theme tokens (``set_tokens``). Marks are thin with rounded ends, hairlines are
1px, labels use text tokens (never the series colour), and every mark has a
hover tooltip. Charts size their own height from their data.

Colour follows the site's two-colour system: marks are the ink (cream on green,
green on cream) at stepped lightness, tracks and grids are faint tints of it,
and the signal colour marks only live data (today, limit violations). Axis and
legend labels are in the mono label voice; headline numbers in Archivo.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date

from PySide6.QtCore import Property, QPointF, QRectF, Qt, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QFontMetricsF,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from jarvis.gui.data import (
    ContributionGrid,
    RailBudget,
    Timeline,
    VoltageWindow,
    fmt_day,
    fmt_num,
)
from jarvis.gui import motion
from jarvis.gui.motion import ease_out, phase, stagger
from jarvis.gui import theme
from jarvis.gui.theme import Tokens


def qc(hex_: str, alpha: float = 1.0) -> QColor:
    c = QColor(hex_)
    c.setAlphaF(max(0.0, min(1.0, alpha)))
    return c


def on_color(fill: QColor | str, t: Tokens) -> str:
    """Readable text on ``fill``: the page colour on light marks, the text on dark."""
    c = QColor(fill)
    return t.bg if c.lightnessF() > 0.5 else t.text if t.name == "dark" else t.on_accent


def blend(a: str, b: str, t: float) -> QColor:
    """Mix colour ``a`` towards ``b`` by ``t`` (0..1), opaque."""
    ca, cb = QColor(a), QColor(b)
    return QColor(round(ca.red() + (cb.red() - ca.red()) * t),
                  round(ca.green() + (cb.green() - ca.green()) * t),
                  round(ca.blue() + (cb.blue() - ca.blue()) * t))


class Hit:
    __slots__ = ("rect", "tip", "key", "url")

    def __init__(self, rect: QRectF, tip: str, key: object = None, url: str | None = None):
        self.rect, self.tip, self.key, self.url = rect, tip, key, url


class Chart(QWidget):
    """Base: tokens, fonts, hit areas with tooltips (and optional click-to-open)."""

    hovered = Signal(object)

    def __init__(self, tokens: Tokens, parent: QWidget | None = None):
        super().__init__(parent)
        self.tokens = tokens
        self.data = None
        self.hits: list[Hit] = []
        self.hover_key: object = None
        self._progress = 1.0  # 0..1 load animation; marks scale with it in paint()
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)

    # -- API
    def set_tokens(self, tokens: Tokens) -> None:
        self.tokens = tokens
        self.update()

    def set_data(self, data, animate: bool = False) -> None:
        self.data = data
        self.setFixedHeight(self.preferred_height())
        if animate and data is not None:
            motion.grow(self, b"progress", motion.SLOW + 300)
        else:
            self._progress = 1.0
        self.update()

    def _get_progress(self) -> float:
        return self._progress

    def set_progress(self, value: float) -> None:
        self._progress = motion.clamp01(float(value))
        self.update()

    # Animated 0 -> 1 when new data arrives (``set_data(..., animate=True)``).
    progress = Property(float, _get_progress, set_progress)

    def preferred_height(self) -> int:
        return 120

    # -- helpers
    def font_px(self, px: int, weight: QFont.Weight = QFont.Weight.Normal,
                voice: str = "ui") -> QFont:
        """``voice``: "ui" (Plex Sans), "mono" (uppercase labels), "num" (Archivo)."""
        if voice == "mono":
            return theme.mono_font(px, caps=True, spacing_em=0.08)
        if voice == "num":
            return theme.display_font(px, 700 if weight <= QFont.Weight.DemiBold else 800, 86,
                                      spacing_em=0)
        f = QFont(self.font())
        f.setPixelSize(px)
        f.setWeight(weight)
        return f

    def text(self, p: QPainter, x: float, y: float, s: str, color: str | QColor, px: int = 12,
             align: str = "left", weight: QFont.Weight = QFont.Weight.Normal,
             valign: str = "middle", voice: str = "ui") -> float:
        """Draw ``s`` with its vertical centre (or baseline) at ``y``; returns its width."""
        f = self.font_px(px, weight, voice)
        fm = QFontMetricsF(f)
        w = fm.horizontalAdvance(s)
        if align == "right":
            x -= w
        elif align == "center":
            x -= w / 2
        base = y + (fm.ascent() - fm.descent()) / 2 if valign == "middle" else y
        p.setFont(f)
        p.setPen(QColor(color) if isinstance(color, str) else color)
        p.drawText(QPointF(x, base), s)
        return w

    def text_width(self, s: str, px: int = 12, weight: QFont.Weight = QFont.Weight.Normal,
                   voice: str = "ui") -> float:
        return QFontMetricsF(self.font_px(px, weight, voice)).horizontalAdvance(s)

    def elide(self, s: str, width: float, px: int = 12,
              weight: QFont.Weight = QFont.Weight.Normal) -> str:
        return QFontMetricsF(self.font_px(px, weight)).elidedText(
            s, Qt.TextElideMode.ElideRight, width)

    @staticmethod
    def rounded(p: QPainter, r: QRectF, color: QColor | str, radius: float = 3.0) -> None:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color) if isinstance(color, str) else color)
        rad = min(radius, r.height() / 2, max(0.0, r.width() / 2))
        p.drawRoundedRect(r, rad, rad)

    def hatch(self, p: QPainter, r: QRectF, fg: QColor, bg: QColor, radius: float = 3.0,
              step: float = 4.0) -> None:
        """Diagonal 45° stripes inside ``r`` (the "unconfirmed / planned" texture)."""
        path = QPainterPath()
        rad = min(radius, r.height() / 2, max(0.0, r.width() / 2))
        path.addRoundedRect(r, rad, rad)
        p.save()
        p.setClipPath(path)
        p.fillRect(r, bg)
        p.setPen(QPen(fg, 1.4))
        x = r.left() - r.height()
        while x < r.right():
            p.drawLine(QPointF(x, r.bottom()), QPointF(x + r.height(), r.top()))
            x += step
        p.restore()

    def dashed(self, color: QColor | str, width: float = 1.0,
               pattern: tuple[float, float] = (3, 3)) -> QPen:
        pen = QPen(QColor(color) if isinstance(color, str) else color, width)
        pen.setDashPattern(list(pattern))
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        return pen

    def begin(self) -> QPainter:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        return p

    def legend(self, p: QPainter, x: float, y: float,
               items: list[tuple[str, Callable[[QPainter, QRectF], None]]],
               align_right: bool = False) -> None:
        """Swatch + label pairs on one line, vertically centred on ``y``."""
        t = self.tokens
        widths = [14 + self.text_width(label, 10, voice="mono") + 18 for label, _ in items]
        if align_right:
            x -= sum(widths) - 18
        for (label, swatch), w in zip(items, widths, strict=True):
            swatch(p, QRectF(x, y - 4.5, 9, 9))
            self.text(p, x + 14, y, label, t.muted, 10, voice="mono")
            x += w

    # -- events
    def _hit_at(self, pos: QPointF) -> Hit | None:
        for h in reversed(self.hits):
            if h.rect.contains(pos):
                return h
        return None

    def mouseMoveEvent(self, e: QMouseEvent) -> None:  # noqa: N802 - Qt API
        h = self._hit_at(e.position())
        key = h.key if h else None
        if h:
            QToolTip.showText(e.globalPosition().toPoint(), h.tip, self)
        else:
            QToolTip.hideText()
        self.setCursor(Qt.CursorShape.PointingHandCursor if h and h.url
                       else Qt.CursorShape.ArrowCursor)
        if key != self.hover_key:
            self.hover_key = key
            self.hovered.emit(key)
            self.update()

    def leaveEvent(self, e) -> None:  # noqa: N802 - Qt API
        QToolTip.hideText()
        if self.hover_key is not None:
            self.hover_key = None
            self.update()
        super().leaveEvent(e)

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:  # noqa: N802 - Qt API
        h = self._hit_at(e.position())
        if h and h.url and e.button() == Qt.MouseButton.LeftButton:
            QDesktopServices.openUrl(QUrl(h.url))
        super().mouseReleaseEvent(e)

    def paintEvent(self, e: QPaintEvent) -> None:  # noqa: N802 - Qt API
        self.hits = []
        if self.data is None:
            return
        p = self.begin()
        try:
            self.paint(p)
        finally:
            p.end()

    def paint(self, p: QPainter) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


# ================================================================ milestones


class MilestoneTimelineChart(Chart):
    """One row per open milestone on a shared date axis, with a dashed "today" line.

    The track runs to the due date; the solid part is closed/total, the hatched
    amber part is "may be done" (heuristic). Milestones without a due date span
    the axis, faded.
    """

    ROW_H = 38
    TOP = 30
    BOTTOM = 30
    LABEL_W = 132
    RIGHT_W = 170
    BAR_H = 10

    def preferred_height(self) -> int:
        n = len(self.data.bars) if self.data else 0
        return self.TOP + max(1, n) * self.ROW_H + self.BOTTOM

    def x_of(self, d: date) -> float:
        tl: Timeline = self.data
        x0, x1 = self.LABEL_W, self.width() - self.RIGHT_W
        return x0 + (x1 - x0) * tl.frac(d)

    def paint(self, p: QPainter) -> None:
        t, tl = self.tokens, self.data
        x0, x1 = self.LABEL_W, self.width() - self.RIGHT_W
        top, rows_h = self.TOP, max(1, len(tl.bars)) * self.ROW_H
        bottom = top + rows_h

        # week gridlines + tick labels (a label under the "Today" pill is skipped)
        today_x = self.x_of(tl.today)
        pill_half = (self.text_width("Today", 10, voice="mono") + 14) / 2
        for d in tl.ticks:
            x = round(self.x_of(d)) + 0.5
            p.setPen(QPen(qc(t.border), 1))
            p.drawLine(QPointF(x, top - 4), QPointF(x, bottom))
            half = self.text_width(fmt_day(d), 10, voice="mono") / 2
            if abs(x - today_x) > pill_half + half + 4:
                self.text(p, x, top - 15, fmt_day(d), t.faint, 10, "center", voice="mono")

        if not tl.bars:
            self.text(p, x0, top + self.ROW_H / 2, "No open milestones.", t.muted, 13)

        prog = self._progress
        nbars = len(tl.bars)
        for i, b in enumerate(tl.bars):
            cy = top + i * self.ROW_H + self.ROW_H / 2
            # track grows first, then the closed fill and the hatch sweep in
            pi = stagger(prog, i, nbars, 0.7)
            p_track, p_fill = ease_out(phase(pi, 0.0, 0.6)), ease_out(phase(pi, 0.25, 1.0))
            hovered = self.hover_key == b.number
            if hovered:
                self.rounded(p, QRectF(0, cy - self.ROW_H / 2 + 2, self.width(), self.ROW_H - 4),
                             qc(t.fill), 4)
            faded = b.due is None
            title = self.elide(b.title, self.LABEL_W - 16, 13, QFont.Weight.Medium)
            self.text(p, 0, cy, title, t.muted if faded else t.text, 13, weight=QFont.Weight.Medium)
            end_x = self.x_of(b.due) if b.due else x1
            start_x = x0
            full_track = QRectF(start_x, cy - self.BAR_H / 2, max(2.0, end_x - start_x),
                                self.BAR_H)
            track = QRectF(full_track)
            track.setWidth(max(2.0, full_track.width() * p_track))
            overdue = b.due is not None and b.due < tl.today
            if faded:
                p.setOpacity(0.55)
            self.rounded(p, track, qc(t.fill2), 3)
            if b.total:
                w_closed = full_track.width() * b.closed / b.total * p_fill
                w_maybe = full_track.width() * b.maybe / b.total * p_fill
                if w_maybe > 0:
                    r = QRectF(track.left(), track.top(), w_closed + w_maybe, track.height())
                    self.hatch(p, r, qc(t.amber, 0.9), blend(t.amber, t.bg, 0.72), 3)
                if w_closed > 0:
                    self.rounded(p, QRectF(track.left(), track.top(), w_closed, track.height()),
                                 qc(t.green), 3)
            if faded:
                p.setPen(self.dashed(t.faint))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(track.adjusted(0.5, 0.5, -0.5, -0.5), 3, 3)
                p.setOpacity(1.0)
            elif p_track > 0.98:
                # due marker: a small diamond at the due date
                p.setOpacity(phase(pi, 0.55, 0.8))
                c = QColor(t.red if overdue else t.text)
                p.setPen(QPen(qc(t.bg), 2))
                p.setBrush(c)
                s = 5.5
                diamond = QPainterPath()
                diamond.moveTo(end_x, cy - s)
                diamond.lineTo(end_x + s, cy)
                diamond.lineTo(end_x, cy + s)
                diamond.lineTo(end_x - s, cy)
                diamond.closeSubpath()
                p.drawPath(diamond)
                p.setOpacity(1.0)
            label_x = (end_x + 12) if not faded else x1 + 12
            p.setOpacity(phase(pi, 0.6, 1.0))
            self.text(p, label_x, cy, b.label, t.faint if faded else t.muted, 10, voice="mono")
            p.setOpacity(1.0)
            when = ("no due date" if b.due is None else
                    f"due {fmt_day(b.due)} ({_days_phrase((b.due - tl.today).days)})")
            maybe = (f"; {b.maybe} more may be done (unconfirmed: commits resemble them)"
                     if b.maybe else "")
            tip = f"{b.title}: {when}\n{b.closed} of {b.total} issues closed{maybe}"
            if b.url:
                tip += "\nClick to open the milestone on GitHub"
            self.hits.append(Hit(QRectF(0, cy - self.ROW_H / 2, self.width(), self.ROW_H), tip,
                                 b.number, b.url))

        # today line (on top of the bars); it pulses softly once as the load ends
        tx = round(self.x_of(tl.today)) + 0.5
        glow = motion.pulse(phase(prog, 0.62, 1.0)) if prog < 1.0 else 0.0
        if glow > 0:
            for wdt, a in ((9, 0.10), (5, 0.18)):
                p.setPen(QPen(qc(t.red, a * glow), wdt, Qt.PenStyle.SolidLine,
                              Qt.PenCapStyle.RoundCap))
                p.drawLine(QPointF(tx, top - 6), QPointF(tx, bottom + 2))
        p.setOpacity(0.35 + 0.65 * phase(prog, 0.0, 0.25))
        p.setPen(self.dashed(t.red, 1.2, (4, 3)))
        p.drawLine(QPointF(tx, top - 6), QPointF(tx, bottom + 2))
        tw = self.text_width("Today", 10, voice="mono") + 14
        pill = QRectF(tx - tw / 2, top - 24, tw, 16)
        if glow > 0:
            g = 3 * glow
            self.rounded(p, pill.adjusted(-g, -g, g, g), qc(t.red, 0.22 * glow), 8 + g)
        self.rounded(p, pill, qc(t.red), 8)
        self.text(p, tx, pill.center().y(), "Today", t.bg if t.name == "dark" else t.on_accent,
                  10, "center", voice="mono")
        p.setOpacity(1.0)

        # legend
        ly = bottom + self.BOTTOM / 2 + 3
        self.legend(p, x0, ly, [
            ("closed", lambda q, r: self.rounded(q, r, qc(t.green), 2)),
            ("may be done", lambda q, r: self.hatch(q, r, qc(t.amber), blend(t.amber, t.bg, 0.7),
                                                    2, 3.5)),
            ("open", lambda q, r: self.rounded(q, r, qc(t.fill2), 2)),
            ("due date", lambda q, r: _diamond_swatch(q, r, t.text)),
        ])


def _diamond_swatch(p: QPainter, r: QRectF, color: str) -> None:
    c = r.center()
    s = 4.5
    path = QPainterPath()
    path.moveTo(c.x(), c.y() - s)
    path.lineTo(c.x() + s, c.y())
    path.lineTo(c.x(), c.y() + s)
    path.lineTo(c.x() - s, c.y())
    path.closeSubpath()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    p.drawPath(path)


def _days_phrase(n: int) -> str:
    if n == 0:
        return "today"
    if n > 0:
        return f"in {n} day{'s' if n != 1 else ''}"
    return f"{-n} day{'s' if n != -1 else ''} ago"


# ================================================================ contribution grid


class ContributionGridChart(Chart):
    """GitHub-style commits per day: columns are weeks, rows Monday..Sunday."""

    LEFT = 30
    TOP = 18
    GAP = 3
    LEGEND_H = 26

    def cell(self) -> float:
        g: ContributionGrid = self.data
        avail = max(100, self.width() - self.LEFT)
        return max(9.0, min(15.0, avail / g.weeks - self.GAP))

    def preferred_height(self) -> int:
        return int(self.TOP + 7 * (15 + self.GAP) + self.LEGEND_H)

    def resizeEvent(self, e) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(e)
        if self.data is not None:
            self.setFixedHeight(int(self.TOP + 7 * (self.cell() + self.GAP) + self.LEGEND_H))

    def paint(self, p: QPainter) -> None:
        t, g = self.tokens, self.data
        c = self.cell()
        step = c + self.GAP
        right_edge = self.LEFT + g.weeks * step - self.GAP
        for col, name in g.months:
            x = self.LEFT + col * step
            if x + self.text_width(name, 10, voice="mono") > right_edge:
                x = right_edge - self.text_width(name, 10, voice="mono")
            self.text(p, x, self.TOP - 9, name, t.faint, 10, voice="mono")
        for row, name in ((0, "Mon"), (2, "Wed"), (4, "Fri")):
            self.text(p, 0, self.TOP + row * step + c / 2, name, t.faint, 10, voice="mono")
        prog = self._progress
        cols = max(1, g.weeks)
        for d in g.days:
            r = QRectF(self.LEFT + d.col * step, self.TOP + d.row * step, c, c)
            # week columns fill in left to right; each cell eases from the empty colour
            pc = ease_out(stagger(prog, d.col, cols, 0.18)) if prog < 1.0 else 1.0
            if pc >= 1.0 or d.level == 0:
                self.rounded(p, r, qc(t.grid[d.level]), 2.5)
            else:
                self.rounded(p, r, qc(t.grid[0]), 2.5)
                if pc > 0:
                    k = 0.55 + 0.45 * pc
                    inner = QRectF(r.center().x() - c * k / 2, r.center().y() - c * k / 2,
                                   c * k, c * k)
                    self.rounded(p, inner, qc(t.grid[d.level], pc), 2.5)
            if d.day == g.today:  # today is live: the signal ring
                p.setPen(QPen(qc(t.signal, 0.95), 1.5))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(r.adjusted(-1, -1, 1, 1), 3, 3)
            if self.hover_key == d.day:
                p.setPen(QPen(qc(t.muted), 1.2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(r.adjusted(-0.6, -0.6, 0.6, 0.6), 3, 3)
            self.hits.append(Hit(r.adjusted(-1.5, -1.5, 1.5, 1.5), g.tooltip(d), d.day))
        # legend: Less [] [] [] [] [] More, right-aligned under the grid
        right = self.LEFT + g.weeks * step - self.GAP
        y = self.TOP + 7 * step + self.LEGEND_H / 2 + 1
        mw = self.text_width("More", 10, voice="mono")
        x = right - mw
        self.text(p, x, y, "More", t.faint, 10, voice="mono")
        x -= 6
        sw = 10
        for lvl in range(4, -1, -1):
            x -= sw
            self.rounded(p, QRectF(x, y - sw / 2, sw, sw), qc(t.grid[lvl]), 2)
            self.hits.append(Hit(QRectF(x, y - sw / 2, sw, sw),
                                 ("No commits", "1 commit", "2–3 commits", "4–5 commits",
                                  "6 or more commits")[lvl] + " per day", ("legend", lvl)))
            x -= 3
        self.text(p, x - 3, y, "Less", t.faint, 10, "right", voice="mono")


# ================================================================ issues by phase


class PhaseBarsChart(Chart):
    """Horizontal stacked bars: open (the ink) then closed (a soft tint of it) per phase."""

    ROW_H = 30
    LABEL_W = 84
    RIGHT_W = 96
    BAR_H = 12
    LEGEND_H = 26

    def preferred_height(self) -> int:
        n = len(self.data.bars) if self.data else 0
        return max(1, n) * self.ROW_H + self.LEGEND_H

    def paint(self, p: QPainter) -> None:
        t, pb = self.tokens, self.data
        x0, x1 = self.LABEL_W, self.width() - self.RIGHT_W
        scale = (x1 - x0) / max(1, pb.max_total)
        if not pb.bars:
            self.text(p, 0, self.ROW_H / 2, "No issues synced yet.", t.muted, 13)
        open_c, closed_c = qc(t.series[0]), qc(t.tint(0.36))
        nb = len(pb.bars)
        for i, b in enumerate(pb.bars):
            cy = i * self.ROW_H + self.ROW_H / 2
            pi = stagger(self._progress, i, nb, 0.6)
            grow = ease_out(phase(pi, 0.0, 0.85))
            if self.hover_key == b.label:
                self.rounded(p, QRectF(0, cy - self.ROW_H / 2 + 2, self.width(), self.ROW_H - 4),
                             qc(t.fill), 4)
            self.text(p, 0, cy, b.label, t.text, 11, voice="mono")
            x = x0
            w_open, w_closed = b.open * scale * grow, b.closed * scale * grow
            gap = 2 if w_open and w_closed else 0
            if w_open:
                self.rounded(p, QRectF(x, cy - self.BAR_H / 2, w_open, self.BAR_H), open_c, 3)
                x += w_open + gap
            if w_closed:
                self.rounded(p, QRectF(x, cy - self.BAR_H / 2, w_closed - gap, self.BAR_H),
                             closed_c, 3)
                x += w_closed - gap
            if not b.total:
                self.rounded(p, QRectF(x0, cy - 1, 4, 2), qc(t.fill2), 1)
            vx = x + 8
            p.setOpacity(phase(pi, 0.45, 1.0))
            w = self.text(p, vx, cy, f"{b.open}", t.text, 16, voice="num")
            self.text(p, vx + w + 5, cy, "open" + (f" · {b.closed} closed" if b.closed else ""),
                      t.faint, 12)
            p.setOpacity(1.0)
            nums = ", ".join(f"#{n}" for n in b.open_issues[:14])
            more = f" and {len(b.open_issues) - 14} more" if len(b.open_issues) > 14 else ""
            tip = f"{b.label}: {b.open} open, {b.closed} closed"
            if nums:
                tip += f"\nOpen: {nums}{more}"
            self.hits.append(Hit(QRectF(0, cy - self.ROW_H / 2, self.width(), self.ROW_H),
                                 tip, b.label))
        ly = len(pb.bars) * self.ROW_H + self.LEGEND_H / 2 + 2
        self.legend(p, x0, ly, [
            ("open", lambda q, r: self.rounded(q, r, open_c, 2)),
            ("closed", lambda q, r: self.rounded(q, r, closed_c, 2)),
        ])


# ================================================================ power: current budget


class RailBudgetChart(Chart):
    """Worst-case current by part, stacked, against the rail's rating.

    Unknown rating: a dashed box up to the current that keeps the load at 80 %,
    labelled "rating ?". Known rating: a solid marker at the rating and a dashed
    one at 80 % of it.
    """

    TOP = 26
    BAR_H = 22
    AXIS_H = 22
    LEGEND_H = 24
    LEFT = 0
    RIGHT = 10

    def __init__(self, tokens: Tokens, colors: dict[str, str] | None = None,
                 parent: QWidget | None = None):
        super().__init__(tokens, parent)
        self.colors = colors or {}

    def preferred_height(self) -> int:
        return self.TOP + self.BAR_H + self.AXIS_H + self.LEGEND_H

    def x_of(self, amps: float) -> float:
        b: RailBudget = self.data
        return self.LEFT + (self.width() - self.LEFT - self.RIGHT) * amps / b.scale_max

    def color(self, part: str, i: int) -> QColor:
        return QColor(self.colors.get(part) or self.tokens.series[i % len(self.tokens.series)])

    def paint(self, p: QPainter) -> None:
        t, b = self.tokens, self.data
        y = self.TOP
        bar = QRectF(self.LEFT, y, self.width() - self.LEFT - self.RIGHT, self.BAR_H)
        # the scale's track
        self.rounded(p, QRectF(bar.left(), bar.center().y() - 1, bar.width(), 2), qc(t.fill2), 1)

        if b.rating_a is None and b.need_80_a > 0:
            box = QRectF(self.x_of(0), y - 5, self.x_of(b.need_80_a) - self.x_of(0), self.BAR_H + 10)
            p.setPen(self.dashed(t.muted, 1.0, (3, 3)))
            p.setBrush(qc(t.fill, 0.6))
            p.drawRoundedRect(box.adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)
            self.text(p, box.right() - 8, y - 14, f"rating ?  ·  needs ≥ {fmt_num(b.need_80_a)} A "
                      "for 80%", t.muted, 10, "right", voice="mono")
            self.hits.append(Hit(box, f"The {b.rail} supply's current rating isn't in "
                                 f"hardware.yaml.\nTo keep the worst case at or under 80% it needs "
                                 f"at least {fmt_num(b.need_80_a)} A.", "rating"))

        x = self.x_of(0)
        segs = [s for s in b.segments if s.amps]
        prog = self._progress
        grow = ease_out(phase(prog, 0.0, 0.75))
        for i, s in enumerate(segs):
            w = (self.x_of(s.amps) - self.x_of(0)) * grow
            gap = 2 if i < len(segs) - 1 else 0
            r = QRectF(x, y, max(1.5, w - gap), self.BAR_H)
            col = self.color(s.part, b.segments.index(s))
            if s.planned:
                self.hatch(p, r, col, blend(col.name(), t.bg, 0.75), 3)
            else:
                self.rounded(p, r, col, 3)
            if self.hover_key == s.part:
                p.setPen(QPen(qc(t.text, 0.7), 1.2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(r.adjusted(-1, -1, 1, 1), 4, 4)
            each = (f"{s.count} × {fmt_num(s.peak_a)} A = {fmt_num(s.amps)} A" if s.count > 1
                    else f"{fmt_num(s.amps)} A")
            tip = f"{s.part} {s.state}{' (planned, not counted)' if s.planned else ''}: {each}"
            self.hits.append(Hit(r, tip, s.part))
            if w > 60 and grow > 0.9:
                p.setOpacity(phase(prog, 0.65, 0.95))
                self.text(p, r.left() + 7, r.center().y(), fmt_num(s.amps) + " A",
                          on_color(col, t), 13, voice="num")
                p.setOpacity(1.0)
            x += w

        if b.rating_a is not None:
            for amps, label, solid in ((b.rating_a * 0.8, "80%", False),
                                       (b.rating_a, f"rating {fmt_num(b.rating_a)} A", True)):
                mx = round(self.x_of(amps)) + 0.5
                p.setPen(QPen(qc(t.text), 1.6) if solid else self.dashed(t.muted, 1.2))
                p.drawLine(QPointF(mx, y - 6), QPointF(mx, y + self.BAR_H + 6))
                self.text(p, mx, y - 14, label, t.text if solid else t.muted, 10, "center",
                          voice="mono")
        total = b.worst_case_a
        p.setOpacity(phase(prog, 0.7, 1.0))
        if total:
            tx = self.x_of(total + b.planned_a) + 8
            label = f"{fmt_num(total)} A"
            if tx + self.text_width(label, 18, voice="num") < self.width() - self.RIGHT:
                self.text(p, tx, y + self.BAR_H / 2, label, t.text, 18, voice="num")

        p.setOpacity(1.0)
        # axis
        ay = y + self.BAR_H + 13
        step = _nice_step(b.scale_max, 6)
        v = 0.0
        while v <= b.scale_max + 1e-9:
            self.text(p, self.x_of(v), ay, f"{fmt_num(v)} A", t.faint, 10,
                      "left" if v == 0 else "center", voice="mono")
            v += step
        # legend
        ly = y + self.BAR_H + self.AXIS_H + self.LEGEND_H / 2
        items = []
        for i, s in enumerate(b.segments):
            col = self.color(s.part, i)
            if s.planned:
                items.append((s.label, lambda q, r, c=col: self.hatch(
                    q, r, c, blend(c.name(), t.bg, 0.75), 2, 3)))
            else:
                items.append((s.label, lambda q, r, c=col: self.rounded(q, r, c, 2)))
        self.legend(p, self.LEFT, ly, items)


def _nice_step(span: float, target: int) -> float:
    raw = span / max(1, target)
    for s in (0.01, 0.02, 0.025, 0.05, 0.1, 0.2, 0.25, 0.5, 1, 2, 2.5, 5, 10):
        if s >= raw:
            return s
    return 10.0


# ================================================================ power: voltage window


class VoltageWindowChart(Chart):
    """Each part's allowed supply range on one voltage axis, with the rail's band.

    The band is nominal ± tolerance (or the ±5 % example, dashed, when the
    tolerance is unknown); a red segment marks where the band leaves a part's range.
    """

    TOP = 26
    ROW_H = 30
    AXIS_H = 24
    LABEL_W = 120
    RIGHT = 16
    BAR_H = 10

    def __init__(self, tokens: Tokens, colors: dict[str, str] | None = None,
                 parent: QWidget | None = None):
        super().__init__(tokens, parent)
        self.colors = colors or {}

    def preferred_height(self) -> int:
        n = len(self.data.parts) if self.data else 1
        return self.TOP + n * self.ROW_H + self.AXIS_H

    def set_data(self, data, animate: bool = False) -> None:
        names = [r.part for r in data.parts] if data else []
        widest = max((self.text_width(n, 13) for n in names), default=60)
        # name column, plus room for the left value label ("4.8") before the axis starts
        self.LABEL_W = int(min(190, widest + 46))
        super().set_data(data, animate)

    def x_of(self, v: float) -> float:
        w: VoltageWindow = self.data
        x0, x1 = self.LABEL_W, self.width() - self.RIGHT - 18
        return x0 + (x1 - x0) * (v - w.axis_lo) / max(1e-6, w.axis_hi - w.axis_lo)

    def paint(self, p: QPainter) -> None:
        t, w = self.tokens, self.data
        top = self.TOP
        bottom = top + len(w.parts) * self.ROW_H
        # gridlines
        for v in w.ticks:
            x = round(self.x_of(v)) + 0.5
            p.setPen(QPen(qc(t.border), 1))
            p.drawLine(QPointF(x, top - 2), QPointF(x, bottom))
            self.text(p, x, bottom + 12, f"{fmt_num(v)} V", t.faint, 10, "center", voice="mono")
        prog = self._progress
        # supply band (fades in first)
        p.setOpacity(ease_out(phase(prog, 0.0, 0.4)))
        bl, bh = self.x_of(w.band_lo), self.x_of(w.band_hi)
        band = QRectF(bl, top - 4, bh - bl, bottom - top + 6)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qc(t.ink, 0.07 if t.name == "light" else 0.08))
        p.drawRect(band)
        if w.example:
            p.setPen(self.dashed(qc(t.ink, 0.45), 1.0, (3, 3)))
            p.drawLine(QPointF(bl + 0.5, top - 4), QPointF(bl + 0.5, bottom + 2))
            p.drawLine(QPointF(bh - 0.5, top - 4), QPointF(bh - 0.5, bottom + 2))
        nx = round(self.x_of(w.nominal_v)) + 0.5
        p.setPen(self.dashed(t.text, 1.0, (3, 2)))
        p.drawLine(QPointF(nx, top - 6), QPointF(nx, bottom + 2))
        tol = fmt_num(w.tolerance_pct) if w.tolerance_pct is not None else "5"
        label = (f"{fmt_num(w.nominal_v)} V ± {tol}%" + (" (example)" if w.example else ""))
        self.text(p, nx, top - 14, label, t.muted, 10, "center", voice="mono")
        self.hits.append(Hit(band, (
            f"Supply band {fmt_num(w.band_lo)}–{fmt_num(w.band_hi)} V "
            + (f"(an example ±{tol}%: the {w.rail} tolerance isn't in hardware.yaml)"
               if w.example else f"({fmt_num(w.nominal_v)} V ± {tol}%)")), "band"))

        p.setOpacity(1.0)
        n_parts = len(w.parts)
        for i, r in enumerate(w.parts):
            cy = top + i * self.ROW_H + self.ROW_H / 2
            # bars grow from their low end; red segments fade in last
            pi = stagger(phase(prog, 0.1, 0.8), i, n_parts, 0.7)
            grow, p_red = ease_out(pi), ease_out(phase(prog, 0.78, 1.0))
            self.text(p, 0, cy, self.elide(r.part, self.LABEL_W - 40, 13), t.text, 13)
            lo = r.v_min if r.v_min is not None else w.axis_lo
            hi = r.v_max if r.v_max is not None else w.axis_hi
            xr = QRectF(self.x_of(lo), cy - self.BAR_H / 2,
                        max(0.5, (self.x_of(hi) - self.x_of(lo)) * grow), self.BAR_H)
            col = QColor(self.colors.get(r.part) or t.series[i % len(t.series)])
            fill = blend(col.name(), t.bg, 0.15)
            if r.planned:
                self.hatch(p, xr, col, blend(col.name(), t.bg, 0.78), 3)
            else:
                self.rounded(p, xr, fill, 3)
            # violations: where the band leaves this part's range
            red = qc(t.red, p_red)
            if r.low_gap is not None and r.low_gap > 0:
                vr = QRectF(self.x_of(w.band_lo), cy - self.BAR_H / 2 - 2,
                            self.x_of(r.v_min) - self.x_of(w.band_lo), self.BAR_H + 4)
                self.rounded(p, vr, red, 2)
            if r.high_gap is not None and r.high_gap > 0:
                vr = QRectF(self.x_of(r.v_max), cy - self.BAR_H / 2 - 2,
                            self.x_of(w.band_hi) - self.x_of(r.v_max), self.BAR_H + 4)
                self.rounded(p, vr, red, 2)
            # end values, outside any red segment
            p.setOpacity(phase(pi, 0.6, 1.0))
            full_right = self.x_of(hi)
            if r.v_min is not None:
                lx = xr.left()
                if r.low_gap is not None and r.low_gap > 0:
                    lx = self.x_of(w.band_lo)
                self.text(p, lx - 6, cy, fmt_num(r.v_min), t.muted, 10, "right", voice="mono")
            if r.v_max is not None:
                rx = full_right
                if r.high_gap is not None and r.high_gap > 0:
                    rx = self.x_of(w.band_hi)
                self.text(p, rx + 6, cy, fmt_num(r.v_max), t.muted, 10, voice="mono")
            p.setOpacity(1.0)
            if self.hover_key == r.part:
                p.setPen(QPen(qc(t.text, 0.6), 1.2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(xr.adjusted(-1.5, -1.5, 1.5, 1.5), 4, 4)
            lim = f"{fmt_num(r.v_min) if r.v_min is not None else '?'}–" \
                  f"{fmt_num(r.v_max) if r.v_max is not None else '?'} V"
            notes = []
            if r.low_gap is not None and r.low_gap > 0:
                notes.append(f"the band goes {fmt_num(r.low_gap)} V below its minimum")
            if r.high_gap is not None and r.high_gap > 0:
                notes.append(f"the band goes {fmt_num(r.high_gap)} V above its maximum")
            tip = f"{r.part}{' (planned)' if r.planned else ''} accepts {lim}" + ("\n" + "; ".join(notes).capitalize()
                                                if notes else "\nThe band fits inside its range")
            self.hits.append(Hit(QRectF(0, cy - self.ROW_H / 2, self.width(), self.ROW_H), tip,
                                 r.part))


def part_colors(tokens: Tokens, budget: RailBudget | None,
                window: VoltageWindow | None) -> dict[str, str]:
    """One colour per part, shared by a rail's two charts (fixed categorical order)."""
    names: list[str] = []
    for s in (budget.segments if budget else ()):
        if s.part not in names:
            names.append(s.part)
    for r in (window.parts if window else ()):
        if r.part not in names:
            names.append(r.part)
    return {n: tokens.series[i % len(tokens.series)] for i, n in enumerate(names)}


__all__ = [
    "Chart",
    "ContributionGridChart",
    "MilestoneTimelineChart",
    "PhaseBarsChart",
    "RailBudgetChart",
    "VoltageWindowChart",
    "part_colors",
]
