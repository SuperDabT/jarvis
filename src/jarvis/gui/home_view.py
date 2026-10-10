"""The Home workspace: what needs attention today, at a glance.

A greeting with the day's count, then compact panels: today (overdue first,
then due today, each can be ticked off), upcoming deadlines (items and project
milestones, two weeks out), the project (briefing headline and vitals), next
steps (computed by Jarvis from the project data, never the model) and recent
activity. Three columns when there is room, two when narrow, one beside the
chat in a small window. Data comes from
home_data.build_home; Home itself only completes items (through the window).
"""
from __future__ import annotations

from datetime import UTC, datetime
from html import escape

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from jarvis.gui.cockpit import Panel, StreamRow
from jarvis.gui.home_data import HomeData
from jarvis.gui.tasks_view import TaskRow
from jarvis.gui.theme import Tokens, typeset
from jarvis.gui.widgets import _clear, _label

TODAY_MAX = 8
NARROW_BELOW = 1000   # Home's own width: two columns below this
SINGLE_BELOW = 640    # ... and one below this (a small window with the chat open)
KIND_TAG = {"milestone": "milestone", "task": "task", "idea": "idea", "reminder": "reminder"}


def _link(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setObjectName("linkBtn")
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


class HomeView(QWidget):
    open_workspace = Signal(str)
    capture_requested = Signal()
    complete = Signal(int)        # item id
    open_node = Signal(str)       # a graph node from the activity list

    def __init__(self, tokens: Tokens, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tokens = tokens
        self.data: HomeData | None = None
        self.narrow: bool | None = None
        self.columns = 0
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(self.scroll)
        page = QWidget()
        self.page = QVBoxLayout(page)
        self.page.setContentsMargins(16, 14, 16, 14)
        self.page.setSpacing(12)
        self.scroll.setWidget(page)

        # greeting row
        head = QHBoxLayout()
        head.setSpacing(12)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.greeting = _label("", "wsTitle")
        titles.addWidget(self.greeting)
        self.attention = _label("", "homeAttention", rich=True)
        titles.addWidget(self.attention)
        head.addLayout(titles)
        head.addStretch(1)
        self.date = typeset(_label("", "wsSub"), "meta")
        head.addWidget(self.date, 0, Qt.AlignmentFlag.AlignBottom)
        self.capture_btn = QPushButton("+ capture")
        self.capture_btn.setObjectName("primary")
        self.capture_btn.setToolTip("Quick capture (Ctrl+N)")
        self.capture_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.capture_btn.clicked.connect(self.capture_requested)
        head.addWidget(self.capture_btn, 0, Qt.AlignmentFlag.AlignBottom)
        self.page.addLayout(head)

        # panels
        self.today = Panel("today")
        self.deadlines = Panel("upcoming", "next 14 days")
        self.project = Panel("project")
        self.steps = Panel("next steps", "computed from the project data")
        self.activity = Panel("recent activity")
        for p in (self.today, self.deadlines, self.project, self.steps, self.activity):
            p.body.setSpacing(2)
        self.today_link = _link("all tasks →")
        self.today_link.clicked.connect(lambda: self.open_workspace.emit("tasks"))
        self.today.layout().itemAt(0).layout().addWidget(self.today_link)
        self.project_link = _link("open Projects →")
        self.project_link.clicked.connect(lambda: self.open_workspace.emit("projects"))
        self.project.layout().itemAt(0).layout().addWidget(self.project_link)
        self.grid = QGridLayout()
        self.grid.setSpacing(12)
        self.page.addLayout(self.grid)
        self.errors = _label("", "fieldError", wrap=True)
        self.errors.hide()
        self.page.addWidget(self.errors)
        self.page.addStretch(1)
        self.today_rows: list[TaskRow] = []
        self.deadline_rows: list[QWidget] = []
        self._place(3)

    # ------------------------------------------------------------ layout

    def _place(self, columns: int) -> None:
        if columns == self.columns:
            return
        self.columns = columns
        self.narrow = columns < 3
        panels = (self.today, self.deadlines, self.project, self.steps, self.activity)
        for p in panels:
            self.grid.removeWidget(p)
        for c in range(3):
            self.grid.setColumnStretch(c, 0)
        top = Qt.AlignmentFlag.AlignTop
        if columns == 1:
            for r, p in enumerate(panels):
                self.grid.addWidget(p, r, 0, top)
            self.grid.setColumnStretch(0, 1)
        elif columns == 2:
            self.grid.addWidget(self.today, 0, 0, top)
            self.grid.addWidget(self.deadlines, 1, 0, top)
            self.grid.addWidget(self.project, 0, 1, top)
            self.grid.addWidget(self.steps, 1, 1, top)
            self.grid.addWidget(self.activity, 2, 0, 1, 2, top)
            for c in range(2):
                self.grid.setColumnStretch(c, 1)
        else:
            self.grid.addWidget(self.today, 0, 0, 2, 1, top)
            self.grid.addWidget(self.deadlines, 0, 1, top)
            self.grid.addWidget(self.steps, 1, 1, top)
            self.grid.addWidget(self.project, 0, 2, top)
            self.grid.addWidget(self.activity, 1, 2, top)
            self.grid.setColumnStretch(0, 5)
            self.grid.setColumnStretch(1, 4)
            self.grid.setColumnStretch(2, 4)

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
        self.date.setText(f"{data.today:%A %b} {data.today.day}")
        self._fill_today(data)
        self._fill_deadlines(data)
        self._fill_project(data)
        self._fill_steps(data)
        self._fill_activity(data)
        self.errors.setText(" · ".join(data.errors))
        self.errors.setVisible(bool(data.errors))

    def _fill_today(self, d: HomeData) -> None:
        _clear(self.today.body)
        self.today_rows = []
        rows = (*d.overdue, *d.due_today)
        self.today.head.setText(f"today · {len(rows)}" if rows else "today")
        for r in rows[:TODAY_MAX]:
            w = TaskRow(r, self.tokens, compact=True)
            w.toggled.connect(self.complete)
            w.selected.connect(lambda _id: self.open_workspace.emit("tasks"))
            self.today.body.addWidget(w)
            self.today_rows.append(w)
        if len(rows) > TODAY_MAX:
            more = _link(f"+{len(rows) - TODAY_MAX} more in Tasks →")
            more.clicked.connect(lambda: self.open_workspace.emit("tasks"))
            self.today.body.addWidget(more, 0, Qt.AlignmentFlag.AlignLeft)
        if not rows:
            self._empty(self.today.body, "Nothing due today or overdue.",
                        "Capture with Ctrl+N; things with a date show up here on the day.")

    def _fill_deadlines(self, d: HomeData) -> None:
        _clear(self.deadlines.body)
        self.deadline_rows = []
        for dl in d.deadlines:
            row = QWidget()
            lay = QHBoxLayout(row)
            lay.setContentsMargins(2, 4, 2, 4)
            lay.setSpacing(10)
            when = typeset(_label(dl.label + (f" {dl.time}" if dl.time else ""), "deadlineWhen"), "label")
            when.setFixedWidth(136)
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

    def _fill_project(self, d: HomeData) -> None:
        _clear(self.project.body)
        self.project.head.setText(f"project · {d.project_name}" if d.project_name else "project")
        if d.headline:
            self.headline = _label(d.headline, "homeHeadline", wrap=True)
            self.project.body.addWidget(self.headline)
            by = typeset(_label(f"briefing · {d.briefing_by}", "panelHint"), "code")
            self.project.body.addWidget(by)
        else:
            self.headline = None
            self._empty(self.project.body, "No briefing yet.", "Refresh (Ctrl+R) writes one.")
        if d.vitals:
            grid = QGridLayout()
            grid.setHorizontalSpacing(12)
            grid.setVerticalSpacing(3)
            grid.setContentsMargins(0, 6, 0, 0)
            grid.setColumnStretch(1, 1)
            for r, v in enumerate(d.vitals):
                grid.addWidget(typeset(_label(v.key, "vKey"), "label"), r, 0)
                val = typeset(_label(v.value, "vVal"), "value")
                val.setProperty("live", "true" if v.live else "false")
                val.setProperty("level", v.level or "")
                grid.addWidget(val, r, 1)
            self.project.body.addLayout(grid)

    def _fill_steps(self, d: HomeData) -> None:
        _clear(self.steps.body)
        for i, s in enumerate(d.next_steps, 1):
            row = QWidget()
            lay = QHBoxLayout(row)
            lay.setContentsMargins(2, 3, 2, 5)
            lay.setSpacing(10)
            lay.addWidget(_label(str(i), "stepNum"), 0, Qt.AlignmentFlag.AlignTop)
            col = QVBoxLayout()
            col.setSpacing(1)
            col.addWidget(_label(s.text, "stepText", wrap=True))
            if s.reason:
                col.addWidget(_label(s.reason, "stepReason", wrap=True))
            lay.addLayout(col, 1)
            self.steps.body.addWidget(row)
        if not d.next_steps:
            self._empty(self.steps.body, "Nothing pressing in the project data.", "")

    def _fill_activity(self, d: HomeData) -> None:
        _clear(self.activity.body)
        now = datetime.now(UTC)
        for item in d.activity:
            r = StreamRow(item, now)
            r.clicked.connect(self.open_node)
            self.activity.body.addWidget(r)
        if not d.activity:
            self._empty(self.activity.body, "No activity yet.", "")

    def _empty(self, box, title: str, hint: str) -> None:
        box.addWidget(_label(title, "homeEmpty", wrap=True))
        if hint:
            box.addWidget(_label(hint, "homeEmptyHint", wrap=True))
