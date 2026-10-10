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
when asked. Three columns when there is room, two when narrow, one beside the
chat in a small window. Data comes from home_data.build_home.
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

TODAY_MAX = 10
NARROW_BELOW = 1000   # Home's own width: two columns below this
SINGLE_BELOW = 640    # ... and one below this (a small window with the chat open)
KIND_TAG = {"milestone": "milestone", "task": "task", "idea": "idea", "reminder": "reminder"}


def _link(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setObjectName("linkBtn")
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


def _add_to_head(panel: Panel, w: QWidget) -> None:
    panel.layout().itemAt(0).layout().addWidget(w)


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
        col.setContentsMargins(8, 7, 8, 8)
        col.setSpacing(3)
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
        self.page.setContentsMargins(20, 16, 20, 16)
        self.page.setSpacing(14)
        self.scroll.setWidget(page)

        # greeting row
        head = QHBoxLayout()
        head.setSpacing(12)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.greeting = _label("", "wsTitle")
        titles.addWidget(self.greeting)
        self.attention = _label("", "homeAttention", rich=True)
        titles.addWidget(self.attention)
        head.addLayout(titles)
        head.addStretch(1)
        self.date = typeset(_label("", "wsSub"), "meta")
        head.addWidget(self.date, 0, Qt.AlignmentFlag.AlignBottom)
        self.capture_btn = QPushButton("+ Capture")
        self.capture_btn.setObjectName("primary")
        self.capture_btn.setToolTip("Quick capture (Ctrl+N)")
        self.capture_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.capture_btn.clicked.connect(self.capture_requested)
        head.addWidget(self.capture_btn, 0, Qt.AlignmentFlag.AlignBottom)
        self.page.addLayout(head)

        # sections
        self.today = Panel("Today")
        self.today.setProperty("emphasis", "true")
        typeset(self.today.head, "title")
        self.deadlines = Panel("Upcoming", "next 14 days")
        self.inbox = Panel("Inbox", "no date, no project")
        self.project = Panel("Projects")
        self.overview = Panel("Overview")
        for p in (self.deadlines, self.inbox, self.project):
            p.body.setSpacing(2)
        self.today.body.setSpacing(1)
        self.today_link = _link("All tasks →")
        self.today_link.clicked.connect(lambda: self.open_workspace.emit("tasks"))
        _add_to_head(self.today, self.today_link)
        self.project_link = _link("Open Projects →")
        self.project_link.clicked.connect(lambda: self.open_workspace.emit("projects"))
        _add_to_head(self.project, self.project_link)
        self.grid = QGridLayout()
        self.grid.setSpacing(12)
        self.cols: list[QWidget] = []
        for c in range(3):
            col = QWidget()
            lay = QVBoxLayout(col)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(12)
            lay.addStretch(1)
            self.grid.addWidget(col, 0, c)
            self.cols.append(col)
        self.page.addLayout(self.grid)
        self.errors = _label("", "fieldError", wrap=True)
        self.errors.hide()
        self.page.addWidget(self.errors)
        self.page.addStretch(1)
        self.today_rows: list[TaskRow] = []
        self.inbox_rows: list[TaskRow] = []
        self.deadline_rows: list[QWidget] = []
        self.project_rows: list[ProjectRow] = []
        self.stats: dict[str, QWidget] = {}
        self._place(3)

    # ------------------------------------------------------------ layout

    def _panels(self) -> tuple[Panel, ...]:
        return (self.today, self.deadlines, self.inbox, self.project, self.overview)

    def _place(self, columns: int) -> None:
        """Panels into ``columns`` independent stacks (a short panel never leaves
        a gap beside a tall one)."""
        if columns == self.columns:
            return
        self.columns = columns
        self.narrow = columns < 3
        for p in self._panels():
            p.setParent(None)
        for c, col in enumerate(self.cols):
            col.setVisible(c < columns)
            self.grid.setColumnStretch(c, 0)
        stacks = {
            1: ((self.today, self.deadlines, self.inbox, self.project, self.overview),),
            2: ((self.today, self.inbox), (self.overview, self.deadlines, self.project)),
            3: ((self.today,), (self.deadlines, self.inbox), (self.overview, self.project)),
        }[columns]
        stretch = {1: (1,), 2: (5, 4), 3: (5, 4, 3)}[columns]
        for c, panels in enumerate(stacks):
            lay = self.cols[c].layout()
            for p in panels:
                lay.insertWidget(lay.count() - 1, p)    # above the stretch
                p.show()
            self.grid.setColumnStretch(c, stretch[c])

    def resizeEvent(self, e) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(e)
        w = self.width()
        self._place(1 if w < SINGLE_BELOW else 2 if w < NARROW_BELOW else 3)

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

    def _fill_deadlines(self, d: HomeData) -> None:
        _clear(self.deadlines.body)
        self.deadline_rows = []
        for dl in d.deadlines:
            row = QWidget()
            lay = QHBoxLayout(row)
            lay.setContentsMargins(2, 4, 2, 4)
            lay.setSpacing(10)
            when = typeset(_label(dl.label + (f" {dl.time}" if dl.time else ""),
                                  "deadlineWhen"), "label")
            when.setFixedWidth(118)
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
        row.setSpacing(8)
        for key, value, label, level in (
                ("done", o.done_week, "done · 7 days", "good" if o.done_week else ""),
                ("open", o.open, "open", ""),
                ("overdue", o.overdue, "overdue", "bad" if o.overdue else "")):
            cell = QVBoxLayout()
            cell.setSpacing(0)
            num = typeset(_label(str(value), "statNum"), "big")
            num.setProperty("tone", level)
            cell.addWidget(num)
            cell.addWidget(typeset(_label(label, "vKey"), "meta"))
            row.addLayout(cell, 1)
            self.stats[key] = num
        self.overview.body.addLayout(row)

    def _empty(self, box, title: str, hint: str) -> None:
        box.addWidget(_label(title, "homeEmpty", wrap=True))
        if hint:
            box.addWidget(_label(hint, "homeEmptyHint", wrap=True))
