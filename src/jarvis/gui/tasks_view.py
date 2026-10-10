"""The Tasks workspace: a board of the user's items, Linear-style, in Home's look.

Columns by when (Overdue, Today, This week, Later, Someday, and Done / Deleted
on request), compact cards with chips, tabs for the kind, a project filter and
a search. Drag a card to another column to reschedule it; a dialog confirms
first, and the change can be undone. C (or "+ New task") opens the create
window. Click or Enter opens the editor beside the board.

Keyboard, with the board focused: arrows move between cards, Space ticks the
card off (or reopens it), Enter edits it, Del deletes it (Undo brings it back),
Shift+Left / Right moves it to the neighbouring column (confirmed). Ctrl+F
searches; Esc clears the search or closes the editor.

All writes go through jarvis.items on the window's connection.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, tzinfo

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
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
from jarvis.gui import home_cards as hc
from jarvis.gui.chat_panel import FlowLayout
from jarvis.gui.home_style import PALETTE, tasks_stylesheet
from jarvis.gui.tasks_board import (
    BoardColumn,
    CreateDialog,
    MoveDialog,
    TaskCard,
)
from jarvis.gui.tasks_data import (
    COLUMNS,
    KIND_FILTERS,
    TasksData,
    drop_target,
    load_tasks,
    matches,
    parse_when_input,
    when_label,
)
from jarvis.gui.theme import Tokens
from jarvis.gui.widgets import _label

EDITOR_W = 320
# Tasks' own width (smaller with the chat open beside it): below FILTERS_BELOW the
# search moves under the tabs; below EDITOR_ALONE_BELOW an open editor takes the
# board's place rather than squeezing it.
FILTERS_BELOW = 700
EDITOR_ALONE_BELOW = 660
UNDO_MS = 12_000
PRIORITY_LABELS = ("none", "! low", "!! high", "!!! urgent")
EMPTY_ALL = ("Nothing open.", "Press C to add a task: a title, and a date at the end if it "
             "has one (“lab report friday 5pm”).")
EMPTY_FILTERED = ("Nothing matches these filters.",
                  "Pick “All” or another project to see the rest.")
COLUMN_EMPTY = {"overdue": "Nothing late.", "today": "Nothing due today.",
                "week": "Nothing this week.", "later": "Nothing later.",
                "someday": "No undated items.", "done": "Nothing done this week.",
                "deleted": "Nothing deleted."}


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


def _typing(w) -> bool:
    return isinstance(w, (QLineEdit, QPlainTextEdit, QComboBox))


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
        self.pal = PALETTE
        self.now_fn = now_fn or datetime.now
        self.data: TasksData | None = None
        self.project_filter: str | None = None
        self.kind_filter = "all"
        self.query = ""
        self.selected_id: int | None = None
        self.rows: dict[int, TaskCard] = {}
        self.sections: dict[str, BoardColumn] = {}
        self.columns: dict[str, BoardColumn] = {}
        self.last_deleted: int | None = None
        self._undo: Callable[[], None] | None = None
        self.move_dialog: MoveDialog | None = None
        self.create_dialog: CreateDialog | None = None
        self.draft_store = None
        self.setObjectName("tasksPage")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(tasks_stylesheet(self.pal))

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.content = QWidget()
        self.content.setObjectName("tasksPage")      # the window renames this view itself
        self.content.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        root.addWidget(self.content, 1)
        outer = QVBoxLayout(self.content)
        outer.setContentsMargins(20, 16, 20, 14)
        outer.setSpacing(12)

        # top bar: title + summary, search, new task
        top = QHBoxLayout()
        top.setSpacing(10)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        title = hc.label("Tasks", "", "big")
        f = title.font()
        f.setPixelSize(22)
        f.setWeight(f.Weight.DemiBold)
        title.setFont(f)
        titles.addWidget(title)
        self.summary = hc.label("", "hMuted", "label")
        titles.addWidget(self.summary)
        top.addLayout(titles)
        top.addStretch(1)
        self.search = QLineEdit()
        self.search.setObjectName("tkSearch")
        self.search.setPlaceholderText("Search  (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._search_changed)
        top.addWidget(self.search)
        self.new_btn = QPushButton("+ New task   C")
        self.new_btn.setObjectName("tkPrimary")
        self.new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_btn.setToolTip("New task (C). Quick capture in plain words: Ctrl+N")
        self.new_btn.clicked.connect(lambda: self.open_create())
        top.addWidget(self.new_btn)
        self.capture_btn = self.new_btn
        self._top = top
        outer.addLayout(top)

        # filters: kind tabs, project, done / deleted
        self.filters = QWidget()
        fl = FlowLayout(self.filters, 6)          # wraps when narrow (beside the chat)
        self.kind_buttons: dict[str, QPushButton] = {}
        for k in KIND_FILTERS:
            b = QPushButton("All" if k == "all" else f"{k.title()}s")
            b.setObjectName("tkTab")
            b.setCheckable(True)
            b.setChecked(k == "all")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=k: self.set_kind(k))
            fl.addWidget(b)
            self.kind_buttons[k] = b
        self.project_box = QComboBox()
        self.project_box.setObjectName("tkProject")
        self.project_box.setMinimumWidth(140)
        self.project_box.setToolTip("Show one project's items")
        self.project_box.currentIndexChanged.connect(self._project_changed)
        fl.addWidget(self.project_box)
        self.done_toggle = QPushButton("Show done")
        self.deleted_toggle = QPushButton("Show deleted")
        for b in (self.done_toggle, self.deleted_toggle):
            b.setObjectName("tkToggle")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.toggled.connect(lambda _on: self._render())
            fl.addWidget(b)
        self.filters_below = False
        self._search_row = QHBoxLayout()
        self._search_row.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.filters)
        outer.addLayout(self._search_row)

        # undo strip after a delete or a move
        self.undo_bar = QFrame()
        self.undo_bar.setObjectName("tkUndo")
        self.undo_bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        ub = QHBoxLayout(self.undo_bar)
        ub.setContentsMargins(14, 7, 10, 7)
        self.undo_text = hc.label("", "", "label")
        ub.addWidget(self.undo_text, 1)
        self.undo_btn = hc.button("Undo")
        self.undo_btn.clicked.connect(self.undo)
        ub.addWidget(self.undo_btn)
        self.undo_bar.hide()
        outer.addWidget(self.undo_bar)
        self._undo_timer = QTimer(self, singleShot=True, interval=UNDO_MS)
        self._undo_timer.timeout.connect(self.undo_bar.hide)

        # empty state (nothing open at all, or nothing for the filters)
        self.empty = QFrame()
        self.empty.setObjectName("hCard")
        self.empty.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        el = QHBoxLayout(self.empty)
        el.setContentsMargins(16, 12, 16, 12)
        ecol = QVBoxLayout()
        self.empty_title = hc.label("", "", "body")
        ecol.addWidget(self.empty_title)
        self.empty_text = hc.label("", "hMuted", "meta", wrap=True)
        ecol.addWidget(self.empty_text)
        el.addLayout(ecol, 1)
        self.empty_btn = hc.button("+ New task   C")
        self.empty_btn.clicked.connect(lambda: self.open_create())
        el.addWidget(self.empty_btn)
        self.empty.hide()
        outer.addWidget(self.empty)

        # the board | the editor
        body = QHBoxLayout()
        body.setSpacing(14)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("tkBoard")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; }")
        self.scroll.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.scroll.installEventFilter(self)
        holder = QWidget()
        holder.setObjectName("tasksPage")
        self.board = QHBoxLayout(holder)
        self.board.setContentsMargins(0, 0, 0, 4)
        self.board.setSpacing(12)
        for key in (*COLUMNS, "done", "deleted"):
            col = BoardColumn(key, self.pal)
            col.dropped.connect(self.request_move)
            col.add.connect(self._add_in)
            self.board.addWidget(col, 1)
            self.columns[key] = col
        self.scroll.setWidget(holder)
        body.addWidget(self.scroll, 1)
        self.editor = ItemEditor(tokens)
        self.editor.now_fn = lambda: self._now()
        self.editor.saved.connect(self.save_item)
        self.editor.closed.connect(lambda: self.select(None))
        self.editor.toggle.connect(self.toggle)
        self.editor.delete.connect(self.delete)
        self.editor_scroll = QScrollArea()
        self.editor_scroll.setWidgetResizable(True)
        self.editor_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.editor_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.editor_scroll.setStyleSheet("QScrollArea { background: transparent; }")
        self.editor_scroll.setFixedWidth(EDITOR_W + 10)
        self.editor_scroll.setWidget(self.editor)
        self.editor_scroll.hide()
        body.addWidget(self.editor_scroll, 0)
        outer.addLayout(body, 1)
        self.editor_alone = False

        QShortcut(QKeySequence(Qt.Key.Key_Delete), self, activated=self._delete_selected,
                  context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self.focus_search,
                  context=Qt.ShortcutContext.WidgetWithChildrenShortcut)

    # ------------------------------------------------------------ data

    def _now(self) -> datetime:
        return it.local_now(self.now_fn(), self.tz)

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
        self.project_box.addItem("All projects", None)
        for p in projects:
            self.project_box.addItem(f"#{p.slug}", p.slug)
        idx = self.project_box.findData(self.project_filter)
        self.project_box.setCurrentIndex(max(0, idx))
        self.project_box.blockSignals(False)

    def _render(self) -> None:
        d = self.data
        if d is None:
            return
        self.rows, self.sections = {}, {}
        cols = dict(d.columns())
        cols["done"] = d.done_recent()
        cols["deleted"] = d.deleted
        shown = {"done": self.done_toggle.isChecked(), "deleted": self.deleted_toggle.isChecked()}
        overdue = len(cols["overdue"])
        self.summary.setText(f"{d.open_count} open" + (f" · {overdue} overdue" if overdue
                                                        else ""))
        for key, col in self.columns.items():
            visible = shown.get(key, True)
            col.setVisible(visible)
            if not visible:
                col.set_cards([])
                continue
            rows = [r for r in cols[key] if not self.query or matches(r, self.query)]
            cards = []
            for r in rows:
                c = TaskCard(r, self.pal)
                c.selected.connect(self._clicked)
                c.toggled.connect(self.toggle)
                c.delete.connect(self.delete)
                c.restore.connect(self.restore)
                cards.append(c)
                self.rows[r.id] = c
            col.set_cards(cards, "No matches." if self.query else COLUMN_EMPTY[key])
            self.sections[key] = col
        filtered = self.project_filter is not None or self.kind_filter != "all"
        if not d.open_count:
            title, text = EMPTY_FILTERED if filtered else EMPTY_ALL
            self.empty_title.setText(title)
            self.empty_text.setText(text)
        self.empty.setVisible(not d.open_count)
        if self.selected_id is not None and self.selected_id not in self.rows:
            self.select(None)
        elif self.selected_id is not None:
            self.rows[self.selected_id].set_selected(True)

    def cards_in(self, key: str) -> list[str]:
        """The titles shown in a column (tests, and a quick look)."""
        col = self.sections.get(key)
        return [] if col is None else [c.row.title for c in col.cards]

    # ------------------------------------------------------------ filters, search

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

    def _search_changed(self, text: str) -> None:
        self.query = text.strip()
        self._render()

    def focus_search(self) -> None:
        self.search.setFocus()
        self.search.selectAll()

    def escape(self) -> bool:
        """Esc (from the window): clear the search, else close the editor. True when
        it did something (the window then stops there)."""
        if self.search.text():
            self.search.clear()
            self.scroll.setFocus()
            return True
        if not self.editor_scroll.isHidden():
            self.select(None)
            self.scroll.setFocus()
            return True
        return False

    # ------------------------------------------------------------ selection, keys

    def _clicked(self, item_id: int) -> None:
        self.select(item_id)

    def highlight(self, item_id: int | None) -> None:
        """Move the selection (the keyboard); the editor follows if it is open."""
        if self.editor_scroll.isHidden():
            if self.selected_id in self.rows:
                self.rows[self.selected_id].set_selected(False)
            self.selected_id = item_id if item_id in self.rows else None
            if self.selected_id is not None:
                card = self.rows[self.selected_id]
                card.set_selected(True)
                self._reveal(card)
        else:
            self.select(item_id)

    def select(self, item_id: int | None) -> None:
        """Select a card and open it in the editor (None closes the editor)."""
        if self.selected_id in self.rows:
            self.rows[self.selected_id].set_selected(False)
        self.selected_id = item_id
        if item_id is None or item_id not in self.rows:
            self.selected_id = None
            self.editor_scroll.hide()
            self.editor.item = None
            self._reflow()
            return
        card = self.rows[item_id]
        card.set_selected(True)
        self._reveal(card)
        self.editor.edit(card.row.item, self.data.projects, self.data.today)
        self.editor_scroll.show()
        self._reflow()

    def _reveal(self, card: TaskCard) -> None:
        col = next((c for c in self.sections.values() if card in c.cards), None)
        if col is not None:
            self.scroll.ensureWidgetVisible(col, 0, 0)
            col.scroll.ensureWidgetVisible(card, 0, 12)

    def _grid(self) -> list[tuple[str, list[int]]]:
        return [(k, [c.row.id for c in col.cards]) for k, col in self.sections.items()
                if col.isVisible() and col.cards]

    def _position(self) -> tuple[int, int] | None:
        for ci, (_, ids) in enumerate(self._grid()):
            if self.selected_id in ids:
                return ci, ids.index(self.selected_id)
        return None

    def step(self, dx: int, dy: int) -> None:
        """Arrows: move the selection to the next card (dy) or column (dx)."""
        grid = self._grid()
        if not grid:
            return
        pos = self._position()
        if pos is None:
            self.highlight(grid[0][1][0])
            return
        ci, ri = pos
        if dx:
            ci = max(0, min(len(grid) - 1, ci + dx))
            ri = min(ri, len(grid[ci][1]) - 1)
        else:
            ri = max(0, min(len(grid[ci][1]) - 1, ri + dy))
        self.highlight(grid[ci][1][ri])

    def column_of(self, item_id: int) -> str | None:
        return next((k for k, col in self.sections.items()
                     if any(c.row.id == item_id for c in col.cards)), None)

    def eventFilter(self, obj, e) -> bool:  # noqa: N802 - Qt API
        if obj is self.scroll and e.type() == QEvent.Type.KeyPress:
            return self._board_key(e)
        return super().eventFilter(obj, e)

    def _board_key(self, e) -> bool:
        key, mods = e.key(), e.modifiers()
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        arrows = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0),
                  Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1)}
        if key in arrows:
            dx, dy = arrows[key]
            if shift and dx and self.selected_id is not None:
                self.move_next(self.selected_id, dx)
            else:
                self.step(dx, dy)
            return True
        if key == Qt.Key.Key_Space and self.selected_id is not None:
            done_id = self.selected_id
            column = self.column_of(done_id)
            index = next((i for i, c in enumerate(self.sections[column].cards)
                          if c.row.id == done_id), 0) if column else 0
            self.toggle(done_id)
            if done_id not in self.rows and column in self.sections:   # it left its column
                ids = [c.row.id for c in self.sections[column].cards]
                if ids:
                    self.highlight(ids[min(index, len(ids) - 1)])
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.selected_id is not None:
            self.select(self.selected_id)
            self.editor.title.setFocus()
            return True
        return False

    def keyPressEvent(self, e) -> None:  # noqa: N802 - Qt API
        if not self._board_key(e):
            super().keyPressEvent(e)

    # ------------------------------------------------------------ layout

    def _reflow(self) -> None:
        """Fit a narrow Tasks (a small window, or the chat open beside it)."""
        w = self.width()
        below = w < FILTERS_BELOW
        if below != self.filters_below:
            self.filters_below = below
            self._top.removeWidget(self.search)
            self._search_row.removeWidget(self.search)
            if below:
                self._search_row.addWidget(self.search, 1)
            else:
                self._top.insertWidget(self._top.indexOf(self.new_btn), self.search)
        self.editor_alone = w < EDITOR_ALONE_BELOW and not self.editor_scroll.isHidden()
        self.scroll.setVisible(not self.editor_alone)

    def resizeEvent(self, e) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(e)
        self._reflow()

    # ------------------------------------------------------------ writes

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

    def _offer_undo(self, text: str, fn: Callable[[], None]) -> None:
        self._undo = fn
        self.undo_text.setText(text)
        self.undo_bar.show()
        self._undo_timer.start()

    def undo(self) -> None:
        fn, self._undo = self._undo, None
        self.undo_bar.hide()
        if fn is not None:
            fn()

    undo_delete = undo        # kept: the old name

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
            self._offer_undo(f"Deleted “{item.title}”", lambda: self.restore(item_id))
            if self.selected_id == item_id:
                self.select(None)

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
        if self.selected_id is not None and not self.editor.isAncestorOf(self.focusWidget()) \
                and not _typing(self.focusWidget()):
            self.delete(self.selected_id)

    def _find(self, item_id: int) -> it.Item | None:
        try:
            return it.get_item(self.conn, item_id, include_deleted=True)
        except it.ItemError:
            return None

    # ------------------------------------------------------------ moving

    def move_next(self, item_id: int, dx: int) -> None:
        """Shift+Left / Right: ask to move the card to the neighbouring column."""
        here = self.column_of(item_id)
        if here is None or here not in COLUMNS:
            return
        k = COLUMNS.index(here) + dx
        if 0 < k < len(COLUMNS):              # never into Overdue
            self.request_move(item_id, COLUMNS[k])

    def request_move(self, item_id: int, column: str) -> None:
        """A card dropped on ``column``: confirm first (``move_dialog``)."""
        item = self._find(item_id)
        if item is None or item.deleted or self.column_of(item_id) == column:
            return
        if column == "overdue" or column == "someday" and item.remind_at and not item.due_at:
            self.status.emit("Not moved: " + ("nothing is moved into Overdue"
                                              if column == "overdue" else
                             "a reminder needs a date and time; edit it instead"), "warn")
            return
        dlg = MoveDialog(item, column, self._now().date(), self._now(), self.pal, self)
        dlg.setStyleSheet(tasks_stylesheet(self.pal))
        dlg.accepted.connect(lambda: self.move(item_id, column, dlg.day))
        self.move_dialog = dlg
        dlg.open()

    def move(self, item_id: int, column: str, day=None) -> bool:
        """Move a card (after the user confirmed): its date, or Done."""
        item = self._find(item_id)
        if item is None:
            return False
        if column == "done":
            ok = self._write(lambda: it.complete_item(self.conn, item_id),
                             lambda i: f"Done: “{i.title}”")
            if ok:
                self._offer_undo(f"Done: “{item.title}”", lambda: self._write(
                    lambda: it.reopen_item(self.conn, item_id),
                    lambda i: f"Reopened “{i.title}”") and None)
            return ok
        try:
            field, value = drop_target(item, column, self._now().date(), day)
        except ValueError as e:
            self.status.emit(f"Not moved: {e}", "warn")
            return False
        before = item.due_at if field == "due" else item.remind_at
        where = when_label(value, self._now().date()) if value else "Someday (no date)"
        ok = self._write(lambda: it.update_item(self.conn, item_id, **{field: value}),
                         lambda i: f"Moved: “{i.title}” → {where}")
        if ok:
            self._offer_undo(f"Moved “{item.title}” to {where}", lambda: self._write(
                lambda: it.update_item(self.conn, item_id, **{field: before}),
                lambda i: f"Moved back: “{i.title}”") and None)
        return ok

    # ------------------------------------------------------------ creating

    def _add_in(self, column: str) -> None:
        today = self._now().date()
        from datetime import timedelta
        preset = {"today": today.isoformat(),
                  "week": (today + timedelta(days=1)).isoformat()}.get(column)
        self.open_create(preset, column)

    def open_create(self, preset_due: str | None = None, column: str | None = None) -> None:
        """The create window; the new card is selected after Ctrl+Enter."""
        if self.data is None:
            self.reload()
        store = self.draft_store
        if store is None:
            from jarvis.gui.voice_turn import ui_settings
            store = self.draft_store = ui_settings()
        dlg = CreateDialog(self.conn, self._now, self.data.projects if self.data else (),
                           preset_due=preset_due, draft_store=store, pal=self.pal,
                           parent=self)
        dlg.setStyleSheet(tasks_stylesheet(self.pal))
        if column == "later":
            dlg.open_chip("due")
        dlg.created.connect(self._created)
        self.create_dialog = dlg
        dlg.open()

    def _created(self, item_id: int) -> None:
        item = self._find(item_id)
        self.status.emit(f"Created “{item.title}”" if item else "Created", "ok")
        self.changed.emit()
        self.reload(force=True)
        self.highlight(item_id)


__all__ = ["EDITOR_W", "ItemEditor", "TasksView"]
