"""The main window: Jarvis as a project brain.

Top bar (name, state pill, pulse, clock, refresh / ask), a status line, then
three columns: vitals / milestones / pulse / power on the left; the project
graph in the middle with the briefing floating over its top-left; the
inspector and the thought stream on the right. A one-line ticker at the
bottom. The chat ("Ask Jarvis") slides in over the right column (Ctrl+K /
Ctrl+J), and its cited sources light up in the graph.

The window reads the database (re-read on file changes and every 30 s; only
changed parts are rebuilt) and writes only done / not-useful marks and chat
messages. Refresh runs the shared pipeline on a background thread under the
refresh lock, so it never overlaps ``jarvis watch``.
"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QFileSystemWatcher, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QCloseEvent,
    QDesktopServices,
    QGuiApplication,
    QKeySequence,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from jarvis import cards as cards_mod
from jarvis import db
from jarvis.config import Config
from jarvis.gui import motion, theme
from jarvis.gui.brain import BrainGraph
from jarvis.gui.brain_data import FILTER_KINDS, BrainData, _parse, ago, load_brain
from jarvis.gui.capture_overlay import WIDTH as CAPTURE_W
from jarvis.gui.capture_overlay import CaptureFn, CaptureOverlay, PlainFn
from jarvis.gui.chat_panel import PANEL_WIDTH, AskFn, ChatPanel
from jarvis.gui.cockpit import (
    BriefingHud,
    CommitPanel,
    Inspector,
    IssuesPanel,
    MilestonesPanel,
    StreamPanel,
    VitalsPanel,
    mono,
    pill,
    set_pill,
)
from jarvis.gui.dashboard_data import Dashboard, build_dashboard
from jarvis.gui.data import (
    WindowData,
    describe_error,
    load_window_data,
    local_stamp,
    refresh_message,
)
from jarvis.gui.finding_view import load_finding
from jarvis.gui.home_data import build_home
from jarvis.gui.home_view import HomeView
from jarvis.gui.shell import (
    RAIL_COMPACT,
    RAIL_WIDE,
    WIDE_FROM,
    WORKSPACES,
    NavRail,
    PlaceholderPage,
)
from jarvis.gui.tasks_view import TasksView
from jarvis.gui.widgets import ICON_SIZE, PulseDot, _label, refresh_icon, repolish
from jarvis.lock import RefreshBusy, refresh_lock

DEFAULT_SIZE = (1500, 940)
LEFT_W = 290
RIGHT_W = 360
# Below this window width the cockpit's side columns narrow, so the graph keeps room.
NARROW_BELOW = 1200
LEFT_W_NARROW = 272   # the longest vitals key + value still fit
RIGHT_W_NARROW = 256
# With the chat open the graph keeps at least this much; in a window too small for
# vitals + graph + chat, the left column steps aside while the chat is open.
CHAT_GRAPH_MIN = 420
ACTIVE_MILESTONE_KEY = "projects/active_milestone"
POLL_MS = 30_000
DEBOUNCE_MS = 400
STATUS_CLEAR_MS = 8_000
CLOCK_MS = 1_000
IDLE_HINT = ("hover = inspect · click = lock · scroll = zoom · Ctrl+R refresh · "
             "Ctrl+N capture · Ctrl+K ask · Ctrl+Space talk · Esc release")
SHELL_HINT = "Ctrl+1–5 switch · Ctrl+N capture · Ctrl+K ask · Ctrl+Space talk"
START_WORKSPACE = "home"
BUSY_TEXT = "A refresh is already running in the background"
SPIN_MS = 33
SPIN_DEG_PER_S = 300
REFRESH_TEXT = " refresh"
REFRESHING_TEXT = " refreshing…"
FILTER_LABELS = {"issue": "issue", "commit": "commit", "card": "suggestion",
                 "check": "check", "paper": "paper", "item": "item"}
SHORT_LABELS = {"card": "card"}  # a narrow graph's legend

LEGEND_COLORS = {"issue": theme.DARK.ink, "commit": theme.DARK.tint(0.6),
                 "card": theme.DARK.signal, "check": theme.mix(theme.DARK.bg, theme.DARK.signal, 0.6),
                 "paper": theme.DARK.tint(0.45), "item": theme.DARK.signal_text}

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
    """Worker body, on its own SQLite connection, under the refresh lock."""
    conn = None
    try:
        with refresh_lock(cfg.db_path):
            conn = db.connect(cfg.db_path)
            result = fn(conn, cfg)
    except RefreshBusy:
        bridge.busy.emit()
    except Exception as e:  # noqa: BLE001 - any failure goes to the ticker
        bridge.failed.emit(describe_error(e))
    else:
        bridge.succeeded.emit(result)
    finally:
        if conn is not None:
            conn.close()


def ref_to_node(ref: str) -> str | None:
    """A chat source ref as a graph node id ("#7" -> "issue:7")."""
    if ref.startswith("#") and ref[1:].isdigit():
        return f"issue:{ref[1:]}"
    kind, _, rest = ref.partition(":")
    if kind == "commit" and rest:
        return f"commit:{rest[:7]}"
    if kind in ("card", "check") and rest:
        return f"{kind}:{rest}"
    return None


class _GraphStage(QWidget):
    """The centre: the graph, with the briefing HUD, the legend and the counts
    floating over it."""

    def __init__(self, graph: BrainGraph, hud: BriefingHud, legend: QWidget, counts: QLabel):
        super().__init__()
        self.graph, self.hud, self.legend, self.counts = graph, hud, legend, counts
        for w in (graph, hud, legend, counts):
            w.setParent(self)
        hud.raise_()

    def resizeEvent(self, e) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(e)
        self.place()

    def place(self) -> None:
        w, h = self.width(), self.height()
        self.graph.setGeometry(0, 0, w, h)
        self.hud.setFixedWidth(min(BriefingHud.WIDTH, max(260, w - 28)))
        self.hud.adjustSize()
        self.hud.move(14, 14)
        # a narrow graph: the legend drops its bullets (the labels keep their colours)
        buttons = self.legend.findChildren(QPushButton)
        for b in buttons:
            b.setText(b.property("full"))
        self.legend.adjustSize()
        if self.legend.width() > w - 24:
            for b in buttons:
                b.setText(b.property("short"))
        self.legend.adjustSize()
        self.legend.move(12, h - self.legend.height() - 8)
        self.counts.adjustSize()
        self.counts.move(w - self.counts.width() - 14, h - self.counts.height() - 12)
        # no room beside the legend: the counts give way
        self.counts.setVisible(self.counts.x() > self.legend.geometry().right() + 12)
        self.graph.set_inset_left(self.hud.width() + 14 if self.hud.expanded else 0)


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
        chat_open: bool = False,
        voice: dict[str, Any] | None = None,
        capture_fn: CaptureFn | None = None,
        plain_capture_fn: PlainFn | None = None,
        start: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__()
        self.cfg = cfg or Config.from_env()
        self.conn = conn or db.connect(self.cfg.db_path)
        self._owns_conn = conn is None
        self.refresh_fn = refresh_fn or _default_refresh
        self.scheme = "dark"
        self.tokens = theme.DARK
        self.data: WindowData | None = None
        self.brain: BrainData | None = None
        self._refreshing = False
        self._thread: threading.Thread | None = None
        self._bridge = _Bridge(self)
        self._bridge.succeeded.connect(self._on_refresh_ok)
        self._bridge.failed.connect(self._on_refresh_failed)
        self._bridge.busy.connect(self._on_refresh_busy)
        self._ask_fn = ask_fn
        self._voice_kw = dict(voice or {})
        self._capture_fns = (capture_fn, plain_capture_fn)
        self._now_fn = clock or (lambda: datetime.now().astimezone())
        self.open_url: Callable[[QUrl], Any] = QDesktopServices.openUrl   # tests swap it
        self._status_sticky = False

        self.setWindowTitle(f"Jarvis · {self.cfg.repo.split('/')[-1]}")
        self.resize(*DEFAULT_SIZE)
        self.setMinimumSize(980, 600)
        self._build()
        self._shortcuts()
        self.show_workspace(start or START_WORKSPACE)
        self.set_chat_open(chat_open, focus=False)

        self._status_timer = QTimer(self, singleShot=True, interval=STATUS_CLEAR_MS)
        self._status_timer.timeout.connect(self._idle_ticker)
        self._spin_timer = QTimer(self, interval=SPIN_MS)
        self._spin_timer.timeout.connect(self._spin)
        self._spin_angle = 0.0
        self._clock_timer = QTimer(self, interval=CLOCK_MS)
        self._clock_timer.timeout.connect(self._tick_clock)
        self._clock_timer.start()
        self._tick_clock()

        self.reload(force=True)

        self._debounce = QTimer(self, singleShot=True, interval=DEBOUNCE_MS)
        self._debounce.timeout.connect(self.reload)
        self._watcher: QFileSystemWatcher | None = None
        if watch:
            self._start_watching()
            self._poll = QTimer(self, interval=POLL_MS)
            self._poll.timeout.connect(self.reload)
            self._poll.start()

        if auto_refresh:
            QTimer.singleShot(250, self.refresh)

    # ------------------------------------------------------------ layout

    def _build(self) -> None:
        page = QWidget()
        page.setObjectName("page")
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_topbar())

        # rail | workspaces | chat
        middle = QHBoxLayout()
        middle.setContentsMargins(0, 0, 0, 0)
        middle.setSpacing(0)
        self.rail = NavRail(self.tokens)
        self.rail.changed.connect(self.show_workspace)
        middle.addWidget(self.rail)
        self.stack = QStackedWidget()
        self.stack.setObjectName("workspaces")
        self.workspaces: dict[str, QWidget] = {}
        for ws in WORKSPACES:
            if ws.key == "projects":
                w = self._build_projects()
            elif ws.key == "tasks":
                w = self.tasks_view = TasksView(self.conn, self.tokens, now_fn=self._now_fn)
                w.changed.connect(self.reload)
                w.status.connect(lambda text, level: self.set_status(text, level))
                w.capture_requested.connect(self.open_capture)
            elif ws.key == "home":
                w = self.home_view = HomeView(self.tokens)
                w.open_workspace.connect(self.show_workspace)
                w.capture_requested.connect(self.open_capture)
                w.complete.connect(self.complete_item)
                w.open_node.connect(self.select_node)
            else:
                w = PlaceholderPage(ws.key, self.tokens)
                w.open_projects.connect(lambda: self.show_workspace("projects"))
                w.capture.connect(self.open_capture)
            w.setObjectName(f"workspace_{ws.key}")
            self.stack.addWidget(w)
            self.workspaces[ws.key] = w
        middle.addWidget(self.stack, 1)

        # the chat belongs to the shell: it opens beside any workspace
        self.chat = ChatPanel(self.cfg, self.tokens, self._ask_fn, **self._voice_kw)
        self.chat.close_requested.connect(lambda: self.set_chat_open(False))
        self.chat.answered.connect(self._on_answer)
        middle.addWidget(self.chat)

        outer.addLayout(middle, 1)
        outer.addWidget(self._build_ticker())
        self.setCentralWidget(page)

        # quick capture floats over everything (Ctrl+N)
        self.capture = CaptureOverlay(self.cfg, *self._capture_fns, parent=page)
        self.capture.captured.connect(self._on_captured)

    def _build_projects(self) -> QWidget:
        """The engineering cockpit: status line, then vitals / milestones / pulse /
        power, the graph with the briefing over it, and the inspector + stream."""
        ws = QWidget()
        lay = QVBoxLayout(ws)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._build_status_row())

        body = QHBoxLayout()
        body.setContentsMargins(12, 10, 12, 10)
        body.setSpacing(10)

        # left: vitals, issues, milestones, commit activity
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.setFixedWidth(LEFT_W)
        left = QWidget()
        left.setFixedWidth(LEFT_W - 6)
        self._left_inner = left
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(10)
        self.vitals = VitalsPanel()
        self.issues = IssuesPanel()
        self.issues.selected.connect(self.select_issue)
        self.issues.view_all.connect(lambda url: self.open_url(QUrl(url)))
        self.milestones = MilestonesPanel(self.tokens)
        self.milestones.picked.connect(self.pick_milestone)
        self.commits = CommitPanel(self.tokens)
        self.dash: Dashboard | None = None
        for w in (self.vitals, self.issues, self.milestones, self.commits):
            ll.addWidget(w)
        ll.addStretch(1)
        left_scroll.setWidget(left)
        self.left_column = left_scroll
        body.addWidget(left_scroll)

        # centre: the graph
        frame = QFrame()
        frame.setObjectName("panel")
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(1, 1, 1, 1)
        self.graph = BrainGraph(self.tokens)
        self.graph.hovered.connect(self._on_hover)
        self.graph.locked.connect(self._on_lock)
        self.hud = BriefingHud(self.tokens)
        self.hud.toggled.connect(lambda _on: self.stage.place())
        self.legend = self._build_legend()
        self.counts = mono(_label("", "panelHint"), 10)
        self.stage = _GraphStage(self.graph, self.hud, self.legend, self.counts)
        fl.addWidget(self.stage)
        body.addWidget(frame, 1)

        # right: inspector + stream (the chat takes this room when it is open)
        self.right = QWidget()
        self.right.setFixedWidth(RIGHT_W)
        rl = QVBoxLayout(self.right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(10)
        self.inspector = Inspector(self.tokens)
        self.inspector.copied.connect(
            lambda c: self.set_status(f"Copied the prompt for “{c.title}”. Paste it into Claude.",
                                      "ok"))
        self.inspector.done.connect(lambda c: self.mark_done(c.id))
        self.inspector.not_useful.connect(lambda c, r: self.mark_not_useful(c.id, r))
        self.inspector.open_node.connect(self.select_node)
        self.inspector.open_item.connect(self.open_item)
        self.inspector.complete_item.connect(self.complete_item)
        self.inspector.explain.connect(self.explain_finding)
        self.inspector.open_source.connect(lambda url: self.open_url(QUrl(url)))
        self.inspector.copy_prompt.connect(self._copy_finding_prompt)
        rl.addWidget(self.inspector, 3)
        self.stream = StreamPanel()
        self.stream.open_node.connect(self.select_node)
        rl.addWidget(self.stream, 2)
        body.addWidget(self.right)

        lay.addLayout(body, 1)
        return ws

    def _build_topbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("topBar")
        bar.setFixedHeight(48)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(18, 0, 12, 0)
        lay.setSpacing(10)
        self.logo = mono(_label("jarvis", "logo"), 15, 700)
        lay.addWidget(self.logo)
        self.crumb = mono(_label("", "crumb"), 12)
        lay.addWidget(self.crumb)
        lay.addSpacing(4)
        self.state_pill = pill("idle")
        lay.addWidget(self.state_pill, 0, Qt.AlignmentFlag.AlignVCenter)
        self.watch_dot = PulseDot()
        lay.addWidget(self.watch_dot)
        self.pulse_text = mono(_label("", "pulseText"), 11)
        lay.addWidget(self.pulse_text)
        lay.addStretch(1)
        self.clock = mono(_label("", "clock"), 12)
        lay.addWidget(self.clock)
        lay.addSpacing(6)
        self.refresh_btn = QPushButton(REFRESH_TEXT)
        self.refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_btn.setToolTip("Sync GitHub, run checks and rewrite the briefing (Ctrl+R)")
        self.refresh_btn.setIconSize(ICON_SIZE)
        self.refresh_btn.setIcon(refresh_icon(self.tokens.muted))
        fm = self.refresh_btn.fontMetrics()
        self.refresh_btn.setMinimumWidth(fm.horizontalAdvance(REFRESHING_TEXT) + 44)
        self.refresh_btn.clicked.connect(self.refresh)
        lay.addWidget(self.refresh_btn)
        self.chat_btn = QPushButton("✦ ask jarvis")
        self.chat_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chat_btn.setToolTip("Show or hide the chat (Ctrl+J; Ctrl+K to type a question)")
        self.chat_btn.clicked.connect(lambda: self.set_chat_open(not self.chat.isVisible()))
        lay.addWidget(self.chat_btn)
        return bar

    def _build_status_row(self) -> QWidget:
        row = QFrame()
        row.setObjectName("statusRow")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(13, 8, 13, 0)  # lines up with the panels below
        lay.setSpacing(10)
        self.status_text = mono(_label("", "statusText"), 11)
        lay.addWidget(self.status_text)
        self.status_pills = QHBoxLayout()
        self.status_pills.setSpacing(6)
        lay.addLayout(self.status_pills)
        lay.addStretch(1)
        return row

    def _build_legend(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.filter_buttons: dict[str, QPushButton] = {}
        for kind in FILTER_KINDS:
            b = QPushButton(f"● {FILTER_LABELS[kind]}")
            b.setProperty("full", b.text())
            b.setProperty("short", SHORT_LABELS.get(kind, FILTER_LABELS[kind]))
            b.setObjectName("filter")
            b.setCheckable(True)
            b.setChecked(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip(f"Show or hide {FILTER_LABELS[kind]} nodes")
            b.toggled.connect(self._apply_filters)
            b.setStyleSheet(f"QPushButton#filter:checked {{ color: {LEGEND_COLORS[kind]}; }}")
            lay.addWidget(b)
            self.filter_buttons[kind] = b
        # open items with no project or link stay out of the graph unless asked for
        self.unfiled_btn = QPushButton("+ unfiled")
        self.unfiled_btn.setObjectName("filter")
        self.unfiled_btn.setCheckable(True)
        self.unfiled_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.unfiled_btn.setProperty("full", "+ unfiled")
        self.unfiled_btn.setProperty("short", "+")
        self.unfiled_btn.toggled.connect(lambda _on: self.reload())
        self.unfiled_btn.hide()
        lay.addWidget(self.unfiled_btn)
        return w

    def _build_ticker(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("ticker")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(18, 6, 18, 7)
        lay.setSpacing(16)
        self.status_label = mono(_label("", "tickerText"), 11)
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        lay.addWidget(self.status_label, 1)
        self.hint_label = mono(_label(IDLE_HINT, "tickerHint"), 10)
        lay.addWidget(self.hint_label)
        return bar

    def _shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+R"), self, activated=self.refresh)
        QShortcut(QKeySequence("F5"), self, activated=self.refresh)
        QShortcut(QKeySequence("Ctrl+Q"), self, activated=self.close)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self._on_escape)
        QShortcut(QKeySequence("Ctrl+K"), self, activated=self.focus_chat)
        QShortcut(QKeySequence("Ctrl+J"), self,
                  activated=lambda: self.set_chat_open(not self.chat.isVisible()))
        QShortcut(QKeySequence("Ctrl+B"), self,
                  activated=lambda: self.hud.set_expanded(not self.hud.expanded))
        QShortcut(QKeySequence("Ctrl+Space"), self, activated=self.toggle_voice)
        QShortcut(QKeySequence("Ctrl+N"), self, activated=self.open_capture)
        for ws in WORKSPACES:
            QShortcut(QKeySequence(ws.shortcut), self,
                      activated=lambda k=ws.key: self.show_workspace(k))

    # ------------------------------------------------------------ workspaces

    @property
    def workspace(self) -> str:
        """The key of the workspace on screen ("home", "tasks", ..., "projects")."""
        current = self.stack.currentWidget()
        return next(k for k, w in self.workspaces.items() if w is current)

    def show_workspace(self, key: str) -> None:
        if key not in self.workspaces:
            raise KeyError(f"no workspace {key!r}")
        self.stack.setCurrentWidget(self.workspaces[key])
        self.rail.select(key)
        name = next(w.name for w in WORKSPACES if w.key == key).lower()
        if key == "projects":
            self.crumb.setText(f"/ {name} / {self.cfg.repo.split('/')[-1]}")
            self.crumb.setToolTip(self.cfg.repo)
        else:
            self.crumb.setText(f"/ {name}")
            self.crumb.setToolTip("")
        self.hint_label.setText(IDLE_HINT if key == "projects" else SHELL_HINT)
        if key == "projects":
            self.stage.place()

    # ------------------------------------------------------------ quick capture

    def open_capture(self) -> None:
        self._place_capture()
        self.capture.open()

    def _place_capture(self) -> None:
        page = self.centralWidget()
        w = min(CAPTURE_W, max(320, page.width() - 40))
        self.capture.setFixedWidth(w)
        self.capture.adjustSize()
        self.capture.move((page.width() - w) // 2, 64)

    def _fit_columns(self) -> None:
        """Narrow the cockpit's side columns in a small window; with the chat
        open (it takes the right column's place) and no room left for the graph,
        hide the left column too."""
        narrow = self.width() < NARROW_BELOW
        lw, rw = (LEFT_W_NARROW, RIGHT_W_NARROW) if narrow else (LEFT_W, RIGHT_W)
        if self.left_column.maximumWidth() != lw:
            self.left_column.setFixedWidth(lw)
            self._left_inner.setFixedWidth(lw - 6)
            self.right.setFixedWidth(rw)
        rail = RAIL_WIDE if self.width() >= WIDE_FROM else RAIL_COMPACT
        graph = self.width() - rail - PANEL_WIDTH - lw - 34   # body margins + spacing
        self.left_column.setVisible(self.chat.isHidden() or graph >= CHAT_GRAPH_MIN)

    def _on_captured(self, result: Any) -> None:
        from jarvis.capture import summary

        self.set_status(summary(result), "ok")
        self._place_capture()
        self.reload()  # Tasks and Home show it straight away

    # ------------------------------------------------------------ tasks + home

    def _project_name(self) -> str:
        from jarvis import items

        try:
            return items.get_project(self.conn, self.cfg.repo).name
        except (items.ItemError, sqlite3.Error):
            return self.cfg.repo.split("/")[-1]

    def _reload_life(self, data: Any, brain: Any) -> None:
        """Tasks and Home: items are cheap to read, and each view rebuilds only
        when its own data changed."""
        self.tasks_view.reload()
        try:
            home = build_home(self.conn, data, brain, self._now_fn(),
                              project_name=self._project_name())
        except sqlite3.Error as e:
            self.set_status(f"Could not read Home: {e}", "warn", sticky=True)
            return
        self.home_view.set_data(home)

    def open_item(self, item_id: int) -> None:
        """Edit an item in Tasks (from the graph's inspector). Filters that would
        hide it are cleared first."""
        self.show_workspace("tasks")
        tv = self.tasks_view
        if item_id not in tv.rows:
            tv.set_kind("all")
            tv.set_project(None)
        tv.select(item_id)

    def complete_item(self, item_id: int) -> None:
        """Tick an item off (from Home or the graph's inspector)."""
        from jarvis import items

        try:
            item = items.complete_item(self.conn, item_id)
        except items.ItemError as e:
            self.set_status(f"Not changed: {e}", "warn")
            return
        self.set_status(f"Done: “{item.title}”", "ok")
        self.reload()

    # ------------------------------------------------------------ chat

    def set_chat_open(self, open_: bool, focus: bool = True) -> None:
        self.chat.setVisible(open_)
        self.right.setVisible(not open_)
        self._fit_columns()
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
        """Esc closes quick capture first, then stops voice, then releases a locked
        node, then closes the chat."""
        if not self.capture.isHidden():
            self.capture.close_overlay()
            return
        if self.chat.stop_voice():
            return
        if self.graph.lock_id:
            self.graph.lock(None)
            return
        if self.chat.isVisible():
            self.set_chat_open(False)

    def _on_answer(self, answer: Any) -> None:
        refs = [getattr(s, "ref", "") for s in (getattr(answer, "cited_sources", None)
                                                 or getattr(answer, "sources", None) or [])]
        self.graph.set_highlight({n for n in (ref_to_node(r) for r in refs) if n})

    # ------------------------------------------------------------ graph / inspector

    def _apply_filters(self) -> None:
        hidden = {k for k, b in self.filter_buttons.items() if not b.isChecked()}
        self.graph.set_hidden_kinds(hidden)

    # ------------------------------------------------------------ dashboard

    @property
    def chosen_milestone(self) -> int | None:
        """The milestone the user made active (a UI preference), None = nearest due."""
        v = self.chat.settings.value(ACTIVE_MILESTONE_KEY)
        try:
            return int(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def pick_milestone(self, number: int) -> None:
        """Make a milestone active (-1: back to the nearest-due one); Vitals,
        Issues and Milestones follow."""
        if number < 0:
            self.chat.settings.remove(ACTIVE_MILESTONE_KEY)
        else:
            self.chat.settings.setValue(ACTIVE_MILESTONE_KEY, int(number))
        self.chat.settings.sync()
        self.milestones.expanded = False
        self._render_dashboard(force=True)

    def _render_dashboard(self, force: bool = False, animate: bool = False) -> None:
        timeline = self.data.timeline if self.data is not None else None
        try:
            dash = build_dashboard(self.conn, self.cfg.repo, timeline, self.chosen_milestone,
                                   self._now_fn().date())
        except sqlite3.Error:
            dash = None
        if dash == self.dash and not force:
            return
        self.dash = dash
        self.vitals.set_data(dash, animate=animate)
        self.issues.set_data(dash)
        self.milestones.set_data(dash)

    def select_issue(self, number: int) -> None:
        """An issue from the Issues tile: the inspector when it is in the graph,
        else its GitHub page."""
        nid = f"issue:{number}"
        if self.graph.graph.node(nid) is not None:
            self.select_node(nid)
            return
        row = next((r for r in (self.dash.issues if self.dash else ()) if r.number == number),
                   None)
        if row is not None and row.url:
            self.open_url(QUrl(row.url))

    def select_node(self, node_id: str) -> None:
        """Lock a node (from the stream or the inspector's links)."""
        self.show_workspace("projects")
        if self.chat.isVisible():
            self.set_chat_open(False)
        kind = node_id.split(":")[0]
        if kind in self.filter_buttons and not self.filter_buttons[kind].isChecked():
            self.filter_buttons[kind].setChecked(True)
        self.graph.lock(node_id)

    def _on_hover(self, node_id: str | None) -> None:
        if self.graph.lock_id:
            return
        self._inspect(node_id, locked=False)

    def _on_lock(self, node_id: str | None) -> None:
        self._inspect(node_id or self.graph.hover_id, locked=bool(node_id))

    def _inspect(self, node_id: str | None, locked: bool) -> None:
        g = self.graph.graph
        node = g.node(node_id) if node_id else None
        card = None
        if node is not None and node.card_id is not None:
            try:
                card = cards_mod.get_card(self.conn, node.card_id)
            except sqlite3.Error:
                card = None
        finding = None
        check_id = (node.id[6:] if node is not None and node.kind == "check"
                    else card.check_id if card is not None else None)
        if check_id:
            try:
                finding = load_finding(self.conn, self.cfg, check_id)
            except (sqlite3.Error, OSError, ValueError):
                finding = None   # the inspector still shows the node
        self.inspector.show_node(node, g, card, locked, finding=finding)

    # ------------------------------------------------------------ finding actions

    def explain_finding(self, question: str) -> None:
        """Explain: the chat answers it from the synced data (as a typed question)."""
        self.set_chat_open(True, focus=False)
        if self.chat.turn_busy:
            self.set_status("Jarvis is still answering; ask again when it's done.", "warn")
            return
        self.chat.ask(question)

    def _copy_finding_prompt(self, prompt: str) -> None:
        QGuiApplication.clipboard().setText(prompt)
        self.set_status("Copied a fix prompt for Claude. Nothing was changed.", "ok")

    # ------------------------------------------------------------ data

    def reload(self, force: bool = False, animate: bool = True) -> None:
        """Re-read the DB; rebuild only the parts whose data changed."""
        try:
            new = load_window_data(self.conn, self.cfg.repo,
                                   hardware_path=self.cfg.hardware_path)
            brain = load_brain(self.conn, self.cfg.repo, self._now_fn(),
                               loose=self.unfiled_btn.isChecked())
        except sqlite3.Error as e:
            self.set_status(f"Could not read the database: {e}", "warn", sticky=True)
            return
        old, old_brain = self.data, self.brain
        self.data, self.brain = new, brain
        anim = animate and motion.enabled()

        def changed(attr: str) -> bool:
            return force or old is None or getattr(new, attr) != getattr(old, attr)

        def bchanged(attr: str) -> bool:
            return force or old_brain is None or getattr(brain, attr) != getattr(old_brain, attr)

        self._render_meta()
        if changed("briefing"):
            self.hud.set_data(new.briefing, animate=anim)
            self.stage.place()
        self._render_dashboard(force=force or old is None, animate=anim)
        if changed("grid"):
            self.commits.set_data(new.grid)
        if bchanged("graph"):
            self.graph.set_data(brain.graph, animate=anim)
            g = brain.graph
            shown_loose = g.count("item") and any(n.id == "hub:unfiled" for n in g.nodes)
            self.unfiled_btn.setVisible(bool(g.loose or shown_loose))
            self.unfiled_btn.setToolTip(
                f"Show open items with no project or link ({g.loose} hidden)" if g.loose
                else "Hide the unfiled items")
            self.counts.setText(f"{len(g.nodes)} nodes · {len(g.edges)} links")
            self.stage.place()
            sel = self.graph.lock_id or self.graph.hover_id
            self._inspect(sel, locked=bool(self.graph.lock_id))
        if bchanged("stream"):
            self.stream.set_data(brain.stream, animate=anim and old_brain is not None)
            if not self._status_sticky and not self._status_timer.isActive():
                self._idle_ticker()
        self._reload_life(new, brain)
        errors = new.errors + brain.errors
        if errors:
            self.set_status(" · ".join(errors), "warn", sticky=True)

    def _render_meta(self) -> None:
        d, b = self.data, self.brain
        watch = d.watch if d else None
        watching = bool(watch and watch.watching)
        self.watch_dot.set_color(self.tokens.signal if watching else self.tokens.faint)
        self.watch_dot.set_breathing(watching)
        self.watch_dot.setToolTip(watch.tooltip if watch else "")
        self.pulse_text.setText(watch.text if watch else "not watching")
        self.pulse_text.setToolTip(watch.tooltip if watch else "")
        self._render_state()
        # status line: what was synced, and what is clean
        parts, tips = [], []
        if d and d.last_sync:
            dt = _parse(d.last_sync)
            parts.append(f"synced {ago(dt, datetime.now(UTC))}")
            tips.append(f"Last GitHub sync: {local_stamp(d.last_sync)}")
        if d and d.briefing:
            parts.append(f"briefing by {d.briefing.source_label}")
            tips.append(f"Briefing written {local_stamp(d.briefing.created_at)}")
        self.status_text.setText(" · ".join(parts) if parts else "not synced yet")
        self.status_text.setToolTip("\n".join(tips))
        while self.status_pills.count():
            it = self.status_pills.takeAt(0)
            if it.widget():
                it.widget().hide()  # gone now, not when the loop gets to it
                it.widget().deleteLater()
        if b is not None:
            fails = b.graph.count("check") and sum(
                1 for n in b.graph.nodes if n.kind == "check" and n.status == "fail")
            unsure = sum(1 for n in b.graph.nodes if n.kind == "check"
                         and n.status in ("unknown", "warn"))
            cards = b.graph.count("card")
            if fails:
                self.status_pills.addWidget(pill(f"checks {fails} failing", level="fail"))
            elif unsure:
                self.status_pills.addWidget(pill(f"checks {unsure} unconfirmed"))
            elif b.graph.count("check"):
                self.status_pills.addWidget(pill("checks clean ✓", live=True))
            elif d is not None and d.power_note:
                # no (valid) hardware.yaml: say why there are no electrical checks
                hw = pill("no hardware checks")
                hw.setToolTip(d.power_note)
                self.status_pills.addWidget(hw)
            if cards:
                self.status_pills.addWidget(pill(f"{cards} suggestion{'s' if cards != 1 else ''}",
                                                 live=True))
            if d and d.briefing and d.briefing.source == "model":
                self.status_pills.addWidget(pill("briefing verified ✓", live=True))

    def _render_state(self) -> None:
        if self._refreshing:
            set_pill(self.state_pill, "syncing", live=True)
        elif self.chat.turn_busy:
            set_pill(self.state_pill, "thinking", live=True)
        elif self.data and self.data.watch and self.data.watch.watching:
            set_pill(self.state_pill, "watching", live=True)
        else:
            set_pill(self.state_pill, "idle")

    def _tick_clock(self) -> None:
        self.clock.setText(datetime.now().strftime("%H:%M:%S"))
        self._render_state()

    # ------------------------------------------------------------ card actions

    def mark_done(self, card_id: int) -> None:
        card = cards_mod.get_card(self.conn, card_id)
        title = card.title if card else f"card {card_id}"
        self._close_card(lambda: cards_mod.mark_done(self.conn, card_id), f"Marked done: “{title}”")

    def mark_not_useful(self, card_id: int, reason: str) -> None:
        card = cards_mod.get_card(self.conn, card_id)
        title = card.title if card else f"card {card_id}"
        self._close_card(lambda: cards_mod.set_feedback(self.conn, card_id, reason),
                         f"Dismissed as {reason}: “{title}”. Thanks, noted.")

    def _close_card(self, write: Callable[[], None], message: str) -> None:
        try:
            write()
        except (sqlite3.Error, ValueError) as e:
            self.set_status(f"Could not save that: {e}", "warn", sticky=True)
            return
        self.graph.lock(None)
        self.set_status(message, "ok")
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
        self.refresh_btn.setText(REFRESHING_TEXT)
        self._render_state()
        self._sync_spinner()
        self.set_status("Syncing GitHub and writing the briefing. The model can take a while.",
                        "idle", sticky=True)
        self._thread = threading.Thread(
            target=_run_refresh, args=(self.cfg, self.refresh_fn, self._bridge),
            name="jarvis-refresh", daemon=True,
        )
        self._thread.start()

    def _spin(self) -> None:
        if not self.isVisible() or self.isMinimized():
            return
        self._spin_angle = (self._spin_angle + SPIN_DEG_PER_S * SPIN_MS / 1000) % 360
        self.refresh_btn.setIcon(refresh_icon(self.tokens.signal, self._spin_angle))

    def _end_refresh(self) -> None:
        self._refreshing = False
        self._spin_timer.stop()
        self._spin_angle = 0.0
        self.refresh_btn.setIcon(refresh_icon(self.tokens.muted))
        self.refresh_btn.setText(REFRESH_TEXT)
        self.refresh_btn.setEnabled(True)
        self._render_state()

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

    # ------------------------------------------------------------ ticker

    def set_status(self, text: str, level: str = "idle", sticky: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setToolTip(text)
        self.status_label.setProperty("level", level)
        repolish(self.status_label)
        self._status_sticky = sticky
        self._status_timer.stop()
        if not sticky:
            self._status_timer.start()

    def _idle_ticker(self) -> None:
        """Back to the newest stream event."""
        self._status_sticky = False
        item = self.brain.stream[0] if self.brain and self.brain.stream else None
        text = f"[origin:{item.origin}] {item.text}" if item else "nothing new yet"
        self.status_label.setText(text)
        self.status_label.setToolTip(text)
        self.status_label.setProperty("level", "idle")
        repolish(self.status_label)

    # ------------------------------------------------------------ theme (dark only)

    def apply_scheme(self, scheme: str | None) -> None:
        """Kept for older callers: there is one (dark) theme."""
        self.tokens = theme.DARK

    # ------------------------------------------------------------ watch

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
        self.stage.place()

    def resizeEvent(self, e) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(e)
        wide = self.width() >= WIDE_FROM
        if wide != self.rail.wide:
            self.rail.set_wide(wide)
        self._fit_columns()
        if not self.capture.isHidden():
            self._place_capture()

    def hideEvent(self, e) -> None:  # noqa: N802 - Qt API
        super().hideEvent(e)
        self._spin_timer.stop()

    def changeEvent(self, e) -> None:  # noqa: N802 - Qt API
        super().changeEvent(e)
        if e.type() == QEvent.Type.WindowStateChange:
            self._sync_spinner()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        self.chat.shutdown()
        if self._owns_conn:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass
        super().closeEvent(event)
