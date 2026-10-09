"""The main window: a Notion-like project dashboard plus the "Ask Jarvis" chat.

Layout, top to bottom in the page column: title and watch status, the briefing
callout, four KPI tiles, the milestone timeline, the commit grid next to issues
by phase, the power charts per rail, and the suggestions table (click a row to
peek). The chat panel sits on the right and can be hidden (Ctrl+J).

The window reads the database (re-read on file changes and every 30 s, only
changed sections are rebuilt) and writes only done / not-useful marks and chat
messages. Refresh runs the shared pipeline on a background thread under the
refresh lock, so it never overlaps ``jarvis watch``.
"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QFileSystemWatcher, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from jarvis import cards as cards_mod
from jarvis import db
from jarvis.config import Config
from jarvis.gui import motion, theme
from jarvis.gui.charts import (
    ContributionGridChart,
    MilestoneTimelineChart,
    PhaseBarsChart,
    RailBudgetChart,
    VoltageWindowChart,
    part_colors,
)
from jarvis.gui.chat_panel import AskFn, ChatPanel
from jarvis.gui.data import (
    RailPower,
    SuggestionRow,
    WindowData,
    describe_error,
    fmt_num,
    load_window_data,
    local_stamp,
    refresh_message,
)
from jarvis.gui.peek import PeekPanel
from jarvis.gui.widgets import (
    TEMPLATE_TIP,
    BriefingCallout,
    Columns,
    ICON_SIZE,
    KpiRow,
    PulseDot,
    SuggestionsTable,
    _clear,
    _label,
    repolish,
    refresh_icon,
    section_header,
)
from jarvis.lock import RefreshBusy, refresh_lock

DEFAULT_SIZE = (1400, 920)
CONTENT_MAX_WIDTH = 1100
POLL_MS = 30_000
DEBOUNCE_MS = 400
STATUS_CLEAR_MS = 8_000
IDLE_HINT = ("Ctrl+R refresh  ·  Ctrl+K ask Jarvis  ·  Ctrl+Space talk  ·  "
             "Ctrl+J toggle chat")
EMPTY_CARDS = SuggestionsTable.EMPTY
BUSY_TEXT = "A refresh is already running in the background"
SECTION_GAP = 40
SPIN_MS = 33  # Refresh icon frame interval while refreshing (~30 fps)
SPIN_DEG_PER_S = 300

REFRESH_TEXT = " Refresh"
REFRESHING_TEXT = " Refreshing…"

RefreshFn = Callable[[sqlite3.Connection, Config], Any]


def _default_refresh(conn: sqlite3.Connection, cfg: Config) -> Any:
    from jarvis.pipeline import refresh

    return refresh(conn, cfg)


class _Bridge(QObject):
    """Carries results from the worker thread back to the GUI thread (queued signals)."""

    succeeded = Signal(object)
    failed = Signal(str)
    busy = Signal()


def _run_refresh(cfg: Config, fn: RefreshFn, bridge: _Bridge) -> None:
    """Worker body. Uses its own SQLite connection: connections are per-thread.

    Holds the refresh lock (``<db>.lock``) so it never overlaps ``jarvis watch``;
    if the daemon is mid-tick, it reports busy and the DB watcher shows its result.
    """
    conn = None
    try:
        with refresh_lock(cfg.db_path):
            conn = db.connect(cfg.db_path)
            result = fn(conn, cfg)
    except RefreshBusy:
        bridge.busy.emit()
    except Exception as e:  # noqa: BLE001 - any failure goes to the status line
        bridge.failed.emit(describe_error(e))
    else:
        bridge.succeeded.emit(result)
    finally:
        if conn is not None:
            conn.close()


class _Pane(QWidget):
    """The left pane; tells the peek drawer when it resizes."""

    resized = Signal()

    def resizeEvent(self, e) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(e)
        self.resized.emit()


class MainWindow(QMainWindow):
    def __init__(
        self,
        cfg: Config | None = None,
        conn: sqlite3.Connection | None = None,
        *,
        auto_refresh: bool = True,
        watch: bool = True,
        refresh_fn: RefreshFn | None = None,
        scheme: str | None = None,
        ask_fn: AskFn | None = None,
        chat_open: bool = True,
        voice: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self.cfg = cfg or Config.from_env()
        self.conn = conn or db.connect(self.cfg.db_path)
        self._owns_conn = conn is None
        self.refresh_fn = refresh_fn or _default_refresh
        self.scheme = scheme  # None = follow the system
        self.tokens = theme.tokens_for(scheme)
        self.data: WindowData | None = None
        self._refreshing = False
        self._thread: threading.Thread | None = None
        self._bridge = _Bridge(self)
        self._bridge.succeeded.connect(self._on_refresh_ok)
        self._bridge.failed.connect(self._on_refresh_failed)
        self._bridge.busy.connect(self._on_refresh_busy)
        self._ask_fn = ask_fn
        self._voice_kw = dict(voice or {})  # ChatPanel voice seams (tests inject fakes)

        self.setWindowTitle(f"Jarvis · {self.cfg.repo.split('/')[-1]}")
        self.resize(*DEFAULT_SIZE)
        self.setMinimumSize(760, 520)
        self._build()
        self._shortcuts()
        self.set_chat_open(chat_open, focus=False)

        self._status_timer = QTimer(self, singleShot=True, interval=STATUS_CLEAR_MS)
        self._status_timer.timeout.connect(lambda: self.set_status(IDLE_HINT, "idle"))
        self._spin_timer = QTimer(self, interval=SPIN_MS)
        self._spin_timer.timeout.connect(self._spin)
        self._spin_step = 0
        self._spin_angle = 0.0

        self.reload(force=True)

        self._debounce = QTimer(self, singleShot=True, interval=DEBOUNCE_MS)
        self._debounce.timeout.connect(self.reload)
        self._watcher: QFileSystemWatcher | None = None
        if watch:
            self._start_watching()
            self._poll = QTimer(self, interval=POLL_MS)
            self._poll.timeout.connect(self.reload)
            self._poll.start()

        app = QGuiApplication.instance()
        if scheme is None and app is not None and hasattr(app.styleHints(), "colorSchemeChanged"):
            app.styleHints().colorSchemeChanged.connect(self._on_scheme_changed)

        if auto_refresh:
            QTimer.singleShot(250, self.refresh)

    # ------------------------------------------------------------ layout

    def _build(self) -> None:
        page = QWidget()
        page.setObjectName("page")
        root = QHBoxLayout(page)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.pane = _Pane()
        pane_lay = QVBoxLayout(self.pane)
        pane_lay.setContentsMargins(0, 0, 0, 0)
        pane_lay.setSpacing(0)
        topbar = self._build_topbar()
        pane_lay.addWidget(topbar)
        self.pane.peek_top = topbar.height()

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        holder.setObjectName("dash")
        hl = QHBoxLayout(holder)
        hl.setContentsMargins(56, 34, 56, 64)
        hl.addStretch(1)
        self.column = QWidget()
        self.column.setMaximumWidth(CONTENT_MAX_WIDTH)
        self.column.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        hl.addWidget(self.column, 100)
        hl.addStretch(1)
        self.col = QVBoxLayout(self.column)
        self.col.setContentsMargins(0, 0, 0, 0)
        self.col.setSpacing(0)
        self.scroll.setWidget(holder)
        pane_lay.addWidget(self.scroll, 1)
        self._build_page()

        bar = QFrame()
        bar.setObjectName("statusBar")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(20, 6, 20, 7)
        self.status_label = _label(IDLE_HINT, "status")
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        bl.addWidget(self.status_label, 1)
        pane_lay.addWidget(bar)

        root.addWidget(self.pane, 1)
        self.chat = ChatPanel(self.cfg, self.tokens, self._ask_fn, **self._voice_kw)
        self.chat.close_requested.connect(lambda: self.set_chat_open(False))
        root.addWidget(self.chat)

        self.peek = PeekPanel(self.tokens, self.pane)
        self.peek.copied.connect(self._on_copied)
        self.peek.done.connect(self.mark_done)
        self.peek.not_useful.connect(self.mark_not_useful)
        self.peek.closed.connect(lambda: self.table.select(None))
        self.pane.resized.connect(self.peek.reposition)

        self.setCentralWidget(page)

    def _build_topbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("topBar")
        bar.setFixedHeight(46)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(16, 0, 12, 0)
        lay.setSpacing(4)
        lay.addWidget(_label("Jarvis", "crumb"))
        lay.addWidget(_label("  /  ", "crumb"))
        self.crumb = _label(f"📡  {self.cfg.repo.split('/')[-1]}", "crumbStrong")
        lay.addWidget(self.crumb)
        lay.addStretch(1)
        self.refresh_btn = QPushButton(REFRESH_TEXT)
        self.refresh_btn.setObjectName("ghost")
        self.refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_btn.setToolTip("Sync GitHub, run checks and rewrite the briefing (Ctrl+R)")
        self.refresh_btn.setIconSize(ICON_SIZE)
        self.refresh_btn.setIcon(refresh_icon(self.tokens.muted))
        fm = self.refresh_btn.fontMetrics()
        self.refresh_btn.setMinimumWidth(fm.horizontalAdvance(REFRESHING_TEXT) + 44)
        self.refresh_btn.clicked.connect(self.refresh)
        lay.addWidget(self.refresh_btn)
        self.chat_btn = QPushButton("✦  Ask Jarvis")
        self.chat_btn.setObjectName("ghost")
        self.chat_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chat_btn.setToolTip("Show or hide the chat (Ctrl+J; Ctrl+K to type a question)")
        self.chat_btn.clicked.connect(lambda: self.set_chat_open(not self.chat.isVisible()))
        lay.addWidget(self.chat_btn)
        return bar

    def _build_page(self) -> None:
        c = self.col
        repo_name = self.cfg.repo.split("/")[-1]
        self.title_label = _label(f"📡 {repo_name}", "pageTitle")
        self.title_label.setToolTip(self.cfg.repo)
        c.addWidget(self.title_label)
        c.addSpacing(6)

        meta = QHBoxLayout()
        meta.setSpacing(8)
        self.watch_dot = PulseDot()
        self.watch_label = _label("", "meta")
        self.watch_sep = _label("·", "metaSep")
        self.meta_label = _label("", "meta")
        for w in (self.watch_dot, self.watch_label, self.watch_sep, self.meta_label):
            meta.addWidget(w)
        meta.addStretch(1)
        c.addLayout(meta)
        c.addSpacing(22)

        self.briefing_view = BriefingCallout(self.tokens)
        c.addWidget(self.briefing_view)
        c.addSpacing(16)

        self.kpi_row = KpiRow()
        c.addWidget(self.kpi_row)
        c.addSpacing(SECTION_GAP)

        c.addWidget(section_header("🗓  Milestones", "open milestones on a shared date axis"))
        c.addSpacing(12)
        self.timeline = MilestoneTimelineChart(self.tokens)
        c.addWidget(self.timeline)
        c.addSpacing(SECTION_GAP)

        self.two = Columns(narrow=820)
        left, right = self.two.column(52), self.two.column(48)
        self.grid_head = section_header("Activity", "commits per day")
        left.addWidget(self.grid_head)
        left.addSpacing(12)
        self.grid_chart = ContributionGridChart(self.tokens)
        left.addWidget(self.grid_chart)
        left.addSpacing(6)
        self.grid_caption = _label("", "caption", wrap=True)
        left.addWidget(self.grid_caption)
        left.addStretch(1)
        right.addWidget(section_header("Open issues by phase"))
        right.addSpacing(12)
        self.phase_chart = PhaseBarsChart(self.tokens)
        right.addWidget(self.phase_chart)
        right.addSpacing(6)
        self.phase_caption = _label("", "caption", wrap=True)
        right.addWidget(self.phase_caption)
        right.addStretch(1)
        c.addWidget(self.two)
        c.addSpacing(SECTION_GAP)

        c.addWidget(section_header("⚡  Power", "every number from hardware.yaml"))
        c.addSpacing(12)
        self.power_box = QVBoxLayout()
        self.power_box.setSpacing(0)
        c.addLayout(self.power_box)
        self.power_widgets: list[QWidget] = []
        c.addSpacing(SECTION_GAP)

        self.suggestions_head = section_header("Suggestions")
        c.addWidget(self.suggestions_head)
        c.addSpacing(8)
        self.table = SuggestionsTable(self.tokens)
        self.table.opened.connect(self.open_peek)
        self.table.copied.connect(self._on_copied)
        c.addWidget(self.table)
        c.addStretch(1)

    def _shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+R"), self, activated=self.refresh)
        QShortcut(QKeySequence("F5"), self, activated=self.refresh)
        QShortcut(QKeySequence("Ctrl+Q"), self, activated=self.close)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self._on_escape)
        QShortcut(QKeySequence("Ctrl+K"), self, activated=self.focus_chat)
        QShortcut(QKeySequence("Ctrl+J"), self,
                  activated=lambda: self.set_chat_open(not self.chat.isVisible()))
        # Window-wide, so it also works while typing in the chat box.
        QShortcut(QKeySequence("Ctrl+Space"), self, activated=self.toggle_voice)

    # ------------------------------------------------------------ chat / peek

    def set_chat_open(self, open_: bool, focus: bool = True) -> None:
        self.chat.setVisible(open_)
        self.chat_btn.setProperty("active", "true" if open_ else "false")
        repolish(self.chat_btn)
        if open_ and focus:
            self.chat.focus_input()

    def focus_chat(self) -> None:
        self.set_chat_open(True)

    def toggle_voice(self) -> None:
        """Ctrl+Space: talk to Jarvis (opens the chat if it is hidden)."""
        if not self.chat.isVisible() and not self.chat.voice_active:
            self.set_chat_open(True, focus=False)
        self.chat.toggle_voice()

    def _on_escape(self) -> None:
        """Esc stops voice (listening or speaking) first; otherwise it closes the
        peek drawer if it is open; otherwise it does nothing."""
        if self.chat.stop_voice():
            return
        if self.peek.is_open:
            self.peek.hide_panel()

    def open_peek(self, row: SuggestionRow) -> None:
        self.table.select(row.id)
        self.peek.open_row(row)

    # ------------------------------------------------------------ data

    def reload(self, force: bool = False, animate: bool = True) -> None:
        """Re-read the DB; rebuild only the sections whose data changed.

        Rebuilt sections animate in (``animate``); unchanged ones are left alone,
        so the 30 s poll with nothing new does no work and starts no animation.
        """
        try:
            new = load_window_data(self.conn, self.cfg.repo,
                                   hardware_path=self.cfg.hardware_path)
        except sqlite3.Error as e:
            self.set_status(f"Could not read the database: {e}", "warn", sticky=True)
            return
        old = self.data
        if not force and new == old:
            return
        self.data = new
        def changed(attr: str) -> bool:
            return force or old is None or getattr(new, attr) != getattr(old, attr)

        anim = animate and motion.enabled()
        self._render_meta()
        if changed("briefing"):
            self.briefing_view.set_data(new.briefing, self.tokens, animate=anim)
        if changed("kpis"):
            self.kpi_row.set_kpis(new.kpis, animate=anim)
        if changed("timeline"):
            self.timeline.set_tokens(self.tokens)
            self.timeline.set_data(new.timeline, animate=anim)
        if changed("grid"):
            self.grid_chart.set_tokens(self.tokens)
            self.grid_chart.set_data(new.grid, animate=anim)
            self.grid_caption.setText(new.grid.caption if new.grid else "")
        if changed("phases"):
            self.phase_chart.set_tokens(self.tokens)
            self.phase_chart.set_data(new.phases, animate=anim)
            n = new.phases.total_open if new.phases else 0
            self.phase_caption.setText(f"{n} open issue{'s' if n != 1 else ''}; "
                                       "an issue counts once, under its lowest phase label")
        if changed("power") or changed("power_note"):
            self._render_power(new.power, new.power_note, anim)
        if changed("rows"):
            self._render_rows(new.rows, anim)
        if new.errors:
            self.set_status(" · ".join(new.errors), "warn", sticky=True)

    def _render_meta(self) -> None:
        d = self.data
        watch = d.watch if d else None
        self.watch_label.setText(watch.text if watch else "")
        self.watch_label.setToolTip(watch.tooltip if watch else "")
        self.watch_dot.setToolTip(watch.tooltip if watch else "")
        watching = bool(watch and watch.watching)
        self.watch_dot.set_color(self.tokens.green if watching else self.tokens.faint)
        self.watch_dot.set_breathing(watching)
        for w in (self.watch_label, self.watch_dot, self.watch_sep):
            w.setVisible(bool(watch and watch.text))
        parts, tips = [], []
        if d and d.briefing:
            parts.append(f"briefing by {d.briefing.source_label}")
            tips.append(f"Briefing written: {local_stamp(d.briefing.created_at)} "
                        f"by {d.briefing.source_label}")
            if d.briefing.source == "template":
                tips.append(TEMPLATE_TIP)
        if d and d.updated_at:
            parts.append(f"updated {local_stamp(d.updated_at)}")
        if d and d.last_sync:
            tips.append(f"Last GitHub sync: {local_stamp(d.last_sync)}")
        self.meta_label.setText("  ·  ".join(parts) if parts else "not synced yet")
        self.meta_label.setToolTip("\n".join(tips))

    def _render_power(self, rails: tuple[RailPower, ...], note: str | None,
                      animate: bool = False) -> None:
        _clear(self.power_box)
        self.power_widgets = []
        self.budget_charts: list[RailBudgetChart] = []
        self.window_charts: list[VoltageWindowChart] = []
        if not rails:
            lb = _label(note or "No rails with parts in hardware.yaml.", "emptyText", wrap=True)
            self.power_box.addWidget(lb)
            self.power_widgets.append(lb)
            return
        for i, r in enumerate(rails):
            if i:
                self.power_box.addSpacing(34)
            colors = part_colors(self.tokens, r.budget, r.window)
            head = QHBoxLayout()
            head.setSpacing(10)
            head.addWidget(_label(f"{r.rail} rail", "railHead"), 0, Qt.AlignmentFlag.AlignBaseline)
            b = r.budget
            facts = [f"{fmt_num(r.nominal_v)} V nominal"]
            if b is not None:
                facts.append(f"rating {fmt_num(b.rating_a)} A" if b.rating_a is not None
                             else "rating unknown")
            if r.window is not None:
                facts.append(f"tolerance ±{fmt_num(r.window.tolerance_pct)}%"
                             if r.window.tolerance_pct is not None else "tolerance unknown")
            if b and all(s.planned for s in b.segments):
                facts.append("planned parts only")
            head.addWidget(_label("  ·  ".join(facts), "railSub"), 0, Qt.AlignmentFlag.AlignBaseline)
            head.addStretch(1)
            self.power_box.addLayout(head)
            self.power_box.addSpacing(10)
            cols = Columns(narrow=820)
            if b is not None:
                lay = cols.column()
                lay.addWidget(_label("Worst-case current", "sectionHint"))
                lay.addSpacing(6)
                ch = RailBudgetChart(self.tokens, colors)
                ch.set_data(b, animate=animate)
                lay.addWidget(ch)
                lay.addSpacing(6)
                lay.addWidget(_label(b.caption, "caption", wrap=True))
                lay.addStretch(1)
                self.budget_charts.append(ch)
            if r.window is not None:
                lay = cols.column()
                lay.addWidget(_label("Voltage window", "sectionHint"))
                lay.addSpacing(6)
                vc = VoltageWindowChart(self.tokens, colors)
                vc.set_data(r.window, animate=animate)
                lay.addWidget(vc)
                lay.addSpacing(6)
                lay.addWidget(_label(r.window.caption, "caption", wrap=True))
                lay.addStretch(1)
                self.window_charts.append(vc)
            self.power_box.addWidget(cols)
            self.power_widgets.append(cols)

    def _render_rows(self, rows: tuple[SuggestionRow, ...], animate: bool = False) -> None:
        open_id = self.peek.row.id if self.peek.is_open and self.peek.row else None
        self.table.set_rows(rows, self.tokens, animate=animate)
        n = len(rows)
        self.suggestions_head.head.setText(f"SUGGESTIONS  ·  {n}" if n else "SUGGESTIONS")
        if open_id is not None:
            match = next((r for r in rows if r.id == open_id), None)
            if match is None:
                self.peek.hide_panel()
            else:
                self.table.select(open_id)
                self.peek.show_row(match)

    # ------------------------------------------------------------ card actions

    @property
    def card_widgets(self) -> dict:
        return self.table.rows

    @property
    def empty_label(self) -> QLabel | None:
        return self.table.empty_label

    def _on_copied(self, row) -> None:
        self.set_status(f"Copied the prompt for “{row.card.title}”. Paste it into Claude.", "ok")

    def mark_done(self, row) -> None:
        self._close_card(row, lambda: cards_mod.mark_done(self.conn, row.card.id),
                         f"Marked done: “{row.card.title}”")

    def mark_not_useful(self, row, reason: str) -> None:
        self._close_card(row, lambda: cards_mod.set_feedback(self.conn, row.card.id, reason),
                         f"Dismissed as {reason}: “{row.card.title}”. Thanks, noted.")

    def _close_card(self, row, write: Callable[[], None], message: str) -> None:
        try:
            write()
        except (sqlite3.Error, ValueError) as e:
            self.set_status(f"Could not save that: {e}", "warn", sticky=True)
            return
        if self.peek.row is not None and self.peek.row.id == row.id:
            self.peek.hide_panel()
        self.table.remove(row.id)
        n = len(self.table.rows)
        self.suggestions_head.head.setText(f"SUGGESTIONS  ·  {n}" if n else "SUGGESTIONS")
        self.set_status(message, "ok")
        # A closed card can change facts (electrical counts); re-read the rest quietly.
        QTimer.singleShot(0, self.reload)

    # ------------------------------------------------------------ refresh

    @property
    def refreshing(self) -> bool:
        return self._refreshing

    def refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        self.refresh_btn.setEnabled(False)
        self._spin_step = 0
        self.refresh_btn.setText(REFRESHING_TEXT)
        self._spin()
        self._sync_spinner()
        self.set_status("Syncing GitHub and writing the briefing. The model can take a while.",
                        "idle", sticky=True)
        # A daemon thread, so closing the window never waits on a slow model call.
        # SQLite's WAL journal keeps the DB consistent if the process exits mid-write.
        self._thread = threading.Thread(
            target=_run_refresh, args=(self.cfg, self.refresh_fn, self._bridge),
            name="jarvis-refresh", daemon=True,
        )
        self._thread.start()

    def _spin(self) -> None:
        """Turn the Refresh icon (only while refreshing and visible)."""
        if not self.isVisible() or self.isMinimized():
            return
        self._spin_step += 1
        self._spin_angle = (self._spin_angle + SPIN_DEG_PER_S * SPIN_MS / 1000) % 360
        self.refresh_btn.setIcon(refresh_icon(self.tokens.muted, self._spin_angle))

    def _end_refresh(self) -> None:
        self._refreshing = False
        self._spin_timer.stop()
        self._spin_angle = 0.0
        self.refresh_btn.setIcon(refresh_icon(self.tokens.muted))
        self.refresh_btn.setText(REFRESH_TEXT)
        self.refresh_btn.setEnabled(True)

    def _on_refresh_ok(self, result: Any) -> None:
        self._end_refresh()
        self.reload()
        level, text = refresh_message(result)
        self.set_status(text, level, sticky=level == "warn")

    def _on_refresh_busy(self) -> None:
        self._end_refresh()
        self.reload()
        self.set_status(BUSY_TEXT, "idle")

    def _on_refresh_failed(self, message: str) -> None:
        self._end_refresh()
        self.reload()
        self.set_status(message, "warn", sticky=True)

    def wait_for_refresh(self, timeout: float = 30.0) -> None:
        """Block until the worker is done and its result is delivered (for tests)."""
        if self._thread is not None:
            self._thread.join(timeout)
        app = QApplication.instance()
        for _ in range(50):
            if not self._refreshing:
                break
            app.processEvents()

    # ------------------------------------------------------------ status, theme, watch

    def set_status(self, text: str, level: str = "idle", sticky: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setToolTip(text)
        self.status_label.setProperty("level", level)
        repolish(self.status_label)
        self._status_timer.stop()
        if not sticky and text != IDLE_HINT:
            self._status_timer.start()

    def _retheme(self) -> None:
        for w in (self.timeline, self.grid_chart, self.phase_chart):
            w.set_tokens(self.tokens)
        self.peek.tokens = self.tokens
        self.peek.shadow.tokens = self.tokens
        self.chat.set_tokens(self.tokens)
        self.refresh_btn.setIcon(refresh_icon(self.tokens.muted, self._spin_angle))
        self.reload(force=True, animate=False)  # a theme switch is not new data
        if self.peek.isVisible() and self.peek.row is not None:
            self.peek.show_row(self.peek.row)

    def _on_scheme_changed(self, *_: Any) -> None:
        self.tokens = theme.apply(QGuiApplication.instance())
        self._retheme()

    def apply_scheme(self, scheme: str | None) -> None:
        self.scheme = scheme
        self.tokens = theme.apply(QGuiApplication.instance(), scheme)
        self._retheme()

    def _watch_paths(self) -> list[str]:
        p = Path(self.cfg.db_path)
        return [str(x) for x in (p, Path(f"{p}-wal")) if x.exists()]

    def _start_watching(self) -> None:
        p = Path(self.cfg.db_path)
        if not p.parent.exists():
            return
        self._watcher = QFileSystemWatcher(self)
        self._watcher.addPath(str(p.parent))
        files = self._watch_paths()
        if files:
            self._watcher.addPaths(files)
        hw = Path(self.cfg.hardware_path)
        if hw.exists():
            self._watcher.addPath(str(hw))
        self._watcher.fileChanged.connect(self._on_fs_change)
        self._watcher.directoryChanged.connect(self._on_fs_change)

    def _on_fs_change(self, *_: Any) -> None:
        # Files can be replaced (WAL reset, checkpoint, editor save); re-add missing ones.
        if self._watcher is not None:
            watched = set(self._watcher.files())
            want = self._watch_paths()
            if Path(self.cfg.hardware_path).exists():
                want.append(str(self.cfg.hardware_path))
            missing = [f for f in want if f not in watched]
            if missing:
                self._watcher.addPaths(missing)
        self._debounce.start()

    def _sync_spinner(self) -> None:
        run = (self._refreshing and motion.enabled() and self.isVisible()
               and not self.isMinimized())
        if run and not self._spin_timer.isActive():
            self._spin_timer.start()
        elif not run:
            self._spin_timer.stop()

    def showEvent(self, e) -> None:  # noqa: N802 - Qt API
        super().showEvent(e)
        self._sync_spinner()

    def hideEvent(self, e) -> None:  # noqa: N802 - Qt API
        super().hideEvent(e)
        self._spin_timer.stop()

    def changeEvent(self, e) -> None:  # noqa: N802 - Qt API
        super().changeEvent(e)
        if e.type() == QEvent.Type.WindowStateChange:
            self._sync_spinner()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        self.chat.shutdown()  # mic and speaker off, voice workers joined
        if self._owns_conn:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass
        super().closeEvent(event)
