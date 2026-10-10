"""School: the tutor (left) and the stage (right), per Canvas course.

The top bar has a tab per course (from Canvas), the course's module (which the
user can change; kept in the UI settings), the next exam or quiz and "New
session". The tutor streams Gemma's replies; a check question arrives as a card
whose answer stays hidden until asked; a photo of a problem can go with a
question. The stage (``school_stage``) is a web page created the first time
School is shown, so later visuals appear without a load.

Below ``NARROW`` px the stage leaves the row: the tutor has the width, and the
stage opens over it as a sheet with a back button when there's a visual.
"""
from __future__ import annotations

import base64
import sqlite3
import threading
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QBuffer,
    QByteArray,
    QEvent,
    QIODevice,
    QObject,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QIcon, QImage, QKeyEvent, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from jarvis import canvas, db
from jarvis.config import Config
from jarvis.gui import motion
from jarvis.gui.chat_panel import FlowLayout, MessageText, UserMessage, markdown_html
from jarvis.gui.home_style import PALETTE, school_stylesheet
from jarvis.gui.orb import Orb
from jarvis.gui.theme import Tokens
from jarvis.school import modules, tutor

NARROW = 1000                   # px: below this the stage becomes a sheet
TUTOR_STRETCH, STAGE_STRETCH = 4, 6
PHOTO_MAX = 1280                # px, the longest side of a photo sent to the model
EXAM_DAYS = 60                  # how far ahead the exam chip looks
THINKING_ORB = 20
GENERAL_COURSE = tutor.GENERAL_COURSE   # the tab for studying outside a course

TutorFn = Callable[[Config, tutor.Request, Callable[[str], None]], tutor.Reply]


def default_tutor(cfg: Config, req: tutor.Request, on_token: Callable[[str], None]) -> tutor.Reply:
    """Ask Gemma (the agent model) on a worker thread, with its own connection."""
    from jarvis.ollama import OllamaClient

    conn = db.connect(cfg.db_path)
    client = OllamaClient(cfg.ollama_url, cfg.agent_model or cfg.model, keep_alive="10m")
    try:
        return tutor.ask(client, conn, req, on_token)
    finally:
        client.close()
        conn.close()


def photo_b64(path: str | Path) -> str | None:
    """A photo as base64 PNG, its longest side at most PHOTO_MAX; None if unreadable."""
    img = QImage(str(path))
    if img.isNull():
        return None
    if max(img.width(), img.height()) > PHOTO_MAX:
        img = img.scaled(PHOTO_MAX, PHOTO_MAX, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return base64.b64encode(bytes(data)).decode()


def next_exam(conn: sqlite3.Connection, course: str, today: date) -> canvas.CanvasEvent | None:
    """The course's next exam (a commitment), else its next quiz, within EXAM_DAYS."""
    from datetime import timedelta

    events = [e for e in canvas.upcoming(conn, today, today + timedelta(days=EXAM_DAYS))
              if e.course_short == course]
    return (next((e for e in events if e.commitment), None)
            or next((e for e in events if "quiz" in e.title.lower()), None))


def days_text(day: date, today: date) -> str:
    n = (day - today).days
    return "today" if n <= 0 else "tomorrow" if n == 1 else f"in {n} days"


def camera_icon(color: str, px: int = 16, ratio: float = 2.0) -> QIcon:
    """A line camera (the attach-a-photo button)."""
    size = round(px * ratio)
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    pm.setDevicePixelRatio(ratio)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color), 1.5)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    u = px / 16
    p.drawRoundedRect(QRectF(1.5 * u, 4.5 * u, 13 * u, 9.5 * u), 2 * u, 2 * u)
    p.drawPolyline([QPointF(5.5 * u, 4.5 * u), QPointF(6.5 * u, 2.5 * u),
                    QPointF(9.5 * u, 2.5 * u), QPointF(10.5 * u, 4.5 * u)])
    p.drawEllipse(QRectF(5.25 * u, 6.5 * u, 5.5 * u, 5.5 * u))
    p.end()
    return QIcon(pm)


def _label(text: str, name: str, wrap: bool = False) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName(name)
    lab.setWordWrap(wrap)
    return lab


def _button(text: str, name: str, tip: str = "") -> QPushButton:
    b = QPushButton(text)
    b.setObjectName(name)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if tip:
        b.setToolTip(tip)
    return b


# ---------------------------------------------------------------- messages


class CheckCard(QFrame):
    """A check question; its answer opens on "Show answer"."""

    def __init__(self, question: str, answer: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("scCheck")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)
        lay.addWidget(_label("CHECK YOURSELF", "scKicker"))
        self.question = MessageText("botText")
        self.question.set_html(markdown_html(question))
        lay.addWidget(self.question)
        self.answer = MessageText("scAnswer")
        self.answer.set_html(markdown_html(answer or "No answer was given."))
        self.answer.hide()
        lay.addWidget(self.answer)
        self.toggle = _button("Show answer", "scLink")
        self.toggle.clicked.connect(self.flip)
        lay.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignLeft)

    @property
    def open(self) -> bool:
        return not self.answer.isHidden()

    def flip(self) -> None:
        show = not self.open
        self.toggle.setText("Hide answer" if show else "Show answer")
        motion.reveal(self.answer, show)


class TutorMessage(QWidget):
    """The tutor's reply: thinking, then streamed text, then text and check cards."""

    def __init__(self, tokens: Tokens, parent: QWidget | None = None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 6, 0)
        lay.setSpacing(6)
        lay.addWidget(_label("✦ Tutor", "scName"))
        self.thinking = QWidget()
        tl = QHBoxLayout(self.thinking)
        tl.setContentsMargins(0, 2, 0, 2)
        tl.setSpacing(8)
        self.orb = Orb(tokens, THINKING_ORB, "thinking")
        tl.addWidget(self.orb)
        tl.addWidget(_label("thinking…", "thinking"), 1)
        lay.addWidget(self.thinking)
        self.body = MessageText("botText", links=True)
        self.body.hide()
        lay.addWidget(self.body)
        self.checks: list[CheckCard] = []
        self.checks_box = QVBoxLayout()
        self.checks_box.setSpacing(6)
        lay.addLayout(self.checks_box)
        self.raw = ""
        self.shown = ""
        self.reply: tutor.Reply | None = None

    def add_token(self, chunk: str) -> None:
        self.raw += chunk
        visible = tutor.streaming_text(self.raw).lstrip()
        if not visible:
            return
        if self.thinking.isVisible() or not self.thinking.isHidden():
            self.thinking.hide()
            self.orb.set_state("idle")
            self.body.show()
        if visible.startswith(self.shown):
            self.body.add_text(visible[len(self.shown):])
        else:
            self.body.set_plain(visible)
        self.shown = visible

    def finish(self, reply: tutor.Reply) -> None:
        self.reply = reply
        self.thinking.hide()
        self.orb.set_state("idle")
        self.body.setVisible(bool(reply.text))
        self.body.set_html(markdown_html(reply.text))
        for c in reply.checks:
            card = CheckCard(c["q"], c.get("a", ""))
            self.checks.append(card)
            self.checks_box.addWidget(card)
            motion.fade_rise(card)

    def fail(self, message: str) -> None:
        self.thinking.hide()
        self.orb.set_state("idle")
        self.body.show()
        self.body.setObjectName("botError")
        self.body.set_plain(message)


# ---------------------------------------------------------------- composer


class SchoolComposer(QFrame):
    """The question box, with 📷 to attach a photo (sent with the next question)."""

    submitted = Signal(str)

    MAX_LINES = 6

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("composer")
        self.setProperty("focus", "false")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 6, 6, 6)
        outer.setSpacing(4)
        self.attach_row = QWidget()
        ar = QHBoxLayout(self.attach_row)
        ar.setContentsMargins(0, 0, 0, 0)
        ar.setSpacing(6)
        self.attach_label = _label("", "scAttach")
        ar.addWidget(self.attach_label)
        self.attach_clear = _button("×", "scGhostIcon", "Remove the photo")
        self.attach_clear.clicked.connect(self.clear_photo)
        ar.addWidget(self.attach_clear)
        ar.addStretch(1)
        self.attach_row.hide()
        outer.addWidget(self.attach_row)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.edit = QPlainTextEdit()
        self.edit.setObjectName("composerEdit")
        self.edit.setPlaceholderText("Ask the tutor…")
        self.edit.setTabChangesFocus(True)
        self.edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.edit.document().setDocumentMargin(2)
        self.edit.installEventFilter(self)
        self.edit.textChanged.connect(self._changed)
        row.addWidget(self.edit, 1)
        self.photo_btn = _button("", "scIcon", "Attach a photo of a problem or slide")
        self.photo_btn.setIcon(camera_icon(PALETTE.muted))
        self.photo_btn.setIconSize(QSize(16, 16))
        self.photo_btn.setFixedSize(28, 28)
        self.photo_btn.clicked.connect(self.pick_photo)
        row.addWidget(self.photo_btn, 0, Qt.AlignmentFlag.AlignBottom)
        self.send_btn = _button("↑", "send", "Send (Enter)")
        self.send_btn.setFixedSize(28, 28)
        self.send_btn.clicked.connect(self.submit)
        row.addWidget(self.send_btn, 0, Qt.AlignmentFlag.AlignBottom)
        outer.addLayout(row)
        self.photo: tuple[str, str] | None = None      # (file name, base64 PNG)
        self._busy = False
        self._changed()

    def eventFilter(self, obj: QObject, e: QEvent) -> bool:  # noqa: N802 - Qt API
        if obj is self.edit:
            if e.type() == QEvent.Type.KeyPress:
                k: QKeyEvent = e
                if k.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
                        k.modifiers() & Qt.KeyboardModifier.ShiftModifier):
                    self.submit()
                    return True
            elif e.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
                self.setProperty("focus", "true" if e.type() == QEvent.Type.FocusIn else "false")
                self.style().unpolish(self)
                self.style().polish(self)
        return super().eventFilter(obj, e)

    def _changed(self) -> None:
        lines = round(self.edit.document().size().height()) or 1
        n = max(1, min(self.MAX_LINES, lines))
        self.edit.setFixedHeight(int(n * self.edit.fontMetrics().lineSpacing() + 10))
        self.send_btn.setEnabled(not self._busy and bool(self.edit.toPlainText().strip()))

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._changed()

    def pick_photo(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Attach a photo", str(Path.home()),
                                              "Images (*.png *.jpg *.jpeg *.webp *.bmp)")
        if path:
            self.attach(path)

    def attach(self, path: str | Path) -> bool:
        data = photo_b64(path)
        if data is None:
            return False
        self.photo = (Path(path).name, data)
        self.attach_label.setText(f"Photo: {Path(path).name}")
        self.attach_row.show()
        return True

    def clear_photo(self) -> None:
        self.photo = None
        self.attach_row.hide()

    def submit(self) -> None:
        text = self.edit.toPlainText().strip()
        if not text or self._busy:
            return
        self.edit.clear()
        self.submitted.emit(text)


# ---------------------------------------------------------------- page


class _Bridge(QObject):
    token = Signal(int, str)
    finished = Signal(int, object)
    failed = Signal(int, str)


def _emit(sig, *args) -> None:
    try:
        sig.emit(*args)
    except RuntimeError:        # the window closed while the model was still talking
        pass


def _run(cfg: Config, fn: TutorFn, req: tutor.Request, rid: int, bridge: _Bridge) -> None:
    try:
        reply = fn(cfg, req, lambda t: _emit(bridge.token, rid, t))
    except Exception as e:  # noqa: BLE001 - shown in the reply, never crashes the window
        _emit(bridge.failed, rid, f"The tutor couldn't answer: {type(e).__name__}: {e}")
    else:
        _emit(bridge.finished, rid, reply)


class SchoolView(QWidget):
    status = Signal(str, str)

    def __init__(self, cfg: Config, conn: sqlite3.Connection, tokens: Tokens, *,
                 now_fn: Callable[[], datetime] | None = None,
                 tutor_fn: TutorFn | None = None,
                 stage_factory: Callable[[], QWidget] | None = None,
                 settings: Any = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.cfg, self.conn, self.tokens = cfg, conn, tokens
        self.now_fn = now_fn or datetime.now
        self.tutor_fn = tutor_fn or default_tutor
        self.stage_factory = stage_factory
        self._settings = settings
        self.stage: QWidget | None = None
        self.course = ""
        self.module = modules.GENERAL
        self.session_id = ""
        self.messages: list[QWidget] = []
        self.pending: TutorMessage | None = None
        self.busy = False
        self._rid = 0
        self._threads: list[threading.Thread] = []
        self.sheet_open = False
        self._bridge = _Bridge(self)
        self._bridge.token.connect(self._on_token)
        self._bridge.finished.connect(self._on_finished)
        self._bridge.failed.connect(self._on_failed)
        self._build()
        self.reload()

    # ------------------------------------------------------------ settings

    @property
    def settings(self):
        if self._settings is None:
            from jarvis.gui.voice_turn import ui_settings

            self._settings = ui_settings()
        return self._settings

    def _setting(self, key: str) -> str:
        v = self.settings.value(key, "")
        return str(v) if v else ""

    # ------------------------------------------------------------ layout

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.content = QWidget()
        self.content.setObjectName("schoolPage")
        self.content.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.content.setStyleSheet(school_stylesheet(PALETTE))
        outer.addWidget(self.content)
        page = QVBoxLayout(self.content)
        page.setContentsMargins(20, 16, 20, 16)
        page.setSpacing(12)

        # top bar: title + course tabs | module, exam, new session
        top = QHBoxLayout()
        top.setSpacing(14)
        title = _label("School", "scTitle")
        top.addWidget(title)
        self.tabs_bar = QWidget()
        self.tabs_bar.setObjectName("scTabs")
        self.tabs_lay = QHBoxLayout(self.tabs_bar)
        self.tabs_lay.setContentsMargins(0, 0, 0, 4)
        self.tabs_lay.setSpacing(2)
        self.tab_group = QButtonGroup(self)
        self.tab_group.setExclusive(True)
        self.tabs: dict[str, QPushButton] = {}
        self.underline = QFrame(self.tabs_bar)
        self.underline.setObjectName("scUnderline")
        self.underline.setFixedHeight(2)
        top.addWidget(self.tabs_bar)
        top.addStretch(1)
        self.exam = _label("", "scExam")
        self.exam.hide()
        top.addWidget(self.exam)
        self.module_box = QComboBox()
        self.module_box.setObjectName("scModule")
        self.module_box.setToolTip("The module this course uses")
        for m in modules.MODULES.values():
            self.module_box.addItem(m.name, m.key)
        self.module_box.activated.connect(self._module_picked)
        top.addWidget(self.module_box)
        self.new_btn = _button("New session", "hButton", "Start a fresh conversation")
        self.new_btn.clicked.connect(self.new_session)
        top.addWidget(self.new_btn)
        page.addLayout(top)

        # body: tutor | stage
        self.body = QHBoxLayout()
        self.body.setSpacing(14)
        self.tutor_card = QFrame()
        self.tutor_card.setObjectName("hCard")
        tc = QVBoxLayout(self.tutor_card)
        tc.setContentsMargins(14, 12, 14, 12)
        tc.setSpacing(10)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("scScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.feed = QWidget()
        self.feed.setObjectName("scFeed")
        self.feed_lay = QVBoxLayout(self.feed)
        self.feed_lay.setContentsMargins(0, 0, 4, 0)
        self.feed_lay.setSpacing(14)
        self.empty = self._build_empty()
        self.feed_lay.addWidget(self.empty)
        self.feed_lay.addStretch(1)
        self.scroll.setWidget(self.feed)
        self.scroll.verticalScrollBar().rangeChanged.connect(self._follow)
        tc.addWidget(self.scroll, 1)
        self.composer = SchoolComposer()
        self.composer.submitted.connect(self.send)
        tc.addWidget(self.composer)
        self.body.addWidget(self.tutor_card, TUTOR_STRETCH)

        self.stage_card = QFrame()
        self.stage_card.setObjectName("hCard")
        sc = QVBoxLayout(self.stage_card)
        sc.setContentsMargins(1, 1, 1, 1)
        sc.setSpacing(0)
        self.stage_head = QWidget()
        sh = QHBoxLayout(self.stage_head)
        sh.setContentsMargins(12, 8, 12, 4)
        self.back_btn = _button("← Tutor", "scLink", "Back to the tutor (Esc)")
        self.back_btn.clicked.connect(self.close_sheet)
        self.back_btn.hide()
        sh.addWidget(self.back_btn)
        self.stage_title = _label("Stage", "scStageTitle")
        sh.addWidget(self.stage_title)
        sh.addStretch(1)
        sc.addWidget(self.stage_head)
        self.stage_box = QVBoxLayout()
        self.stage_box.setContentsMargins(0, 0, 0, 0)
        sc.addLayout(self.stage_box, 1)
        self.body.addWidget(self.stage_card, STAGE_STRETCH)
        page.addLayout(self.body, 1)

    def _build_empty(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 18, 4, 4)
        lay.setSpacing(8)
        self.empty_title = _label("What are we learning?", "scEmptyTitle", wrap=True)
        lay.addWidget(self.empty_title)
        self.empty_text = _label("", "hMuted", wrap=True)
        lay.addWidget(self.empty_text)
        self.starters_box = QWidget()
        self.starters = FlowLayout(self.starters_box, 6)
        lay.addWidget(self.starters_box)
        self.starter_buttons: list[QPushButton] = []
        return w

    # ------------------------------------------------------------ data

    def reload(self) -> None:
        """Rebuild the course tabs (Canvas courses the user hasn't hidden, then
        General) and keep the current course."""
        hidden = set(self.hidden_courses())
        names = [c for c in canvas.courses(self.conn) if c not in hidden] + [GENERAL_COURSE]
        if list(self.tabs) != names:
            for b in self.tabs.values():
                self.tab_group.removeButton(b)
                b.deleteLater()
            self.tabs = {}
            for name in names:
                b = _button(name, "scTab")
                b.setCheckable(True)
                b.clicked.connect(lambda _=False, n=name: self.set_course(n))
                b.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                b.customContextMenuRequested.connect(
                    lambda pos, n=name, btn=b: self._tab_menu(n, btn.mapToGlobal(pos)))
                if name == GENERAL_COURSE:
                    b.setToolTip("Study anything, not tied to a course")
                else:
                    b.setToolTip("Right-click to hide this course from School")
                self.tab_group.addButton(b)
                self.tabs_lay.addWidget(b)
                self.tabs[name] = b
        last = self._setting("school/course")
        course = self.course if self.course in self.tabs else last if last in self.tabs else names[0]
        self.set_course(course, force=True)

    # ------------------------------------------------------------ hidden courses

    def hidden_courses(self) -> list[str]:
        raw = self.settings.value("school/hidden", [])
        if isinstance(raw, str):
            raw = [raw] if raw else []
        return [str(c) for c in raw or []]

    def hide_course(self, course: str) -> None:
        """Take a Canvas course off School's tabs (Canvas and Home keep it)."""
        if course == GENERAL_COURSE or course in self.hidden_courses():
            return
        self.settings.setValue("school/hidden", self.hidden_courses() + [course])
        if self.course == course:
            self.course = ""
            self.settings.setValue("school/course", "")
        self.reload()
        self.status.emit(f"{course} is hidden from School. Right-click a tab to show it again.",
                         "info")

    def unhide_course(self, course: str) -> None:
        self.settings.setValue("school/hidden", [c for c in self.hidden_courses() if c != course])
        self.reload()

    def _tab_menu(self, course: str, at) -> None:
        from PySide6.QtWidgets import QMenu

        menu = QMenu(self)
        menu.setObjectName("tkMenu")
        if course != GENERAL_COURSE:
            menu.addAction(f"Hide {course} from School", lambda: self.hide_course(course))
        hidden = self.hidden_courses()
        if hidden:
            if not menu.isEmpty():
                menu.addSeparator()
            for c in hidden:
                menu.addAction(f"Show {c} again", lambda c=c: self.unhide_course(c))
        if not menu.isEmpty():
            self.tab_menu = menu
            menu.popup(at)

    def module_for(self, course: str) -> modules.Module:
        picked = self._setting(f"school/module/{course}")
        return modules.get(picked) if picked else modules.for_course(course)

    def set_course(self, course: str, force: bool = False) -> None:
        if course == self.course and not force:
            return
        changed = course != self.course
        self.course = course
        self.settings.setValue("school/course", course)
        btn = self.tabs.get(course)
        if btn is not None:
            btn.setChecked(True)
            self._move_underline(btn, animate=changed and bool(self.isVisible()))
        self.module = self.module_for(course)
        self.module_box.setCurrentIndex(max(0, self.module_box.findData(self.module.key)))
        self._show_exam()
        if changed or not self.session_id:
            self.session_id = (self._setting(f"school/session/{course}")
                               or tutor.new_session_id(course))
            self.settings.setValue(f"school/session/{course}", self.session_id)
            self._load_session()

    def _move_underline(self, btn: QPushButton, animate: bool) -> None:
        self.tabs_bar.layout().activate()
        g = btn.geometry()
        end = QRect(g.x() + 8, self.tabs_bar.height() - 2, max(8, g.width() - 16), 2)
        self.underline.raise_()
        self.underline.show()
        if animate:
            motion.animate(self.underline, b"geometry", self.underline.geometry(), end,
                           duration=motion.BASE)
        else:
            self.underline.setGeometry(end)

    def resizeEvent(self, e) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(e)
        btn = self.tabs.get(self.course)
        if btn is not None:
            self._move_underline(btn, animate=False)
        self._layout_mode()

    def _module_picked(self, index: int) -> None:
        key = self.module_box.itemData(index)
        self.settings.setValue(f"school/module/{self.course}", key)
        self.module = modules.get(key)
        self._fill_starters()

    def _show_exam(self) -> None:
        today = self.now_fn().date()
        ev = next_exam(self.conn, self.course, today) if self.course != GENERAL_COURSE else None
        self.exam_event = ev
        if ev is None:
            self.exam.hide()
            return
        self.exam.setText(f"{ev.title} · {days_text(ev.day, today)}")
        self.exam.setToolTip(f"{ev.title}: {ev.day:%a %b} {ev.day.day}")
        self.exam.show()

    def upcoming_text(self) -> str:
        ev = getattr(self, "exam_event", None)
        if ev is None:
            return ""
        return f"{ev.title} {days_text(ev.day, self.now_fn().date())} ({ev.day:%a %b} {ev.day.day})"

    def _fill_starters(self) -> None:
        for b in self.starter_buttons:
            b.deleteLater()
        self.starter_buttons = []
        self.empty_text.setText(f"{self.module.name}: {self.module.summary} Ask anything, "
                                "or start with one of these.")
        for s in self.module.starters:
            b = _button(s, "scStarter")
            b.clicked.connect(lambda _=False, q=s: self.send(q))
            self.starters.addWidget(b)
            self.starter_buttons.append(b)

    # ------------------------------------------------------------ session

    def _clear_feed(self) -> None:
        for w in self.messages:
            w.deleteLater()
        self.messages = []
        self.pending = None

    def _load_session(self) -> None:
        self._clear_feed()
        self._fill_starters()
        for role, content in tutor.transcript(self.conn, self.session_id):
            if role == "user":
                self._add(UserMessage(content))
            else:
                m = TutorMessage(self.tokens)
                text, blocks = tutor.split_blocks(content)
                m.finish(tutor.Reply(text, content, blocks))
                self._add(m, animate=False)
        self.empty.setVisible(not self.messages)

    def new_session(self) -> None:
        if self.busy:
            return
        self.session_id = tutor.new_session_id(self.course)
        self.settings.setValue(f"school/session/{self.course}", self.session_id)
        self._load_session()
        if self.stage is not None and hasattr(self.stage, "clear"):
            self.stage.clear()
        self.composer.edit.setFocus()

    def _add(self, w: QWidget, animate: bool = True) -> None:
        self.feed_lay.insertWidget(self.feed_lay.count() - 1, w)
        self.messages.append(w)
        self.empty.hide()
        if animate:
            motion.fade_rise(w)

    def _follow(self, _lo: int, hi: int) -> None:
        bar = self.scroll.verticalScrollBar()
        if self.busy or hi - bar.value() < 160:
            bar.setValue(hi)

    # ------------------------------------------------------------ asking

    def send(self, text: str) -> None:
        text = text.strip()
        if not text or self.busy:
            return
        photo = self.composer.photo
        self._add(UserMessage(text + (f"\n(photo: {photo[0]})" if photo else "")))
        self.pending = TutorMessage(self.tokens)
        self._add(self.pending)
        req = tutor.Request(course=self.course, module=self.module, question=text,
                            session_id=self.session_id, today=self.now_fn().date(),
                            image_b64=photo[1] if photo else None,
                            upcoming=self.upcoming_text())
        self.composer.clear_photo()
        self.busy = True
        self.composer.set_busy(True)
        self._rid += 1
        t = threading.Thread(target=_run, args=(self.cfg, self.tutor_fn, req, self._rid,
                                                self._bridge), daemon=True)
        self._threads.append(t)
        t.start()
        self.last_request = req

    def _done(self) -> None:
        self.busy = False
        self.pending = None
        self.composer.set_busy(False)
        self._threads = [t for t in self._threads if t.is_alive()]

    def _on_token(self, rid: int, chunk: str) -> None:
        if rid == self._rid and self.pending is not None:
            self.pending.add_token(chunk)

    def _on_finished(self, rid: int, reply: tutor.Reply) -> None:
        if rid != self._rid or self.pending is None:
            return
        self.pending.finish(reply)
        self._done()

    def _on_failed(self, rid: int, message: str) -> None:
        if rid != self._rid or self.pending is None:
            return
        self.pending.fail(message)
        self.status.emit("The tutor couldn't answer.", "error")
        self._done()

    # ------------------------------------------------------------ stage

    def showEvent(self, e) -> None:  # noqa: N802 - Qt API
        super().showEvent(e)
        self.ensure_stage()
        btn = self.tabs.get(self.course)
        if btn is not None:
            self._move_underline(btn, animate=False)

    def ensure_stage(self) -> QWidget:
        """Create the stage once (warm: later visuals need no page load)."""
        if self.stage is None:
            if self.stage_factory is not None:
                self.stage = self.stage_factory()
            else:
                from jarvis.gui.school_stage import SchoolStage

                self.stage = SchoolStage()
            self.stage_box.addWidget(self.stage)
        return self.stage

    def _layout_mode(self) -> None:
        narrow = self.width() < NARROW
        if not narrow:
            self.sheet_open = False
        self.tutor_card.setVisible(not (narrow and self.sheet_open))
        self.stage_card.setVisible(not narrow or self.sheet_open)
        self.back_btn.setVisible(narrow and self.sheet_open)

    @property
    def narrow(self) -> bool:
        return self.width() < NARROW

    def open_sheet(self) -> None:
        """Narrow: the stage covers the tutor (when a visual arrives)."""
        if self.narrow:
            self.sheet_open = True
            self._layout_mode()
            motion.fade_rise(self.stage_card, dy=24, duration=motion.PANEL)

    def close_sheet(self) -> None:
        self.sheet_open = False
        self._layout_mode()

    def escape(self) -> bool:
        """Esc: close the stage sheet. True if it did something."""
        if self.sheet_open:
            self.close_sheet()
            return True
        return False


__all__ = ["CheckCard", "SchoolComposer", "SchoolView", "TutorMessage", "default_tutor",
           "photo_b64"]
