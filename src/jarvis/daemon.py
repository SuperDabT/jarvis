"""The fast loop: ``jarvis watch`` keeps the database current on its own.

Every ``interval`` seconds one *tick* runs:

1. Conditional GitHub requests through the existing ``sync`` (unchanged
   resources cost a 304), plus the repo events endpoint, only to learn GitHub's
   ``X-Poll-Interval``.
2. Three cheap fingerprints: did any GitHub resource change, did the effective
   hardware.yaml change (repo copy, else the local file), did the facts hash
   change (date-based facts move at local midnight).
3. ``decide`` (pure): if nothing changed and no model wording is owed, stop
   ("skipped"). Otherwise run the electrical checks and build the briefing; both
   cache their model calls by content hash, so a run with nothing new for the
   model costs no model call.

Between ticks the local hardware file is polled by mtime every ``HW_POLL_S``
seconds; an edit runs a local tick (checks + briefing, no GitHub) within seconds.

Every refresh, here or in the window or the CLI, holds ``<db>.lock``
(:mod:`jarvis.lock`), so two of them never run at once; a tick that finds it
taken logs "busy, skipped". The daemon itself holds ``<db>.daemon.lock``.

Errors (network, GitHub 5xx, rate limit, Ollama down) never end the loop: the
tick is logged, ``last_error`` is recorded, and the next tick is pulled in with
exponential backoff (60 s doubling to 15 min, or until a known rate-limit reset),
back to the normal interval after a clean tick. While Ollama is down the checks
and briefing use their templates and ``model_pending`` makes the next tick run
again, so the model wording arrives once Ollama is back.

The slow loop (phase 6) lives in the same process: every
``JARVIS_RESEARCH_INTERVAL`` hours (default 6, min 1; the first run waits about two
minutes after startup and respects the stored schedule) one research pass runs under
the same refresh lock (:mod:`jarvis.research`): arXiv and library releases -> a
few research cards, one notification per run with new cards, one log line per run.
Every fast tick also auto-resolves research cards whose issue was closed.

SIGTERM / SIGINT stop the loop: a tick that is talking to GitHub finishes (it
is bounded by the HTTP timeout and keeps ETags consistent with the stored data);
a tick waiting on the model, or a research pass, is aborted. Locks are released and the exit code is 0.
"""
from __future__ import annotations

import hashlib
import os
import re
import signal
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from jarvis import db, notify
from jarvis.notify import new_card_message
from jarvis.briefing import Briefing, build_briefing
from jarvis.config import Config
from jarvis.facts import compute_facts, facts_hash
from jarvis.github import GitHubClient, GitHubError
from jarvis.hardware import find_hardware
from jarvis.lock import DaemonLock, DaemonRunning, RefreshBusy, refresh_lock
from jarvis.ollama import OllamaClient
from jarvis.pipeline import run_checks
from jarvis.research.runner import resolve_closed, run_research
from jarvis.research.settings import load_settings
from jarvis.research.sources import ArxivClient
from jarvis.sync import sync

DEFAULT_INTERVAL_S = 180
MIN_INTERVAL_S = 60
NO_TOKEN_MIN_INTERVAL_S = 600  # ~6 requests per tick -> ~36/hour of the 60 allowed
HW_POLL_S = 10
BUSY_RETRY_S = 30
BACKOFF_BASE_S = 60
BACKOFF_CAP_S = 900
RATE_LIMIT_SLACK_S = 5
STOP_POLL_S = 0.5
# The slow loop (research): every JARVIS_RESEARCH_INTERVAL hours, first run a little
# after startup so it does not compete with the first fast tick.
DEFAULT_RESEARCH_HOURS = 6.0
MIN_RESEARCH_HOURS = 1.0
RESEARCH_STARTUP_DELAY_S = 120
RESEARCH_RETRY_S = 1800


class Shutdown(BaseException):
    """Raised by the signal handler to abort a tick that is waiting on the model.

    A BaseException so no ``except Exception`` on the way swallows it.
    """


# ---------------------------------------------------------------- pure pieces


def effective_interval(requested: float, poll_interval: float | None = None) -> int:
    """Never below GitHub's X-Poll-Interval, never below MIN_INTERVAL_S."""
    return int(max(requested, poll_interval or 0, MIN_INTERVAL_S))


def token_interval(requested: int, token: str | None) -> int:
    """Unauthenticated, even 304s count against GitHub's 60 requests/hour."""
    return requested if token else max(requested, NO_TOKEN_MIN_INTERVAL_S)


def interval_from_env(env: Mapping[str, str] | None = None) -> int:
    raw = (env if env is not None else os.environ).get("JARVIS_INTERVAL", "").strip()
    if not raw:
        return DEFAULT_INTERVAL_S
    try:
        return int(float(raw))
    except ValueError:
        return DEFAULT_INTERVAL_S


def research_interval_from_env(env: Mapping[str, str] | None = None) -> int:
    """Seconds between research runs: $JARVIS_RESEARCH_INTERVAL hours (default 6, min 1)."""
    raw = (env if env is not None else os.environ).get("JARVIS_RESEARCH_INTERVAL", "").strip()
    try:
        hours = float(raw) if raw else DEFAULT_RESEARCH_HOURS
    except ValueError:
        hours = DEFAULT_RESEARCH_HOURS
    return int(max(hours, MIN_RESEARCH_HOURS) * 3600)


@dataclass
class Backoff:
    """60, 120, 240, 480, 900, 900, ... seconds for consecutive failures."""

    base: float = BACKOFF_BASE_S
    cap: float = BACKOFF_CAP_S
    failures: int = 0

    def fail(self) -> float:
        self.failures += 1
        return min(self.base * 2 ** (self.failures - 1), self.cap)

    def reset(self) -> None:
        self.failures = 0


_RETRY_AFTER_RE = re.compile(r"retry after (\d+)s")
_EPOCH_RE = re.compile(r"\(epoch (\d+)\)")


def rate_limit_wait(text: str, now_epoch: float) -> float | None:
    """Seconds to wait for a GitHub rate limit named in an error text, if known.

    Uses Retry-After (secondary limits) or, when the primary limit is exhausted
    (remaining=0), the X-RateLimit-Reset epoch. Other 403s return None.
    """
    if not re.search(r"GitHub (403|429)\b", text):
        return None
    m = _RETRY_AFTER_RE.search(text)
    if m:
        return float(m.group(1)) + RATE_LIMIT_SLACK_S
    m = _EPOCH_RE.search(text)
    if m and "remaining=0" in text:
        return max(0.0, int(m.group(1)) - now_epoch) + RATE_LIMIT_SLACK_S
    return None


@dataclass(frozen=True)
class Decision:
    run: bool
    reasons: tuple[str, ...]
    summary: str  # what was looked at, for the "skipped" log line


def decide(
    github: Mapping[str, str] | None,
    hw_hash: str,
    prev_hw_hash: str | None,
    facts: str,
    prev_facts: str | None,
    model_pending: bool = False,
) -> Decision:
    """Run checks + briefing, or skip? Pure.

    ``github`` maps resource -> sync outcome ("changed", "unchanged", "missing",
    "error: ..."), or None when GitHub was not asked (a local hardware tick or
    GitHub unreachable). ``prev_*`` are the values stored by the last run (None:
    never ran). ``model_pending``: the last run fell back to template wording
    because Ollama was down, so run again to get the model wording.
    """
    reasons: list[str] = []
    first = prev_facts is None
    if first:
        reasons.append("first run")
    changed = [name for name, o in (github or {}).items() if o == "changed"]
    reasons += [f"{name} changed" for name in changed]
    if not first and hw_hash != prev_hw_hash:
        reasons.append("hardware changed")
    if not first and facts != prev_facts:
        reasons.append("facts changed")
    if model_pending:
        reasons.append("retrying model wording")

    if github is None:
        gh = "github not checked"
    elif any(str(o).startswith("error") for o in github.values()):
        gh = "github error"
    else:
        gh = "github changed" if changed else "github unchanged"
    hw = "hardware changed" if (not first and hw_hash != prev_hw_hash) else "hardware unchanged"
    return Decision(bool(reasons), tuple(reasons), f"{gh}, {hw}")


# ---------------------------------------------------------------- one tick


@dataclass
class TickState:
    hardware_hash: str | None = None
    facts_hash: str | None = None
    model_pending: bool = False


def load_state(conn) -> TickState:
    row = db.get_daemon_state(conn)
    if row is None:
        return TickState()
    return TickState(row["hardware_hash"], row["facts_hash"], bool(row["model_pending"]))


def hardware_hash(conn, cfg: Config) -> str:
    """Hash of the hardware.yaml the checks would use right now (repo copy, else local)."""
    src = find_hardware(conn, cfg.hardware_path)
    if src is None:
        return "none"
    blob = f"{src.kind}\n{src.label}\n{src.text}".encode()
    return hashlib.sha256(blob).hexdigest()


@dataclass
class TickResult:
    kind: str  # "github" (full tick) or "local" (hardware file edit)
    ran: bool = False
    busy: bool = False
    decision: Decision | None = None
    sync: dict[str, str] | None = None
    cards_new: int = 0
    cards_resolved: int = 0
    notes: list[str] = field(default_factory=list)
    briefing: Briefing | None = None
    errors: list[str] = field(default_factory=list)
    rate_limit_wait: float | None = None
    model_pending: bool = False
    duration: float = 0.0

    @property
    def error(self) -> str | None:
        return "; ".join(self.errors) if self.errors else None


def _model_down(b: Briefing, notes: list[str]) -> str | None:
    reason = b.fallback_reason or ""
    if reason.startswith("model unavailable"):
        return reason
    for n in notes:
        if "model unavailable" in n:
            return n
    return None


def tick(
    conn,
    cfg: Config,
    *,
    gh: Any | None,
    ollama: Any | None,
    now: datetime | None = None,
    abortable: Callable[[], AbstractContextManager] = nullcontext,
) -> TickResult:
    """One pass of the fast loop under the refresh lock. ``gh=None``: skip GitHub.

    Raises nothing for a busy lock (``busy=True``); GitHub/network failures are
    recorded in ``errors`` and the tick still evaluates local changes. Other
    exceptions (e.g. the database) propagate to the caller.
    """
    now = now or datetime.now(UTC)
    res = TickResult("github" if gh is not None else "local")
    try:
        with refresh_lock(cfg.db_path):
            _tick_locked(conn, cfg, gh, ollama, now, abortable, res)
    except RefreshBusy:
        res.busy = True
    return res


def _note_error(res: TickResult, text: str, now: datetime) -> None:
    res.errors.append(text)
    wait = rate_limit_wait(text, now.timestamp())
    if wait is not None:
        res.rate_limit_wait = max(res.rate_limit_wait or 0.0, wait)


def _tick_locked(conn, cfg, gh, ollama, now, abortable, res: TickResult) -> None:
    state = load_state(conn)
    if gh is not None:
        if hasattr(gh, "events"):
            try:
                gh.events()  # mainly for X-Poll-Interval; a 304 most of the time
            except GitHubError as e:
                _note_error(res, f"events: {e}", now)
            except httpx.HTTPError as e:
                _note_error(res, f"GitHub unreachable: {type(e).__name__}: {e}", now)
        if not any(e.startswith("GitHub unreachable") for e in res.errors):
            try:
                res.sync = sync(conn, gh)
            except httpx.HTTPError as e:
                _note_error(res, f"GitHub unreachable: {type(e).__name__}: {e}", now)
            else:
                for name, outcome in res.sync.items():
                    if outcome.startswith("error"):
                        _note_error(res, f"{name}: {outcome.removeprefix('error: ')}", now)

    # Research cards whose issue is now closed: local and cheap, every tick.
    res.cards_resolved = resolve_closed(conn)
    hw = hardware_hash(conn, cfg)
    fh = facts_hash(compute_facts(conn, now=now))
    github = res.sync if res.sync is not None else None
    if gh is not None and github is None:
        github = {"github": "error"}  # asked, but unreachable
    res.decision = decide(github, hw, state.hardware_hash, fh, state.facts_hash,
                          state.model_pending)
    if res.decision.run:
        res.ran = True
        with abortable():
            res.cards_new, resolved, res.notes = run_checks(conn, cfg, ollama)
            res.cards_resolved += resolved
            res.briefing = build_briefing(conn, ollama, now=now)
        down = _model_down(res.briefing, res.notes)
        res.model_pending = down is not None
        if down:
            res.errors.append(f"Ollama: {down}")
        after = facts_hash(compute_facts(conn, now=now))
        db.update_daemon_state(conn, hardware_hash=hw, facts_hash=after,
                               model_pending=int(res.model_pending),
                               last_change_at=now.isoformat(timespec="seconds"))
    db.update_daemon_state(conn, last_tick_at=now.astimezone(UTC).isoformat(timespec="seconds"),
                           last_error=_short(res.error) if res.error else None)


def _short(text: str, n: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 3] + "..."


# ---------------------------------------------------------------- log lines


def _briefing_desc(b: Briefing | None, dur: float) -> str:
    if b is None:
        return f"({dur:.1f}s)"
    if b.from_cache:
        return f"briefing cached ({dur:.1f}s)"
    if b.source == "model":
        return f"briefing regenerated ({b.model}, {dur:.1f}s)"
    reason = b.fallback_reason or "template"
    if reason.startswith("model unavailable"):
        reason = "Ollama unavailable"
    elif reason.startswith("model output rejected"):
        reason = "model reply rejected"
    return f"briefing template ({reason}, {dur:.1f}s)"


def format_line(res: TickResult, now: datetime, next_in: float | None = None) -> str:
    ts = now.astimezone().strftime("%H:%M:%S")
    label = "tick" if res.kind == "github" else "hardware edit"
    if res.busy:
        line = f"{ts} {label}: busy, skipped (another refresh is running)"
    elif res.decision is None:
        line = f"{ts} {label}: failed ({res.duration:.1f}s)"
    elif not res.decision.run:
        line = f"{ts} {label}: {res.decision.summary} -> skipped ({res.duration:.1f}s)"
    else:
        what = ", ".join(res.decision.reasons)
        line = (f"{ts} {label}: {what} -> checks: {res.cards_new} new "
                f"{res.cards_resolved} resolved, {_briefing_desc(res.briefing, res.duration)}")
    if res.error:
        line += f" ! {_short(res.error, 240)}"
        if next_in is not None:
            line += f"; retry in {_human_s(next_in)}"
    return line


def _human_s(s: float) -> str:
    s = int(round(s))
    if s >= 3600 and s % 3600 == 0:
        return f"{s // 3600}h"
    return f"{s}s" if s < 120 else f"{s // 60}m{s % 60:02d}s" if s % 60 else f"{s // 60}m"


def verbose_lines(res: TickResult) -> list[str]:
    out = []
    if res.sync is not None:
        out.append("  sync: " + ", ".join(f"{k}={v}" for k, v in res.sync.items()))
    if res.decision is not None:
        out.append(f"  decision: {'run' if res.decision.run else 'skip'}"
                   + (f" ({', '.join(res.decision.reasons)})" if res.decision.reasons else ""))
    out += [f"  note: {n}" for n in res.notes]
    if res.briefing is not None:
        b = res.briefing
        out.append(f"  briefing: {b.source}, facts {b.facts_hash[:12]}, "
                   f"{'cached' if b.from_cache else 'new'}"
                   + (f", fallback: {b.fallback_reason}" if b.fallback_reason else ""))
    out += [f"  error: {e}" for e in res.errors]
    return out


# ---------------------------------------------------------------- the loop


def _file_sig(path: Path | None) -> tuple[int, int] | None:
    if path is None:
        return None
    try:
        st = Path(path).expanduser().stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


class Daemon:
    """The loop around ``tick``. Clock, sleep, wall time and stop are injectable."""

    def __init__(
        self,
        cfg: Config,
        conn,
        *,
        interval: int = DEFAULT_INTERVAL_S,
        gh: Any | None = None,
        ollama: Any | None = None,
        out: Callable[[str], None] | None = None,
        verbose: bool = False,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] | None = None,
        wall: Callable[[], datetime] = lambda: datetime.now(UTC),
        stop: threading.Event | None = None,
        hw_poll_s: float = HW_POLL_S,
        notify: Callable[[str, str], Any] | None = None,
        research: Callable[[datetime], Any] | None = None,
        research_interval: int = int(DEFAULT_RESEARCH_HOURS * 3600),
        research_delay: float = RESEARCH_STARTUP_DELAY_S,
    ) -> None:
        self.cfg, self.conn = cfg, conn
        self.interval = interval
        self.gh, self.ollama = gh, ollama
        self.out = out or (lambda line: print(line, flush=True))
        self.verbose = verbose
        self.clock, self.wall = clock, wall
        self.stop = stop or threading.Event()
        self.sleep = sleep or self._sleep
        self.hw_poll_s = hw_poll_s
        self.notify = notify
        # The slow loop: ``research(now)`` runs one research pass (see jarvis.research)
        # and returns a result with .new, .errors, .error and .summary().
        self.research = research
        self.research_interval = max(int(research_interval), int(MIN_RESEARCH_HOURS * 3600))
        self.research_delay = research_delay
        self.research_runs: list[Any] = []
        self.backoff = Backoff()
        self.in_model = False
        self.ticks: list[TickResult] = []

    def _sleep(self, seconds: float) -> None:
        """Sleep in short steps so a stop request is noticed within STOP_POLL_S.

        Plain time.sleep, not Event.wait: the signal handler calls stop.set(), and
        Event.wait holds the event's lock at moments where that could deadlock.
        """
        end = self.clock() + seconds
        while not self.stop.is_set():
            left = end - self.clock()
            if left <= 0:
                return
            time.sleep(min(left, STOP_POLL_S))

    # -- signals ---------------------------------------------------------
    def request_stop(self, *_: Any) -> None:
        """Signal handler body: stop after this tick, or abort a model wait now."""
        self.stop.set()
        if self.in_model:
            raise Shutdown

    @contextmanager
    def _abortable(self) -> Iterator[None]:
        if self.stop.is_set():
            raise Shutdown
        self.in_model = True
        try:
            yield
        finally:
            self.in_model = False

    # -- scheduling ------------------------------------------------------
    def current_interval(self) -> int:
        return effective_interval(self.interval, getattr(self.gh, "poll_interval", None))

    def delay_after(self, res: TickResult) -> float:
        if res.busy:
            return BUSY_RETRY_S
        if res.error:
            d = max(self.backoff.fail(), getattr(self.gh, "poll_interval", 0) or 0)
            if res.rate_limit_wait is not None:
                d = max(d, res.rate_limit_wait)
            return d
        self.backoff.reset()
        return self.current_interval()

    # -- one tick, logged ------------------------------------------------
    def run_tick(self, full: bool) -> TickResult:
        t0 = self.clock()
        now = self.wall()
        try:
            res = tick(self.conn, self.cfg, gh=self.gh if full else None, ollama=self.ollama,
                       now=now, abortable=self._abortable)
        except Shutdown:
            raise
        except Exception as e:  # noqa: BLE001 - the loop must survive anything
            res = TickResult("github" if full else "local")
            res.errors.append(f"{type(e).__name__}: {e}")
            try:
                self.conn.rollback()
                db.update_daemon_state(self.conn, last_error=_short(res.error),
                                       last_tick_at=now.isoformat(timespec="seconds"))
            except Exception:  # noqa: BLE001 - the DB itself may be the problem
                pass
        res.duration = self.clock() - t0
        self.ticks.append(res)
        return res

    def _log(self, res: TickResult, next_in: float | None) -> None:
        self.out(format_line(res, self.wall(), next_in))
        if self.verbose:
            for line in verbose_lines(res):
                self.out(line)
        if self.notify and res.cards_new and not res.busy:
            try:
                self.notify(*new_card_message(self.conn, res.cards_new))
            except Exception as e:  # noqa: BLE001
                self.out(f"  notify failed: {_short(str(e), 120)}")

    # -- the slow loop (research) ------------------------------------------
    def first_research_delay(self) -> float:
        """Seconds until the first research run: at least ``research_delay`` after
        startup, and not before the stored schedule (last run + interval)."""
        wait = float(self.research_delay)
        try:
            row = db.get_daemon_state(self.conn)
            last = row["last_research_at"] if row is not None else None
        except Exception:  # noqa: BLE001
            last = None
        if last:
            try:
                due = datetime.fromisoformat(last) + timedelta(seconds=self.research_interval)
                wait = max(wait, (due - self.wall()).total_seconds())
            except ValueError:
                pass
        return wait

    def _set_next_research(self, delay: float) -> None:
        db.update_daemon_state(self.conn, next_research_at=(
            self.wall() + timedelta(seconds=delay)).isoformat(timespec="seconds"))

    def run_research(self) -> float:
        """One research pass under the refresh lock, logged. Returns the delay until
        the next one. Abortable on SIGTERM like a model call."""
        t0 = self.clock()
        now = self.wall()
        ts = now.astimezone().strftime("%H:%M:%S")
        try:
            with refresh_lock(self.cfg.db_path), self._abortable():
                r = self.research(now)
        except RefreshBusy:
            self.out(f"{ts} research: busy, retry in {BUSY_RETRY_S}s")
            return BUSY_RETRY_S
        except Shutdown:
            raise
        except Exception as e:  # noqa: BLE001 - the loop must survive anything
            try:
                self.conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            delay = min(RESEARCH_RETRY_S, self.research_interval)
            text = _short(f"{type(e).__name__}: {e}", 240)
            self.out(f"{ts} research: failed ({self.clock() - t0:.1f}s) ! {text}; "
                     f"retry in {_human_s(delay)}")
            db.update_daemon_state(self.conn, research_summary=f"failed: {text}")
            return delay
        self.research_runs.append(r)
        delay = self.research_interval
        line = f"{ts} research: {r.summary()}"
        if r.error:
            delay = min(RESEARCH_RETRY_S, self.research_interval)
            line += f" ! {_short(r.error, 240)}; retry in {_human_s(delay)}"
        self.out(line)
        if self.verbose:
            for n in getattr(r, "notes", []) or []:
                self.out(f"  note: {n}")
        if self.notify and r.new:
            try:
                self.notify(*new_card_message(self.conn, r.new))
            except Exception as e:  # noqa: BLE001
                self.out(f"  notify failed: {_short(str(e), 120)}")
        db.update_daemon_state(self.conn, last_research_at=now.isoformat(timespec="seconds"),
                               research_summary=_short(r.summary(), 300))
        return delay

    def _hardware_edited(self) -> bool:
        try:
            return hardware_hash(self.conn, self.cfg) != load_state(self.conn).hardware_hash
        except Exception:  # noqa: BLE001
            return False

    # -- main loop -------------------------------------------------------
    def run(self, once: bool = False) -> int:
        """Run until stopped (or one tick with ``once``). Returns the exit code."""
        if not once:
            db.update_daemon_state(
                self.conn, pid=os.getpid(),
                started_at=self.wall().isoformat(timespec="seconds"),
                interval_s=self.current_interval(), next_tick_at=None)
            self.out(f"{self.wall().astimezone():%H:%M:%S} watching {self.cfg.repo} every "
                     f"{self.current_interval()}s, hardware file {self.cfg.hardware_path} "
                     f"every {self.hw_poll_s:g}s (db {self.cfg.db_path})"
                     + (f", research every {_human_s(self.research_interval)}"
                        if self.research is not None else ""))
        hw_sig = _file_sig(self.cfg.hardware_path)
        next_full = self.clock()
        next_research: float | None = None
        if self.research is not None and not once:
            first = self.first_research_delay()
            next_research = self.clock() + first
            self._set_next_research(first)
        code = 0
        try:
            while not self.stop.is_set():
                if self.clock() >= next_full:
                    res = self.run_tick(full=True)
                    if once:
                        self._log(res, None)
                        code = 1 if res.error else 0
                        break
                    delay = self.delay_after(res)
                    self._log(res, delay)
                    next_full = self.clock() + delay
                    hw_sig = _file_sig(self.cfg.hardware_path)
                    db.update_daemon_state(
                        self.conn, interval_s=self.current_interval(),
                        next_tick_at=(self.wall() + timedelta(seconds=delay))
                        .isoformat(timespec="seconds"))
                elif next_research is not None and self.clock() >= next_research:
                    delay = self.run_research()
                    next_research = self.clock() + delay
                    self._set_next_research(delay)
                else:
                    sig = _file_sig(self.cfg.hardware_path)
                    if sig != hw_sig:
                        if self._hardware_edited():
                            res = self.run_tick(full=False)
                            self._log(res, None)
                            if not res.busy:
                                hw_sig = sig  # busy: try again on the next poll
                        else:
                            hw_sig = sig
                wake = next_full if next_research is None else min(next_full, next_research)
                remaining = wake - self.clock()
                if remaining > 0 and not self.stop.is_set():
                    self.sleep(min(remaining, self.hw_poll_s))
        except Shutdown:
            try:
                self.conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            self.out(f"{self.wall().astimezone():%H:%M:%S} stop: aborted the tick in progress")
        finally:
            if not once:
                try:
                    db.update_daemon_state(self.conn, pid=None, next_tick_at=None)
                except Exception:  # noqa: BLE001
                    pass
        if not once:
            self.out(f"{self.wall().astimezone():%H:%M:%S} stopped")
        return code


# ---------------------------------------------------------------- entry point


def install_signal_handlers(daemon: Daemon) -> dict[int, Any]:
    old = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        old[sig] = signal.signal(sig, daemon.request_stop)
    return old


def run_watch(cfg: Config, interval: int | None = None, once: bool = False,
              verbose: bool = False) -> int:
    """``jarvis watch``: the CLI entry. Exit 0 on a clean stop, 1 if one is running."""
    requested = interval if interval is not None else interval_from_env()
    if requested < MIN_INTERVAL_S:
        print(f"interval {requested}s is below the {MIN_INTERVAL_S}s floor; "
              f"using {MIN_INTERVAL_S}s", file=sys.stderr)
    floored = token_interval(requested, cfg.token)
    if floored != requested:
        print(f"no GITHUB_TOKEN: checking every {floored}s instead of "
              f"{requested}s to stay under GitHub's unauthenticated limit", file=sys.stderr)
        requested = floored
    lock = DaemonLock(cfg.db_path)
    if not once:
        try:
            lock.acquire()
        except DaemonRunning as e:
            print(f"{e}; not starting a second one (lock {lock.path})", file=sys.stderr)
            return 1
    if not cfg.token:
        print("GITHUB_TOKEN not set; continuing unauthenticated", file=sys.stderr)
    conn = db.connect(cfg.db_path)
    gh = GitHubClient(cfg.repo, cfg.token, conn)
    ollama = OllamaClient(cfg.ollama_url, cfg.model)
    arxiv = ArxivClient()

    def research(now: datetime):
        settings, note = load_settings(cfg.research_path)
        return run_research(conn, settings, repo=cfg.repo, ollama=ollama, arxiv=arxiv, gh=gh,
                            now=now, notes=[note] if note else None)

    daemon = Daemon(cfg, conn, interval=requested, gh=gh, ollama=ollama, verbose=verbose,
                    notify=None if once else notify.send, research=research,
                    research_interval=research_interval_from_env())
    old = install_signal_handlers(daemon)
    try:
        return daemon.run(once=once)
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)
        gh.close()
        ollama.close()
        arxiv.close()
        conn.close()
        lock.release()
