"""The Tasks board's pieces: columns, cards, the move dialog and the create window.

- ``BoardColumn``: a column (Overdue, Today, This week, Later, Someday, Done)
  with its dot, name, count badge and "+", and its cards. A drop target: a card
  dropped on it asks to move there (``dropped``); nothing moves without a yes.
- ``TaskCard``: one item, Linear-style: a check circle, the title, and a row of
  chips (date, category, project, priority, reminder, notes). Drag it to another
  column to reschedule.
- ``MoveDialog``: "Move *Lab report* to …?" with a day picker for This week, a
  date field (words like "next monday" work) for Later, a plain question for
  Today / Someday / Done. Accepting returns the choice; the view writes.
- ``CreateDialog``: the create window (C). A title (a date phrase at its end
  fills the Due chip), notes, and chips for kind, priority, project, due and
  reminder; Ctrl+Enter creates through jarvis.items, Esc keeps a draft.

Colours come from home_style's palette; nothing here reads the database except
CreateDialog's single create_item on Ctrl+Enter.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timedelta

from PySide6.QtCore import QEvent, QMimeData, QPoint, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QDrag, QKeySequence, QPainter, QPainterPath, QPen, QShortcut
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from jarvis import items as it
from jarvis.gui import home_cards as hc
from jarvis.gui.chat_panel import FlowLayout
from jarvis.gui.home_style import PALETTE, HomePalette
from jarvis.gui.tasks_data import (
    COLUMN_NAMES,
    WEEK_DAYS,
    TaskRowView,
    category,
    day_label,
    parse_title_date,
    parse_when_input,
    when_label,
)
from jarvis.gui.theme import typeset

MIME = "application/x-jarvis-item"
COLUMN_MIN, COLUMN_MAX = 220, 320
PRIORITY_NAMES = ("No priority", "Low", "High", "Urgent")
COLUMN_TONE = {"overdue": "red", "today": "amber", "week": "blue", "later": "grey",
               "someday": "grey", "done": "green"}
DROP_TARGETS = ("today", "week", "later", "someday", "done")


# ---------------------------------------------------------------- the card


class Check(QAbstractButton):
    """The round done box: a ring (red when late), a green disc with a tick when done."""

    def __init__(self, done: bool, late: bool, pal: HomePalette = PALETTE,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.pal, self.late = pal, late
        self.setCheckable(True)
        self.setChecked(done)
        self.setFixedSize(18, 18)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Reopen (Space)" if done else "Mark done (Space)")
        self._hover = False

    def enterEvent(self, e) -> None:  # noqa: N802 - Qt API
        self._hover = True
        self.update()

    def leaveEvent(self, e) -> None:  # noqa: N802 - Qt API
        self._hover = False
        self.update()

    def paintEvent(self, e) -> None:  # noqa: N802 - Qt API
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(2, 2, 14, 14)
        pal = self.pal
        if self.isChecked():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(pal.green))
            p.drawEllipse(r)
            path = QPainterPath()
            path.moveTo(5.6, 9.2)
            path.lineTo(8.0, 11.6)
            path.lineTo(12.6, 6.8)
            p.setPen(QPen(QColor(pal.bg), 1.7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                          Qt.PenJoinStyle.RoundJoin))
            p.drawPath(path)
        else:
            line = pal.green if self._hover else pal.red if self.late else pal.faint
            p.setPen(QPen(QColor(line), 1.4))
            p.setBrush(QColor(pal.wash(pal.green, 0.2)) if self._hover else Qt.BrushStyle.NoBrush)
            p.drawEllipse(r)
        p.end()


def chip(text: str, color: str = "", pal: HomePalette = PALETTE, tip: str = "") -> hc.QLabel:
    """A small property chip on a card (quiet unless it has a colour)."""
    lb = hc.label(text, "tkChip", "pill")
    if color:
        lb.setStyleSheet(f"color: {color}; border-color: {pal.wash(color, 0.45)};")
    if tip:
        lb.setToolTip(tip)
    lb.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    return lb


class TaskCard(QFrame):
    """One item on the board. Click: select (the editor opens); drag: move."""

    selected = Signal(int)
    toggled = Signal(int)
    delete = Signal(int)
    restore = Signal(int)

    def __init__(self, row: TaskRowView, pal: HomePalette = PALETTE,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.row = row
        self.pal = pal
        self.setObjectName("tkCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setProperty("selected", "false")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._press: QPoint | None = None
        item = row.item
        deleted = row.group == "deleted"
        col = QVBoxLayout(self)
        col.setContentsMargins(12, 10, 10, 10)
        col.setSpacing(7)
        top = QHBoxLayout()
        top.setSpacing(9)
        self.check = Check(item.done, bool(row.late), pal)
        self.check.clicked.connect(lambda: self.toggled.emit(row.id))
        self.check.setVisible(not deleted)
        top.addWidget(self.check, 0, Qt.AlignmentFlag.AlignTop)
        self.title = hc.label(item.title, "tkTitle", "body", wrap=True)
        self.title.setProperty("done", "true" if (item.done or deleted) else "false")
        top.addWidget(self.title, 1)
        self.action = QPushButton("restore" if deleted else "×")
        self.action.setObjectName("tkAction")
        self.action.setCursor(Qt.CursorShape.PointingHandCursor)
        self.action.setToolTip("Restore" if deleted else "Delete (Del)")
        self.action.clicked.connect(lambda: (self.restore if deleted else self.delete)
                                    .emit(row.id))
        keep = self.action.sizePolicy()
        keep.setRetainSizeWhenHidden(True)
        self.action.setSizePolicy(keep)
        self.action.setVisible(deleted)
        top.addWidget(self.action, 0, Qt.AlignmentFlag.AlignTop)
        col.addLayout(top)
        chip_box = QWidget()
        chips = FlowLayout(chip_box, 5)            # wraps in a narrow column
        chip_box.setContentsMargins(27, 0, 0, 0)
        self.chips: list = []
        if row.when:
            c = chip(row.when.removeprefix("remind "), pal.red if row.late else "", pal,
                     row.late or "")
            chips.addWidget(c)
            self.chips.append(c)
        cat = category(row)
        c = chip(cat, pal.category(cat), pal)
        chips.addWidget(c)
        self.chips.append(c)
        if row.project:
            c = chip(f"#{row.project}", pal.purple, pal, row.project_name or "")
            chips.addWidget(c)
            self.chips.append(c)
        if item.priority:
            c = chip("!" * item.priority, pal.amber if item.priority < 3 else pal.red, pal,
                     PRIORITY_NAMES[item.priority])
            chips.addWidget(c)
            self.chips.append(c)
        if item.remind_at:
            c = chip("⏰", "", pal, row.remind or row.when)
            chips.addWidget(c)
            self.chips.append(c)
        if item.body:
            c = chip("≡", "", pal, item.body.splitlines()[0][:120])
            chips.addWidget(c)
            self.chips.append(c)
        col.addWidget(chip_box)
        self._on = False

    def set_selected(self, on: bool) -> None:
        self._on = on
        self.setProperty("selected", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        if self.row.group != "deleted":
            self.action.setVisible(on)

    def enterEvent(self, e) -> None:  # noqa: N802 - Qt API
        if self.row.group != "deleted":
            self.action.setVisible(True)
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:  # noqa: N802 - Qt API
        if self.row.group != "deleted":
            self.action.setVisible(self._on)
        super().leaveEvent(e)

    def mousePressEvent(self, e) -> None:  # noqa: N802 - Qt API
        if e.button() == Qt.MouseButton.LeftButton:
            self._press = e.position().toPoint()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e) -> None:  # noqa: N802 - Qt API
        if (self._press is not None and self.row.group not in ("deleted", "done")
                and (e.position().toPoint() - self._press).manhattanLength()
                >= QApplication.startDragDistance()):
            self._press = None
            drag = QDrag(self)
            mime = QMimeData()
            mime.setData(MIME, str(self.row.id).encode())
            drag.setMimeData(mime)
            drag.setPixmap(self.grab())
            drag.setHotSpot(e.position().toPoint())
            drag.exec(Qt.DropAction.MoveAction)
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802 - Qt API
        if (e.button() == Qt.MouseButton.LeftButton and self._press is not None
                and self.row.group != "deleted"):
            self.selected.emit(self.row.id)
        self._press = None
        super().mouseReleaseEvent(e)


# ---------------------------------------------------------------- the column


class BoardColumn(QFrame):
    """A column: its head (dot, name, count, "+") and its cards; a drop target."""

    dropped = Signal(int, str)       # item id, this column's key
    add = Signal(str)                # "+": create with this column's date

    def __init__(self, key: str, pal: HomePalette = PALETTE, parent: QWidget | None = None):
        super().__init__(parent)
        self.key = key
        self.pal = pal
        self.setObjectName("tkColumn")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setProperty("drop", "false")
        self.setMinimumWidth(COLUMN_MIN)
        self.setMaximumWidth(COLUMN_MAX)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAcceptDrops(key in DROP_TARGETS)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 12, 10, 10)
        lay.setSpacing(8)
        head_box = QWidget()
        head_box.setFixedHeight(26)                # the same with or without "+"
        head = QHBoxLayout(head_box)
        head.setContentsMargins(4, 0, 2, 0)
        head.setSpacing(8)
        head.addWidget(hc.Dot(pal.tone(COLUMN_TONE.get(key, "grey")), 8))
        self.head = hc.label(COLUMN_NAMES.get(key, key.title()), "hTitle", "head")
        head.addWidget(self.head)
        self.count = hc.badge("0")
        head.addWidget(self.count)
        head.addStretch(1)
        self.plus = None
        if key in ("today", "week", "later", "someday"):
            self.plus = QPushButton("+")
            self.plus.setObjectName("tkPlus")
            self.plus.setFixedSize(22, 22)
            self.plus.setCursor(Qt.CursorShape.PointingHandCursor)
            self.plus.setToolTip(f"New task in {self.head.text()}")
            self.plus.clicked.connect(lambda: self.add.emit(key))
            head.addWidget(self.plus)
        lay.addWidget(head_box)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; }")
        holder = QWidget()
        holder.setObjectName("tkColumnBody")
        self.rows_box = QVBoxLayout(holder)
        self.rows_box.setContentsMargins(0, 0, 2, 0)
        self.rows_box.setSpacing(8)
        self.scroll.setWidget(holder)
        lay.addWidget(self.scroll, 1)
        self.cards: list[TaskCard] = []

    def set_cards(self, cards: list[TaskCard], empty_text: str = "") -> None:
        hc.clear(self.rows_box)
        self.cards = cards
        for c in cards:
            self.rows_box.addWidget(c)
        if not cards and empty_text:
            self.rows_box.addWidget(hc.label(empty_text, "hEmpty", "meta", wrap=True))
        self.rows_box.addStretch(1)
        self.count.setText(str(len(cards)))

    def _highlight(self, on: bool) -> None:
        self.setProperty("drop", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, e) -> None:  # noqa: N802 - Qt API
        if e.mimeData().hasFormat(MIME):
            e.acceptProposedAction()
            self._highlight(True)

    def dragLeaveEvent(self, e) -> None:  # noqa: N802 - Qt API
        self._highlight(False)

    def dropEvent(self, e) -> None:  # noqa: N802 - Qt API
        self._highlight(False)
        try:
            item_id = int(bytes(e.mimeData().data(MIME)).decode())
        except ValueError:
            return
        e.acceptProposedAction()
        self.dropped.emit(item_id, self.key)


# ---------------------------------------------------------------- moving


class MoveDialog(QDialog):
    """Confirm a move before anything is written. ``day`` holds the chosen day
    (This week / Later); ``column`` the target. The view writes on accept."""

    def __init__(self, item: it.Item, column: str, today: date, now: datetime,
                 pal: HomePalette = PALETTE, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("tkDialog")
        self.setWindowTitle("Move")
        self.setModal(True)
        self.item, self.column, self.today, self.now = item, column, today, now
        self.day: date | None = today if column == "today" else None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(12)
        name = COLUMN_NAMES[column]
        question = {"today": f"Move “{item.title}” to today?",
                    "someday": f"Remove the date from “{item.title}”?",
                    "done": f"Mark “{item.title}” done?"}.get(
            column, f"Move “{item.title}” to {name.lower()}: which day?")
        self.question = hc.label(question, "hTitle", "title", wrap=True)
        lay.addWidget(self.question)
        if item.when and column != "done":
            lay.addWidget(hc.label(f"Now: {when_label(item.when, today)}", "hMuted", "meta"))
        self.day_buttons: list[QPushButton] = []
        self.date_input: QLineEdit | None = None
        if column == "week":
            row = QHBoxLayout()
            row.setSpacing(6)
            for k in range(1, WEEK_DAYS + 1):
                d = today + timedelta(days=k)
                b = QPushButton(f"{d:%a}\n{d.day}")
                b.setObjectName("tkDay")
                b.setCheckable(True)
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.setFixedSize(48, 46)
                b.clicked.connect(lambda _=False, d=d: self._pick(d))
                row.addWidget(b)
                self.day_buttons.append(b)
            lay.addLayout(row)
            self._pick(today + timedelta(days=1))
        elif column == "later":
            self.date_input = QLineEdit()
            self.date_input.setObjectName("tkInput")
            self.date_input.setPlaceholderText("e.g. next monday, Nov 3, 2026-11-03")
            self.date_input.textChanged.connect(self._read_date)
            lay.addWidget(self.date_input)
        self.error = hc.label("", "hError", "meta", wrap=True)
        self.error.hide()
        lay.addWidget(self.error)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_btn = hc.button("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_btn)
        self.ok_btn = QPushButton({"done": "Mark done", "someday": "Remove date"}.get(
            column, "Move"))
        self.ok_btn.setObjectName("tkPrimary")
        self.ok_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.ok_btn.setDefault(True)
        self.ok_btn.clicked.connect(self._accept)
        buttons.addWidget(self.ok_btn)
        lay.addLayout(buttons)
        self.ok_btn.setEnabled(column != "later")

    def _pick(self, d: date) -> None:
        self.day = d
        for b in self.day_buttons:
            b.setChecked(b.text().split("\n")[1] == str(d.day))

    def _read_date(self, text: str) -> None:
        self.day = None
        self.error.hide()
        if not text.strip():
            self.ok_btn.setEnabled(False)
            return
        try:
            stored = parse_when_input(text, self.now, need_time=False)
        except it.ItemError as e:
            self._say(str(e))
            return
        d = date.fromisoformat(stored[:10]) if stored else None
        if d is None or d <= self.today + timedelta(days=WEEK_DAYS):
            self._say("Later is after the next 7 days; for sooner, use This week.")
            return
        self.day = d
        self.question.setText(f"Move “{self.item.title}” to {day_label(d, self.today)}?")
        self.ok_btn.setEnabled(True)

    def _say(self, text: str) -> None:
        self.error.setText(text)
        self.error.show()
        self.ok_btn.setEnabled(False)

    def _accept(self) -> None:
        if self.column in ("week", "later") and self.day is None:
            return
        self.accept()


# ---------------------------------------------------------------- creating


class ChipButton(QPushButton):
    """A property chip in the create window: shows its value, opens its picker."""

    def __init__(self, name: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.name = name
        self.setObjectName("tkPropChip")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        typeset(self, "label")

    def show_value(self, value: str, set_: bool) -> None:
        self.setText(value)
        self.setProperty("set", "true" if set_ else "false")
        self.style().unpolish(self)
        self.style().polish(self)


class CreateDialog(QDialog):
    """The create window (Linear-style). ``created(item_id)`` after Ctrl+Enter or
    Create; closing any other way keeps a draft (``draft_store``) and writes nothing."""

    created = Signal(int)

    def __init__(self, conn: sqlite3.Connection, now_fn: Callable[[], datetime],
                 projects: tuple[it.Project, ...], *, preset_due: str | None = None,
                 draft_store=None, pal: HomePalette = PALETTE, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("tkDialog")
        self.setWindowTitle("New task")
        self.setModal(True)
        self.setMinimumWidth(560)
        self.conn, self.now_fn, self.projects, self.pal = conn, now_fn, projects, pal
        self.store = draft_store
        self.kind = "task"
        self.priority = 0
        self.project: str | None = None
        self.due: str | None = preset_due
        self.remind: str | None = None
        self.title_due: str | None = None    # the date read from the title
        self.title_phrase = ""
        self.ignore_title_date = False
        self.saved = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 16)
        lay.setSpacing(10)
        head = QHBoxLayout()
        head.addWidget(hc.label("New task", "hMuted", "label"))
        head.addStretch(1)
        head.addWidget(hc.label("Ctrl+Enter to create · Esc keeps a draft", "hFaint", "meta"))
        lay.addLayout(head)
        self.title = QLineEdit()
        self.title.setObjectName("tkTitleInput")
        self.title.setPlaceholderText("Task title — add a date at the end, e.g. “friday 5pm”")
        self.title.textChanged.connect(self._read_title)
        lay.addWidget(self.title)
        self.found = QPushButton()
        self.found.setObjectName("tkFound")
        self.found.setCursor(Qt.CursorShape.PointingHandCursor)
        self.found.setToolTip("Read from the title; click to keep the words in the title")
        self.found.clicked.connect(self._drop_title_date)
        self.found.hide()
        lay.addWidget(self.found, 0, Qt.AlignmentFlag.AlignLeft)
        self.notes = QPlainTextEdit()
        self.notes.setObjectName("tkNotes")
        self.notes.setPlaceholderText("Notes (optional)")
        self.notes.setFixedHeight(80)
        lay.addWidget(self.notes)
        chips = QHBoxLayout()
        chips.setSpacing(6)
        self.chips: dict[str, ChipButton] = {}
        for key, tip in (("kind", "K"), ("priority", "P"), ("project", "J"), ("due", "D"),
                         ("remind", "R")):
            b = ChipButton(key)
            b.setToolTip(f"{key.title()} ({tip})")
            b.clicked.connect(lambda _=False, key=key: self.open_chip(key))
            chips.addWidget(b)
            self.chips[key] = b
        chips.addStretch(1)
        lay.addLayout(chips)
        # the inline field for Due / Reminder
        self.when_row = QWidget()
        wr = QHBoxLayout(self.when_row)
        wr.setContentsMargins(0, 0, 0, 0)
        wr.setSpacing(8)
        self.when_label = hc.label("", "hMuted", "label")
        wr.addWidget(self.when_label)
        self.when_input = QLineEdit()
        self.when_input.setObjectName("tkInput")
        self.when_input.returnPressed.connect(self._apply_when)
        wr.addWidget(self.when_input, 1)
        self.when_clear = hc.link("Clear")
        self.when_clear.clicked.connect(lambda: self._set_when(None))
        wr.addWidget(self.when_clear)
        self.when_row.hide()
        self.when_field = ""
        lay.addWidget(self.when_row)
        self.error = hc.label("", "hError", "meta", wrap=True)
        self.error.hide()
        lay.addWidget(self.error)
        buttons = QHBoxLayout()
        self.discard_btn = hc.link("Discard draft")
        self.discard_btn.clicked.connect(self.discard)
        buttons.addWidget(self.discard_btn)
        buttons.addStretch(1)
        self.cancel_btn = hc.button("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_btn)
        self.create_btn = QPushButton("Create task")
        self.create_btn.setObjectName("tkPrimary")
        self.create_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.create_btn.clicked.connect(self.create)
        buttons.addWidget(self.create_btn)
        lay.addLayout(buttons)
        for keys in ("Ctrl+Return", "Ctrl+Enter"):
            QShortcut(QKeySequence(keys), self, activated=self.create)
        for w in (self.title, self.notes, self.when_input):     # where Ctrl+Enter is typed
            w.installEventFilter(self)
        for key, name in (("K", "kind"), ("P", "priority"), ("J", "project"), ("D", "due"),
                          ("R", "remind")):
            sc = QShortcut(QKeySequence(key), self)
            sc.activated.connect(lambda name=name: self._chip_key(name))
        if preset_due is None:
            self._load_draft()
        self._sync()
        self.title.setFocus()

    # ---- properties

    def _sync(self) -> None:
        today = self.now_fn().date()
        due = self.effective_due()
        self.chips["kind"].show_value(self.kind.title(), self.kind != "task")
        self.chips["priority"].show_value(PRIORITY_NAMES[self.priority] if self.priority
                                          else "Priority", bool(self.priority))
        self.chips["project"].show_value(f"#{self.project}" if self.project else "Project",
                                         bool(self.project))
        self.chips["due"].show_value(f"Due {when_label(due, today)}" if due else "Due date",
                                     bool(due))
        self.chips["remind"].show_value(f"Remind {when_label(self.remind, today)}"
                                        if self.remind else "Reminder", bool(self.remind))

    def effective_due(self) -> str | None:
        """The due date the item gets: one set on the chip wins over the title's."""
        if self.due is not None:
            return self.due
        return None if self.ignore_title_date else self.title_due

    def clean_title(self) -> str:
        text = self.title.text().strip()
        if self.title_due and not self.ignore_title_date and self.due is None:
            return parse_title_date(text, self.now_fn())[0]
        return text

    def _read_title(self, text: str) -> None:
        clean, stored, phrase = parse_title_date(text, self.now_fn())
        self.title_due, self.title_phrase = stored, phrase
        if stored and not self.ignore_title_date and self.due is None:
            self.found.setText(f"Due {when_label(stored, self.now_fn().date())} "
                               f"(from “{phrase}”)  ×")
            self.found.show()
        else:
            self.found.hide()
        self._sync()

    def _drop_title_date(self) -> None:
        self.ignore_title_date = True
        self.found.hide()
        self._sync()

    def _chip_key(self, name: str) -> None:
        """K / P / J / D / R: only when not typing in a text field."""
        if isinstance(self.focusWidget(), (QLineEdit, QPlainTextEdit)):
            return
        self.open_chip(name)

    def open_chip(self, name: str) -> None:
        if name in ("due", "remind"):
            self.when_field = name
            self.when_label.setText("Due" if name == "due" else "Remind me")
            self.when_input.setPlaceholderText("e.g. friday, tomorrow 5pm, 2026-10-20" if
                                               name == "due" else "e.g. tomorrow 9am")
            current = self.effective_due() if name == "due" else self.remind
            self.when_input.setText((current or "").replace("T", " "))
            self.when_row.show()
            self.when_input.setFocus()
            self.when_input.selectAll()
            return
        menu = QMenu(self)
        menu.setObjectName("tkMenu")
        if name == "kind":
            for k in it.KINDS:
                a = menu.addAction(k.title())
                a.triggered.connect(lambda _=False, k=k: self._set("kind", k))
        elif name == "priority":
            for n, label in enumerate(PRIORITY_NAMES):
                a = menu.addAction(label)
                a.triggered.connect(lambda _=False, n=n: self._set("priority", n))
        else:
            a = menu.addAction("No project")
            a.triggered.connect(lambda: self._set("project", None))
            for p in self.projects:
                a = menu.addAction(f"#{p.slug} · {p.name}")
                a.triggered.connect(lambda _=False, s=p.slug: self._set("project", s))
        self.menu = menu
        b = self.chips[name]
        menu.popup(b.mapToGlobal(b.rect().bottomLeft()))

    def _set(self, name: str, value) -> None:
        setattr(self, name, value)
        self._sync()

    def _apply_when(self) -> None:
        try:
            stored = parse_when_input(self.when_input.text(), self.now_fn(),
                                      need_time=self.when_field == "remind")
        except it.ItemError as e:
            self._error(str(e))
            return
        self._set_when(stored)

    def _set_when(self, stored: str | None) -> None:
        if self.when_field == "due":
            self.due = stored
            if stored is None:
                self.ignore_title_date = True
                self.found.hide()
        elif self.when_field == "remind":
            self.remind = stored
        self._error("")
        self.when_row.hide()
        self.title.setFocus()
        self._sync()

    def _error(self, text: str) -> None:
        self.error.setText(text)
        self.error.setVisible(bool(text))

    def eventFilter(self, obj, e) -> bool:  # noqa: N802 - Qt API
        if (e.type() == QEvent.Type.KeyPress
                and e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and e.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.create()
            return True
        return super().eventFilter(obj, e)

    # ---- create, drafts

    def create(self) -> None:
        title = self.clean_title()
        if not title:
            self._error("Give it a title.")
            self.title.setFocus()
            return
        if self.when_row.isVisible() and self.when_input.text().strip():
            self._apply_when()
            if self.error.isVisible():
                return
        try:
            item = it.create_item(self.conn, title, kind=self.kind, priority=self.priority,
                                  body=self.notes.toPlainText().strip() or None,
                                  due=self.effective_due(), remind=self.remind,
                                  project=self.project, now=self.now_fn())
        except it.ItemError as e:
            self._error(str(e))
            return
        self.saved = True
        self._clear_draft()
        self.created.emit(item.id)
        self.accept()

    def draft(self) -> dict:
        return {"title": self.title.text(), "notes": self.notes.toPlainText(),
                "kind": self.kind, "priority": self.priority, "project": self.project,
                "due": self.due, "remind": self.remind}

    def _load_draft(self) -> None:
        if self.store is None:
            return
        try:
            d = json.loads(self.store.value("tasks/draft", "") or "{}")
        except (TypeError, ValueError):
            return
        if not isinstance(d, dict) or not d.get("title") and not d.get("notes"):
            return
        self.kind = d.get("kind") if d.get("kind") in it.KINDS else "task"
        self.priority = d.get("priority") if d.get("priority") in range(4) else 0
        self.project = d.get("project") or None
        self.due, self.remind = d.get("due") or None, d.get("remind") or None
        self.title.setText(str(d.get("title") or ""))
        self.notes.setPlainText(str(d.get("notes") or ""))
        self.discard_btn.show()

    def _clear_draft(self) -> None:
        if self.store is not None:
            self.store.remove("tasks/draft")
            self.store.sync()

    def discard(self) -> None:
        self._clear_draft()
        self.saved = True          # nothing to keep
        self.reject()

    def done(self, result: int) -> None:  # noqa: D102 - Qt API: every close comes here
        if not self.saved and self.store is not None:
            d = self.draft()
            if d["title"].strip() or d["notes"].strip():
                self.store.setValue("tasks/draft", json.dumps(d))
                self.store.sync()
        super().done(result)
