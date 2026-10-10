"""The cockpit's panels: vitals, milestones, pulse, power (left column), the
briefing HUD over the graph, the inspector and the thought stream (right).

Mono for labels and numbers, Inter for anything to read. Lower-case labels.
"""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from html import escape

from PySide6.QtCore import QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QGuiApplication,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from jarvis.cards import FEEDBACK_REASONS, Card
from jarvis.gui import motion, theme
from jarvis.gui.brain import MilestoneMini
from jarvis.gui.brain_data import (
    EDGE_LABELS,
    Graph,
    Node,
    StreamItem,
    ago,
)
from jarvis.gui.chat_panel import FlowLayout
from jarvis.gui.dashboard_data import Dashboard, IssueRow
from jarvis.gui.data import (
    BriefingView,
    ContributionGrid,
    GridDay,
    MilestoneBar,
    fmt_day,
    plural,
)
from jarvis.gui.finding_view import FindingView
from jarvis.gui.theme import Tokens
from jarvis.gui.widgets import SentenceRow, _clear, _label, repolish, set_font

COPY_LABEL = "copy prompt"
# The inspector's summary: this much text and this many relationships, problems
# first; the rest (up to DETAIL_LINKS) is under "details".
SUMMARY_CHARS = 220
SUMMARY_LINKS = 3
DETAIL_LINKS = 12
ATTENTION_RANK = {"fail": 0, "overdue": 0, "warn": 1, "unknown": 2, "active": 3}
ATTENTION_WORDS = {"fail": "fail", "overdue": "overdue", "warn": "warn", "unknown": "unsure",
                   "active": "suggestion"}
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
    """Three answers about the active milestone: when is it due, how far along is
    it, is it on track (with the reason; "too early to tell" when the data can't
    say)."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__("vitals", parent=parent)
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(12)
        self.grid.setVerticalSpacing(2)
        self.grid.setColumnStretch(1, 1)
        self.body.addLayout(self.grid)
        self.values: dict[str, QLabel] = {}
        self.subs: dict[str, QLabel] = {}

    def set_data(self, dash: Dashboard | None, animate: bool = False) -> None:
        _clear(self.grid)
        self.values, self.subs = {}, {}
        m = dash.active if dash else None
        if m is None:
            rows = [("deadline", "none", "no open milestones", "muted", "")]
        else:
            days = dash.days_left
            if days is None:
                dl = ("none", f"{m.title} has no due date", "muted")
            elif days < 0:
                dl = (f"{plural(-days, 'day')} late", f"{m.title} · {fmt_day(m.due)}", "bad")
            elif days == 0:
                dl = ("today", f"{m.title} · {fmt_day(m.due)}", "warn")
            else:
                dl = (f"{plural(days, 'day')} left", f"{m.title} · {fmt_day(m.due)}",
                      "warn" if days <= 3 else "default")
            pct = round(m.closed / m.total * 100) if m.total else 0
            maybe = f" · {m.maybe} may be done" if m.maybe else ""
            r = dash.risk
            rows = [("deadline", dl[0], dl[1], dl[2], ""),
                    ("progress", f"{m.closed} / {m.total} closed", f"{pct}%{maybe}",
                     "good" if m.total and m.closed == m.total else "default",
                     "Unconfirmed: recent commits look like the same work." if m.maybe else ""),
                    ("risk", r.label, r.why, r.tone, "")]
        for i, (key, val, sub, tone, tip) in enumerate(rows):
            kl = mono(_label(key, "vKey"), 11)
            vl = mono(_label(val, "vVal"), 11, 600)
            vl.setProperty("tone", tone)
            sl = _label(sub, "vSub", wrap=True)
            sl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            for w in (kl, vl, sl):
                w.setToolTip(tip)
            self.grid.addWidget(kl, i * 2, 0, Qt.AlignmentFlag.AlignLeft)
            self.grid.addWidget(vl, i * 2, 1, Qt.AlignmentFlag.AlignLeft)
            self.grid.addWidget(sl, i * 2 + 1, 1)
            self.grid.setRowMinimumHeight(i * 2 + 1, 0)
            self.values[key], self.subs[key] = vl, sl
            if animate:
                motion.count_up_text(vl, val)


class IssueRowButton(QPushButton):
    """One issue: number, title, its label tag, and a "›" to inspect it."""

    def __init__(self, row: IssueRow, parent: QWidget | None = None):
        super().__init__(parent)
        self.row = row
        self.setObjectName("issueRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"#{row.number} {row.title}" + (f"  [{row.tag}]" if row.tag else "")
                        + "\nclick: inspect it")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 4, 4, 4)
        lay.setSpacing(6)
        num = mono(_label(f"#{row.number}", "issueNum"), 10)
        lay.addWidget(num)
        self.title_label = _label(row.title, "issueTitle")
        self.title_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        lay.addWidget(self.title_label, 1)
        if row.tag:
            tag = mono(_label(row.tag, "issueTag"), 9)
            tag.setProperty("kind", row.tag_kind)
            lay.addWidget(tag)
        lay.addWidget(mono(_label("›", "issueGo"), 12))
        self.setMinimumHeight(28)

    def resizeEvent(self, e) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(e)
        fm = self.title_label.fontMetrics()
        self.title_label.setText(fm.elidedText(self.row.title, Qt.TextElideMode.ElideRight,
                                               max(20, self.title_label.width())))


class IssuesPanel(Panel):
    """What to work on: the active milestone's open issues, best first (by their
    labels only), and the open count."""

    selected = Signal(int)        # issue number
    view_all = Signal(str)        # URL of the issue list

    def __init__(self, parent: QWidget | None = None):
        super().__init__("issues", parent=parent)
        self.rows: list[IssueRowButton] = []
        self.count = mono(_label("", "issueCount"), 18, 700)
        self.view_btn = QPushButton("view all →")
        self.view_btn.setObjectName("linkBtn")
        self.view_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._url: str | None = None
        self.view_btn.clicked.connect(lambda: self._url and self.view_all.emit(self._url))

    def set_data(self, dash: Dashboard | None) -> None:
        _clear(self.body)
        self.rows = []
        if dash is None:
            self.body.addWidget(mono(_label("no issue data", "vNote"), 10))
            return
        top = QHBoxLayout()
        top.setSpacing(8)
        self.count = mono(_label(str(dash.open_total), "issueCount"), 18, 700)
        top.addWidget(self.count)
        where = (f"open · {dash.milestone_open} in {dash.active.title}" if dash.active
                 else "open")
        self.where = mono(_label(where, "vKey"), 10)
        self.where.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        top.addWidget(self.where, 1, Qt.AlignmentFlag.AlignBottom)
        self.view_btn = QPushButton("view all →")
        self.view_btn.setObjectName("linkBtn")
        self.view_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._url = dash.view_all_url
        self.view_btn.setToolTip(("The milestone's issues on GitHub" if dash.active
                                  else "The issues on GitHub") + f"\n{self._url}")
        self.view_btn.clicked.connect(lambda: self.view_all.emit(self._url))
        top.addWidget(self.view_btn, 0, Qt.AlignmentFlag.AlignBottom)
        self.body.addLayout(top)
        for r in dash.issues:
            b = IssueRowButton(r)
            b.clicked.connect(lambda _=False, n=r.number: self.selected.emit(n))
            self.body.addWidget(b)
            self.rows.append(b)
        if not dash.issues:
            self.body.addWidget(mono(_label(
                "no open issues in this milestone" if dash.active else "no open issues",
                "vNote"), 10))
        elif not any(r.tag for r in dash.issues):
            hint = mono(_label("no priority labels: ordered by issue number", "vNote"), 9)
            self.body.addWidget(hint)


class MilestonesPanel(Panel):
    """The active milestone up front; the others folded, any of them can be made
    active (``picked``; -1 = back to the nearest-due one)."""

    picked = Signal(int)

    def __init__(self, tokens: Tokens, parent: QWidget | None = None):
        super().__init__("milestones", parent=parent)
        self.tokens = tokens
        self.rows: list[QWidget] = []
        self.expanded = False
        self.toggle_btn: QPushButton | None = None
        self._dash: Dashboard | None = None

    def set_expanded(self, on: bool) -> None:
        self.expanded = bool(on)
        self.set_data(self._dash)

    def set_data(self, dash: Dashboard | None) -> None:
        self._dash = dash
        _clear(self.body)
        self.rows = []
        self.toggle_btn = None
        m = dash.active if dash else None
        if m is None:
            self.body.addWidget(mono(_label("no open milestones", "vNote"), 11))
            return
        self.hint.setText("picked" if dash.picked else "nearest due")
        self.body.addWidget(self._block(m, dash.today, active=True))
        if not dash.others and not dash.picked:
            return
        n = len(dash.others)
        self.toggle_btn = QPushButton((f"▾ hide {plural(n, 'other')}" if self.expanded
                                       else f"▸ {plural(n, 'other milestone')}") if n
                                      else "▸ options")
        self.toggle_btn.setObjectName("linkBtn")
        self.toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle_btn.clicked.connect(lambda: self.set_expanded(not self.expanded))
        self.body.addWidget(self.toggle_btn, 0, Qt.AlignmentFlag.AlignLeft)
        if not self.expanded:
            return
        for b in dash.others:
            w = self._block(b, dash.today, active=False)
            self.body.addWidget(w)
        if dash.picked:
            auto = QPushButton("use the nearest-due milestone")
            auto.setObjectName("linkBtn")
            auto.setCursor(Qt.CursorShape.PointingHandCursor)
            auto.clicked.connect(lambda: self.picked.emit(-1))
            self.body.addWidget(auto, 0, Qt.AlignmentFlag.AlignLeft)

    def _block(self, b: MilestoneBar, today, active: bool) -> QWidget:
        w = QPushButton() if not active else QWidget()
        w.setObjectName("msActive" if active else "msRow")
        col = QVBoxLayout(w)
        col.setContentsMargins(0 if active else 6, 2, 0 if active else 6, 4)
        col.setSpacing(4)
        top = QHBoxLayout()
        title = mono(_label(b.title, "msTitle" if active else "vVal"), 12 if active else 11, 600)
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        top.addWidget(title, 1)
        days = (b.due - today).days if b.due else None
        when = ("no due date" if days is None else f"{-days}d late" if days < 0
                else "today" if days == 0 else f"{days}d")
        maybe = f" +{b.maybe}?" if b.maybe else ""
        top.addWidget(mono(_label(f"{b.closed}/{b.total}{maybe} · {when}", "vKey"), 11))
        col.addLayout(top)
        col.addWidget(MilestoneMini(b, self.tokens))
        if active and b.due:
            col.addWidget(mono(_label(f"due {fmt_day(b.due)}", "vNote"), 10))
        tip = f"{b.title}: {b.label}"
        if not active:
            w.setCursor(Qt.CursorShape.PointingHandCursor)
            w.setToolTip(tip + "\nclick: make it the active milestone")
            w.clicked.connect(lambda _=False, n=b.number: self.picked.emit(n))
            w.setMinimumHeight(40)
        else:
            w.setToolTip(tip + (f"\n{b.url}" if b.url else ""))
        self.rows.append(w)
        return w


class CommitHeatmap(QWidget):
    """Commits per day, GitHub style: one column per week (Monday on top),
    brighter green for more commits; days before the synced history are drawn
    hollow (no data, not zero). Hover shows the day, its count and commits."""

    GAP = 2

    def __init__(self, tokens: Tokens, parent: QWidget | None = None):
        super().__init__(parent)
        self.tokens = tokens
        self.grid: ContributionGrid | None = None
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(120)

    def cell(self) -> float:
        weeks = self.grid.weeks if self.grid else 26
        return max(3.0, (self.width() - self.GAP * (weeks - 1)) / weeks)

    def set_data(self, grid: ContributionGrid | None) -> None:
        self.grid = grid
        self.updateGeometry()
        self.resizeEvent(None)
        self.update()

    def resizeEvent(self, e) -> None:  # noqa: N802 - Qt API
        if e is not None:
            super().resizeEvent(e)
        h = round(7 * self.cell() + 6 * self.GAP)
        if self.height() != h:
            self.setFixedHeight(h)

    def day_at(self, x: float, y: float) -> GridDay | None:
        if self.grid is None:
            return None
        step = self.cell() + self.GAP
        col, row = int(x // step), int(y // step)
        return next((d for d in self.grid.days if d.col == col and d.row == row), None)

    def mouseMoveEvent(self, e) -> None:  # noqa: N802 - Qt API
        d = self.day_at(e.position().x(), e.position().y())
        if d is not None and self.grid is not None:
            QToolTip.showText(e.globalPosition().toPoint(), self.grid.tooltip(d), self)
        else:
            QToolTip.hideText()

    def paintEvent(self, e) -> None:  # noqa: N802 - Qt API
        if self.grid is None:
            return
        t = self.tokens
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = self.cell()
        step = c + self.GAP
        signal = QColor(t.signal)
        empty = QColor(t.fill2)
        for d in self.grid.days:
            r = QRectF(d.col * step, d.row * step, c, c)
            if not d.known:
                p.setPen(QPen(QColor(t.border), 1))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 1.5, 1.5)
                continue
            p.setPen(Qt.PenStyle.NoPen)
            if d.level == 0:
                p.setBrush(empty)
            else:
                col = QColor(signal)
                col.setAlphaF(HEAT_ALPHA[d.level])
                p.setBrush(col)
            p.drawRoundedRect(r, 1.5, 1.5)
        if self.grid.today:
            today = next((d for d in self.grid.days if d.day == self.grid.today), None)
            if today is not None:
                p.setPen(QPen(QColor(t.text), 1))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(QRectF(today.col * step, today.row * step, c, c)
                                  .adjusted(0.5, 0.5, -0.5, -0.5), 1.5, 1.5)
        p.end()


HEAT_ALPHA = {1: 0.3, 2: 0.5, 3: 0.75, 4: 1.0}


class CommitPanel(Panel):
    """The repository's commits per day (synced history only)."""

    def __init__(self, tokens: Tokens, parent: QWidget | None = None):
        super().__init__("commit activity", parent=parent)
        self.tokens = tokens
        self.heatmap = CommitHeatmap(tokens)
        self.body.addWidget(self.heatmap)
        self.caption = mono(_label("", "vNote", wrap=True), 10)
        self.body.addWidget(self.caption)
        self.empty = mono(_label("No commit history synced yet. Refresh (Ctrl+R) syncs it "
                                 "from GitHub.", "vNote", wrap=True), 10)
        self.body.addWidget(self.empty)

    def set_data(self, grid: ContributionGrid | None) -> None:
        ok = grid is not None and grid.available
        self.heatmap.setVisible(ok)
        self.caption.setVisible(ok)
        self.empty.setVisible(not ok)
        if not ok:
            self.hint.setText("")
            self.heatmap.set_data(None)
            return
        self.heatmap.set_data(grid)
        self.hint.setText(plural(grid.total, "commit"))
        self.caption.setText(grid.range_label + (" · hover a day for its commits"
                                                 if grid.total else " · no commits yet"))
        partial = grid.covered_from is not None and grid.covered_from > grid.start
        self.caption.setToolTip("Hollow squares: before the synced history, no data."
                                if partial else "Commits per day from the synced repository "
                                "history.")
        self.set_live(bool(grid.days and grid.days[-1].count))


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
    explain = Signal(str)              # a question for the chat about a finding
    open_source = Signal(str)          # a finding's source (file:// or GitHub URL)
    copy_prompt = Signal(str)          # a finding's prompt for Claude

    FLASH_MS = 1400
    HINT = ("hover a node to inspect it · click to lock the selection · "
            "scroll to zoom · drag to pan")

    def __init__(self, tokens: Tokens, parent: QWidget | None = None):
        super().__init__("inspector", parent=parent)
        self.tokens = tokens
        # The body scrolls: a finding's explanation, source and next step can be
        # taller than the column, and squeezed wrapped text would be cut off.
        lay = self.layout()
        lay.removeItem(self.body)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("inspScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setSizeAdjustPolicy(QScrollArea.SizeAdjustPolicy.AdjustToContents)
        holder = QWidget()
        holder.setObjectName("inspBody")
        self.body = QVBoxLayout(holder)
        self.body.setContentsMargins(0, 0, 4, 0)
        self.body.setSpacing(6)
        self.scroll.setWidget(holder)
        lay.addWidget(self.scroll, 1)
        self.node: Node | None = None
        self.card: Card | None = None
        self.locked_id: str | None = None
        self._flash = QTimer(self, singleShot=True, interval=self.FLASH_MS)
        self._flash.timeout.connect(self._unflash)
        self.copy_btn: QPushButton | None = None
        self.finding: FindingView | None = None
        self.show_node(None)

    def show_node(self, node: Node | None, graph: Graph | None = None,
                  card: Card | None = None, locked: bool = False,
                  now: datetime | None = None, finding: FindingView | None = None) -> None:
        """Summary first: what it is, what needs attention, a few relationships;
        everything else under "details". A finding (a check, or a card about one)
        gets its explanation, source and next step, with the actions."""
        self.node, self.card, self.finding = node, card, finding
        _clear(self.body)
        self.copy_btn = None
        self.details_btn = None
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
        details: list[QWidget] = []
        detail = (node.detail or "").strip()
        if finding is not None:
            self._finding_section(finding, fix_prompt=card is None)  # a card has its own
            if detail and detail != finding.why:
                details.append(self._text(detail))
        elif detail:
            short = detail if len(detail) <= SUMMARY_CHARS else \
                detail[:SUMMARY_CHARS].rsplit(" ", 1)[0].rstrip(" ,.;") + "…"
            self.body.addWidget(self._text(short))
            if short != detail:
                details.append(self._text(detail))
        links = graph.neighbours(node.id) if graph is not None else []
        links = sorted(links, key=lambda eo: (ATTENTION_RANK.get(eo[1].status or "", 9),))
        attention = Counter(o.status for _, o in links if o.status in ATTENTION_RANK)
        if attention and node.kind in ("hub", "part", "core"):
            words = " · ".join(f"{n} {ATTENTION_WORDS.get(st, st)}"
                               for st, n in sorted(attention.items(),
                                                   key=lambda kv: ATTENTION_RANK[kv[0]]))
            self.attention = mono(_label(f"needs attention · {words}", "inspAttention"), 10, 500)
            self.body.addWidget(self.attention)
        if links:
            self.body.addWidget(mono(_label(f"links · {len(links)}", "panelHead"), 10, 500))
            for edge, other in links[:SUMMARY_LINKS]:
                self.body.addWidget(self._link(edge, other))
            for edge, other in links[SUMMARY_LINKS:DETAIL_LINKS]:
                details.append(self._link(edge, other))
            if len(links) > DETAIL_LINKS:
                details.append(mono(_label(f"+{len(links) - DETAIL_LINKS} more", "vNote"), 10))
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
        if card is not None:
            self._card_actions(card, details)
        if node.kind == "item" and node.item_id is not None:
            self._item_actions(node.item_id)
        if details:
            self._details(details)
        self.body.addStretch(1)

    def _text(self, text: str) -> QLabel:
        d = _label(text, "inspText", wrap=True)
        d.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return d

    def _link(self, edge, other: Node) -> QPushButton:
        name = other.label if other.kind in ("issue", "milestone", "commit", "hub",
                                             "core", "part") else other.title
        if len(name) > 38:
            name = name[:37].rstrip() + "…"
        mark = f" · {ATTENTION_WORDS[other.status]}" if other.status in ATTENTION_RANK else ""
        b = QPushButton(f"{EDGE_LABELS.get(edge.kind, edge.kind)} → {name}" + mark)
        b.setObjectName("filter")
        b.setToolTip(other.title)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(lambda _=False, i=other.id: self.open_node.emit(i))
        b.setStyleSheet("text-align: left;")
        # never wider than the column (the tooltip has the full name)
        b.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        return b

    def _details(self, widgets: list[QWidget]) -> None:
        """The rest, folded away: more relationships, full text, the raw prompt."""
        self.details_btn = QPushButton("details ▸")
        self.details_btn.setObjectName("linkBtn")
        self.details_btn.setCheckable(True)
        self.details_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.details = QWidget()
        lay = QVBoxLayout(self.details)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        for w in widgets:
            lay.addWidget(w)
        self.details.hide()

        def toggle(on: bool) -> None:
            self.details.setVisible(on)
            self.details_btn.setText("details ▾" if on else "details ▸")

        self.details_btn.toggled.connect(toggle)
        self.body.addWidget(self.details_btn, 0, Qt.AlignmentFlag.AlignLeft)
        self.body.addWidget(self.details)

    def _section(self, head: str, text: str, name: str = "inspText") -> QLabel:
        self.body.addWidget(mono(_label(head, "panelHead"), 10, 500))
        lb = _label(text, name, wrap=True)
        lb.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lb.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)  # paths
        self.body.addWidget(lb)
        return lb

    def _finding_section(self, f: FindingView, fix_prompt: bool = True) -> None:
        """The verified result, the next step and the actions first; then where it
        comes from, what the check means, and the AI's wording (marked)."""
        self.verified = self._section(f"verified · {f.status_words} · from hardware.yaml", f.why)
        self.next_text = self._section("next step · suggested, not verified", f.next_step)
        src = f.source
        actions = QWidget()
        row = FlowLayout(actions, 6)      # wraps in a narrow column
        self.explain_btn = QPushButton("explain")
        self.explain_btn.setObjectName("primary")
        self.explain_btn.setToolTip("Ask Jarvis in the chat (answers from the synced data)")
        self.explain_btn.clicked.connect(lambda: self.explain.emit(f.explain_question))
        self.source_btn = QPushButton("open source")
        if src is not None and src.url:
            self.source_btn.setToolTip(src.url)
            self.source_btn.clicked.connect(lambda: self.open_source.emit(src.url))
        else:
            self.source_btn.setEnabled(False)
            self.source_btn.setToolTip("No hardware.yaml found to open")
        self.fix_btn = None
        buttons = [self.explain_btn, self.source_btn]
        if fix_prompt:
            self.fix_btn = QPushButton("copy fix prompt")
            self.fix_btn.setToolTip("Put a prompt for Claude on the clipboard: the finding with "
                                    "its numbers, asking how to fix it. Nothing is changed here.")
            self.fix_btn.clicked.connect(lambda: self.copy_prompt.emit(f.prompt))
            buttons.append(self.fix_btn)
        for b in buttons:
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            row.addWidget(b)
        self.body.addSpacing(2)
        self.body.addWidget(actions)
        if src is not None:
            where = src.label
            if src.field:
                where += f"\nfield {src.field}"
                if src.line:
                    where += f" · line {src.line}" + ("" if src.present else
                                                      " (not written yet: add it there)")
                else:
                    where += " · not found in the file"
            self.source_text = self._section("source", where, "inspSource")
            self.edit_note = mono(_label(src.edit_note, "vNote", wrap=True), 10)
            self.body.addWidget(self.edit_note)
        if f.explain:
            self.explained = self._section("what it checks", f.explain + " " + f.matters)
        if f.ai_why:
            self.ai_text = self._section(f"AI summary · {f.ai_model or 'model'} · not verified",
                                         f.ai_why, "inspAi")

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

    def _card_actions(self, card: Card, details: list[QWidget]) -> None:
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
        details.append(self.prompt)

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


__all__ = ["Panel", "VitalsPanel", "IssuesPanel", "MilestonesPanel", "CommitPanel",
           "BriefingHud", "Inspector", "StreamPanel", "StreamRow", "pill", "set_pill",
           "COPY_LABEL", "QUrl", "QDesktopServices"]
