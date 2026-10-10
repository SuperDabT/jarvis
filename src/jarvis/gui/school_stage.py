"""School's stage: a local web page (Qt WebEngine) that draws the visuals.

The page and its scripts are files in ``gui/stage_web/``, served through the
private ``jarvis-stage:`` scheme (Chromium refuses ES-module imports from
``file:`` pages, and a scheme serving one folder is narrower anyway). The page
runs on an off-the-record profile of its own: it can't navigate anywhere else
(a clicked web link opens in the system browser), downloads are refused and
permission requests (camera, microphone, location) denied.

Python computes; the page only draws. Python calls the page's ``stage`` API
with ``runJavaScript``; calls made before the page has loaded wait for it.

WebEngine needs two things before the QApplication exists: its widgets module
imported (it shares OpenGL contexts) and the scheme registered. ``prepare()``
does both; ``app.make_app`` and the tests' conftest call it.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from jarvis.gui import motion
from jarvis.gui.home_style import PALETTE, HomePalette

log = logging.getLogger(__name__)

SCHEME = "jarvis-stage"
ROOT = Path(__file__).with_name("stage_web")
PAGE = "stage.html"
_TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css",
          ".svg": "image/svg+xml", ".png": "image/png", ".json": "application/json"}
_prepared: bool | None = None


def prepare() -> bool:
    """Import WebEngine and register the stage scheme; call before QApplication.
    False when WebEngine isn't available (the stage then says so)."""
    global _prepared
    if _prepared is not None:
        return _prepared
    try:
        import PySide6.QtWebEngineWidgets  # noqa: F401 - must load before the app
        from PySide6.QtWebEngineCore import QWebEngineUrlScheme
    except ImportError:
        _prepared = False
        return False
    s = QWebEngineUrlScheme(SCHEME.encode())
    s.setSyntax(QWebEngineUrlScheme.Syntax.Path)
    s.setFlags(QWebEngineUrlScheme.Flag.SecureScheme | QWebEngineUrlScheme.Flag.LocalScheme
               | QWebEngineUrlScheme.Flag.CorsEnabled)
    QWebEngineUrlScheme.registerScheme(s)
    _prepared = True
    return True


def theme_css(p: HomePalette = PALETTE) -> str:
    """The navy palette as CSS variables (served as ``theme.css``)."""
    names = ("bg", "card", "card_hi", "border", "border_hi", "text", "muted", "faint",
             "accent", "green", "amber", "red", "blue", "purple", "grey")
    body = "".join(f"  --{n.replace('_', '-')}: {getattr(p, n)};\n" for n in names)
    try:                          # the app's reading font, when Qt can say which
        from jarvis.gui import theme
        body += f'  --font: "{theme.family("sans")}";\n'
    except Exception:  # noqa: BLE001 - the page falls back to the system sans
        pass
    return ":root {\n" + body + "}\n"


def resolve(path: str) -> tuple[bytes, str] | None:
    """The bytes and type served for a page path, or None. Only files inside the
    stage folder (and the generated theme) are ever served."""
    path = path.lstrip("/")
    if path == "theme.css":
        return theme_css().encode(), "text/css"
    f = (ROOT / path).resolve()
    if ROOT.resolve() not in f.parents or not f.is_file():
        return None
    return f.read_bytes(), _TYPES.get(f.suffix, "application/octet-stream")


def navigation(url: str, clicked: bool) -> str:
    """What the stage does with a navigation: "load" (its own pages), "external"
    (a clicked web link: the system browser) or "refuse" (anything else)."""
    u = QUrl(url)
    if u.scheme() == SCHEME:
        return "load"
    if clicked and u.scheme() in ("http", "https"):
        return "external"
    return "refuse"


def _web_classes() -> tuple[type, type]:
    """The WebEngine subclasses, defined on first use (the import is heavy)."""
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineUrlSchemeHandler

    class Handler(QWebEngineUrlSchemeHandler):
        def requestStarted(self, job) -> None:  # noqa: N802 - Qt API
            got = resolve(job.requestUrl().path())
            if got is None:
                job.fail(job.Error.UrlNotFound)
                return
            data, kind = got
            buf = QBuffer(job)
            buf.setData(QByteArray(data))
            buf.open(QIODevice.OpenModeFlag.ReadOnly)
            job.reply(kind.encode(), buf)

    class Page(QWebEnginePage):
        def __init__(self, profile, parent=None):
            super().__init__(profile, parent)
            self.errors: list[str] = []

        def acceptNavigationRequest(self, url, kind, main_frame) -> bool:  # noqa: N802
            clicked = kind == QWebEnginePage.NavigationType.NavigationTypeLinkClicked
            what = navigation(url.toString(), clicked)
            if what == "external":
                QDesktopServices.openUrl(url)
            return what == "load"

        def javaScriptConsoleMessage(self, level, message, line, source) -> None:  # noqa: N802
            if level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
                self.errors.append(f"{message} ({source}:{line})")
            log.debug("stage js: %s (%s:%s)", message, source, line)

    return Handler, Page


class SchoolStage(QWidget):
    """The stage widget: the page, its API calls, and a plain message when
    WebEngine can't run here."""

    loaded = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("scStageView")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.ready = False
        self._queue: list[str] = []
        self.view = None
        self.page = None
        if not prepare():
            msg = QLabel("The visual stage needs Qt WebEngine, which isn't installed.")
            msg.setObjectName("hEmpty")
            msg.setWordWrap(True)
            lay.addWidget(msg)
            return
        from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEngineSettings
        from PySide6.QtWebEngineWidgets import QWebEngineView

        handler_cls, page_cls = _web_classes()
        self.profile = QWebEngineProfile()       # no storage name: off the record
        self._handler = handler_cls(self.profile)
        self.profile.installUrlSchemeHandler(SCHEME.encode(), self._handler)
        self.profile.downloadRequested.connect(lambda item: item.cancel())
        self.page = page_cls(self.profile, self)
        self.profile.setParent(self.page)        # the page must go before its profile
        self.page.setBackgroundColor(QColor(PALETTE.card))   # never a white flash
        self.page.permissionRequested.connect(lambda perm: perm.deny())
        s = self.page.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        s.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, False)
        self.view = QWebEngineView(self)
        self.view.setPage(self.page)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.page.loadFinished.connect(self._loaded)
        lay.addWidget(self.view)
        motion_on = "1" if motion.enabled() else "0"
        self.view.load(QUrl(f"{SCHEME}:/{PAGE}?motion={motion_on}"))

    def _loaded(self, ok: bool) -> None:
        self.ready = ok
        if ok:
            for js in self._queue:
                self.page.runJavaScript(js)
            self._queue.clear()
        else:
            log.warning("the School stage page failed to load")
        self.loaded.emit(ok)

    def call(self, fn: str, *args: Any) -> None:
        """``stage.<fn>(*args)`` in the page (JSON arguments); queued until it loads."""
        js = f"window.stage && window.stage.{fn}({', '.join(json.dumps(a) for a in args)});"
        if self.page is None:
            return
        if self.ready:
            self.page.runJavaScript(js)
        else:
            self._queue.append(js)

    def show_scene(self, scene: str, data: dict[str, Any]) -> None:
        self.call("show", scene, data)

    def clear(self) -> None:
        self.call("clear")
