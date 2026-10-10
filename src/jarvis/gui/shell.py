"""The application shell's own widgets: the navigation rail and the placeholder
workspaces. The window (window.py) puts them together:

    top bar
    rail | workspaces (QStackedWidget) | chat
    ticker

Workspaces, in rail order (Ctrl+1 .. Ctrl+5): Home, Tasks, School, Projects
(the engineering cockpit) and Knowledge. Rail icons are painted here (no icon
files): a house, a checked box, a mortarboard, code brackets, an open book.
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Property, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from jarvis.gui import motion, theme
from jarvis.gui.theme import Tokens, mix
from jarvis.gui.widgets import _label


@dataclass(frozen=True)
class Workspace:
    key: str
    name: str
    shortcut: str


WORKSPACES = (
    Workspace("home", "Home", "Ctrl+1"),
    Workspace("tasks", "Tasks", "Ctrl+2"),
    Workspace("school", "School", "Ctrl+3"),
    Workspace("projects", "Projects", "Ctrl+4"),
    Workspace("knowledge", "Knowledge", "Ctrl+5"),
)
KEYS = tuple(w.key for w in WORKSPACES)

RAIL_WIDE = 176      # icon + label side by side
RAIL_COMPACT = 76    # icon over a small caption
WIDE_FROM = 1320     # window width at which the rail shows labels beside icons


# ---------------------------------------------------------------- icons


def paint_icon(p: QPainter, key: str, box: QRectF, color: QColor) -> None:
    """A line icon for a workspace, drawn into ``box`` (square, ~22 px)."""
    s = box.width() / 24.0

    def pt(x: float, y: float) -> QPointF:
        return QPointF(box.left() + x * s, box.top() + y * s)

    pen = QPen(color, max(1.3, 1.6 * s), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
               Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    path = QPainterPath()
    if key == "home":
        path.moveTo(pt(3.5, 11))
        path.lineTo(pt(12, 3.8))
        path.lineTo(pt(20.5, 11))
        path.moveTo(pt(5.5, 9.5))
        path.lineTo(pt(5.5, 20))
        path.lineTo(pt(18.5, 20))
        path.lineTo(pt(18.5, 9.5))
        path.moveTo(pt(10, 20))
        path.lineTo(pt(10, 14.5))
        path.lineTo(pt(14, 14.5))
        path.lineTo(pt(14, 20))
    elif key == "tasks":
        path.addRoundedRect(QRectF(pt(4, 4), pt(20, 20)), 3 * s, 3 * s)
        path.moveTo(pt(8, 12.2))
        path.lineTo(pt(11, 15.2))
        path.lineTo(pt(16.2, 9))
    elif key == "school":
        path.moveTo(pt(2.5, 9.5))
        path.lineTo(pt(12, 5))
        path.lineTo(pt(21.5, 9.5))
        path.lineTo(pt(12, 14))
        path.closeSubpath()
        path.moveTo(pt(6.5, 11.5))
        path.lineTo(pt(6.5, 16))
        path.cubicTo(pt(9, 18.8), pt(15, 18.8), pt(17.5, 16))
        path.lineTo(pt(17.5, 11.5))
        path.moveTo(pt(21.5, 9.5))
        path.lineTo(pt(21.5, 14))
    elif key == "projects":
        path.moveTo(pt(8.5, 6.5))
        path.lineTo(pt(3, 12))
        path.lineTo(pt(8.5, 17.5))
        path.moveTo(pt(15.5, 6.5))
        path.lineTo(pt(21, 12))
        path.lineTo(pt(15.5, 17.5))
        path.moveTo(pt(13.2, 6))
        path.lineTo(pt(10.8, 18))
    elif key == "knowledge":
        path.moveTo(pt(12, 6.5))
        path.cubicTo(pt(9.5, 4.8), pt(6, 4.6), pt(3, 5.5))
        path.lineTo(pt(3, 18.5))
        path.cubicTo(pt(6, 17.6), pt(9.5, 17.8), pt(12, 19.5))
        path.cubicTo(pt(14.5, 17.8), pt(18, 17.6), pt(21, 18.5))
        path.lineTo(pt(21, 5.5))
        path.cubicTo(pt(18, 4.6), pt(14.5, 4.8), pt(12, 6.5))
        path.lineTo(pt(12, 19.5))
    p.drawPath(path)


# ---------------------------------------------------------------- the rail


class NavButton(QAbstractButton):
    """One workspace in the rail: painted icon and label, mint when current."""

    def __init__(self, ws: Workspace, tokens: Tokens, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ws = ws
        self.tokens = tokens
        self.wide = True
        self._hover = 0.0
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setToolTip(f"{ws.name} ({ws.shortcut})")
        self.setAccessibleName(ws.name)
        self.setObjectName(f"nav_{ws.key}")

    def set_wide(self, wide: bool) -> None:
        self.wide = wide
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return QSize(RAIL_WIDE - 20, 44) if self.wide else QSize(RAIL_COMPACT - 16, 54)

    def _get_hover(self) -> float:
        return self._hover

    def _set_hover(self, v: float) -> None:
        self._hover = float(v)
        self.update()

    hover = Property(float, _get_hover, _set_hover)   # 0..1, fades (motion.HOVER)

    def enterEvent(self, e) -> None:  # noqa: N802 - Qt API
        motion.animate(self, b"hover", self._hover, 1.0, motion.HOVER)
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:  # noqa: N802 - Qt API
        motion.animate(self, b"hover", self._hover, 0.0, motion.HOVER)
        super().leaveEvent(e)

    def paintEvent(self, e) -> None:  # noqa: N802 - Qt API
        t = self.tokens
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        on = self.isChecked()
        if on:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(t.tint(0.09)))
            p.drawRoundedRect(r, 8, 8)
        elif self.isDown() or self._hover > 0:
            fill = QColor(t.fill2 if self.isDown() else t.hover)
            fill.setAlphaF(1.0 if self.isDown() else min(1.0, self._hover))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(fill)
            p.drawRoundedRect(r, 8, 8)
        if self.hasFocus() and not on:   # keyboard focus stays visible
            p.setPen(QPen(QColor(t.muted), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r, 10, 10)
        ink = QColor(t.muted) if not on else QColor(t.signal)
        if not on and self._hover > 0:   # muted -> text as the hover fades in
            k = min(1.0, self._hover)
            hi = QColor(t.text)
            ink = QColor(round(ink.red() + (hi.red() - ink.red()) * k),
                         round(ink.green() + (hi.green() - ink.green()) * k),
                         round(ink.blue() + (hi.blue() - ink.blue()) * k))
        color = QColor(t.text) if on else ink   # selected: white label, mint icon
        font = theme.pick_font()
        if self.wide:
            icon = QRectF(r.left() + 13, r.center().y() - 11, 22, 22)
            paint_icon(p, self.ws.key, icon, ink)
            font.setPixelSize(14)
            font.setWeight(QFont.Weight(theme.sans_weight(600 if on else 400)))
            p.setFont(font)
            p.setPen(color)
            p.drawText(QRectF(icon.right() + 13, r.top(), r.width() - 60, r.height()),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.ws.name)
        else:
            icon = QRectF(r.center().x() - 11, r.top() + 7, 22, 22)
            paint_icon(p, self.ws.key, icon, ink)
            font.setPixelSize(11)
            p.setFont(font)
            p.setPen(color)
            p.drawText(QRectF(r.left(), icon.bottom() + 2, r.width(), 14),
                       Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, self.ws.name)
        p.end()


class NavRail(QFrame):
    """The workspace switcher. ``changed(key)`` when the user picks one."""

    changed = Signal(str)

    def __init__(self, tokens: Tokens, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("rail")
        self.buttons: dict[str, NavButton] = {}
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 14, 10, 14)
        lay.setSpacing(6)
        for ws in WORKSPACES:
            b = NavButton(ws, tokens)
            b.clicked.connect(lambda _=False, k=ws.key: self.changed.emit(k))
            self._group.addButton(b)
            self.buttons[ws.key] = b
            lay.addWidget(b)
        lay.addStretch(1)
        self.hint = _label("Ctrl+1–5", "railHint")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.hint)
        self._lay = lay
        self.wide = True
        self.set_wide(True)

    def set_wide(self, wide: bool) -> None:
        self.wide = wide
        self.setFixedWidth(RAIL_WIDE if wide else RAIL_COMPACT)
        side = 10 if wide else 8
        self._lay.setContentsMargins(side, 14, side, 14)
        for b in self.buttons.values():
            b.set_wide(wide)
        self.hint.setText("Ctrl+1–5 switch" if wide else "⌃1–5")

    def select(self, key: str) -> None:
        self.buttons[key].setChecked(True)

    @property
    def current(self) -> str | None:
        b = self._group.checkedButton()
        return b.ws.key if b else None


# ---------------------------------------------------------------- placeholders


@dataclass(frozen=True)
class PlaceholderText:
    title: str
    lead: str
    plan: str        # what it will hold
    when: str        # when it arrives


PLACEHOLDERS = {
    "school": PlaceholderText(
        "School", "Courses, assignments and exams.",
        "A home for classes and their deadlines, linked to your tasks.",
        "Not built yet: planned for a later phase."),
    "knowledge": PlaceholderText(
        "Knowledge", "Notes and research.",
        "Notes, saved papers and what you've learned, connected in the graph.",
        "Not built yet: planned for a later phase."),
}


class PlaceholderPage(QWidget):
    """A workspace that is not built yet: says so, says what it will be, and
    offers only real actions (open Projects, quick capture)."""

    open_projects = Signal()
    capture = Signal()

    def __init__(self, key: str, tokens: Tokens, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.key = key
        text = PLACEHOLDERS[key]
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.addStretch(2)
        row = QHBoxLayout()
        row.addStretch(1)
        card = QFrame()
        card.setObjectName("placeholder")
        card.setMaximumWidth(560)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(28, 26, 28, 24)
        lay.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(12)
        self.icon = _IconBadge(key, tokens)
        head.addWidget(self.icon)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.title = _label(text.title, "placeholderTitle")
        titles.addWidget(self.title)
        self.lead = _label(text.lead, "placeholderLead")
        titles.addWidget(self.lead)
        head.addLayout(titles, 1)
        self.badge = _label("not built yet", "placeholderBadge")
        head.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(head)
        lay.addSpacing(4)
        self.plan = _label(text.plan, "placeholderText", wrap=True)
        lay.addWidget(self.plan)
        self.when = _label(text.when, "placeholderWhen", wrap=True)
        lay.addWidget(self.when)
        lay.addSpacing(6)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.capture_btn = QPushButton("+ quick capture  Ctrl+N")
        self.capture_btn.clicked.connect(self.capture)
        self.projects_btn = QPushButton("open Projects  Ctrl+4")
        self.projects_btn.setObjectName("ghost")
        self.projects_btn.clicked.connect(self.open_projects)
        for b in (self.capture_btn, self.projects_btn):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            actions.addWidget(b)
        actions.addStretch(1)
        lay.addLayout(actions)
        row.addWidget(card, 10)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(3)


class _IconBadge(QWidget):
    """The workspace icon in a soft mint circle (the mockup's node badges)."""

    def __init__(self, key: str, tokens: Tokens) -> None:
        super().__init__()
        self.key, self.tokens = key, tokens
        self.setFixedSize(44, 44)

    def paintEvent(self, e) -> None:  # noqa: N802 - Qt API
        t = self.tokens
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.setPen(QPen(QColor(mix(t.bg, t.signal, 0.45)), 1.2))
        p.setBrush(QColor(mix(t.bg, t.signal, 0.1)))
        p.drawEllipse(r)
        paint_icon(p, self.key, QRectF(r.center().x() - 11, r.center().y() - 11, 22, 22),
                   QColor(t.signal))
        p.end()
