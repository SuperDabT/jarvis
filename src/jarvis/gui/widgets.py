"""Building blocks of the dashboard page: briefing callout, KPI tiles, section
headers, Notion-style tags and the suggestions table."""
from __future__ import annotations

import re
from html import escape

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QMouseEvent
from PySide6.QtWidgets import (
    QBoxLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from jarvis.gui.data import BriefingView as BriefingData
from jarvis.gui.data import EvidenceLink, Kpi, SentenceView, SuggestionRow
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
LINE_HEIGHT = 150  # percent, for paragraphs
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


def tag(text: str, color: str, tokens: Tokens, tip: str = "") -> QLabel:
    """A Notion tag: small rounded label on one of the tag colours."""
    bg, fg = tokens.tag(color)
    lb = _label(text, "tag")
    lb.setStyleSheet(f"#tag {{ background: {bg}; color: {fg}; }}")
    lb.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    if tip:
        lb.setToolTip(tip)
    return lb


def section_header(title: str, hint: str = "") -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(2, 0, 2, 0)
    lay.setSpacing(10)
    head = _label(title.upper(), "sectionHead")
    lay.addWidget(head, 0, Qt.AlignmentFlag.AlignBottom)
    lay.addStretch(1)
    if hint:
        h = _label(hint, "sectionHint")
        lay.addWidget(h, 0, Qt.AlignmentFlag.AlignBottom)
        w.hint = h
    w.head = head
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
                  f'color:{tokens.unsure}; font-size:11px; font-weight:600">'
                  f"&nbsp;unconfirmed&nbsp;</span>")
    if s.links:
        extra += (f'&nbsp;&nbsp;<span style="font-size:12px">'
                  f"{links_html(s.links, tokens.faint)}</span>")
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


class BriefingCallout(QFrame):
    """The briefing as a Notion callout: 💡, bold headline, sentences as bullets."""

    def __init__(self, tokens: Tokens, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("callout")
        self.tokens = tokens
        self.data: BriefingData | None = None
        self.rows: list[SentenceRow] = []
        outer = QHBoxLayout(self)
        outer.setContentsMargins(18, 16, 22, 18)
        outer.setSpacing(12)
        icon = _label("💡", "calloutIcon")
        icon.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        icon.setFixedWidth(26)
        outer.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        self._lay = QVBoxLayout()
        self._lay.setContentsMargins(0, 2, 0, 0)
        self._lay.setSpacing(7)
        outer.addLayout(self._lay, 1)
        self.set_data(None)

    def set_data(self, data: BriefingData | None, tokens: Tokens | None = None) -> None:
        self.tokens = tokens or self.tokens
        self.data = data
        _clear(self._lay)
        self.rows = []
        if data is None:
            self.headline = _label("No briefing yet", "headline", wrap=True)
            self._lay.addWidget(self.headline)
            self._lay.addWidget(_label(
                "Refresh to fetch the repo and write the first briefing.", "emptyText", wrap=True))
            return
        self.headline = _label(data.headline or "Project status", "headline", wrap=True)
        self.headline.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._lay.addWidget(self.headline)
        self._lay.addSpacing(2)
        if not data.sentences:
            self._lay.addWidget(_label("Nothing to report.", "emptyText", wrap=True))
        for s in data.sentences:
            row = SentenceRow(s, self.tokens)
            self.rows.append(row)
            self._lay.addWidget(row)


# ---------------------------------------------------------------- KPI tiles


class KpiTile(QFrame):
    def __init__(self, kpi: Kpi, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("kpi")
        self.kpi = kpi
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 13, 16, 14)
        lay.setSpacing(2)
        self.label = _label(kpi.label, "kpiLabel")
        self.value = _label(kpi.value, "kpiValue")
        self.value.setProperty("tone", kpi.tone)
        self.sub = _label(kpi.sub, "kpiSub")
        self.sub.setProperty("unsure", "true" if "may be done" in kpi.sub else "false")
        for w in (self.label, self.value, self.sub):
            w.setMinimumWidth(0)
            w.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        lay.addWidget(self.label)
        lay.addSpacing(2)
        lay.addWidget(self.value)
        lay.addWidget(self.sub)
        if kpi.tooltip:
            self.setToolTip(kpi.tooltip)


class KpiRow(QWidget):
    """Four tiles in a row; two by two when the page is narrow."""

    NARROW = 700

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(12)
        self.grid.setVerticalSpacing(12)
        self.tiles: list[KpiTile] = []
        self._cols = 4

    def set_kpis(self, kpis: tuple[Kpi, ...]) -> None:
        _clear(self.grid)
        self.tiles = [KpiTile(k) for k in kpis]
        self._place()

    def _place(self) -> None:
        for t in self.tiles:
            self.grid.removeWidget(t)
        for i in range(4):
            self.grid.setColumnStretch(i, 0)
        for i, tile in enumerate(self.tiles):
            self.grid.addWidget(tile, i // self._cols, i % self._cols)
        for i in range(self._cols):
            self.grid.setColumnStretch(i, 1)

    def resizeEvent(self, e) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(e)
        cols = 4 if self.width() >= self.NARROW else 2
        if cols != self._cols:
            self._cols = cols
            self._place()


class Columns(QWidget):
    """Two (or more) columns side by side that stack when the page is narrow."""

    def __init__(self, narrow: int = 760, gap: int = 48, parent: QWidget | None = None):
        super().__init__(parent)
        self.narrow, self.gap = narrow, gap
        self.box = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(gap)
        self.stretches: list[int] = []

    def add(self, w: QWidget, stretch: int = 1) -> QWidget:
        self.box.addWidget(w, stretch)
        self.stretches.append(stretch)
        return w

    def column(self, stretch: int = 1) -> QVBoxLayout:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.add(w, stretch)
        return lay

    @property
    def stacked(self) -> bool:
        return self.box.direction() == QBoxLayout.Direction.TopToBottom

    def resizeEvent(self, e) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(e)
        stack = self.width() < self.narrow
        if stack != self.stacked:
            self.box.setDirection(QBoxLayout.Direction.TopToBottom if stack
                                  else QBoxLayout.Direction.LeftToRight)
            self.box.setSpacing(28 if stack else self.gap)
            for i, st in enumerate(self.stretches):
                self.box.setStretch(i, 0 if stack else st)


# ---------------------------------------------------------------- suggestions table

TYPE_W, STATUS_W, ACTION_W = 132, 118, 70


class TableRow(QFrame):
    """One suggestion: type tag, title (+ a faint why), status tag, Copy button."""

    opened = Signal(object)  # SuggestionRow
    copied = Signal(object)  # SuggestionRow
    FLASH_MS = 1400

    def __init__(self, row: SuggestionRow, tokens: Tokens, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("row")
        self.row = row
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("selected", "false")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 9, 6, 9)
        lay.setSpacing(12)

        tcell = QWidget()
        tcell.setFixedWidth(TYPE_W)
        tl = QHBoxLayout(tcell)
        tl.setContentsMargins(0, 0, 0, 0)
        self.type_tag = tag(row.type_label, row.type_color, tokens)
        tl.addWidget(self.type_tag)
        tl.addStretch(1)
        lay.addWidget(tcell, 0, Qt.AlignmentFlag.AlignVCenter)

        mid = QVBoxLayout()
        mid.setSpacing(1)
        self.title = _label(row.card.title, "rowTitle")
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.title.setToolTip(row.card.title)
        mid.addWidget(self.title)
        why = " ".join((row.card.why or "").split())
        self.why = _label(why, "rowWhy")
        self.why.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        if why:
            mid.addWidget(self.why)
        lay.addLayout(mid, 1)

        scell = QWidget()
        scell.setFixedWidth(STATUS_W)
        sl = QHBoxLayout(scell)
        sl.setContentsMargins(0, 0, 0, 0)
        tips = {"needs value": "A value this check needs is missing from hardware.yaml",
                "warning": "Close to a limit", "fail": "Over a limit",
                "new": "Created in the last 24 hours"}
        self.status_tag = tag(row.status_label, row.status_color, tokens,
                              tips.get(row.status_label, ""))
        sl.addWidget(self.status_tag)
        sl.addStretch(1)
        lay.addWidget(scell, 0, Qt.AlignmentFlag.AlignVCenter)

        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setObjectName("rowCopy")
        self.copy_btn.setFixedWidth(ACTION_W - 6)
        self.copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_btn.setToolTip("Copy the ready-made prompt for Claude")
        self.copy_btn.clicked.connect(self._copy)
        lay.addWidget(self.copy_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self._flash = QTimer(self, singleShot=True, interval=self.FLASH_MS)
        self._flash.timeout.connect(self._unflash)
        self._elide()

    @property
    def card(self):
        return self.row.card

    def _elide(self) -> None:
        for lb, text in ((self.title, self.row.card.title),
                         (self.why, " ".join((self.row.card.why or "").split()))):
            w = max(40, lb.width())
            lb.setText(lb.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, w))

    def resizeEvent(self, e) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(e)
        self._elide()

    def event(self, e: QEvent) -> bool:
        if e.type() in (QEvent.Type.Polish, QEvent.Type.Show):
            QTimer.singleShot(0, self._elide)
        return super().event(e)

    def set_selected(self, on: bool) -> None:
        self.setProperty("selected", "true" if on else "false")
        repolish(self)

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:  # noqa: N802 - Qt API
        if e.button() == Qt.MouseButton.LeftButton:
            self.opened.emit(self.row)
        super().mouseReleaseEvent(e)

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.card.claude_prompt or "")
        self.copy_btn.setText("Copied")
        self.copy_btn.setProperty("flash", "true")
        repolish(self.copy_btn)
        self._flash.start()
        self.copied.emit(self.row)

    def _unflash(self) -> None:
        self.copy_btn.setText("Copy")
        self.copy_btn.setProperty("flash", "false")
        repolish(self.copy_btn)


class SuggestionsTable(QWidget):
    """A Notion database table of the active suggestions. Click a row to peek."""

    opened = Signal(object)  # SuggestionRow
    copied = Signal(object)

    EMPTY = "No open suggestions. Electrical and research cards show up here."

    def __init__(self, tokens: Tokens, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("table")
        self.tokens = tokens
        self.rows: dict[int, TableRow] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        head = QFrame()
        head.setObjectName("tableHead")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(6, 7, 6, 7)
        hl.setSpacing(12)
        for text, width in (("◉  Type", TYPE_W), ("Aa  Suggestion", None),
                            ("◎  Status", STATUS_W), ("", ACTION_W - 6)):
            lb = _label(text, "colHead")
            if width:
                lb.setFixedWidth(width)
                hl.addWidget(lb)
            else:
                hl.addWidget(lb, 1)
        lay.addWidget(head)
        self.body = QVBoxLayout()
        self.body.setSpacing(0)
        lay.addLayout(self.body)
        self.empty_label: QLabel | None = None

    def set_rows(self, rows: tuple[SuggestionRow, ...], tokens: Tokens | None = None) -> None:
        self.tokens = tokens or self.tokens
        _clear(self.body)
        self.rows = {}
        self.empty_label = None
        if not rows:
            self.empty_label = _label(self.EMPTY, "emptyText", wrap=True)
            self.empty_label.setContentsMargins(6, 14, 6, 6)
            self.body.addWidget(self.empty_label)
            return
        for r in rows:
            w = TableRow(r, self.tokens)
            w.opened.connect(self.opened)
            w.copied.connect(self.copied)
            self.rows[r.id] = w
            self.body.addWidget(w)

    def remove(self, card_id: int) -> None:
        w = self.rows.pop(card_id, None)
        if w is not None:
            self.body.removeWidget(w)
            w.setParent(None)
            w.deleteLater()
        if not self.rows:
            self.set_rows(())

    def select(self, card_id: int | None) -> None:
        for cid, w in self.rows.items():
            w.set_selected(cid == card_id)
