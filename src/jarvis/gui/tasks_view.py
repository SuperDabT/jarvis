"""The Tasks workspace: open items in Overdue / Today / Upcoming / Someday, with
filters, an editor, done / reopen, delete / restore. All writes go through
jarvis.items on the window's connection; new items come from quick capture.

Rows are compact: a check circle, the title, one line of when / late /
reminder / project, and the priority marks. Overdue rows show how late they are
in red and nothing else turns red. Click a row to edit it in the side panel;
Delete removes the selected row (Undo brings it back).
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, tzinfo
from html import escape

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPainterPath, QPen, QShortcut
from PySide6.QtWidgets import (
    QAbstractButton,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from jarvis import items as it
from jarvis.gui.tasks_data import (
    GROUP_LABELS,
    KIND_FILTERS,
    TaskRowView,
    TasksData,
    load_tasks,
    parse_when_input,
)
from jarvis.gui.theme import Tokens, mix
from jarvis.gui.widgets import _clear, _label, repolish

EDITOR_W = 320
LIST_MAX = 960        # the list stops growing here on wide screens (rows stay readable)
UNDO_MS = 12_000
PRIORITY_LABELS = ("none", "! low", "!! high", "!!! urgent")
EMPTY_ALL = ("Nothing open.", ("Capture a task, idea or reminder with Ctrl+N; it lands here, "
                              "sorted by when it's due."))
EMPTY_FILTERED = ("Nothing matches these filters.",
                  "Pick “all” or another project to see the rest.")


# ---------------------------------------------------------------- pieces


class CheckCircle(QAbstractButton):
    """The round done box: empty when open, a mint disc with a tick when done."""

    def __init__(self, tokens: Tokens, done: bool = False, late: bool = False,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tokens, self.late = tokens, late
        self.setCheckable(True)
        self.setChecked(done)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(20, 20)
        self.setToolTip("Reopen" if done else "Mark done")
        self._hover = False

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return QSize(20, 20)

    def enterEvent(self, e) -> None:  # noqa: N802 - Qt API
        self._hover = True
        self.update()

    def leaveEvent(self, e) -> None:  # noqa: N802 - Qt API
        self._hover = False
        self.update()

    def paintEvent(self, e) -> None:  # noqa: N802 - Qt API
        t = self.tokens
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(2.5, 2.5, 15, 15)
        if self.isChecked():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(t.signal))
            p.drawEllipse(r)
            path = QPainterPath()
            path.moveTo(6.3, 10.2)
            path.lineTo(8.8, 12.7)
            path.lineTo(13.8, 7.4)
            p.setPen(QPen(QColor(t.bg), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                          Qt.PenJoinStyle.RoundJoin))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
        else:
            line = t.signal if self._hover else (mix(t.bg, t.red, 0.7) if self.late
                                                 else t.btn_line)
            p.setPen(QPen(QColor(line), 1.4))
            p.setBrush(QColor(mix(t.bg, t.signal, 0.12)) if self._hover else Qt.BrushStyle.NoBrush)
            p.drawEllipse(r)
        p.end()


def meta_html(row: TaskRowView, tokens: Tokens, show_kind: bool = True) -> str:
    """The row's second line: kind · when · late · reminder · #project."""
    bits = []
    if show_kind and row.item.kind != "task":
        bits.append(escape(row.item.kind))
    if row.when:
        bits.append(escape(row.when))
    if row.late:
        bits.append(f"<span style='color:{tokens.red}'>{escape(row.late)}</span>")
    if row.remind:
        bits.append(escape(row.remind))
    if row.project:
        bits.append(f"<span style='color:{tokens.signal_text}'>#{escape(row.project)}</span>")
    if row.group == "done" and row.item.completed_at:
        bits.append("done")
    if row.group == "deleted":
        bits.append("deleted")
    return " · ".join(bits)


class TaskRow(QFrame):
    """One item. ``compact`` (Home): no delete button and no body line."""

    selected = Signal(int)
    toggled = Signal(int)
    delete = Signal(int)
    restore = Signal(int)

    def __init__(self, row: TaskRowView, tokens: Tokens, compact: bool = False,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.row = row
        self.setObjectName("taskRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setProperty("selected", "false")
        self.setProperty("late", "true" if row.late else "false")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(10)
        deleted = row.group == "deleted"
        self.check = CheckCircle(tokens, row.item.done, bool(row.late))
        self.check.clicked.connect(lambda: self.toggled.emit(row.id))
        self.check.setVisible(not deleted)
        lay.addWidget(self.check, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(1)
        self.title = _label(row.title, "taskTitle")
        self.title.setProperty("done", "true" if (row.item.done or deleted) else "false")
        self.title.setWordWrap(True)
        col.addWidget(self.title)
        meta = meta_html(row, tokens)
        self.meta = _label(meta, "taskMeta", rich=True)
        self.meta.setTextFormat(Qt.TextFormat.RichText)
        self.meta.setVisible(bool(meta))
        col.addWidget(self.meta)
        if row.item.body and not compact:
            body = row.item.body.splitlines()[0]
            self.body = _label(body[:140] + ("…" if len(body) > 140 else ""), "taskBody")
            col.addWidget(self.body)
        lay.addLayout(col, 1)
        self.prio = _label("!" * row.item.priority, "taskPrio")
        self.prio.setToolTip(PRIORITY_LABELS[row.item.priority])
        self.prio.setVisible(row.item.priority > 0)
        lay.addWidget(self.prio, 0, Qt.AlignmentFlag.AlignTop)
        self.action = None
        if deleted:
            self.action = QPushButton("restore")
            self.action.clicked.connect(lambda: self.restore.emit(row.id))
        elif not compact:
            self.action = QPushButton("×")
            self.action.setToolTip("Delete (Del)")
            self.action.clicked.connect(lambda: self.delete.emit(row.id))
            self.action.setObjectName("rowAction")
        if self.action is not None:
            self.action.setCursor(Qt.CursorShape.PointingHandCursor)
            lay.addWidget(self.action, 0, Qt.AlignmentFlag.AlignTop)
            if not deleted:  # the delete button shows on hover / selection only
                keep = self.action.sizePolicy()
                keep.setRetainSizeWhenHidden(True)
                self.action.setSizePolicy(keep)
                self.action.hide()
        self._on = False

    def _sync_action(self, hover: bool = False) -> None:
        if self.action is not None and self.row.group != "deleted":
            self.action.setVisible(hover or self._on)

    def enterEvent(self, e) -> None:  # noqa: N802 - Qt API
        self._sync_action(True)
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:  # noqa: N802 - Qt API
        self._sync_action(False)
        super().leaveEvent(e)

    def set_selected(self, on: bool) -> None:
        self._on = on
        self.setProperty("selected", "true" if on else "false")
        repolish(self)
        self._sync_action()

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802 - Qt API
        if e.button() == Qt.MouseButton.LeftButton and self.row.group != "deleted":
            self.selected.emit(self.row.id)
        super().mouseReleaseEvent(e)


class _Section(QFrame):
    def __init__(self, key: str, label: str, rows: tuple[TaskRowView, ...]) -> None:
        super().__init__()
        self.key = key
        self.setObjectName("taskSection")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 10, 6, 6)
        lay.setSpacing(2)
        head = QHBoxLayout()
        head.setContentsMargins(8, 0, 8, 4)
        head.setSpacing(8)
        self.head = _label(label, "sectionHead")
        self.head.setProperty("group", key)
        head.addWidget(self.head)
        self.count = _label(str(len(rows)), "sectionCount")
        self.count.setProperty("group", key)
        head.addWidget(self.count)
        head.addStretch(1)
        lay.addLayout(head)
        self.rows_box = QVBoxLayout()
        self.rows_box.setSpacing(2)
        lay.addLayout(self.rows_box)


# ---------------------------------------------------------------- editor


class ItemEditor(QFrame):
    """Edit the selected item. Due / remind take dates, "YYYY-MM-DD HH:MM" or
    phrases such as "tomorrow 5pm" (worked out without a model)."""

    saved = Signal(int, dict)       # item id, changed fields for items.update_item
    closed = Signal()
    toggle = Signal(int)
    delete = Signal(int)

    def __init__(self, tokens: Tokens, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(EDITOR_W)
        self.item: it.Item | None = None
        self.projects: tuple[it.Project, ...] = ()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 14)
        lay.setSpacing(6)
        head = QHBoxLayout()
        self.head = _label("edit", "panelHead")
        head.addWidget(self.head)
        head.addStretch(1)
        close = QPushButton("×")
        close.setObjectName("ghost")
        close.setToolTip("Close")
        close.clicked.connect(self.closed)
        head.addWidget(close)
        lay.addLayout(head)

        def field(label: str, w: QWidget) -> QWidget:
            lay.addWidget(_label(label, "fieldLabel"))
            lay.addWidget(w)
            return w

        self.title = field("title", QLineEdit())
        self.title.setObjectName("field")
        row = QHBoxLayout()
        row.setSpacing(8)
        kcol, pcol = QVBoxLayout(), QVBoxLayout()
        kcol.addWidget(_label("type", "fieldLabel"))
        self.kind = QComboBox()
        self.kind.addItems(list(it.KINDS))
        kcol.addWidget(self.kind)
        pcol.addWidget(_label("priority", "fieldLabel"))
        self.priority = QComboBox()
        self.priority.addItems(list(PRIORITY_LABELS))
        pcol.addWidget(self.priority)
        row.addLayout(kcol, 1)
        row.addLayout(pcol, 1)
        lay.addLayout(row)
        self.due = field("due", QLineEdit())
        self.due.setObjectName("field")
        self.due.setPlaceholderText("e.g. Thursday, 2026-10-15 17:00")
        self.remind = field("remind me", QLineEdit())
        self.remind.setObjectName("field")
        self.remind.setPlaceholderText("e.g. tomorrow 5pm")
        self.project = field("project", QComboBox())
        self.body = field("notes", QPlainTextEdit())
        self.body.setObjectName("fieldBody")
        self.body.setFixedHeight(84)
        self.error = _label("", "fieldError", wrap=True)
        self.error.hide()
        lay.addWidget(self.error)
        buttons = QHBoxLayout()
        self.save_btn = QPushButton("save")
        self.save_btn.setObjectName("primary")
        self.save_btn.clicked.connect(self.save)
        buttons.addWidget(self.save_btn)
        self.done_btn = QPushButton("done")
        self.done_btn.clicked.connect(lambda: self.item and self.toggle.emit(self.item.id))
        buttons.addWidget(self.done_btn)
        buttons.addStretch(1)
        self.delete_btn = QPushButton("delete")
        self.delete_btn.setObjectName("ghost")
        self.delete_btn.clicked.connect(lambda: self.item and self.delete.emit(self.item.id))
        buttons.addWidget(self.delete_btn)
        lay.addLayout(buttons)
        self.stamp = _label("", "fieldLabel", wrap=True)
        lay.addWidget(self.stamp)
        lay.addStretch(1)
        for w in (self.save_btn, self.done_btn, self.delete_btn):
            w.setCursor(Qt.CursorShape.PointingHandCursor)
        for e in (self.title, self.due, self.remind):
            e.returnPressed.connect(self.save)
        self.now_fn: Callable[[], datetime] = datetime.now

    def edit(self, item: it.Item, projects: tuple[it.Project, ...], today) -> None:
        from jarvis.gui.tasks_data import when_label

        self.item, self.projects = item, projects
        self.title.setText(item.title)
        self.kind.setCurrentText(item.kind)
        self.priority.setCurrentIndex(item.priority)
        self.due.setText(_editable(item.due_at))
        self.due.setToolTip(when_label(item.due_at, today))
        self.remind.setText(_editable(item.remind_at))
        self.project.clear()
        self.project.addItem("no project", None)
        for p in projects:
            self.project.addItem(f"#{p.slug} · {p.name}", p.id)
        idx = self.project.findData(item.project_id) if item.project_id is not None else 0
        self.project.setCurrentIndex(max(0, idx))
        self.body.setPlainText(item.body or "")
        self.done_btn.setText("reopen" if item.done else "done")
        self.stamp.setText(f"added {item.created_at[:10]} · changed {item.updated_at[:10]}")
        self._error("")
        self.show()

    def changes(self) -> dict:
        """The fields to save, checked (raises ItemError with a plain message)."""
        now = self.now_fn()
        title = self.title.text()
        out = {
            "title": title, "kind": self.kind.currentText(),
            "priority": self.priority.currentIndex(),
            "due": parse_when_input(self.due.text(), now, need_time=False),
            "remind": parse_when_input(self.remind.text(), now, need_time=True),
            "project": self.project.currentData(),
            "body": self.body.toPlainText(),
        }
        if not title.strip():
            raise it.ItemError("title can't be empty", "title")
        return out

    def save(self) -> None:
        if self.item is None:
            return
        try:
            fields = self.changes()
        except it.ItemError as e:
            self._error(str(e))
            return
        self._error("")
        self.saved.emit(self.item.id, fields)

    def _error(self, text: str) -> None:
        self.error.setText(text)
        self.error.setVisible(bool(text))


def _editable(stamp: str | None) -> str:
    return (stamp or "").replace("T", " ")


# ---------------------------------------------------------------- workspace


class TasksView(QWidget):
    """The Tasks workspace. ``changed`` after every write (the window reloads);
    ``status(text, level)`` for the ticker; ``capture_requested`` for Ctrl+N."""

    changed = Signal()
    status = Signal(str, str)
    capture_requested = Signal()

    def __init__(self, conn: sqlite3.Connection, tokens: Tokens, *, tz: tzinfo | None = None,
                 now_fn: Callable[[], datetime] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.conn, self.tokens, self.tz = conn, tokens, tz
        self.now_fn = now_fn or datetime.now
        self.data: TasksData | None = None
        self.project_filter: str | None = None
        self.kind_filter = "all"
        self.selected_id: int | None = None
        self.rows: dict[int, TaskRow] = {}
        self.sections: dict[str, _Section] = {}
        self.last_deleted: int | None = None

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.content = QWidget()
        root.addWidget(self.content, 1)
        root.addStretch(0)
        outer = QVBoxLayout(self.content)
        outer.setContentsMargins(16, 14, 16, 12)
        outer.setSpacing(10)

        # header: title + summary, filters, capture
        head = QHBoxLayout()
        head.setSpacing(10)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        titles.addWidget(_label("Tasks", "wsTitle"))
        self.summary = _label("", "wsSub")
        titles.addWidget(self.summary)
        head.addLayout(titles)
        head.addStretch(1)
        self.kind_buttons: dict[str, QPushButton] = {}
        for k in KIND_FILTERS:
            b = QPushButton(k if k == "all" else f"{k}s")
            b.setObjectName("filterChip")
            b.setCheckable(True)
            b.setChecked(k == "all")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=k: self.set_kind(k))
            head.addWidget(b)
            self.kind_buttons[k] = b
        self.project_box = QComboBox()
        self.project_box.setMinimumWidth(130)
        self.project_box.setToolTip("Show one project's items")
        self.project_box.currentIndexChanged.connect(self._project_changed)
        head.addWidget(self.project_box)
        self.capture_btn = QPushButton("+ capture")
        self.capture_btn.setObjectName("primary")
        self.capture_btn.setToolTip("Quick capture (Ctrl+N)")
        self.capture_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.capture_btn.clicked.connect(self.capture_requested)
        head.addWidget(self.capture_btn)
        outer.addLayout(head)

        # undo strip after a delete
        self.undo_bar = QFrame()
        self.undo_bar.setObjectName("undoBar")
        ub = QHBoxLayout(self.undo_bar)
        ub.setContentsMargins(12, 6, 8, 6)
        self.undo_text = _label("", "undoText")
        ub.addWidget(self.undo_text, 1)
        self.undo_btn = QPushButton("undo")
        self.undo_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.undo_btn.clicked.connect(self.undo_delete)
        ub.addWidget(self.undo_btn)
        self.undo_bar.hide()
        outer.addWidget(self.undo_bar)
        self._undo_timer = QTimer(self, singleShot=True, interval=UNDO_MS)
        self._undo_timer.timeout.connect(self.undo_bar.hide)

        # list | editor
        body = QHBoxLayout()
        body.setSpacing(12)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        self.list = QVBoxLayout(holder)
        self.list.setContentsMargins(0, 0, 6, 0)
        self.list.setSpacing(14)
        self.scroll.setWidget(holder)
        body.addWidget(self.scroll, 1)
        self.editor = ItemEditor(tokens)
        self.editor.now_fn = lambda: self._now()
        self.editor.saved.connect(self.save_item)
        self.editor.closed.connect(lambda: self.select(None))
        self.editor.toggle.connect(self.toggle)
        self.editor.delete.connect(self.delete)
        # the editor scrolls on its own, so a short window never squeezes its fields
        self.editor_scroll = QScrollArea()
        self.editor_scroll.setWidgetResizable(True)
        self.editor_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.editor_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.editor_scroll.setFixedWidth(EDITOR_W + 10)
        self.editor_scroll.setWidget(self.editor)
        self.editor_scroll.hide()
        body.addWidget(self.editor_scroll, 0)
        outer.addLayout(body, 1)
        self._fit_width()

        self.empty = QFrame()
        self.empty.setObjectName("emptyState")
        el = QVBoxLayout(self.empty)
        el.setContentsMargins(18, 16, 18, 16)
        self.empty_title = _label("", "emptyTitleSm")
        el.addWidget(self.empty_title)
        self.empty_text = _label("", "emptyHint", wrap=True)
        el.addWidget(self.empty_text)
        self.empty_btn = QPushButton("+ capture  Ctrl+N")
        self.empty_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.empty_btn.clicked.connect(self.capture_requested)
        er = QHBoxLayout()
        er.addWidget(self.empty_btn)
        er.addStretch(1)
        el.addLayout(er)

        self.done_toggle = QPushButton()
        self.deleted_toggle = QPushButton()
        for b in (self.done_toggle, self.deleted_toggle):
            b.setObjectName("filter")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.toggled.connect(lambda _on: self._render())

        QShortcut(QKeySequence(Qt.Key.Key_Delete), self, activated=self._delete_selected,
                  context=Qt.ShortcutContext.WidgetWithChildrenShortcut)

    # ------------------------------------------------------------ data

    def _now(self) -> datetime:
        from jarvis.items import local_now

        return local_now(self.now_fn(), self.tz)

    def reload(self, force: bool = False) -> None:
        try:
            data = load_tasks(self.conn, self._now(), self.tz, project=self.project_filter,
                              kind=self.kind_filter)
        except (sqlite3.Error, it.ItemError) as e:
            self.status.emit(f"Could not read your items: {e}", "warn")
            return
        if data == self.data and not force:
            return
        self.data = data
        self._fill_projects(data.projects)
        self._render()

    def _fill_projects(self, projects) -> None:
        want = [None, *(p.slug for p in projects)]
        have = [self.project_box.itemData(i) for i in range(self.project_box.count())]
        if want == have:
            return
        self.project_box.blockSignals(True)
        self.project_box.clear()
        self.project_box.addItem("all projects", None)
        for p in projects:
            self.project_box.addItem(f"#{p.slug}", p.slug)
        idx = self.project_box.findData(self.project_filter)
        self.project_box.setCurrentIndex(max(0, idx))
        self.project_box.blockSignals(False)

    def _render(self) -> None:
        d = self.data
        if d is None:
            return
        scroll_y = self.scroll.verticalScrollBar().value()
        self.empty.setParent(None)
        self.done_toggle.setParent(None)
        self.deleted_toggle.setParent(None)
        _clear(self.list)
        self.rows, self.sections = {}, {}
        self.summary.setText(d.summary())
        if not d.open_count:
            filtered = self.project_filter is not None or self.kind_filter != "all"
            title, text = EMPTY_FILTERED if filtered else EMPTY_ALL
            self.empty_title.setText(title)
            self.empty_text.setText(text)
            self.list.addWidget(self.empty)
        for key, rows in d.groups:
            if rows:
                self._add_section(key, GROUP_LABELS[key], rows)
        footer = QHBoxLayout()
        footer.setSpacing(4)
        self.done_toggle.setText(f"{'▾' if self.done_toggle.isChecked() else '▸'} done · "
                                 f"{len(d.done)}")
        self.deleted_toggle.setText(f"{'▾' if self.deleted_toggle.isChecked() else '▸'} "
                                    f"deleted · {len(d.deleted)}")
        self.done_toggle.setVisible(bool(d.done))
        self.deleted_toggle.setVisible(bool(d.deleted))
        footer.addWidget(self.done_toggle)
        footer.addWidget(self.deleted_toggle)
        footer.addStretch(1)
        self.list.addLayout(footer)
        if self.done_toggle.isChecked() and d.done:
            self._add_section("done", "done recently", d.done)
        if self.deleted_toggle.isChecked() and d.deleted:
            self._add_section("deleted", "deleted", d.deleted)
        self.list.addStretch(1)
        # keep the selection and the editor in step with the data
        if self.selected_id is not None and self.selected_id not in self.rows:
            self.select(None)
        elif self.selected_id is not None:
            self.rows[self.selected_id].set_selected(True)
        QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(scroll_y))

    def _add_section(self, key: str, label: str, rows) -> None:
        sec = _Section(key, label, rows)
        for r in rows:
            w = TaskRow(r, self.tokens)
            w.selected.connect(self.select)
            w.toggled.connect(self.toggle)
            w.delete.connect(self.delete)
            w.restore.connect(self.restore)
            sec.rows_box.addWidget(w)
            self.rows[r.id] = w
        self.sections[key] = sec
        self.list.addWidget(sec)

    # ------------------------------------------------------------ filters

    def set_kind(self, kind: str) -> None:
        self.kind_filter = kind
        for k, b in self.kind_buttons.items():
            b.setChecked(k == kind)
        self.reload(force=True)

    def set_project(self, slug: str | None) -> None:
        self.project_filter = slug
        idx = self.project_box.findData(slug)
        self.project_box.blockSignals(True)
        self.project_box.setCurrentIndex(max(0, idx))
        self.project_box.blockSignals(False)
        self.reload(force=True)

    def _project_changed(self, _i: int) -> None:
        self.project_filter = self.project_box.currentData()
        self.reload(force=True)

    # ------------------------------------------------------------ actions

    def select(self, item_id: int | None) -> None:
        if self.selected_id in self.rows:
            self.rows[self.selected_id].set_selected(False)
        self.selected_id = item_id
        if item_id is None or item_id not in self.rows:
            self.selected_id = None
            self.editor_scroll.hide()
            self.editor.item = None
            self._fit_width()
            return
        self.rows[item_id].set_selected(True)
        self.editor.edit(self.rows[item_id].row.item, self.data.projects, self.data.today)
        self.editor_scroll.show()
        self._fit_width()

    def _fit_width(self) -> None:
        extra = EDITOR_W + 22 if not self.editor_scroll.isHidden() else 0
        self.content.setMaximumWidth(LIST_MAX + 32 + extra)

    def _write(self, fn: Callable[[], it.Item], message: Callable[[it.Item], str]) -> bool:
        try:
            item = fn()
        except it.ItemError as e:
            self.status.emit(f"Not changed: {e}", "warn")
            return False
        self.status.emit(message(item), "ok")
        self.changed.emit()
        self.reload()
        return True

    def toggle(self, item_id: int) -> None:
        item = self._find(item_id)
        if item is None:
            return
        if item.done:
            self._write(lambda: it.reopen_item(self.conn, item_id), lambda i: f"Reopened “{i.title}”")
        else:
            self._write(lambda: it.complete_item(self.conn, item_id), lambda i: f"Done: “{i.title}”")
        if self.editor.item is not None and self.editor.item.id == item_id:
            fresh = self._find(item_id)
            if fresh is not None and item_id in self.rows:
                self.editor.edit(fresh, self.data.projects, self.data.today)

    def delete(self, item_id: int) -> None:
        if self._write(lambda: it.delete_item(self.conn, item_id),
                       lambda i: f"Deleted “{i.title}”"):
            item = it.get_item(self.conn, item_id, include_deleted=True)
            self.last_deleted = item_id
            self.undo_text.setText(f"Deleted “{item.title}”")
            self.undo_bar.show()
            self._undo_timer.start()
            if self.selected_id == item_id:
                self.select(None)

    def undo_delete(self) -> None:
        if self.last_deleted is not None:
            self.restore(self.last_deleted)

    def restore(self, item_id: int) -> None:
        restored = self._write(lambda: it.restore_item(self.conn, item_id),
                               lambda i: f"Restored “{i.title}”")
        if restored and self.last_deleted == item_id:
            self.last_deleted = None
            self.undo_bar.hide()

    def save_item(self, item_id: int, fields: dict) -> None:
        try:
            item = it.update_item(self.conn, item_id, **fields)
        except it.ItemError as e:
            self.editor._error(str(e))
            return
        self.status.emit(f"Saved “{item.title}”", "ok")
        self.changed.emit()
        self.reload()
        if item_id in self.rows:
            self.select(item_id)

    def _delete_selected(self) -> None:
        if self.selected_id is not None and not self.editor.isAncestorOf(self.focusWidget()):
            self.delete(self.selected_id)

    def _find(self, item_id: int) -> it.Item | None:
        try:
            return it.get_item(self.conn, item_id, include_deleted=True)
        except it.ItemError:
            return None


__all__ = ["EDITOR_W", "CheckCircle", "ItemEditor", "TaskRow", "TasksView", "meta_html"]
