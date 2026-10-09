"""The suggestion "peek": a Notion-style side drawer with the full card.

Shows title, properties (type, status, source, created), why, and the full
prompt for Claude, with Copy / Done / Not useful actions. The window owns the
DB writes; this widget only emits signals.
"""
from __future__ import annotations

from html import escape

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QLinearGradient, QPainter
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from jarvis.cards import FEEDBACK_REASONS
from jarvis.gui.data import SuggestionRow, local_stamp
from jarvis.gui.theme import Tokens, mono_family
from jarvis.gui.widgets import (
    COPY_LABEL,
    FEEDBACK_LABELS,
    _clear,
    _label,
    hairline,
    paragraph,
    repolish,
    tag,
)

PEEK_WIDTH = 500
SHADOW_W = 18


class _Shadow(QWidget):
    def __init__(self, tokens: Tokens, parent: QWidget):
        super().__init__(parent)
        self.tokens = tokens
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def paintEvent(self, e) -> None:  # noqa: N802 - Qt API
        p = QPainter(self)
        g = QLinearGradient(self.width(), 0, 0, 0)
        a = 26 if self.tokens.name == "light" else 90
        g.setColorAt(0, QColor(0, 0, 0, a))
        g.setColorAt(1, QColor(0, 0, 0, 0))
        p.fillRect(self.rect(), g)
        p.end()


class PeekPanel(QFrame):
    """Overlay drawer on the right edge of its parent widget."""

    copied = Signal(object)  # SuggestionRow
    done = Signal(object)  # SuggestionRow
    not_useful = Signal(object, str)  # SuggestionRow, reason
    closed = Signal()

    FLASH_MS = 1400

    def __init__(self, tokens: Tokens, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("peek")
        self.tokens = tokens
        self.row: SuggestionRow | None = None
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # A soft shadow strip to the left (a sibling, so no graphics effect has to
        # re-render the text widgets inside the panel).
        self.shadow = _Shadow(tokens, parent)
        self.shadow.hide()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        bar = QHBoxLayout()
        bar.setContentsMargins(14, 10, 14, 6)
        bar.setSpacing(4)
        self.close_btn = QPushButton("✕")
        self.close_btn.setObjectName("ghost")
        self.close_btn.setToolTip("Close (Esc)")
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.hide_panel)
        bar.addWidget(self.close_btn)
        self.kind_label = _label("", "crumb")
        bar.addWidget(self.kind_label)
        bar.addStretch(1)
        self.source_btn = QPushButton("Open source ↗")
        self.source_btn.setObjectName("ghost")
        self.source_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.source_btn.clicked.connect(self._open_source)
        bar.addWidget(self.source_btn)
        outer.addLayout(bar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        self.body = QVBoxLayout(body)
        self.body.setContentsMargins(36, 14, 32, 18)
        self.body.setSpacing(0)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        actions = QFrame()
        actions.setObjectName("peekActions")
        al = QHBoxLayout(actions)
        al.setContentsMargins(36, 12, 32, 16)
        al.setSpacing(8)
        self.copy_btn = QPushButton(COPY_LABEL)
        self.copy_btn.setObjectName("primary")
        self.copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_btn.setToolTip("Puts the prompt on the clipboard to paste into Claude")
        self.copy_btn.clicked.connect(self._copy)
        self.done_btn = QPushButton("Done")
        self.done_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.done_btn.setToolTip("Mark as handled; it will not come back")
        self.done_btn.clicked.connect(lambda: self.row and self.done.emit(self.row))
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
            act.triggered.connect(
                lambda _=False, r=reason: self.row and self.not_useful.emit(self.row, r))
            self.feedback_actions[reason] = act
        self.feedback_btn.setMenu(menu)
        al.addWidget(self.copy_btn)
        al.addWidget(self.done_btn)
        al.addWidget(self.feedback_btn)
        al.addStretch(1)
        outer.addWidget(hairline())
        outer.addWidget(actions)

        self._flash = QTimer(self, singleShot=True, interval=self.FLASH_MS)
        self._flash.timeout.connect(self._unflash)
        self._anim = QPropertyAnimation(self, b"geometry", self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.hide()

    # ------------------------------------------------------------ content

    def show_row(self, row: SuggestionRow) -> None:
        self.row = row
        c = row.card
        t = self.tokens
        _clear(self.body)
        self.kind_label.setText(f"Suggestions  /  {row.type_label}")
        self.source_btn.setVisible(bool(c.source_url))
        self.source_btn.setToolTip(c.source_url or "")

        self.title = _label(c.title, "peekTitle", wrap=True)
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body.addWidget(self.title)
        self.body.addSpacing(16)

        props = QGridLayout()
        props.setHorizontalSpacing(14)
        props.setVerticalSpacing(9)
        props.setColumnMinimumWidth(0, 96)

        def prop(r: int, key: str, w: QWidget) -> None:
            k = _label(key, "propKey")
            props.addWidget(k, r, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            props.addWidget(w, r, 1, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        prop(0, "◉  Type", tag(row.type_label, row.type_color, t))
        prop(1, "◎  Status", tag(row.status_label, row.status_color, t))
        ref = c.source_ref or "source"
        if c.source_url:
            src = _label(f'<a href="{escape(c.source_url, quote=True)}" style="color:{t.text}">'
                         f"{escape(ref)}</a>", "propVal", rich=True)
            src.setToolTip(c.source_url)
        else:
            src = _label(ref, "propVal")
        prop(2, "↗  Source", src)
        prop(3, "◷  Created", _label(local_stamp(c.created_at) or "–", "propVal"))
        if c.check_id or c.paper_id:
            prop(4, "#  " + ("Check" if c.check_id else "Paper"),
                 _label(c.check_id or c.paper_id, "propVal"))
        props.setColumnStretch(1, 1)
        self.body.addLayout(props)
        self.body.addSpacing(18)
        self.body.addWidget(hairline())
        self.body.addSpacing(16)

        if c.why:
            why = _label(paragraph(c.why, 155), "peekWhy", wrap=True, rich=True)
            why.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.why_label = why
            self.body.addWidget(why)
            self.body.addSpacing(22)

        head = _label("PROMPT FOR CLAUDE", "sectionHead")
        self.body.addWidget(head)
        self.body.addSpacing(8)
        self.prompt = QPlainTextEdit()
        self.prompt.setObjectName("promptBox")
        self.prompt.setReadOnly(True)
        self.prompt.setPlainText(c.claude_prompt or "")
        f = QFont(mono_family())
        f.setPixelSize(12)
        self.prompt.setFont(f)
        self.prompt.setMinimumHeight(260)
        self.prompt.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.body.addWidget(self.prompt, 1)
        self._unflash()

    # ------------------------------------------------------------ show / hide

    def _sync_shadow(self) -> None:
        g = self.geometry()
        self.shadow.setGeometry(g.left() - SHADOW_W, g.top(), SHADOW_W, g.height())
        self.shadow.setVisible(self.isVisible())
        if self.isVisible():
            self.shadow.raise_()
            self.raise_()

    def moveEvent(self, e) -> None:  # noqa: N802 - Qt API
        super().moveEvent(e)
        self._sync_shadow()

    def resizeEvent(self, e) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(e)
        self._sync_shadow()

    def showEvent(self, e) -> None:  # noqa: N802 - Qt API
        super().showEvent(e)
        self._sync_shadow()

    def hideEvent(self, e) -> None:  # noqa: N802 - Qt API
        super().hideEvent(e)
        self.shadow.hide()

    def target_rect(self) -> QRect:
        p = self.parentWidget()
        w = min(PEEK_WIDTH, max(320, p.width() - 80))
        top = getattr(p, "peek_top", 0)
        return QRect(p.width() - w, top, w, p.height() - top)

    def reposition(self) -> None:
        if self.isVisible() and self._anim.state() != QPropertyAnimation.State.Running:
            self.setGeometry(self.target_rect())

    def open_row(self, row: SuggestionRow, animate: bool = True) -> None:
        was_open = self.isVisible()
        self.show_row(row)
        end = self.target_rect()
        self.raise_()
        if was_open or not animate:
            self.setGeometry(end)
            self.show()
            return
        start = QRect(end)
        start.moveLeft(end.left() + 60)
        self.setGeometry(start)
        self.show()
        self._anim.stop()
        self._anim.setStartValue(start)
        self._anim.setEndValue(end)
        self._anim.start()

    def hide_panel(self) -> None:
        if self.isVisible():
            self.hide()
            self.row = None
            self.closed.emit()

    # ------------------------------------------------------------ actions

    def _open_source(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        if self.row and self.row.card.source_url:
            QDesktopServices.openUrl(QUrl(self.row.card.source_url))

    def _copy(self) -> None:
        if self.row is None:
            return
        QGuiApplication.clipboard().setText(self.row.card.claude_prompt or "")
        self.copy_btn.setText("Copied ✓")
        self.copy_btn.setProperty("flash", "true")
        repolish(self.copy_btn)
        self._flash.start()
        self.copied.emit(self.row)

    def _unflash(self) -> None:
        self.copy_btn.setText(COPY_LABEL)
        self.copy_btn.setProperty("flash", "false")
        repolish(self.copy_btn)
