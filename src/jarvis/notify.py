from __future__ import annotations

import shutil
import sqlite3
import subprocess
from collections.abc import Callable

MAX_TITLES = 3


def new_card_message(conn: sqlite3.Connection, count: int) -> tuple[str, str]:
    """(summary, body) for a desktop notification about ``count`` new cards."""
    rows = conn.execute(
        "SELECT title FROM cards WHERE status = 'active' "
        "ORDER BY updated_at DESC, id DESC LIMIT ?",
        (min(count, MAX_TITLES),),
    ).fetchall()
    summary = "1 new suggestion" if count == 1 else f"{count} new suggestions"
    lines = [f"• {r['title']}" for r in rows]
    if count > len(lines):
        lines.append(f"and {count - len(lines)} more")
    return summary, "\n".join(lines)


def send(summary: str, body: str,
         run: Callable[..., object] = subprocess.run) -> bool:
    """Send via notify-send. Returns False if it is missing or fails."""
    exe = shutil.which("notify-send")
    if not exe:
        return False
    try:
        run([exe, "--app-name=Jarvis", "--icon=dialog-information", summary, body],
            check=True, timeout=5)
    except (subprocess.SubprocessError, OSError):
        return False
    return True
