"""Home's building blocks: cards, icons, dots, pills, bars, the week chart and the
banner (Qt; painted with QPainter, styled by gui/home_style.py).

Nothing here reads the database: the widgets show what HomeView gives them.
Colours come from the HomePalette by meaning (a tone or a category), never
per widget. Animations go through gui/motion, so reduced motion turns them off.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Property, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from jarvis.gui import motion
from jarvis.gui.home_style import PALETTE, HomePalette
from jarvis.gui.theme import typeset

IMAGES = Path(__file__).with_name("images")
USER_BANNER = Path("~/.config/jarvis/banner.jpg")   # the user's own banner, if they add one


def shadowed(w: QWidget, blur: float = 14.0) -> QWidget:
    """A soft dark shadow behind text that sits on a photo, so it stays readable."""
    from PySide6.QtWidgets import QGraphicsDropShadowEffect
    fx = QGraphicsDropShadowEffect(w)
    fx.setBlurRadius(blur)
    fx.setOffset(0, 1)
    fx.setColor(QColor(0, 0, 0, 200))
    w.setGraphicsEffect(fx)
    return w


def label(text: str = "", name: str = "", role: str = "body", wrap: bool = False) -> QLabel:
    lb = QLabel(text)
    lb.setTextFormat(Qt.TextFormat.PlainText)
    if name:
        lb.setObjectName(name)
    if wrap:
        lb.setWordWrap(True)
    typeset(lb, role)
    return lb


def elided(text: str, name: str = "", role: str = "body") -> QLabel:
    """A one-line label that shrinks (its full text in the tooltip)."""
    lb = label(text, name, role)
    lb.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    lb.setMinimumWidth(20)
    lb.setToolTip(text)
    return lb


# ---------------------------------------------------------------- icons


def paint_glyph(p: QPainter, key: str, box: QRectF, color: QColor) -> None:
    """A small line icon (drawn on a 24-unit grid into ``box``)."""
    s = box.width() / 24.0

    def pt(x: float, y: float) -> QPointF:
        return QPointF(box.left() + x * s, box.top() + y * s)

    p.setPen(QPen(color, max(1.3, 1.7 * s), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                  Qt.PenJoinStyle.RoundJoin))
    p.setBrush(Qt.BrushStyle.NoBrush)
    path = QPainterPath()
    if key == "focus":
        path.addEllipse(pt(12, 12), 8 * s, 8 * s)
        path.addEllipse(pt(12, 12), 4 * s, 4 * s)
    elif key in ("calendar", "schedule", "week"):
        path.addRoundedRect(QRectF(pt(4, 5.5), pt(20, 20)), 2.5 * s, 2.5 * s)
        for a, b in ((pt(4, 10), pt(20, 10)), (pt(8.5, 3.5), pt(8.5, 7.5)),
                     (pt(15.5, 3.5), pt(15.5, 7.5))):
            path.moveTo(a)
            path.lineTo(b)
    elif key == "health":
        path.moveTo(pt(3, 13))
        for x, y in ((8, 13), (10.5, 7), (13.5, 18), (16, 12), (21, 12)):
            path.lineTo(pt(x, y))
    elif key == "stats":
        for x, top in ((6, 13), (12, 7), (18, 10)):
            path.moveTo(pt(x, 20))
            path.lineTo(pt(x, top))
    elif key in ("glance", "activity", "clock"):
        path.addEllipse(pt(12, 12), 8.5 * s, 8.5 * s)
        path.moveTo(pt(12, 7.5))
        path.lineTo(pt(12, 12))
        path.lineTo(pt(15.5, 14))
    elif key in ("snapshot", "folder"):
        path.moveTo(pt(3.5, 7))
        path.lineTo(pt(9.5, 7))
        path.lineTo(pt(11.5, 9))
        path.lineTo(pt(20.5, 9))
        path.lineTo(pt(20.5, 19))
        path.lineTo(pt(3.5, 19))
        path.closeSubpath()
    elif key == "flag":
        path.moveTo(pt(6, 21))
        path.lineTo(pt(6, 4))
        path.lineTo(pt(18, 4))
        path.lineTo(pt(15, 8.5))
        path.lineTo(pt(18, 13))
        path.lineTo(pt(6, 13))
    elif key == "bolt":
        path.moveTo(pt(13.5, 3))
        path.lineTo(pt(5.5, 13.5))
        path.lineTo(pt(11.5, 13.5))
        path.lineTo(pt(10.5, 21))
        path.lineTo(pt(18.5, 10.5))
        path.lineTo(pt(12.5, 10.5))
        path.closeSubpath()
    elif key == "plus":
        path.addRoundedRect(QRectF(pt(4, 4), pt(20, 20)), 3 * s, 3 * s)
        path.moveTo(pt(12, 8))
        path.lineTo(pt(12, 16))
        path.moveTo(pt(8, 12))
        path.lineTo(pt(16, 12))
    elif key == "chat":
        path.moveTo(pt(4, 6))
        path.lineTo(pt(20, 6))
        path.lineTo(pt(20, 16))
        path.lineTo(pt(11, 16))
        path.lineTo(pt(7, 19.5))
        path.lineTo(pt(7, 16))
        path.lineTo(pt(4, 16))
        path.closeSubpath()
    elif key == "book":
        path.moveTo(pt(12, 6.5))
        path.cubicTo(pt(9.5, 4.8), pt(6, 4.6), pt(3, 5.5))
        path.lineTo(pt(3, 18.5))
        path.cubicTo(pt(6, 17.6), pt(9.5, 17.8), pt(12, 19.5))
        path.cubicTo(pt(14.5, 17.8), pt(18, 17.6), pt(21, 18.5))
        path.lineTo(pt(21, 5.5))
        path.cubicTo(pt(18, 4.6), pt(14.5, 4.8), pt(12, 6.5))
        path.lineTo(pt(12, 19.5))
    elif key == "spark":
        path.moveTo(pt(12, 3))
        path.cubicTo(pt(12.8, 9), pt(15, 11.2), pt(21, 12))
        path.cubicTo(pt(15, 12.8), pt(12.8, 15), pt(12, 21))
        path.cubicTo(pt(11.2, 15), pt(9, 12.8), pt(3, 12))
        path.cubicTo(pt(9, 11.2), pt(11.2, 9), pt(12, 3))
    elif key == "bulb":
        path.moveTo(pt(9, 17))
        path.cubicTo(pt(9, 14), pt(6, 12.5), pt(6, 9.5))
        path.cubicTo(pt(6, 6), pt(8.8, 3.5), pt(12, 3.5))
        path.cubicTo(pt(15.2, 3.5), pt(18, 6), pt(18, 9.5))
        path.cubicTo(pt(18, 12.5), pt(15, 14), pt(15, 17))
        path.closeSubpath()
        path.moveTo(pt(9.5, 20.5))
        path.lineTo(pt(14.5, 20.5))
    p.drawPath(path)


class Icon(QWidget):
    def __init__(self, key: str, color: str, px: int = 18, parent: QWidget | None = None):
        super().__init__(parent)
        self.key, self.color = key, color
        self.setFixedSize(px, px)

    def paintEvent(self, e) -> None:  # noqa: N802 - Qt API
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        paint_glyph(p, self.key, QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5),
                    QColor(self.color))
        p.end()


class Dot(QWidget):
    """A status dot; ``ring`` draws a soft halo (the mockup's schedule bullets)."""

    def __init__(self, color: str, px: int = 8, ring: bool = False,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.color, self.ring = color, ring
        self.setFixedSize(px + (6 if ring else 0), px + (6 if ring else 0))

    def paintEvent(self, e) -> None:  # noqa: N802 - Qt API
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        r = QRectF(self.rect())
        if self.ring:
            halo = QColor(self.color)
            halo.setAlphaF(0.25)
            p.setBrush(halo)
            p.drawEllipse(r)
            r = r.adjusted(3, 3, -3, -3)
        p.setBrush(QColor(self.color))
        p.drawEllipse(r)
        p.end()


def pill(text: str, color: str, pal: HomePalette = PALETTE) -> QLabel:
    """A category tag: the colour as text over a faint wash of it."""
    lb = label(text, "hPill", "pill")
    lb.setStyleSheet(f"color: {color}; background: {pal.wash(color, 0.16)}; "
                     f"border: 1px solid {pal.wash(color, 0.35)}; border-radius: 5px; "
                     "padding: 1px 6px;")
    lb.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    return lb


def badge(text: str) -> QLabel:
    lb = label(text, "hBadge", "pill")
    lb.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lb.setMinimumWidth(20)
    return lb


def link(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setObjectName("hLink")
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    typeset(b, "label")
    return b


def button(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setObjectName("hButton")
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    typeset(b, "label")
    return b


# ---------------------------------------------------------------- bars


class Bar(QWidget):
    """A thin progress bar (0..1) in a colour; ``progress`` animates it in."""

    def __init__(self, value: float, color: str, pal: HomePalette = PALETTE,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.value, self.color, self.pal = max(0.0, min(1.0, value)), color, pal
        self._progress = 1.0
        self.setFixedHeight(5)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def get_progress(self) -> float:
        return self._progress

    def set_progress(self, v: float) -> None:
        self._progress = v
        self.update()

    progress = Property(float, get_progress, set_progress)

    def animate_in(self, delay: int = 0) -> None:
        if motion.enabled():
            self._progress = 0.0
            motion.grow(self, b"progress", delay=delay, easing="out")

    def paintEvent(self, e) -> None:  # noqa: N802 - Qt API
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        r = QRectF(self.rect())
        p.setBrush(QColor(self.pal.border_hi))
        p.drawRoundedRect(r, 2.5, 2.5)
        w = r.width() * self.value * self._progress
        if w > 0:
            p.setBrush(QColor(self.color))
            p.drawRoundedRect(QRectF(r.left(), r.top(), max(w, 5), r.height()), 2.5, 2.5)
        p.end()


class WeekBars(QWidget):
    """Mon..Sun, three bars a day (Study, Work, Personal): what is due that day.
    Today's label is the accent. The bars grow in (``progress``)."""

    SERIES = ("Study", "Work", "Personal")

    def __init__(self, pal: HomePalette = PALETTE, parent: QWidget | None = None):
        super().__init__(parent)
        self.pal = pal
        self.days: list[tuple[str, tuple[int, int, int], bool]] = []
        self._progress = 1.0
        self.setMinimumHeight(140)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def get_progress(self) -> float:
        return self._progress

    def set_progress(self, v: float) -> None:
        self._progress = v
        self.update()

    progress = Property(float, get_progress, set_progress)

    def set_week(self, days: list[tuple[str, tuple[int, int, int], bool]]) -> None:
        changed = days != self.days
        self.days = days
        if changed and motion.enabled():
            self._progress = 0.0
            motion.grow(self, b"progress")
        self.update()

    def paintEvent(self, e) -> None:  # noqa: N802 - Qt API
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(4, 6, -4, -2)
        font = QFont(self.font())
        font.setPixelSize(11)
        p.setFont(font)
        label_h = 18
        chart = QRectF(r.left(), r.top(), r.width(), r.height() - label_h)
        # faint guide lines
        p.setPen(QPen(QColor(self.pal.border), 1))
        for k in range(4):
            y = chart.top() + chart.height() * k / 3
            p.drawLine(QPointF(chart.left(), y), QPointF(chart.right(), y))
        if not self.days:
            p.end()
            return
        top = max([1, *[max(v) for _, v, _ in self.days]])
        n = len(self.days)
        slot = chart.width() / n
        bar_w = min(10.0, slot / 5)
        colors = [QColor(self.pal.category(s)) for s in self.SERIES]
        for k, (name, values, today) in enumerate(self.days):
            x0 = chart.left() + slot * k + (slot - bar_w * 3 - 4) / 2
            local = motion.stagger(self._progress, k, n)
            eased = motion.ease_out(local)
            for j, v in enumerate(values):
                h = (chart.height() - 4) * v / top * eased
                if v and h > 0:
                    p.setPen(Qt.PenStyle.NoPen)
                    grad = QLinearGradient(0, chart.bottom() - h, 0, chart.bottom())
                    c = QColor(colors[j])
                    grad.setColorAt(0, c)
                    c2 = QColor(colors[j])
                    c2.setAlphaF(0.55)
                    grad.setColorAt(1, c2)
                    p.setBrush(grad)
                    p.drawRoundedRect(QRectF(x0 + j * (bar_w + 2), chart.bottom() - h, bar_w,
                                             h), 2.5, 2.5)
            p.setPen(QColor(self.pal.accent if today else self.pal.faint))
            p.drawText(QRectF(chart.left() + slot * k, chart.bottom() + 2, slot, label_h),
                       Qt.AlignmentFlag.AlignCenter, name)
        p.end()


# ---------------------------------------------------------------- cards


class Card(QFrame):
    """A card: icon, title, optional count badge and "View all →", then ``body``."""

    view_all = Signal()

    def __init__(self, title: str, icon: str, pal: HomePalette = PALETTE, *,
                 count: bool = False, link_text: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("hCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.pal = pal
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.icon = Icon(icon, pal.accent, 18)
        head.addWidget(self.icon)
        self.title = label(title, "hTitle", "head")
        head.addWidget(self.title)
        self.count = badge("")
        self.count.setVisible(count)
        head.addWidget(self.count)
        head.addStretch(1)
        self.head = head
        lay.addLayout(head)
        self.body = QVBoxLayout()
        self.body.setSpacing(6)
        lay.addLayout(self.body, 1)
        self.link = None
        if link_text:
            self.link = link(link_text)
            self.link.clicked.connect(self.view_all)
            lay.addWidget(self.link, 0, Qt.AlignmentFlag.AlignLeft)

    def set_count(self, n: int | None) -> None:
        self.count.setText(str(n) if n is not None else "")
        self.count.setVisible(bool(n))


def clear(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()
        elif item.layout() is not None:
            clear(item.layout())


def empty(text: str, hint: str = "") -> QWidget:
    w = QWidget()
    col = QVBoxLayout(w)
    col.setContentsMargins(0, 2, 0, 2)
    col.setSpacing(2)
    col.addWidget(label(text, "hEmpty", "body", wrap=True))
    if hint:
        col.addWidget(label(hint, "hFaint", "meta", wrap=True))
    return w


class Row(QPushButton):
    """A clickable row: a dot, a title and a quiet second line, optional right text."""

    def __init__(self, title: str, sub: str, color: str, right: str = "",
                 right_color: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("hRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.title_text = title
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(10)
        lay.addWidget(Dot(color, 8), 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        col = QVBoxLayout()
        col.setSpacing(1)
        self.title = elided(title, "", "body")
        col.addWidget(self.title)
        if sub:
            self.sub = elided(sub, "hMuted", "meta")
            col.addWidget(self.sub)
        lay.addLayout(col, 1)
        if right:
            r = label(right, "hMuted", "meta")
            if right_color:
                r.setStyleSheet(f"color: {right_color};")
            lay.addWidget(r, 0, Qt.AlignmentFlag.AlignTop)
        self.setMinimumHeight(lay.sizeHint().height())

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return self.layout().sizeHint()


class SlotRow(QFrame):
    """One thing on a day in Today's Schedule: time, dot, title, category pill, and
    (the user's own items) a check circle to tick it off. Click: open it."""

    toggled = Signal(int)            # item id
    selected = Signal(int)           # item id
    open_url = Signal(str)

    def __init__(self, slot, pal: HomePalette = PALETTE, parent: QWidget | None = None):
        super().__init__(parent)
        self.slot = slot
        self.row = slot                  # tests and the window read .row.title / .row.id
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 3, 2, 3)
        lay.setSpacing(9)
        color = pal.red if slot.late else pal.category(slot.category)
        self.check = None
        if slot.item_id is not None:          # the user's own item: a circle to tick it off
            self.check = QPushButton()
            self.check.setObjectName("hCheck")
            self.check.setFixedSize(16, 16)
            self.check.setStyleSheet(f"border-color: {color};")
            self.check.setCursor(Qt.CursorShape.PointingHandCursor)
            self.check.setToolTip("Mark done")
            self.check.clicked.connect(lambda: self.toggled.emit(slot.item_id))
            lay.addWidget(self.check, 0, Qt.AlignmentFlag.AlignVCenter)
        else:
            lay.addWidget(Dot(color, 7, ring=True), 0, Qt.AlignmentFlag.AlignVCenter)
        when = label(slot.time or "Any time", "hMuted", "meta")
        if slot.late:
            when.setStyleSheet(f"color: {pal.red};")
        when.setFixedWidth(58)
        lay.addWidget(when)
        self.title = elided(slot.title, "", "label")
        lay.addWidget(self.title, 1)
        lay.addWidget(pill(slot.category, pal.category(slot.category), pal))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(slot.title)

    @property
    def id(self) -> int | None:
        return self.slot.item_id

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802 - Qt API
        if e.button() == Qt.MouseButton.LeftButton:
            if self.slot.item_id is not None:
                self.selected.emit(self.slot.item_id)
            elif self.slot.url:
                self.open_url.emit(self.slot.url)
        super().mouseReleaseEvent(e)


class ActionButton(QPushButton):
    """A quick action: icon, title, a quiet line, and a chevron."""

    def __init__(self, icon: str, title: str, sub: str, pal: HomePalette = PALETTE,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("hAction")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(title)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(10)
        lay.addWidget(Icon(icon, pal.accent, 18))
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addWidget(label(title, "", "body"))
        if sub:
            col.addWidget(label(sub, "hFaint", "meta"))
        lay.addLayout(col, 1)
        lay.addWidget(label("›", "hFaint", "title"))
        self.setMinimumHeight(max(44, lay.sizeHint().height()))


# ---------------------------------------------------------------- the banner


def photo(name: str, today_ordinal: int = 0, images: Path | None = None) -> QPixmap | None:
    """A photo by kind ("banner" / "quote"): the user's own banner if they added
    one, else the bundled ``<kind>-*.jpg`` files, one per day in turn; None when
    there are none (the panel then paints a plain gradient)."""
    if name == "banner" and images is None:
        own = USER_BANNER.expanduser()
        if own.is_file():
            pm = QPixmap(str(own))
            if not pm.isNull():
                return pm
    files = sorted((images or IMAGES).glob(f"{name}-*.jpg"))
    if not files:
        return None
    pm = QPixmap(str(files[today_ordinal % len(files)]))
    return None if pm.isNull() else pm


class PhotoPanel(QFrame):
    """A panel with a photo (cover-scaled, rounded) behind a dark gradient, so text
    on it stays readable; a navy gradient when there is no photo."""

    def __init__(self, pal: HomePalette = PALETTE, radius: float = 12.0,
                 side: str = "left", parent: QWidget | None = None):
        super().__init__(parent)
        self.pal, self.radius, self.side = pal, radius, side
        self.pixmap: QPixmap | None = None
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

    def set_photo(self, pm: QPixmap | None) -> None:
        self.pixmap = pm
        self.update()

    def paintEvent(self, e) -> None:  # noqa: N802 - Qt API
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        clip = QPainterPath()
        clip.addRoundedRect(r, self.radius, self.radius)
        p.setClipPath(clip)
        if self.pixmap is not None:
            pm = self.pixmap.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                    Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap(int((self.width() - pm.width()) / 2),
                         int((self.height() - pm.height()) / 2), pm)
        else:
            g = QLinearGradient(0, 0, 0, r.height())
            g.setColorAt(0, QColor("#1b2a4a"))
            g.setColorAt(1, QColor(self.pal.card))
            p.fillRect(r, g)
        shade = QLinearGradient(0, 0, r.width(), 0)
        dark = QColor(self.pal.bg)
        for at, alpha in ((0.0, 0.68), (0.45, 0.22), (0.75, 0.3), (1.0, 0.55)) \
                if self.side == "left" else ((0.0, 0.15), (1.0, 0.7)):
            c = QColor(dark)
            c.setAlphaF(alpha)
            shade.setColorAt(at, c)
        p.fillRect(r, shade)
        p.setClipping(False)
        p.setPen(QPen(QColor(self.pal.border), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r, self.radius, self.radius)
        p.end()
