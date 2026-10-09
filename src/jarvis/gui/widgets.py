"""Building blocks of the dashboard page: briefing callout, KPI tiles, numbered
section headers, tags, the title watermark and the suggestions table."""
from __future__ import annotations

import re
from html import escape

import math
import time

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QWidget,
)

from jarvis.gui.data import EvidenceLink, SentenceView
from jarvis.gui import motion
from jarvis.gui import theme
from jarvis.gui.theme import Tokens

UNSURE_TIP = (
    "Unconfirmed: this is a heuristic guess, for example a commit message that\n"
    "resembles an issue title. The linked issues and commits are the evidence;\n"
    "check them before relying on it."
)
TEMPLATE_TIP = (
    "Plain template wording of the same facts. The local model was unavailable\n"
    "or its reply did not pass the objectivity checks."
)
FEEDBACK_LABELS = {
    "vague": "Too vague",
    "wrong": "Wrong",
    "repeat": "Repeat of another card",
    "other": "Other",
}
LINE_HEIGHT = 135  # percent, for paragraphs
COPY_LABEL = "Copy prompt for Claude"


_OPEN_QUOTE_RE = re.compile(r'(^|[\s(\[{])"')


def smart_quotes(text: str) -> str:
    """Straight double quotes to typographic ones, for display only."""
    return _OPEN_QUOTE_RE.sub("\\1\u201c", text).replace('"', "\u201d")


def paragraph(text: str, line_height: int = LINE_HEIGHT) -> str:
    body = escape(smart_quotes(text), quote=False)
    return f'<p style="line-height:{line_height}%; margin:0">{body}</p>'


def links_html(links: tuple[EvidenceLink, ...], color: str) -> str:
    sep = f'<span style="color:{color}">&nbsp;·&nbsp;</span>'
    return sep.join(
        f'<a href="{escape(lk.url, quote=True)}" style="color:{color}; text-decoration:none">'
        f"{escape(lk.label)}</a>"
        for lk in links
    )


def _label(text: str = "", name: str | None = None, wrap: bool = False,
           rich: bool = False) -> QLabel:
    lb = QLabel(text)
    if name:
        lb.setObjectName(name)
    if wrap:
        lb.setWordWrap(True)
        lb.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    if rich:
        lb.setTextFormat(Qt.TextFormat.RichText)
        lb.setOpenExternalLinks(True)
        lb.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
    else:
        lb.setTextFormat(Qt.TextFormat.PlainText)
    return lb


def _clear(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        if w := item.widget():
            w.setParent(None)
            w.deleteLater()
        elif sub := item.layout():
            _clear(sub)


def repolish(w: QWidget) -> None:
    w.style().unpolish(w)
    w.style().polish(w)


def tag(text: str, role: str, tokens: Tokens, tip: str = "") -> QLabel:
    """A small pill in one of the tag roles (tints of the pair; signal for live)."""
    bg, fg, border = tokens.tag(role)
    lb = _label(text, "tag")
    lb.setStyleSheet(f"#tag {{ background: {bg}; color: {fg}; border: {border}; "
                     f"border-radius: 8px; padding: 1px 8px; }}")
    lb.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    if tip:
        lb.setToolTip(tip)
    return lb


def set_font(w: QWidget, font) -> QWidget:
    w.setFont(font)
    return w


def hairline() -> QFrame:
    f = QFrame()
    f.setObjectName("hairline")
    f.setFixedHeight(1)
    return f


# ---------------------------------------------------------------- briefing


def sentence_html(s: SentenceView, tokens: Tokens) -> str:
    body = escape(smart_quotes(s.text), quote=False)
    extra = ""
    if s.uncertain:
        extra += (f'&nbsp;&nbsp;<span style="background-color:{tokens.unsure_bg}; '
                  f'color:{tokens.unsure}; font-family:\'{theme.family("mono")}\'; '
                  f'font-size:10px">'
                  f"&nbsp;unconfirmed&nbsp;</span>")
    if s.links:
        extra += (f'&nbsp;&nbsp;<span style="font-family:\'{theme.family("mono")}\'; '
                  f'font-size:11px">{links_html(s.links, tokens.faint)}</span>')
    return f'<p style="line-height:{LINE_HEIGHT}%; margin:0">{body}{extra}</p>'


class SentenceRow(QWidget):
    """One briefing sentence as a bullet, with an inline "unconfirmed" tag and its
    evidence links (issues, commits) after the text."""

    def __init__(self, sentence: SentenceView, tokens: Tokens, parent: QWidget | None = None):
        super().__init__(parent)
        self.sentence = sentence
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        dot = _label("•", "bullet")
        dot.setFixedWidth(12)
        dot.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        lay.addWidget(dot, 0, Qt.AlignmentFlag.AlignTop)
        self.text_label = _label(sentence_html(sentence, tokens), "sentence", wrap=True, rich=True)
        lay.addWidget(self.text_label, 1)
        self.pill = sentence.uncertain  # truthy when the tag is shown
        if sentence.uncertain:
            self.setToolTip(UNSURE_TIP)
            self.text_label.setToolTip(UNSURE_TIP)


class PulseDot(QWidget):
    """The "watching" dot. While ``breathing`` it slowly pulses its opacity.

    Its timer runs at a low rate (``FPS``) and only while the dot is visible and
    motion is on, so an idle window stays near zero CPU.
    """

    PERIOD_S = 2.8
    FPS = 10
    MIN_ALPHA = 0.35

    def __init__(self, parent: QWidget | None = None, diameter: int = 8):
        super().__init__(parent)
        self.setObjectName("metaDot")
        self._d = diameter
        self.setFixedSize(diameter + 6, diameter + 6)
        self.color = QColor("#888888")
        self._breathing = False
        self._t0 = time.monotonic()
        self._timer = QTimer(self, interval=round(1000 / self.FPS))
        self._timer.timeout.connect(self.update)

    @property
    def breathing(self) -> bool:
        return self._breathing

    @property
    def running(self) -> bool:
        return self._timer.isActive()

    def set_color(self, color: str) -> None:
        self.color = QColor(color)
        self.update()

    def set_breathing(self, on: bool) -> None:
        self._breathing = bool(on)
        self._sync()
        self.update()

    def _sync(self) -> None:
        w = self.window()
        run = (self._breathing and motion.enabled() and self.isVisible()
               and not (w is not None and w.isMinimized()))
        if run and not self._timer.isActive():
            self._t0 = time.monotonic()
            self._timer.start()
        elif not run and self._timer.isActive():
            self._timer.stop()

    def alpha(self) -> float:
        if not (self._breathing and self._timer.isActive()):
            return 1.0
        x = (time.monotonic() - self._t0) / self.PERIOD_S
        return self.MIN_ALPHA + (1 - self.MIN_ALPHA) * (0.5 + 0.5 * math.cos(2 * math.pi * x))

    def showEvent(self, e) -> None:  # noqa: N802 - Qt API
        super().showEvent(e)
        self._sync()

    def hideEvent(self, e) -> None:  # noqa: N802 - Qt API
        super().hideEvent(e)
        self._timer.stop()

    def changeEvent(self, e) -> None:  # noqa: N802 - Qt API
        super().changeEvent(e)
        self._sync()

    def paintEvent(self, e) -> None:  # noqa: N802 - Qt API
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QColor(self.color)
        a = self.alpha()
        center = QPointF(self.width() / 2, self.height() / 2 + 1)
        if self._breathing and a < 1.0:  # a faint halo that swells as it brightens
            halo = QColor(c)
            halo.setAlphaF(0.18 * (a - self.MIN_ALPHA) / (1 - self.MIN_ALPHA))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(halo)
            p.drawEllipse(center, self._d / 2 + 2.5, self._d / 2 + 2.5)
        c.setAlphaF(a)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        p.drawEllipse(center, self._d / 2, self._d / 2)
        p.end()


def refresh_icon(color: str, angle: float = 0.0, px: int = 14, ratio: float = 2.0) -> QIcon:
    """A circular-arrow icon rotated by ``angle`` degrees (the Refresh spinner)."""
    size = round(px * ratio)
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    pm.setDevicePixelRatio(ratio)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.translate(px / 2, px / 2)
    p.rotate(angle)
    c = QColor(color)
    pen = QPen(c, 1.6)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    r = px / 2 - 2
    p.drawArc(QRectF(-r, -r, 2 * r, 2 * r), 60 * 16, 280 * 16)
    # arrowhead at the arc's start (60 deg, pointing counter-clockwise)
    a = math.radians(60)
    tip = QPointF(r * math.cos(a), -r * math.sin(a))
    head = QPainterPath()
    head.moveTo(tip + QPointF(3.4, -0.6))
    head.lineTo(tip + QPointF(-0.4, -2.8))
    head.lineTo(tip + QPointF(0.4, 1.4))
    head.closeSubpath()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(c)
    p.drawPath(head)
    p.end()
    return QIcon(pm)


ICON_SIZE = QSize(14, 14)
