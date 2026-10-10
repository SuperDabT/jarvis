"""The cockpit's panels: vitals, milestones, pulse, power (left column), the
briefing HUD over the graph, the inspector and the thought stream (right).

Mono for labels and numbers, Inter for anything to read. Lower-case labels.
"""
from __future__ import annotations

from datetime import UTC, datetime
from html import escape

from PySide6.QtCore import QUrl, Qt, QTimer, Signal
from PySide6.QtGui import QDesktopServices, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from jarvis.cards import FEEDBACK_REASONS, Card
from jarvis.gui import motion, theme
from jarvis.gui.brain import MilestoneMini, PulseBars, RailMini
from jarvis.gui.brain_data import EDGE_LABELS, Graph, Node, Pulse, StreamItem, Vital, ago
from jarvis.gui.data import BriefingView, Kpi, RailPower, Timeline, fmt_num
from jarvis.gui.theme import Tokens
from jarvis.gui.widgets import SentenceRow, _clear, _label, repolish, set_font

COPY_LABEL = "copy prompt"
FEEDBACK_LABELS = {"vague": "Too vague", "wrong": "Wrong", "repeat": "Already knew",
                   "other": "Other"}
KIND_NAMES = {"core": "project", "hub": "hub", "milestone": "milestone", "issue": "issue",
              "commit": "commit", "card": "suggestion", "check": "electrical check",
              "part": "part", "paper": "paper"}


def mono(w: QWidget, px: int = 11, weight: int = 400) -> QWidget:
    return set_font(w, theme.mono_font(px, weight=weight))


def pill(text: str, live: bool = False, level: str | None = None) -> QLabel:
    lb = _label(text, "pill")
    mono(lb, 10, 500)
    set_pill(lb, text, live, level)
    return lb


def set_pill(lb: QLabel, text: str, live: bool = False, level: str | None = None) -> None:
    lb.setText(text)
    lb.setProperty("live", "true" if live else "false")
    lb.setProperty("level", level or "")
    repolish(lb)


class Panel(QFrame):
    """A cockpit panel: a lower-case mono heading, an optional hint, a body."""

    def __init__(self, title: str, hint: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(6)
        self.head = mono(_label(title, "panelHead"), 11, 500)
        head.addWidget(self.head)
        head.addStretch(1)
        self.hint = mono(_label(hint, "panelHint"), 10)
        head.addWidget(self.hint)
        lay.addLayout(head)
        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(6)
        lay.addLayout(self.body)

    def set_live(self, on: bool) -> None:
        self.setProperty("live", "true" if on else "false")
        repolish(self)


# ---------------------------------------------------------------- left column


class VitalsPanel(Panel):
    def __init__(self, parent: QWidget | None = None):
        super().__init__("vitals", parent=parent)
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(12)
        self.grid.setVerticalSpacing(5)
        self.grid.setColumnStretch(1, 1)
        self.body.addLayout(self.grid)
        self.values: dict[str, QLabel] = {}

    def set_data(self, kpis: tuple[Kpi, ...], vitals: tuple[Vital, ...],
                 animate: bool = False) -> None:
        _clear(self.grid)
        self.values = {}
        rows: list[tuple[str, str, bool, str | None, str]] = []
        for k in kpis:
            rows.append((k.label.lower(), k.value, k.tone == "good", "fail" if k.tone == "bad"
                         else None, (k.sub + ("\n" + k.tooltip if k.tooltip else "")).strip()))
        for v in vitals:
            rows.append((v.key, v.value, v.live, v.level, v.tip))
        for r, (key, val, live, level, tip) in enumerate(rows):
            kl = mono(_label(key, "vKey"), 11)
            vl = mono(_label(val, "vVal"), 11, 600)
            vl.setProperty("live", "true" if live else "false")
            vl.setProperty("level", level or "")
            vl.setToolTip(tip)
            kl.setToolTip(tip)
            self.grid.addWidget(kl, r, 0, Qt.AlignmentFlag.AlignLeft)
            self.grid.addWidget(vl, r, 1, Qt.AlignmentFlag.AlignLeft)
            self.values[key] = vl
            if animate:
                motion.count_up_text(vl, val)


class MilestonesPanel(Panel):
    def __init__(self, tokens: Tokens, parent: QWidget | None = None):
        super().__init__("milestones", parent=parent)
        self.tokens = tokens
        self.rows: list[QWidget] = []

    def set_data(self, timeline: Timeline | None) -> None:
        _clear(self.body)
        self.rows = []
        bars = timeline.bars if timeline else ()
        if not bars:
            self.body.addWidget(mono(_label("no open milestones", "vNote"), 11))
            return
        today = timeline.today
        for b in bars:
            w = QWidget()
            col = QVBoxLayout(w)
            col.setContentsMargins(0, 0, 0, 2)
            col.setSpacing(4)
            top = QHBoxLayout()
            top.addWidget(mono(_label(b.title, "vVal"), 11, 600))
            top.addStretch(1)
            days = (b.due - today).days if b.due else None
            when = ("no due date" if days is None else "overdue" if days < 0
                    else "today" if days == 0 else f"{days}d")
            maybe = f" +{b.maybe}?" if b.maybe else ""
            top.addWidget(mono(_label(f"{b.closed}/{b.total}{maybe} · {when}", "vKey"), 11))
            col.addLayout(top)
            col.addWidget(MilestoneMini(b, self.tokens))
            w.setToolTip(f"{b.title}: {b.label}" + (f"\n{b.url}" if b.url else ""))
            self.body.addWidget(w)
            self.rows.append(w)


class PulsePanel(Panel):
    def __init__(self, tokens: Tokens, parent: QWidget | None = None):
        super().__init__("pulse", parent=parent)
        self.bars = PulseBars(tokens)
        self.body.addWidget(self.bars)
        self.caption = mono(_label("", "vNote", wrap=True), 10)
        self.body.addWidget(self.caption)

    def set_data(self, pulse: Pulse, animate: bool = False) -> None:
        self.bars.set_data(pulse.counts, animate)
        self.caption.setText(pulse.caption)
        self.caption.setToolTip("Commits, plus issues opened and closed, per day")
        self.set_live(bool(pulse.counts and any(pulse.counts[-2:])))


class PowerPanel(Panel):
    def __init__(self, tokens: Tokens, parent: QWidget | None = None):
        super().__init__("power", "hardware.yaml", parent=parent)
        self.tokens = tokens

    def set_data(self, rails: tuple[RailPower, ...], note: str | None) -> None:
        _clear(self.body)
        if not rails:
            self.body.addWidget(mono(_label(note or "no rails with parts", "vNote"), 10))
            return
        for r in rails:
            b = r.budget
            top = QHBoxLayout()
            top.addWidget(mono(_label(f"{r.rail} rail", "vVal"), 11, 600))
            top.addStretch(1)
            if b is not None:
                rating = f"{fmt_num(b.rating_a)} A" if b.rating_a is not None else "? A"
                txt = f"{fmt_num(b.worst_case_a)} A / {rating}"
            else:
                txt = f"{fmt_num(r.nominal_v)} V"
            val = mono(_label(txt, "vKey"), 11)
            top.addWidget(val)
            self.body.addLayout(top)
            if b is not None:
                bar = RailMini(b, self.tokens)
                bar.setToolTip(b.caption)
                self.body.addWidget(bar)
            if r.window is not None and r.window.tolerance_pct is None:
                self.body.addWidget(mono(_label("tolerance unknown", "vNote"), 10))


# ---------------------------------------------------------------- briefing HUD


class BriefingHud(QFrame):
    """The briefing, floating over the graph's top-left. Collapsible."""

    toggled = Signal(bool)

    WIDTH = 420

    def __init__(self, tokens: Tokens, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("hud")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.tokens = tokens
        self.data: BriefingView | None = None
        self.rows: list[SentenceRow] = []
        self.expanded = True
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 12)
        lay.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.head = mono(_label("briefing", "hudHead"), 11, 500)
        head.addWidget(self.head)
        self.source = mono(_label("", "panelHint"), 10)
        head.addWidget(self.source)
        head.addStretch(1)
        self.toggle_btn = QPushButton("−")
        self.toggle_btn.setObjectName("ghost")
        self.toggle_btn.setToolTip("Collapse or expand the briefing (B)")
        self.toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle_btn.clicked.connect(lambda: self.set_expanded(not self.expanded))
        head.addWidget(self.toggle_btn)
        lay.addLayout(head)
        self.headline = _label("", "headline", wrap=True)
        self.headline.setFont(theme.display_font(17, 650))
        self.headline.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.headline)
        self.content = QWidget()
        self.lines = QVBoxLayout(self.content)
        self.lines.setContentsMargins(0, 0, 0, 0)
        self.lines.setSpacing(6)
        lay.addWidget(self.content)
        self.setFixedWidth(self.WIDTH)

    def set_data(self, data: BriefingView | None, animate: bool = False) -> None:
        self.data = data
        _clear(self.lines)
        self.rows = []
        if data is None:
            self.headline.setText("No briefing yet")
            self.lines.addWidget(_label("Refresh to fetch the repo and write the first briefing.",
                                        "emptyText", wrap=True))
            self.source.setText("")
        else:
            self.headline.setText(data.headline or "Project status")
            self.source.setText(f"· {data.source_label}")
            for s in data.sentences:
                row = SentenceRow(s, self.tokens)
                self.rows.append(row)
                self.lines.addWidget(row)
            if animate:
                motion.fade_rise(self.headline, 6, motion.BASE + 80)
                for i, row in enumerate(self.rows):
                    motion.fade_rise(row, motion.RISE, motion.BASE + 120,
                                     delay=90 + i * motion.STAGGER)
        self.adjustSize()

    def set_expanded(self, on: bool) -> None:
        self.expanded = bool(on)
        self.content.setVisible(self.expanded)
        self.toggle_btn.setText("−" if self.expanded else "+")
        self.adjustSize()
        self.toggled.emit(self.expanded)


# ---------------------------------------------------------------- inspector


class Inspector(Panel):
    """What the graph is pointing at: title, details, links, and for a card the
    actions (copy the Claude prompt, done, not useful)."""

    copied = Signal(object)            # Card
    done = Signal(object)              # Card
    not_useful = Signal(object, str)   # Card, reason
    open_node = Signal(str)            # a neighbour was clicked
    open_item = Signal(int)            # "edit in Tasks" on an item
    complete_item = Signal(int)        # "done" on an item

    FLASH_MS = 1400
    HINT = ("hover a node to inspect it · click to lock the selection · "
            "scroll to zoom · drag to pan")

    def __init__(self, tokens: Tokens, parent: QWidget | None = None):
        super().__init__("inspector", parent=parent)
        self.tokens = tokens
        self.node: Node | None = None
        self.card: Card | None = None
        self.locked_id: str | None = None
        self._flash = QTimer(self, singleShot=True, interval=self.FLASH_MS)
        self._flash.timeout.connect(self._unflash)
        self.copy_btn: QPushButton | None = None
        self.show_node(None)

    def show_node(self, node: Node | None, graph: Graph | None = None,
                  card: Card | None = None, locked: bool = False,
                  now: datetime | None = None) -> None:
        self.node, self.card = node, card
        _clear(self.body)
        self.copy_btn = None
        self.hint.setText("locked · esc to release" if node and locked else "")
        if node is None:
            self.body.addWidget(mono(_label(self.HINT, "vNote"), 11))
            self.body.itemAt(0).widget().setWordWrap(True)
            return
        now = now or datetime.now(UTC)
        kind = mono(_label((node.sub if node.kind == "item" else
                            KIND_NAMES.get(node.kind, node.kind))
                           + (f" · {node.status}" if node.status else "")
                           + (" · live" if node.live else ""), "inspKind"), 10, 500)
        self.body.addWidget(kind)
        title = _label(node.title, "inspTitle", wrap=True)
        title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body.addWidget(title)
        if node.detail:
            d = _label(node.detail, "inspText", wrap=True)
            d.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.body.addWidget(d)
        meta = []
        from jarvis.gui.brain_data import _parse

        if node.when:
            meta.append(ago(_parse(node.when), now))
        if node.url:
            meta.append(f'<a href="{escape(node.url, quote=True)}" '
                        f'style="color:{self.tokens.signal}">open ↗</a>')
        if meta:
            m = mono(_label(" · ".join(meta), "vKey", rich=True), 10)
            m.setOpenExternalLinks(True)
            self.body.addWidget(m)
        if graph is not None:
            links = graph.neighbours(node.id)
            if links:
                self.body.addWidget(mono(_label(f"links · {len(links)}", "panelHead"), 10, 500))
                for edge, other in links[:8]:
                    name = other.label if other.kind in ("issue", "milestone", "commit", "hub",
                                                         "core", "part") else other.title
                    if len(name) > 38:
                        name = name[:37].rstrip() + "…"
                    b = QPushButton(f"{EDGE_LABELS.get(edge.kind, edge.kind)} → {name}")
                    b.setObjectName("filter")
                    b.setToolTip(other.title)
                    b.setCursor(Qt.CursorShape.PointingHandCursor)
                    b.clicked.connect(lambda _=False, i=other.id: self.open_node.emit(i))
                    b.setStyleSheet("text-align: left;")
                    self.body.addWidget(b)
                if len(links) > 8:
                    self.body.addWidget(mono(_label(f"+{len(links) - 8} more", "vNote"), 10))
        if card is not None:
            self._card_actions(card)
        if node.kind == "item" and node.item_id is not None:
            self._item_actions(node.item_id)

    def _item_actions(self, item_id: int) -> None:
        row = QHBoxLayout()
        row.setSpacing(6)
        self.edit_btn = QPushButton("edit in Tasks")
        self.edit_btn.setObjectName("primary")
        self.edit_btn.setToolTip("Open it in the Tasks workspace to edit")
        self.edit_btn.clicked.connect(lambda: self.open_item.emit(item_id))
        self.done_btn = QPushButton("done")
        self.done_btn.setToolTip("Mark it done (it leaves the graph)")
        self.done_btn.clicked.connect(lambda: self.complete_item.emit(item_id))
        for b in (self.edit_btn, self.done_btn):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            row.addWidget(b)
        row.addStretch(1)
        self.body.addSpacing(4)
        self.body.addLayout(row)

    def _card_actions(self, card: Card) -> None:
        row = QHBoxLayout()
        row.setSpacing(6)
        self.copy_btn = QPushButton(COPY_LABEL)
        self.copy_btn.setObjectName("primary")
        self.copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_btn.setToolTip("Put the prompt for Claude on the clipboard")
        self.copy_btn.clicked.connect(self._copy)
        row.addWidget(self.copy_btn)
        done = QPushButton("done")
        done.setCursor(Qt.CursorShape.PointingHandCursor)
        done.setToolTip("Mark as handled; it will not come back")
        done.clicked.connect(lambda: self.done.emit(card))
        row.addWidget(done)
        fb = QPushButton("not useful ▾")
        fb.setCursor(Qt.CursorShape.PointingHandCursor)
        menu = QMenu(fb)
        head = menu.addAction("why isn't it useful?")
        head.setEnabled(False)
        menu.addSeparator()
        self.feedback_actions = {}
        for reason in FEEDBACK_REASONS:
            act = menu.addAction(FEEDBACK_LABELS.get(reason, reason))
            act.triggered.connect(lambda _=False, r=reason: self.not_useful.emit(card, r))
            self.feedback_actions[reason] = act
        fb.setMenu(menu)
        row.addWidget(fb)
        row.addStretch(1)
        self.body.addSpacing(4)
        self.body.addLayout(row)
        self.prompt = QPlainTextEdit()
        self.prompt.setObjectName("promptBox")
        self.prompt.setReadOnly(True)
        self.prompt.setPlainText(card.claude_prompt or "")
        f = QFont(theme.mono_family())
        f.setPixelSize(11)
        self.prompt.setFont(f)
        self.prompt.setFixedHeight(150)
        self.body.addWidget(self.prompt)

    def _copy(self) -> None:
        if self.card is None or self.copy_btn is None:
            return
        QGuiApplication.clipboard().setText(self.card.claude_prompt or "")
        self.copy_btn.setText("copied ✓")
        self.copy_btn.setProperty("flash", "true")
        repolish(self.copy_btn)
        self._flash.start()
        self.copied.emit(self.card)

    def _unflash(self) -> None:
        if self.copy_btn is not None:
            self.copy_btn.setText(COPY_LABEL)
            self.copy_btn.setProperty("flash", "false")
            repolish(self.copy_btn)


# ---------------------------------------------------------------- thought stream


ORIGIN_MARK = {"github": "◆", "jarvis": "✦", "chat": "›", "research": "◇", "checks": "▲"}


class StreamRow(QWidget):
    clicked = Signal(str)

    def __init__(self, item: StreamItem, now: datetime, parent: QWidget | None = None):
        super().__init__(parent)
        self.item = item
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 6)
        lay.setSpacing(8)
        mark = mono(_label(ORIGIN_MARK.get(item.origin, "·"), "vVal"), 11)
        mark.setProperty("live", "true" if item.live else "false")
        mark.setFixedWidth(12)
        lay.addWidget(mark, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(2)
        self.text = mono(_label(item.text, "streamText", wrap=True), 11)
        col.addWidget(self.text)
        from jarvis.gui.brain_data import _parse

        kind = "" if item.kind == item.origin else f"{item.kind} · "
        col.addWidget(mono(_label(f"{kind}origin:{item.origin} · "
                                  f"{ago(_parse(item.when), now)}", "streamMeta"), 10))
        lay.addLayout(col, 1)
        if item.node_id:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setToolTip("Show it in the graph")

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802 - Qt API
        if self.item.node_id:
            self.clicked.emit(self.item.node_id)
        super().mouseReleaseEvent(e)


class StreamPanel(Panel):
    open_node = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__("thought stream", parent=parent)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        self.list = QVBoxLayout(holder)
        self.list.setContentsMargins(0, 0, 4, 0)
        self.list.setSpacing(0)
        self.list.addStretch(1)
        self.scroll.setWidget(holder)
        self.body.addWidget(self.scroll, 1)
        self.layout().setStretch(1, 1)
        self.rows: list[StreamRow] = []
        self._ids: tuple = ()

    def set_data(self, items: tuple[StreamItem, ...], now: datetime | None = None,
                 animate: bool = False) -> None:
        now = now or datetime.now(UTC)
        old = set(self._ids)
        self._ids = tuple((i.when, i.text) for i in items)
        while self.list.count() > 1:
            it = self.list.takeAt(0)
            if it.widget():
                it.widget().hide()  # gone now, not when the loop gets to it
                it.widget().deleteLater()
        self.rows = []
        for i, item in enumerate(items):
            r = StreamRow(item, now)
            r.clicked.connect(self.open_node)
            self.list.insertWidget(i, r)
            self.rows.append(r)
            if animate and old and (item.when, item.text) not in old:
                motion.fade_rise(r, 8, motion.BASE + 60)
        self.head.setText(f"thought stream · {len(items)}")
        self.set_live(any(i.live for i in items[:3]))


__all__ = ["Panel", "VitalsPanel", "MilestonesPanel", "PulsePanel", "PowerPanel",
           "BriefingHud", "Inspector", "StreamPanel", "StreamRow", "pill", "set_pill",
           "COPY_LABEL", "QUrl", "QDesktopServices"]
