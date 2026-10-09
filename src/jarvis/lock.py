"""File locks that keep two refreshes (or two daemons) from running at once.

Both use ``fcntl.flock`` on a small file next to the database, non-blocking:
whoever does not get the lock is told so and skips. flock locks belong to the
open file, so they are released when the holder closes it or its process dies
(even on SIGKILL); a stale lock file on disk is harmless.

- ``<db>.lock``: held for the duration of one refresh (daemon tick, window
  Refresh, CLI refresh). A second refresher gets ``RefreshBusy``.
- ``<db>.daemon.lock``: held for the whole life of ``jarvis watch``; it holds
  the daemon's pid so a second ``jarvis watch`` can say who is running.
"""
from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class RefreshBusy(RuntimeError):
    """Another process (or thread) is running a refresh right now."""


class DaemonRunning(RuntimeError):
    """Another ``jarvis watch`` holds the daemon lock."""

    def __init__(self, pid: int | None) -> None:
        self.pid = pid
        who = f" (pid {pid})" if pid else ""
        super().__init__(f"jarvis watch is already running{who}")


def refresh_lock_path(db_path: str | Path) -> Path:
    return Path(f"{Path(db_path).expanduser()}.lock")


def daemon_lock_path(db_path: str | Path) -> Path:
    return Path(f"{Path(db_path).expanduser()}.daemon.lock")


def _try_lock(path: Path) -> int | None:
    """Open ``path`` and take an exclusive flock. Returns the fd, or None if held."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


def _release(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextmanager
def refresh_lock(db_path: str | Path) -> Iterator[None]:
    """Hold the refresh lock for the block; raise ``RefreshBusy`` if it is taken."""
    fd = _try_lock(refresh_lock_path(db_path))
    if fd is None:
        raise RefreshBusy("A refresh is already running in the background")
    try:
        yield
    finally:
        _release(fd)


def refresh_running(db_path: str | Path) -> bool:
    """True if someone holds the refresh lock right now (probe; takes and drops it)."""
    fd = _try_lock(refresh_lock_path(db_path))
    if fd is None:
        return True
    _release(fd)
    return False


class DaemonLock:
    """Lifetime lock for ``jarvis watch``. ``acquire`` raises ``DaemonRunning``."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = daemon_lock_path(db_path)
        self._fd: int | None = None

    def acquire(self) -> DaemonLock:
        fd = _try_lock(self.path)
        if fd is None:
            raise DaemonRunning(self._read_pid())
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        return self

    def _read_pid(self) -> int | None:
        try:
            text = self.path.read_text().strip()
            return int(text) if text else None
        except (OSError, ValueError):
            return None

    def release(self) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            try:
                os.ftruncate(fd, 0)
            except OSError:
                pass
            _release(fd)

    def __enter__(self) -> DaemonLock:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()
