"""Shared test setup: Qt offscreen, reduced motion, and no real database.

GUI tests must never open real windows (a desktop session sets
QT_QPA_PLATFORM=wayland), and animations are off by default so tests are
deterministic; tests/test_gui_motion.py turns motion on where it checks it.

The user's database lives in ~/.local/share/jarvis. ``sqlite3.connect`` is
wrapped so that any connection to a file there raises instead (Config()'s
default db_path points there), and JARVIS_DB points child processes at a
scratch file.
"""
import os
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["JARVIS_REDUCE_MOTION"] = "1"
# UI settings (auto-listen) go to a scratch file, never the user's ~/.config/jarvis.
os.environ["JARVIS_UI_SETTINGS"] = os.path.join(tempfile.mkdtemp(prefix="jarvis-ui-"),
                                                "ui.ini")
os.environ["JARVIS_DB"] = os.path.join(tempfile.mkdtemp(prefix="jarvis-db-"), "jarvis.db")
# Values Jarvis reads from ~/.config/jarvis/env (the name, the Canvas link) come from an
# empty scratch file, never the user's (which also holds their tokens).
os.environ.pop("JARVIS_NAME", None)
os.environ.pop("JARVIS_CANVAS_ICS", None)
import jarvis.config as _config  # noqa: E402

_config.ENV_FILE = Path(tempfile.mkdtemp(prefix="jarvis-env-")) / "env"

REAL_DATA_DIR = Path("~/.local/share/jarvis").expanduser().resolve()
_real_connect = sqlite3.connect
# Every refused attempt, so one swallowed by a broad ``except`` (a worker thread)
# still fails the run in pytest_sessionfinish. test_isolation's own attempts are
# expected and cleared there.
REFUSED: list[Path] = []


def db_file(database, uri: bool = False) -> Path | None:
    """The file a sqlite3.connect() argument opens, or None for in-memory."""
    target = os.fsdecode(database) if isinstance(database, (bytes, os.PathLike)) else str(database)
    if uri or target.startswith("file:"):
        target = unquote(urlsplit(target).path)
    if target in ("", ":memory:"):
        return None
    return Path(target).expanduser().resolve()


def _guarded_connect(database, *args, **kwargs):
    f = db_file(database, kwargs.get("uri", False))
    if f is not None and (f == REAL_DATA_DIR or REAL_DATA_DIR in f.parents):
        REFUSED.append(f)
        raise RuntimeError(f"test tried to open the real Jarvis database: {f}")
    return _real_connect(database, *args, **kwargs)


sqlite3.connect = _guarded_connect


def pytest_sessionfinish(session, exitstatus):
    if REFUSED:
        print(f"\nREAL DATABASE ACCESS ATTEMPTED (refused): {sorted(set(map(str, REFUSED)))}")
        session.exitstatus = 1
