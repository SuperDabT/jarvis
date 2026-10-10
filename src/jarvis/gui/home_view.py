"""The Home workspace: the day at a glance, and the user's own lists.

Five sections, Today first and strongest:

- Today: overdue items, then the ones due today; each can be ticked off or
  opened in Tasks.
- Upcoming: what has a date in the next two weeks (items and the GitHub
  project's milestones).
- Inbox: open items with no date and no project (captures not yet sorted).
- Projects: each active project's open work, its next item by date (the
  user's order: date, then their own priority) and, for the GitHub project,
  the nearest-due milestone.
- Overview: done in the last 7 days, open, overdue.

Home never ranks, schedules or changes anything by itself: it shows the user's
data in the order they gave it, and only ticks items off (through the window)
when asked. Layout: Today is the page's main list, full height on the
background; the other four are flat sections in one side panel, split by
hairlines. The side panel narrows below NARROW_BELOW and drops under Today in a
small window (beside the chat). Data comes from home_data.build_home.
"""
from __future__ import annotations

from html import escape

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from jarvis.gui.cockpit import Panel
from jarvis.gui.home_data import HomeData, ProjectSummary
from jarvis.gui.tasks_view import TaskRow
from jarvis.gui.theme import Tokens, typeset
from jarvis.gui.widgets import HoverRow, _clear, _label, repolish

TODAY_MAX = 12
NARROW_BELOW = 1000   # Home's own width: a narrower side panel below this
SINGLE_BELOW = 640    # ... and one column below this (a small window with the chat open)
SIDE_W = (400, 330)   # the side panel: wide, narrow
KIND_TAG = {"milestone": "milestone", "task": "task", "idea": "idea", "reminder": "reminder"}


def _link(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setObjectName("linkBtn")
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


def _add_to_head(panel: Panel, w: QWidget) -> None:
    panel.layout().itemAt(0).layout().addWidget(w)


ROW_INSET = 6   # hoverable rows pad their text by this; headings line up with it


def _flat(panel: Panel, name: str, margins: tuple[int, int, int, int]) -> Panel:
    """A Panel restyled as a flat section (no card of its own); its heading is
    inset like the rows so text, checkboxes and headings share one edge."""
    panel.setObjectName(name)
    left, top, right, bottom = margins
    panel.layout().setContentsMargins(left - ROW_INSET, top, right - ROW_INSET, bottom)
    panel.layout().setSpacing(6)
    panel.layout().itemAt(0).layout().setContentsMargins(ROW_INSET, 0, ROW_INSET, 0)
    panel.body.setContentsMargins(0, 0, 0, 0)
    return panel


def _hairline() -> QFrame:
    line = QFrame()
    line.setObjectName("hairline")
    line.setFixedHeight(1)
    return line


class Progress(QWidget):
    """A thin track with a mint fill (``closed`` of ``total``)."""

    def __init__(self, closed: int, total: int, tokens: Tokens, parent: QWidget | None = None):
        super().__init__(parent)
        self.closed, self.total, self.tokens = closed, total, tokens
        self.setFixedHeight(4)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def paintEvent(self, e) -> None:  # noqa: N802 - Qt API
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        r = QRectF(self.rect())
        p.setBrush(QColor(self.tokens.fill2))
        p.drawRoundedRect(r, 2, 2)
        if self.total and self.closed:
            p.setBrush(QColor(self.tokens.signal))
            p.drawRoundedRect(QRectF(r.left(), r.top(), r.width() * self.closed / self.total,
                                     r.height()), 2, 2)
        p.end()


class ProjectRow(HoverRow):
    """One project: name, open count, its next item, and (the GitHub project)
    the nearest-due milestone. Click: its items in Tasks."""

    def __init__(self, s: ProjectSummary, tokens: Tokens, parent: QWidget | None = None):
        super().__init__(tokens, parent)
        self.summary = s
        self.setToolTip(f"{s.name}: open its items in Tasks")
        col = QVBoxLayout(self)
        col.setContentsMargins(ROW_INSET, 5, ROW_INSET, 7)
        col.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(8)
        name = typeset(_label(s.name, "projName"), "value")
        name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        top.addWidget(name, 1)
        if s.status != "active":
            top.addWidget(typeset(_label(s.status, "issueTag"), "tag"))
        count = f"{s.open} open" + (f" · {s.done} done" if s.done else "")
        top.addWidget(typeset(_label(count, "vKey"), "meta"))
        col.addLayout(top)
        if s.next_title:
            nxt = f"Next: {s.next_title}" + (f" · {s.next_when}" if s.next_when else "")
            self.next_label = typeset(_label(nxt, "projNext"), "meta")
            self.next_label.setSizePolicy(QSizePolicy.Policy.Ignored,
                                          QSizePolicy.Policy.Preferred)
            self.next_label.setToolTip(nxt)
            col.addWidget(self.next_label)
        else:
            self.next_label = None
            col.addWidget(typeset(_label("No open items", "vNote"), "meta"))
        if s.milestone:
            ms = QHBoxLayout()
            ms.setSpacing(8)
            ml = typeset(_label(s.milestone, "vSub"), "meta")
            ml.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            ms.addWidget(ml, 1)
            ms.addWidget(typeset(_label(f"{s.milestone_closed}/{s.milestone_total} · "
                                        f"{s.milestone_due}", "vKey"), "meta"))
            col.addSpacing(3)
            col.addLayout(ms)
            col.addWidget(Progress(s.milestone_closed, s.milestone_total, tokens))
        self.setMinimumHeight(col.sizeHint().height())


class HomeView(QWidget):
    open_workspace = Signal(str)
    capture_requested = Signal()
    complete = Signal(int)        # item id
    open_item = Signal(int)       # item id: edit it in Tasks
    open_project = Signal(str)    # project slug: its items in Tasks
    open_node = Signal(str)       # a graph node (kept for the window's wiring)

    def __init__(self, tokens: Tokens, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tokens = tokens
        self.data: HomeData | None = None
        self.narrow: bool | None = None
        self.columns = 0
        self.headline = None      # Home no longer repeats the project briefing
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(self.scroll)
        page = QWidget()
        self.page = QVBoxLayout(page)
        self.page.setContentsMargins(24, 14, 20, 16)
        self.page.setSpacing(12)
        self.scroll.setWidget(page)

        # greeting row
        head = QHBoxLayout()
        head.setSpacing(12)
        titles = QVBoxLayout()
        titles.setSpacing(1)
        self.greeting = _label("", "wsTitle")
        titles.addWidget(self.greeting)
        sub = QHBoxLayout()
        sub.setSpacing(6)
        self.date = typeset(_label("", "homeDate"), "body")
        sub.addWidget(self.date)
        sub.addWidget(typeset(_label("·", "homeDate"), "body"))
        self.attention = _label("", "homeAttention", rich=True)
        typeset(self.attention, "body")
        sub.addWidget(self.attention)
        sub.addStretch(1)
        titles.addLayout(sub)
        head.addLayout(titles, 1)
        self.capture_btn = QPushButton("+ Capture")
        self.capture_btn.setObjectName("primary")
        self.capture_btn.setToolTip("Quick capture (Ctrl+N)")
        self.capture_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.capture_btn.clicked.connect(self.capture_requested)
        head.addWidget(self.capture_btn, 0, Qt.AlignmentFlag.AlignBottom)
        self.page.addLayout(head)

        # sections
        self.today = _flat(Panel("Today"), "homeMain", (ROW_INSET, 4, ROW_INSET, 0))
        typeset(self.today.head, "title")
        self.deadlines = _flat(Panel("Upcoming", "next 14 days"), "homeSection", (14, 11, 14, 11))
        self.inbox = _flat(Panel("Inbox", "no date, no project"), "homeSection",
                           (14, 11, 14, 11))
        self.project = _flat(Panel("Projects"), "homeSection", (14, 11, 14, 12))
        self.overview = _flat(Panel("Overview"), "homeSection", (14, 11, 14, 11))
        for p in (self.deadlines, self.inbox, self.project, self.today):
            p.body.setSpacing(0)
        self.today_link = _link("All tasks →")
        self.today_link.clicked.connect(lambda: self.open_workspace.emit("tasks"))
        _add_to_head(self.today, self.today_link)
        self.project_link = _link("Open Projects →")
        self.project_link.clicked.connect(lambda: self.open_workspace.emit("projects"))
        _add_to_head(self.project, self.project_link)
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(20)
        self.grid.setVerticalSpacing(14)
        # the side panel: four flat sections split by hairlines
        self.side = QFrame()
        self.side.setObjectName("homeSide")
        self.side.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        side = QVBoxLayout(self.side)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(0)
        for i, p in enumerate((self.overview, self.deadlines, self.inbox, self.project)):
            if i:
                side.addWidget(_hairline())
            side.addWidget(p)
        side.addStretch(1)
        self.page.addLayout(self.grid, 1)
        self.errors = _label("", "fieldError", wrap=True)
        self.errors.hide()
        self.page.addWidget(self.errors)
        self.today_rows: list[TaskRow] = []
        self.inbox_rows: list[TaskRow] = []
        self.deadline_rows: list[QWidget] = []
        self.project_rows: list[ProjectRow] = []
        self.stats: dict[str, QWidget] = {}
        self._place(2)
        self._side_width(False)

    # ------------------------------------------------------------ layout

    def _place(self, columns: int) -> None:
        """Two columns (Today, the side panel), or one: the side panel under
        Today. ``narrow``: the side panel at its narrower width (or below)."""
        if columns == self.columns:
            return
        self.columns = columns
        self.grid.removeWidget(self.today)
        self.grid.removeWidget(self.side)
        self.grid.setColumnStretch(0, 1)
        top = Qt.AlignmentFlag.AlignTop
        if columns == 1:
            self.grid.addWidget(self.today, 0, 0, top)
            self.grid.addWidget(self.side, 1, 0, top)
            self.side.setMinimumWidth(0)
            self.side.setMaximumWidth(16777215)
            self.grid.setColumnStretch(1, 0)
        else:
            self.grid.addWidget(self.today, 0, 0)
            self.grid.addWidget(self.side, 0, 1)
            self.grid.setRowStretch(0, 1)

    def _side_width(self, narrow: bool) -> None:
        self.narrow = narrow
        if self.columns == 2:
            self.side.setFixedWidth(SIDE_W[1] if narrow else SIDE_W[0])

    def resizeEvent(self, e) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(e)
        w = self.width()
        self._place(1 if w < SINGLE_BELOW else 2)
        self._side_width(w < NARROW_BELOW)

    # ------------------------------------------------------------ data

    def set_data(self, data: HomeData, force: bool = False) -> None:
        if data == self.data and not force:
            return
        self.data = data
        t = self.tokens
        self.greeting.setText(data.greeting)
        att = escape(data.attention)
        if data.overdue:
            first, _, rest = att.partition(" · ")
            att = f"<span style='color:{t.red}'>{first}</span>" + (f" · {rest}" if rest else "")
        self.attention.setText(att)
        self.date.setText(f"{data.today:%A, %B} {data.today.day}")
        self._fill_today(data)
        self._fill_deadlines(data)
        self._fill_inbox(data)
        self._fill_projects(data)
        self._fill_overview(data)
        self.errors.setText(" · ".join(data.errors))
        self.errors.setVisible(bool(data.errors))

    def _task(self, row, box) -> TaskRow:
        w = TaskRow(row, self.tokens, compact=True)
        w.layout().setContentsMargins(ROW_INSET, 5, ROW_INSET, 5)
        w.toggled.connect(self.complete)
        w.selected.connect(self.open_item)
        box.addWidget(w)
        return w

    def _group_head(self, text: str, level: str = "") -> None:
        lb = typeset(_label(text, "homeGroup"), "label")
        lb.setProperty("level", level)
        repolish(lb)
        self.today.body.addWidget(lb)

    def _fill_today(self, d: HomeData) -> None:
        _clear(self.today.body)
        self.today_rows = []
        rows = (*d.overdue, *d.due_today)
        self.today.head.setText(f"Today · {len(rows)}" if rows else "Today")
        shown = 0
        for label, group, level in (("Overdue", d.overdue, "late"), ("Due today", d.due_today, "")):
            if not group or shown >= TODAY_MAX:
                continue
            if d.overdue:   # the split only matters when something is late
                self._group_head(f"{label} · {len(group)}", level)
            for r in group[:TODAY_MAX - shown]:
                self.today_rows.append(self._task(r, self.today.body))
                shown += 1
        if len(rows) > shown:
            more = _link(f"+{len(rows) - shown} more in Tasks →")
            more.clicked.connect(lambda: self.open_workspace.emit("tasks"))
            self.today.body.addWidget(more, 0, Qt.AlignmentFlag.AlignLeft)
        if not rows:
            self._empty(self.today.body, "Nothing due today or overdue.",
                        "Things you give a date show up here on the day. "
                        "Capture with Ctrl+N; nothing is scheduled for you.")
        self.today.body.addStretch(1)     # the list starts at the top of the column

    def _fill_deadlines(self, d: HomeData) -> None:
        _clear(self.deadlines.body)
        self.deadline_rows = []
        for dl in d.deadlines:
            row = QWidget()
            lay = QHBoxLayout(row)
            lay.setContentsMargins(ROW_INSET, 3, ROW_INSET, 3)
            lay.setSpacing(10)
            when = typeset(_label(dl.label + (f" {dl.time}" if dl.time else ""),
                                  "deadlineWhen"), "label")
            when.setFixedWidth(when.fontMetrics().horizontalAdvance("Wed Oct 14 12:00 PM") + 4)
            lay.addWidget(when, 0, Qt.AlignmentFlag.AlignTop)
            title = _label(dl.title, "deadlineTitle", wrap=True)
            lay.addWidget(title, 1)
            tag = typeset(_label(KIND_TAG.get(dl.kind, dl.kind), "deadlineTag"), "tag")
            tag.setProperty("kind", dl.kind)
            lay.addWidget(tag, 0, Qt.AlignmentFlag.AlignTop)
            if dl.kind == "milestone":
                row.setToolTip("A milestone of the project (GitHub)")
            self.deadlines.body.addWidget(row)
            self.deadline_rows.append(row)
        if not d.deadlines:
            self._empty(self.deadlines.body, "Nothing due in the next two weeks.", "")

    def _fill_inbox(self, d: HomeData) -> None:
        _clear(self.inbox.body)
        self.inbox_rows = []
        self.inbox.head.setText(f"Inbox · {d.inbox_total}" if d.inbox_total else "Inbox")
        for r in d.inbox:
            self.inbox_rows.append(self._task(r, self.inbox.body))
        if d.inbox_total > len(d.inbox):
            more = _link(f"+{d.inbox_total - len(d.inbox)} more in Tasks →")
            more.clicked.connect(lambda: self.open_workspace.emit("tasks"))
            self.inbox.body.addWidget(more, 0, Qt.AlignmentFlag.AlignLeft)
        if not d.inbox:
            self._empty(self.inbox.body, "Inbox is clear.",
                        "Captures without a date or project wait here until you sort them.")

    def _fill_projects(self, d: HomeData) -> None:
        _clear(self.project.body)
        self.project_rows = []
        for s in d.projects:
            row = ProjectRow(s, self.tokens)
            row.clicked.connect(lambda _=False, slug=s.slug: self.open_project.emit(slug))
            self.project.body.addWidget(row)
            self.project_rows.append(row)
        if not d.projects:
            self._empty(self.project.body, "No active projects.", "")

    def _fill_overview(self, d: HomeData) -> None:
        _clear(self.overview.body)
        self.stats = {}
        o = d.overview
        if o is None:
            self._empty(self.overview.body, "No item data.", "")
            return
        row = QHBoxLayout()
        row.setContentsMargins(ROW_INSET, 0, ROW_INSET, 0)
        row.setSpacing(18)
        for key, value, label, level in (
                ("done", o.done_week, "done · 7 days", "good" if o.done_week else ""),
                ("open", o.open, "open", ""),
                ("overdue", o.overdue, "overdue", "bad" if o.overdue else "")):
            pair = QHBoxLayout()
            pair.setSpacing(5)
            num = typeset(_label(str(value), "statNum"), "title")
            num.setProperty("tone", level)
            pair.addWidget(num, 0, Qt.AlignmentFlag.AlignBaseline)
            pair.addWidget(typeset(_label(label, "vKey"), "meta"), 0,
                           Qt.AlignmentFlag.AlignBaseline)
            row.addLayout(pair)
            self.stats[key] = num
        row.addStretch(1)
        self.overview.body.addLayout(row)

    def _empty(self, box, title: str, hint: str) -> None:
        """One quiet line (and a fainter hint): an empty section stays small."""
        for text, name, role in ((title, "homeEmpty", "body"), (hint, "homeEmptyHint", "meta")):
            if text:
                lb = typeset(_label(text, name, wrap=True), role)
                lb.setContentsMargins(ROW_INSET, 0, ROW_INSET, 0)
                box.addWidget(lb)
