"""The two building blocks of the window: the briefing panel and a suggestion card."""
from __future__ import annotations

import re
from html import escape

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from jarvis.cards import FEEDBACK_REASONS
from jarvis.gui.data import BriefingView as BriefingData
from jarvis.gui.data import CardView, EvidenceLink
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


_OPEN_QUOTE_RE = re.compile(r'(^|[\s(\[{])"')


def smart_quotes(text: str) -> str:
    """Straight double quotes to typographic ones, for display only."""
    return _OPEN_QUOTE_RE.sub("\\1\u201c", text).replace('"', "\u201d")


def paragraph(text: str, line_height: int = LINE_HEIGHT) -> str:
    body = escape(smart_quotes(text), quote=False)
    return f'<p style="line-height:{line_height}%; margin:0">{body}</p>'


def links_html(links: tuple[EvidenceLink, ...], color: str) -> str:
    sep = f'<span style="color:{color}">&nbsp;&nbsp;·&nbsp;&nbsp;</span>'
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


class SentenceRow(QFrame):
    """One briefing sentence; uncertain ones get a soft left rule, a pill and a tooltip."""

    def __init__(self, sentence, tokens: Tokens, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("sentenceRow")
        self.sentence = sentence
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(14)
        # A thin rule on the left: soft amber for unconfirmed, invisible otherwise,
        # so every sentence keeps the same indent.
        self.rule = QFrame()
        self.rule.setObjectName("ruleUnsure" if sentence.uncertain else "ruleNone")
        self.rule.setFixedWidth(2)
        outer.addWidget(self.rule)
        lay = QVBoxLayout()
        lay.setContentsMargins(0, 1, 0, 3)
        lay.setSpacing(2)
        outer.addLayout(lay, 1)

        self.text_label = _label(paragraph(sentence.text), "sentence", wrap=True, rich=True)
        self.text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.text_label)

        self.pill = None
        if sentence.uncertain or sentence.links:
            row = QHBoxLayout()
            row.setSpacing(10)
            row.setContentsMargins(0, 0, 0, 0)
            if sentence.uncertain:
                self.pill = _label("unconfirmed", "pill")
                self.pill.setToolTip(UNSURE_TIP)
                row.addWidget(self.pill)
            if sentence.links:
                ev = _label(links_html(sentence.links, tokens.faint), "evidence", rich=True)
                ev.setToolTip("Evidence behind this sentence, on GitHub")
                row.addWidget(ev)
            row.addStretch(1)
            lay.addLayout(row)
        if sentence.uncertain:
            self.setToolTip(UNSURE_TIP)


class BriefingView(QFrame):
    """Headline plus a few readable paragraphs."""

    def __init__(self, tokens: Tokens, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("briefingPanel")
        self.tokens = tokens
        self.data: BriefingData | None = None
        self.rows: list[SentenceRow] = []
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(28, 26, 28, 28)
        self._lay.setSpacing(18)
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


class CardWidget(QFrame):
    """One suggestion: title, why, source, and the three actions."""

    copied = Signal(object)  # Card
    done = Signal(object)  # Card
    not_useful = Signal(object, str)  # Card, reason

    FLASH_MS = 1400

    def __init__(self, view: CardView, tokens: Tokens, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("card")
        self.view = view
        card = view.card
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 18)
        lay.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(10)
        self.title = _label(card.title, "cardTitle", wrap=True)
        top.addWidget(self.title, 1)
        pill_text = None
        if card.status not in ("active", None):
            pill_text = card.status.replace("_", " ")
        elif view.is_new:
            pill_text = "new"
        if pill_text:
            pill = _label(pill_text, "pillNew")
            pill.setToolTip("Created in the last 24 hours" if pill_text == "new" else "")
            top.addWidget(pill, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(top)

        if card.why:
            why = _label(paragraph(card.why, 145), "cardWhy", wrap=True, rich=True)
            why.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lay.addWidget(why)

        if card.source_url or card.source_ref:
            ref = card.source_ref or "source"
            if card.source_url:
                html = (f'From <a href="{escape(card.source_url, quote=True)}" '
                        f'style="color:{tokens.muted}; text-decoration:none">{escape(ref)}</a>')
                src = _label(html, "cardSource", rich=True)
                src.setToolTip(card.source_url)
            else:
                src = _label(f"From {ref}", "cardSource")
            lay.addWidget(src)

        lay.addSpacing(6)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.copy_btn = QPushButton("Copy prompt for Claude")
        self.copy_btn.setObjectName("primary")
        self.copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_btn.setToolTip("Puts a ready-made prompt on the clipboard to paste into Claude")
        self.copy_btn.clicked.connect(self._copy)
        self.done_btn = QPushButton("Done")
        self.done_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.done_btn.setToolTip("Mark as handled; it will not come back")
        self.done_btn.clicked.connect(lambda: self.done.emit(self.view.card))
        self.feedback_btn = QPushButton("Not useful  ▾")
        self.feedback_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.feedback_btn.setToolTip("Dismiss with a reason, so future suggestions improve")
        menu = QMenu(self.feedback_btn)
        head = menu.addAction("Why isn't it useful?")
        head.setEnabled(False)
        menu.addSeparator()
        self.feedback_actions = {}
        for reason in FEEDBACK_REASONS:
            act = menu.addAction(FEEDBACK_LABELS.get(reason, reason.capitalize()))
            act.triggered.connect(lambda _=False, r=reason: self.not_useful.emit(self.view.card, r))
            self.feedback_actions[reason] = act
        self.feedback_btn.setMenu(menu)
        actions.addWidget(self.copy_btn)
        actions.addWidget(self.done_btn)
        actions.addWidget(self.feedback_btn)
        actions.addStretch(1)
        lay.addLayout(actions)

        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self._unflash)

    @property
    def card(self):
        return self.view.card

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.card.claude_prompt or "")
        self.copy_btn.setMinimumWidth(self.copy_btn.width())
        self.copy_btn.setText("Copied")
        self.copy_btn.setProperty("flash", "true")
        repolish(self.copy_btn)
        self._flash_timer.start(self.FLASH_MS)
        self.copied.emit(self.card)

    def _unflash(self) -> None:
        self.copy_btn.setText("Copy prompt for Claude")
        self.copy_btn.setProperty("flash", "false")
        repolish(self.copy_btn)
