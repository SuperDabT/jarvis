"""Entry point for the Jarvis window: ``jarvis-window``."""
from __future__ import annotations

import argparse
import sys

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from jarvis.config import Config
from jarvis.gui import motion, theme

APP_ID = "jarvis"  # app id on Wayland; target it in Hyprland with class:^(jarvis)$


def make_app(argv: list[str] | None = None, scheme: str | None = None) -> QApplication:
    """Create (or reuse) the QApplication with name, font and theme set."""
    app = QApplication.instance()
    if app is None:
        QGuiApplication.setApplicationName(APP_ID)
        QGuiApplication.setApplicationDisplayName("Jarvis")
        QGuiApplication.setDesktopFileName(APP_ID)
        app = QApplication(argv if argv is not None else sys.argv[:1])
    app.setFont(theme.pick_font())
    theme.apply(app, scheme)
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis-window", description="The Jarvis window.")
    parser.add_argument("--no-refresh", action="store_true",
                        help="show cached data only; do not refresh on open")
    parser.add_argument("--theme", choices=("system", "light", "dark"), default="system")
    parser.add_argument("--no-animations", action="store_true",
                        help="reduce motion: no animations (also JARVIS_REDUCE_MOTION=1)")
    args = parser.parse_args(argv)
    if args.no_animations:
        motion.set_enabled(False)
    scheme = None if args.theme == "system" else args.theme

    app = make_app(scheme=scheme)
    from jarvis.gui.window import MainWindow  # after QApplication exists

    win = MainWindow(Config.from_env(), auto_refresh=not args.no_refresh, scheme=scheme)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
