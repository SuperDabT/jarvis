"""Phase 5: the fast loop. No network, no real sleeping."""
from __future__ import annotations

import os
import signal
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_hardware import EXAMPLE

from jarvis import cards as cardlib
from jarvis import db
from jarvis.config import Config
from jarvis.daemon import (
    BACKOFF_CAP_S,
    Backoff,
    Daemon,
    Shutdown,
    TickResult,
    decide,
    effective_interval,
    format_line,
    interval_from_env,
    load_state,
    rate_limit_wait,
    run_watch,
    tick,
)
from jarvis.github import FetchResult, GitHubError
from jarvis.lock import (
    DaemonLock,
    DaemonRunning,
    RefreshBusy,
    refresh_lock,
    refresh_running,
)
from jarvis.ollama import OllamaError

NOW = datetime(2026, 10, 8, 16, 0, tzinfo=UTC)
UNCHANGED = FetchResult(False, 304, None)


class FakeGH:
    """Stands in for GitHubClient: 304 for everything unless told otherwise."""

    def __init__(self, conn):
        self.conn = conn
        self.poll_interval = 60
        self.queued: dict[str, FetchResult] = {}
        self.error: Exception | None = None
        self.requests = 0

    def resource_key(self, name):
        return f"key:{name}"

    def _get(self, name):
        self.requests += 1
        if self.error is not None:
            raise self.error
        return self.queued.pop(name, UNCHANGED)

    def issues(self): return self._get("issues")
    def milestones(self): return self._get("milestones")
    def commits(self): return self._get("commits")
    def file(self, path): return self._get(path)
    def events(self): return self._get("events")


class FakeOllama:
    model = "fake:1b"

    def __init__(self, down: bool = False):
        self.down = down
        self.calls = 0

    def chat_json(self, system, user, schema):
        self.calls += 1
        if self.down:
            raise OllamaError("cannot reach Ollama at http://localhost:11434: refused")
        return {}  # fails validation -> template text; still a model call


def issue(number, title, state="open", updated="2026-10-01T10:00:00Z"):
    return {"number": number, "title": title, "state": state, "labels": [],
            "html_url": f"https://github.com/me/robot/issues/{number}",
            "created_at": "2026-09-20T10:00:00Z", "updated_at": updated}


@pytest.fixture
def env(tmp_path):
    hw = tmp_path / "hardware.yaml"
    hw.write_text(EXAMPLE.read_text())
    cfg = Config(repo="me/robot", db_path=tmp_path / "j.db", hardware_path=hw)
    conn = db.connect(cfg.db_path)
    db.upsert_milestone(conn, {"number": 1, "title": "Core demo", "state": "open",
                               "due_on": "2026-10-18T07:00:00Z", "open_issues": 2,
                               "closed_issues": 1})
    db.upsert_issue(conn, {"number": 1, "title": "Servo jitter", "state": "open",
                           "milestone_number": 1, "created_at": "2026-09-20T10:00:00Z",
                           "updated_at": "2026-10-01T10:00:00Z"})
    yield cfg, conn, FakeGH(conn), FakeOllama()
    conn.close()


# ---------------------------------------------------------------- decide


@pytest.mark.parametrize("github,hw,prev_hw,facts,prev_facts,pending,run,reasons,summary", [
    # first run always runs
    ({"issues": "unchanged"}, "h", None, "f", None, False, True, ("first run",),
     "github unchanged, hardware unchanged"),
    # nothing changed anywhere -> skip
    ({"issues": "unchanged", "hardware.yaml": "missing"}, "h", "h", "f", "f", False,
     False, (), "github unchanged, hardware unchanged"),
    # a GitHub resource changed
    ({"issues": "changed", "commits": "unchanged"}, "h", "h", "f", "f", False, True,
     ("issues changed",), "github changed, hardware unchanged"),
    # local hardware file edited, GitHub not asked
    (None, "h2", "h", "f", "f", False, True, ("hardware changed",),
     "github not checked, hardware changed"),
    # day rollover: only the facts moved
    ({"issues": "unchanged"}, "h", "h", "f2", "f", False, True, ("facts changed",),
     "github unchanged, hardware unchanged"),
    # Ollama was down last time: run again for the model wording
    ({"issues": "unchanged"}, "h", "h", "f", "f", True, True, ("retrying model wording",),
     "github unchanged, hardware unchanged"),
    # GitHub errored and nothing local changed -> skip (the error drives the backoff)
    ({"issues": "error: GitHub 502: bad gateway"}, "h", "h", "f", "f", False, False, (),
     "github error, hardware unchanged"),
    # several at once, in a stable order
    ({"issues": "changed", "milestones": "changed"}, "h2", "h", "f2", "f", False, True,
     ("issues changed", "milestones changed", "hardware changed", "facts changed"),
     "github changed, hardware changed"),
])
def test_decide(github, hw, prev_hw, facts, prev_facts, pending, run, reasons, summary):
    d = decide(github, hw, prev_hw, facts, prev_facts, pending)
    assert (d.run, d.reasons, d.summary) == (run, reasons, summary)


def test_interval_floors():
    assert effective_interval(180, 60) == 180
    assert effective_interval(30, None) == 60
    assert effective_interval(90, 300) == 300
    assert interval_from_env({}) == 180
    assert interval_from_env({"JARVIS_INTERVAL": "240"}) == 240
    assert interval_from_env({"JARVIS_INTERVAL": "junk"}) == 180


def test_backoff_sequence_and_reset():
    b = Backoff()
    assert [b.fail() for _ in range(7)] == [60, 120, 240, 480, 900, 900, 900]
    b.reset()
    assert b.fail() == 60


def test_rate_limit_wait_parsing():
    now = 1_000_000
    primary = (f"GitHub 403: API rate limit exceeded; rate limit remaining=0; "
               f"resets at 1970-01-12T13:48:20+00:00 (epoch {now + 600})")
    assert rate_limit_wait(primary, now) == 605
    assert rate_limit_wait("GitHub 429: slow down; retry after 30s", now) == 35
    # a 403 that is not a rate limit (remaining > 0) waits like any error
    other = f"GitHub 403: Resource not accessible; rate limit remaining=4999; (epoch {now + 600})"
    assert rate_limit_wait(other, now) is None
    assert rate_limit_wait("GitHub 502: bad gateway", now) is None


# ---------------------------------------------------------------- tick


def test_unchanged_tick_makes_no_model_call(env):
    cfg, conn, gh, ollama = env
    first = tick(conn, cfg, gh=gh, ollama=ollama, now=NOW)
    assert first.ran  # first run always runs
    assert first.decision.reasons == ("first run",)
    assert first.briefing is not None and ollama.calls > 0
    assert cardlib.list_cards(conn, "active")
    calls = ollama.calls
    second = tick(conn, cfg, gh=gh, ollama=ollama, now=NOW + timedelta(minutes=3))
    assert not second.decision.run and second.briefing is None
    assert ollama.calls == calls
    assert second.decision.summary == "github unchanged, hardware unchanged"
    assert "-> skipped" in format_line(second, NOW)


def test_changed_issues_run_checks_and_briefing(env):
    cfg, conn, gh, ollama = env
    tick(conn, cfg, gh=gh, ollama=ollama, now=NOW)
    calls = ollama.calls
    gh.queued["issues"] = FetchResult(True, 200, [issue(2, "Add OLED display")])
    r = tick(conn, cfg, gh=gh, ollama=ollama, now=NOW + timedelta(minutes=3))
    assert r.decision.run and "issues changed" in r.decision.reasons
    assert r.briefing is not None and ollama.calls > calls  # new facts -> briefing call
    line = format_line(r, NOW)
    assert "issues changed" in line and "-> checks:" in line


def test_local_hardware_edit_triggers_checks_without_github(env):
    cfg, conn, gh, ollama = env
    tick(conn, cfg, gh=gh, ollama=ollama, now=NOW)
    before = {c.check_id for c in cardlib.list_cards(conn, "active")}
    assert "common_ground:5V" in before
    text = cfg.hardware_path.read_text().replace(
        "    tolerance_pct: null\n    max_current_a: null\n    ground_common: null",
        "    tolerance_pct: 2\n    max_current_a: 3.0\n    ground_common: true", 1)
    cfg.hardware_path.write_text(text)
    requests = gh.requests
    r = tick(conn, cfg, gh=None, ollama=ollama, now=NOW + timedelta(seconds=10))
    assert gh.requests == requests  # GitHub untouched
    assert r.kind == "local" and r.decision.reasons == ("hardware changed",)
    assert r.cards_resolved >= 1
    after = {c.check_id for c in cardlib.list_cards(conn, "active")}
    assert "common_ground:5V" not in after
    # and it settles: the same file again is a skip
    again = tick(conn, cfg, gh=None, ollama=ollama, now=NOW + timedelta(seconds=20))
    assert not again.decision.run


def test_day_rollover_changes_facts(env):
    cfg, conn, gh, ollama = env
    evening = datetime(2026, 10, 8, 21, 0).astimezone()  # local time, whatever the zone
    tick(conn, cfg, gh=gh, ollama=ollama, now=evening)
    same_day = tick(conn, cfg, gh=gh, ollama=ollama, now=evening + timedelta(hours=2, minutes=59))
    assert not same_day.decision.run
    # 00:03 local the next day: "due in N days" moved by one
    tomorrow = tick(conn, cfg, gh=gh, ollama=ollama, now=evening + timedelta(hours=3, minutes=3))
    assert tomorrow.decision.reasons == ("facts changed",)
    assert tomorrow.briefing is not None and not tomorrow.briefing.from_cache


def test_model_down_is_retried_on_a_later_tick(env):
    cfg, conn, gh, _ = env
    down = FakeOllama(down=True)
    r = tick(conn, cfg, gh=gh, ollama=down, now=NOW)
    assert r.model_pending and r.error and "Ollama" in r.error
    assert r.briefing.source == "template"
    assert load_state(conn).model_pending
    # nothing changed, but the model wording is owed: run again
    up = FakeOllama()
    r2 = tick(conn, cfg, gh=gh, ollama=up, now=NOW + timedelta(minutes=3))
    assert r2.decision.reasons == ("retrying model wording",)
    assert up.calls > 0 and not r2.model_pending and r2.error is None
    r3 = tick(conn, cfg, gh=gh, ollama=up, now=NOW + timedelta(minutes=6))
    assert not r3.decision.run


def test_github_unreachable_still_records_and_does_not_raise(env):
    cfg, conn, gh, ollama = env
    tick(conn, cfg, gh=gh, ollama=ollama, now=NOW)
    gh.error = httpx.ConnectError("Name or service not known")
    r = tick(conn, cfg, gh=gh, ollama=ollama, now=NOW + timedelta(minutes=3))
    assert not r.decision.run and "GitHub unreachable" in r.error
    assert r.decision.summary.startswith("github error")
    row = db.get_daemon_state(conn)
    assert "GitHub unreachable" in row["last_error"]


def test_busy_lock_skips_the_tick(env):
    cfg, conn, gh, ollama = env
    with refresh_lock(cfg.db_path):
        r = tick(conn, cfg, gh=gh, ollama=ollama, now=NOW)
    assert r.busy and r.decision is None and gh.requests == 0
    assert "busy, skipped" in format_line(r, NOW)


# ---------------------------------------------------------------- locks


def test_refresh_lock_second_acquirer_is_busy(tmp_path):
    path = tmp_path / "x.db"
    assert not refresh_running(path)
    with refresh_lock(path):
        assert refresh_running(path)
        with pytest.raises(RefreshBusy), refresh_lock(path):
            pass
    with refresh_lock(path):  # released after the block
        pass


def test_daemon_lock_refuses_second_instance(tmp_path, capsys):
    path = tmp_path / "x.db"
    with DaemonLock(path):
        with pytest.raises(DaemonRunning) as e:
            DaemonLock(path).acquire()
        assert e.value.pid == os.getpid()
        cfg = Config(repo="me/robot", db_path=path, hardware_path=tmp_path / "none.yaml")
        assert run_watch(cfg, interval=60) == 1
        assert "already running" in capsys.readouterr().err
    DaemonLock(path).acquire().release()  # free again


# ---------------------------------------------------------------- the loop


class FakeTime:
    """Clock + sleep: sleeping advances the clock; ``hooks`` run at given sleep counts."""

    def __init__(self, daemon_ref, hooks=None, max_sleeps=200):
        self.t = 0.0
        self.sleeps: list[float] = []
        self.hooks = hooks or {}
        self.max_sleeps = max_sleeps
        self.daemon_ref = daemon_ref

    def clock(self):
        return self.t

    def wall(self):
        return NOW + timedelta(seconds=self.t)

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s
        hook = self.hooks.get(len(self.sleeps))
        if hook:
            hook()
        if len(self.sleeps) >= self.max_sleeps:
            self.daemon_ref[0].stop.set()


def make_daemon(env, hooks=None, max_sleeps=200, **kw):
    cfg, conn, gh, ollama = env
    ref = []
    ft = FakeTime(ref, hooks, max_sleeps)
    lines: list[str] = []
    d = Daemon(cfg, conn, interval=180, gh=gh, ollama=ollama, out=lines.append,
               clock=ft.clock, sleep=ft.sleep, wall=ft.wall, hw_poll_s=10, **kw)
    ref.append(d)
    return d, ft, lines


def test_loop_ticks_on_interval_and_stops_promptly(env):
    d, ft, lines = make_daemon(env, max_sleeps=40)  # 400 s of fake time
    assert d.run() == 0
    full = [r for r in d.ticks if r.kind == "github"]
    assert len(full) == 3  # t = 0, 180, 360
    assert all(s <= 10 for s in ft.sleeps)  # sleeps in small steps
    assert "watching me/robot" in lines[0] and lines[-1].endswith("stopped")
    assert sum("-> skipped" in ln for ln in lines) == 2
    row = db.get_daemon_state(d.conn)
    assert row["pid"] is None and row["next_tick_at"] is None  # cleared on exit


def test_loop_stop_event_ends_loop_within_one_step(env):
    d, ft, lines = make_daemon(env, hooks={3: lambda: d.stop.set()})
    d.run()
    assert len(ft.sleeps) == 3


def test_loop_picks_up_hardware_edit_between_ticks(env):
    cfg = env[0]

    def edit():
        cfg.hardware_path.write_text(cfg.hardware_path.read_text().replace(
            "tolerance_pct: null", "tolerance_pct: 2", 1))

    d, ft, lines = make_daemon(env, hooks={2: edit}, max_sleeps=5)
    d.run()
    local = [r for r in d.ticks if r.kind == "local"]
    assert len(local) == 1 and local[0].decision.reasons == ("hardware changed",)
    assert any("hardware edit: hardware changed -> checks" in ln for ln in lines)
    assert len([r for r in d.ticks if r.kind == "github"]) == 1  # no extra GitHub tick


def test_loop_backs_off_on_errors_and_resets(env):
    cfg, conn, gh, ollama = env
    gh.error = httpx.ConnectError("offline")
    delays = []
    d, ft, lines = make_daemon(env, max_sleeps=400)
    real = d.delay_after

    def spy(res):
        v = real(res)
        delays.append(v)
        if len(delays) == 4:
            gh.error = None  # network back
        if len(delays) == 6:
            d.stop.set()
        return v

    d.delay_after = spy
    d.run()
    assert delays == [60, 120, 240, 480, 180, 180]
    assert "retry in 60s" in lines[1]
    assert db.get_daemon_state(conn)["last_error"] is None  # cleared by the clean tick


def test_rate_limit_sleeps_until_reset(env):
    cfg, conn, gh, ollama = env
    reset = int((NOW + timedelta(minutes=40)).timestamp())
    gh.error = GitHubError(403, f"API rate limit exceeded; rate limit remaining=0; "
                                f"resets at x (epoch {reset})")
    d, _ft, _lines = make_daemon(env)
    res = d.run_tick(full=True)
    assert res.rate_limit_wait is not None
    assert d.delay_after(res) == pytest.approx(40 * 60 + 5, abs=1)


def test_once_runs_one_tick_and_keeps_heartbeat_pid_empty(env):
    d, ft, lines = make_daemon(env)
    assert d.run(once=True) == 0
    assert len(d.ticks) == 1 and ft.sleeps == []
    assert len(lines) == 1 and "first run" in lines[0]
    row = db.get_daemon_state(d.conn)
    assert row["last_tick_at"] and row["pid"] is None


def test_sigterm_during_model_call_aborts_cleanly(env):
    cfg, conn, gh, _ = env

    class SlowOllama(FakeOllama):
        def chat_json(self, system, user, schema):
            os.kill(os.getpid(), signal.SIGTERM)  # arrives while "waiting on the model"
            return super().chat_json(system, user, schema)

    env2 = (cfg, conn, gh, SlowOllama())
    d, _ft, lines = make_daemon(env2)
    old = signal.signal(signal.SIGTERM, d.request_stop)
    try:
        assert d.run() == 0
    finally:
        signal.signal(signal.SIGTERM, old)
    assert any("aborted the tick" in ln for ln in lines)
    assert not refresh_running(cfg.db_path)  # lock released
    assert load_state(conn).facts_hash is None  # aborted tick saved no state


def test_sigterm_while_sleeping_stops(env):
    d, ft, lines = make_daemon(env, hooks={2: lambda: os.kill(os.getpid(), signal.SIGTERM)})
    old = signal.signal(signal.SIGTERM, d.request_stop)
    try:
        assert d.run() == 0
    finally:
        signal.signal(signal.SIGTERM, old)
    assert len(ft.sleeps) == 2 and lines[-1].endswith("stopped")


def test_shutdown_is_not_an_exception():
    assert not issubclass(Shutdown, Exception)


def test_format_line_error_and_regenerated():
    from jarvis.briefing import Briefing
    r = TickResult("github", decision=decide({"issues": "changed"}, "h", "h", "f", "f"))
    r.ran = True
    r.cards_new, r.cards_resolved, r.duration = 0, 1, 11.24
    r.briefing = Briefing("H", [], "model", "x", "t", model="gemma4:26b")
    line = format_line(r, NOW)
    assert line.endswith("tick: issues changed -> checks: 0 new 1 resolved, "
                         "briefing regenerated (gemma4:26b, 11.2s)")
    r.errors = ["issues: GitHub 502: bad gateway"]
    assert format_line(r, NOW, 120).endswith("! issues: GitHub 502: bad gateway; retry in 2m")
    assert BACKOFF_CAP_S == 900


def test_cli_refresh_commands_report_busy(tmp_path, monkeypatch, capsys):
    from jarvis import cli

    path = tmp_path / "c.db"
    monkeypatch.setenv("JARVIS_DB", str(path))
    monkeypatch.setenv("JARVIS_HARDWARE", str(tmp_path / "none.yaml"))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with refresh_lock(path):
        assert cli.main(["brief", "--no-model"]) == 75
        assert "already running" in capsys.readouterr().err
        assert cli.main([]) == 0  # default: no sync, shows what is stored
        out = capsys.readouterr()
        assert "already running" in out.err and "No briefing stored yet." in out.out


def test_loop_notifies_only_when_cards_are_new(env):
    sent = []
    d, ft, lines = make_daemon(env, max_sleeps=40, notify=lambda s, b: sent.append((s, b)))
    d.run()
    first = d.ticks[0]
    assert first.cards_new > 0
    assert len(sent) == 1  # first tick only; later skipped ticks stay quiet
    assert sent[0][0].startswith(f"{first.cards_new} new suggestion")
