"""The main window: header, briefing, suggestions, status line."""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QFileSystemWatcher, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from jarvis import cards as cards_mod
from jarvis import db
from jarvis.config import Config
from jarvis.gui import theme
from jarvis.gui.data import (
    CardGroup,
    WindowData,
    describe_error,
    load_window_data,
    local_stamp,
    refresh_message,
)
from jarvis.gui.widgets import (
    TEMPLATE_TIP,
    BriefingView,
    CardWidget,
    _clear,
    _label,
    repolish,
)
from jarvis.lock import RefreshBusy, refresh_lock

DEFAULT_SIZE = (720, 860)
CONTENT_MAX_WIDTH = 760
POLL_MS = 30_000
DEBOUNCE_MS = 400
STATUS_CLEAR_MS = 8_000
IDLE_HINT = "Ctrl+R refresh  ·  Esc close"
EMPTY_CARDS = "No open suggestions."
BUSY_TEXT = "A refresh is already running in the background"

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
    ) -> None:
        super().__init__()
        self.cfg = cfg or Config.from_env()
        self.conn = conn or db.connect(self.cfg.db_path)
        self._owns_conn = conn is None
        self.refresh_fn = refresh_fn or _default_refresh
        self.scheme = scheme  # None = follow the system
        self.tokens = theme.tokens_for(scheme)
        self.data: WindowData | None = None
        self.card_widgets: dict[int, CardWidget] = {}
        self._refreshing = False
        self._thread: threading.Thread | None = None
        self._bridge = _Bridge(self)
        self._bridge.succeeded.connect(self._on_refresh_ok)
        self._bridge.failed.connect(self._on_refresh_failed)
        self._bridge.busy.connect(self._on_refresh_busy)

        self.setWindowTitle(f"Jarvis · {self.cfg.repo.split('/')[-1]}")
        self.resize(*DEFAULT_SIZE)
        self.setMinimumSize(460, 400)
        self._build()
        self._shortcuts()

        self._status_timer = QTimer(self, singleShot=True, interval=STATUS_CLEAR_MS)
        self._status_timer.timeout.connect(lambda: self.set_status(IDLE_HINT, "idle"))
        self._spin_timer = QTimer(self, interval=350)
        self._spin_timer.timeout.connect(self._spin)
        self._spin_step = 0

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
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        hl = QHBoxLayout(holder)
        hl.setContentsMargins(28, 26, 28, 30)
        column = QWidget()
        column.setMaximumWidth(CONTENT_MAX_WIDTH)
        hl.addWidget(column)
        self.col = QVBoxLayout(column)
        self.col.setContentsMargins(0, 0, 0, 0)
        self.col.setSpacing(0)
        self.scroll.setWidget(holder)
        outer.addWidget(self.scroll, 1)

        # header
        header = QHBoxLayout()
        header.setSpacing(12)
        titles = QVBoxLayout()
        titles.setSpacing(4)
        name_row = QHBoxLayout()
        name_row.setSpacing(10)
        self.title_label = _label("Jarvis", "appTitle")
        self.repo_label = _label(self.cfg.repo, "repoName")
        name_row.addWidget(self.title_label, 0, Qt.AlignmentFlag.AlignBaseline)
        name_row.addWidget(self.repo_label, 0, Qt.AlignmentFlag.AlignBaseline)
        name_row.addStretch(1)
        titles.addLayout(name_row)
        self.meta_label = _label("", "meta")
        titles.addWidget(self.meta_label)
        self.watch_label = _label("", "meta")
        self.watch_label.setVisible(False)
        titles.addWidget(self.watch_label)
        header.addLayout(titles, 1)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setObjectName("refresh")
        self.refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_btn.setToolTip("Sync GitHub, run checks and rewrite the briefing (Ctrl+R)")
        fm = self.refresh_btn.fontMetrics()
        self.refresh_btn.setMinimumWidth(fm.horizontalAdvance("Refreshing...") + 36)
        self.refresh_btn.clicked.connect(self.refresh)
        header.addWidget(self.refresh_btn, 0, Qt.AlignmentFlag.AlignTop)
        self.col.addLayout(header)
        self.col.addSpacing(22)

        # briefing
        self.briefing_view = BriefingView(self.tokens)
        self.col.addWidget(self.briefing_view)
        self.col.addSpacing(36)

        # suggestions
        sec = QHBoxLayout()
        sec.setSpacing(10)
        sec.setContentsMargins(4, 0, 4, 0)
        sec.addWidget(_label("Suggestions", "sectionTitle"), 0, Qt.AlignmentFlag.AlignBaseline)
        self.count_label = _label("", "sectionCount")
        sec.addWidget(self.count_label, 0, Qt.AlignmentFlag.AlignBaseline)
        sec.addStretch(1)
        self.col.addLayout(sec)
        self.col.addSpacing(14)
        self.cards_box = QVBoxLayout()
        self.cards_box.setSpacing(12)
        self.col.addLayout(self.cards_box)
        self.col.addStretch(1)

        # status line
        bar = QFrame()
        bar.setObjectName("statusBar")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(28, 8, 28, 9)
        self.status_label = _label(IDLE_HINT, "status")
        self.status_label.setWordWrap(True)
        bl.addWidget(self.status_label, 1)
        outer.addWidget(bar)

        self.setCentralWidget(page)

    def _shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+R"), self, activated=self.refresh)
        QShortcut(QKeySequence("F5"), self, activated=self.refresh)
        QShortcut(QKeySequence("Ctrl+Q"), self, activated=self.close)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self.close)

    # ------------------------------------------------------------ data

    def reload(self, force: bool = False) -> None:
        """Re-read the DB; rebuild only the parts whose data changed."""
        try:
            new = load_window_data(self.conn, self.cfg.repo)
        except sqlite3.Error as e:
            self.set_status(f"Could not read the database: {e}", "warn", sticky=True)
            return
        old = self.data
        if not force and new == old:
            return
        self.data = new
        self._render_meta()
        if force or old is None or new.briefing != old.briefing:
            self.briefing_view.set_data(new.briefing, self.tokens)
        if force or old is None or new.groups != old.groups:
            self._render_cards(new.groups)
        if new.errors:
            self.set_status(" · ".join(new.errors), "warn", sticky=True)

    def _render_meta(self) -> None:
        d = self.data
        parts, tips = [], []
        if d and d.updated_at:
            parts.append(f"updated {local_stamp(d.updated_at)}")
        if d and d.last_sync:
            tips.append(f"Last GitHub sync: {local_stamp(d.last_sync)}")
        if d and d.briefing:
            parts.append(d.briefing.source_label)
            tips.append(f"Briefing written: {local_stamp(d.briefing.created_at)} "
                        f"by {d.briefing.source_label}")
            if d.briefing.source == "template":
                tips.append(TEMPLATE_TIP)
        self.meta_label.setText("  ·  ".join(parts) if parts else "not synced yet")
        self.meta_label.setToolTip("\n".join(tips))
        watch = d.watch if d else None
        self.watch_label.setText(watch.text if watch else "")
        self.watch_label.setToolTip(watch.tooltip if watch else "")
        self.watch_label.setVisible(bool(watch and watch.text))

    def _render_cards(self, groups: tuple[CardGroup, ...]) -> None:
        _clear(self.cards_box)
        self.card_widgets = {}
        self.group_titles: dict[str, QLabel] = {}
        count = sum(len(g.cards) for g in groups)
        self.count_label.setText(str(count) if count else "")
        if not groups:
            self.empty_label = _label(EMPTY_CARDS, "emptyText", wrap=True)
            self.empty_label.setContentsMargins(4, 0, 4, 0)
            self.cards_box.addWidget(self.empty_label)
            return
        first = True
        for g in groups:
            if not first:
                self.cards_box.addSpacing(14)
            first = False
            title = _label(g.label.upper(), "groupTitle")
            title.setContentsMargins(4, 0, 4, 0)
            self.group_titles[g.type] = title
            self.cards_box.addWidget(title)
            for view in g.cards:
                w = CardWidget(view, self.tokens)
                w.copied.connect(self._on_copied)
                w.done.connect(self.mark_done)
                w.not_useful.connect(self.mark_not_useful)
                self.card_widgets[view.id] = w
                self.cards_box.addWidget(w)

    # ------------------------------------------------------------ card actions

    def _on_copied(self, card) -> None:
        self.set_status(f"Copied the prompt for “{card.title}”. Paste it into Claude.",
                        "ok")

    def mark_done(self, card) -> None:
        self._close_card(card, lambda: cards_mod.mark_done(self.conn, card.id),
                         f"Marked done: “{card.title}”")

    def mark_not_useful(self, card, reason: str) -> None:
        self._close_card(card, lambda: cards_mod.set_feedback(self.conn, card.id, reason),
                         f"Dismissed as {reason}: “{card.title}”. Thanks, noted.")

    def _close_card(self, card, write: Callable[[], None], message: str) -> None:
        try:
            write()
        except (sqlite3.Error, ValueError) as e:
            self.set_status(f"Could not save that: {e}", "warn", sticky=True)
            return
        self._remove_card_widget(card)
        self.set_status(message, "ok")
        # Bring the cached view model in line so the watcher's reload is a no-op.
        try:
            self.data = load_window_data(self.conn, self.cfg.repo)
        except sqlite3.Error:
            pass

    def _remove_card_widget(self, card) -> None:
        w = self.card_widgets.pop(card.id, None)
        if w is not None:
            self.cards_box.removeWidget(w)
            w.setParent(None)
            w.deleteLater()
        left_of_type = [c for c in self.card_widgets.values() if c.card.type == card.type]
        if not left_of_type and card.type in getattr(self, "group_titles", {}):
            title = self.group_titles.pop(card.type)
            idx = self.cards_box.indexOf(title)
            self.cards_box.removeWidget(title)
            title.setParent(None)
            title.deleteLater()
            # drop the spacer that preceded this group (or now leads the list)
            for i in (idx - 1, 0):
                if 0 <= i < self.cards_box.count():
                    item = self.cards_box.itemAt(i)
                    if item is not None and item.spacerItem() is not None:
                        self.cards_box.takeAt(i)
                        break
        n = len(self.card_widgets)
        self.count_label.setText(str(n) if n else "")
        if not n:
            self._render_cards(())

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
        self._spin()
        self._spin_timer.start()
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
        self.refresh_btn.setText("Refreshing" + "." * (self._spin_step % 4))
        self._spin_step += 1

    def _end_refresh(self) -> None:
        self._refreshing = False
        self._spin_timer.stop()
        self.refresh_btn.setText("Refresh")
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
        self.status_label.setProperty("level", level)
        repolish(self.status_label)
        self._status_timer.stop()
        if not sticky and text != IDLE_HINT:
            self._status_timer.start()

    def _on_scheme_changed(self, *_: Any) -> None:
        app = QGuiApplication.instance()
        self.tokens = theme.apply(app)
        self.reload(force=True)

    def apply_scheme(self, scheme: str | None) -> None:
        self.scheme = scheme
        self.tokens = theme.apply(QGuiApplication.instance(), scheme)
        self.reload(force=True)

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
        self._watcher.fileChanged.connect(self._on_fs_change)
        self._watcher.directoryChanged.connect(self._on_fs_change)

    def _on_fs_change(self, *_: Any) -> None:
        # Files can be replaced (WAL reset, checkpoint); re-add any that went missing.
        if self._watcher is not None:
            watched = set(self._watcher.files())
            missing = [f for f in self._watch_paths() if f not in watched]
            if missing:
                self._watcher.addPaths(missing)
        self._debounce.start()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        if self._owns_conn:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass
        super().closeEvent(event)
