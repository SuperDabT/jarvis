"""gui/data.py: view models loaded from SQLite, no Qt needed."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from jarvis import cards, db
from jarvis.gui import data

REPO = "me/robot"
NOW = datetime(2026, 10, 8, 16, 0, tzinfo=UTC)

EVIDENCE = {
    "milestone_deadline:1": ["milestone:Core demo"],
    "maybe_done:28": ["#28", "commit:b455d42"],
    "adjusted_progress:1": ["milestone:Core demo", "#28"],
}


def seed(conn, *, briefing: bool = True, n_cards: int = 2) -> list[int]:
    """A briefing (one certain, one uncertain sentence) and ``n_cards`` cards."""
    db.upsert_milestone(conn, {"number": 1, "title": "Core demo", "state": "open",
                               "html_url": f"https://github.com/{REPO}/milestone/1"})
    if briefing:
        sentences = [
            {"text": "Core demo is due Oct 18, with 0 of 9 issues closed.",
             "fact_ids": ["milestone_deadline:1"], "uncertain": False},
            {"text": "#28 may already be done, since commit b455d42 looks like the same work.",
             "fact_ids": ["maybe_done:28", "adjusted_progress:1"], "uncertain": True},
        ]
        db.insert_briefing(conn, "h1", "Old headline", "[]", "template",
                           "2026-10-07T10:00:00+00:00")
        db.insert_briefing(conn, "h2", "Core demo due Oct 18, 0 of 9 closed",
                           json.dumps(sentences), "model", "2026-10-08T15:30:00+00:00",
                           "gemma4:26b")
    ids = []
    specs = [
        ("electrical", "Power check: 5 V rail rating unknown, worst case 1.55 A",
         "Two servos and the LiDAR share the 5 V rail.", "#12",
         f"https://github.com/{REPO}/issues/12", "rail_5v"),
        ("research", "Paper: sparse LiDAR scan matching", "Relates to #30.", "#30",
         None, None),
        ("electrical", "Logic level: TF-Luna TX into a 3.3 V pin", "UART levels differ.",
         "hardware.yaml", None, "uart_level"),
    ]
    for type_, title, why, ref, url, check in specs[:n_cards]:
        cid, _ = cards.upsert_card(conn, type_=type_, title=title, why=why, source_ref=ref,
                                   source_url=url, claude_prompt=f"Prompt for {title}",
                                   check_id=check, paper_id=None if check else "2410.00001")
        ids.append(cid)
    return ids


def test_latest_briefing_with_uncertain_flags_and_links():
    conn = db.connect(":memory:")
    seed(conn)
    b = data.load_briefing(conn, REPO, evidence=EVIDENCE)
    assert b is not None
    assert b.headline == "Core demo due Oct 18, 0 of 9 closed"
    assert b.source == "model" and b.source_label == "gemma4:26b"
    assert [s.uncertain for s in b.sentences] == [False, True]
    first, second = b.sentences
    assert first.links == (data.EvidenceLink(
        "Core demo milestone", f"https://github.com/{REPO}/milestone/1"),)
    labels = [lk.label for lk in second.links]
    # issues first, then commits, then milestones; duplicates (#28 twice) collapsed
    assert labels == ["#28", "b455d42", "Core demo milestone"]
    assert second.links[0].url == f"https://github.com/{REPO}/issues/28"
    assert second.links[1].url == f"https://github.com/{REPO}/commit/b455d42"


def test_template_source_label_and_unknown_facts_give_no_links():
    conn = db.connect(":memory:")
    db.insert_briefing(conn, "h", "Status", json.dumps(
        [{"text": "Something.", "fact_ids": ["gone:1"], "uncertain": False}]),
        "template", "2026-10-08T15:30:00+00:00")
    b = data.load_briefing(conn, REPO, evidence={})
    assert b.source_label == "template"
    assert b.sentences[0].links == ()


def test_briefing_links_from_real_facts():
    """Without an explicit map, evidence comes from facts.compute_facts."""
    conn = db.connect(":memory:")
    due = (datetime.now(UTC) + timedelta(days=5)).strftime("%Y-%m-%dT07:00:00Z")
    db.upsert_milestone(conn, {"number": 1, "title": "Core demo", "state": "open",
                               "due_on": due, "open_issues": 3, "closed_issues": 1,
                               "html_url": f"https://github.com/{REPO}/milestone/1"})
    db.insert_briefing(conn, "h", "Core demo soon", json.dumps(
        [{"text": "Core demo is due soon.", "fact_ids": ["milestone_deadline:1"],
          "uncertain": False}]), "template", "2026-10-08T15:30:00+00:00")
    b = data.load_briefing(conn, REPO)
    assert [lk.url for lk in b.sentences[0].links] == [f"https://github.com/{REPO}/milestone/1"]


def test_cards_grouped_by_type_in_fixed_order():
    conn = db.connect(":memory:")
    seed(conn, n_cards=3)
    groups = data.load_card_groups(conn)
    assert [(g.type, g.label) for g in groups] == [("electrical", "Electrical"),
                                                   ("research", "Research")]
    assert len(groups[0].cards) == 2 and len(groups[1].cards) == 1
    assert all(cv.is_new for g in groups for cv in g.cards)  # just created
    later = datetime.now(UTC) + timedelta(days=3)
    assert not any(cv.is_new for g in data.load_card_groups(conn, now=later) for cv in g.cards)


def test_done_cards_are_not_listed():
    conn = db.connect(":memory:")
    ids = seed(conn, n_cards=2)
    cards.mark_done(conn, ids[0])
    groups = data.load_card_groups(conn)
    assert [g.type for g in groups] == ["research"]


def test_empty_db():
    conn = db.connect(":memory:")
    wd = data.load_window_data(conn, REPO)
    assert wd.briefing is None
    assert wd.groups == ()
    assert wd.card_count == 0
    assert wd.last_sync is None and wd.updated_at is None
    assert wd.errors == ()


def test_window_data_updated_at_is_newest_of_sync_and_briefing():
    conn = db.connect(":memory:")
    seed(conn)
    conn.execute("INSERT INTO sync_log (ran_at, resource, status, changed) "
                 "VALUES ('2026-10-08T15:45:00+00:00', 'issues', 304, 0)")
    wd = data.load_window_data(conn, REPO)
    assert wd.updated_at == "2026-10-08T15:45:00+00:00"
    assert wd.card_count == 2
    # equal when nothing changed, so the window can skip re-rendering
    assert wd == data.load_window_data(conn, REPO)


def test_local_stamp():
    now = datetime.now(UTC)
    assert data.local_stamp(now.isoformat(), now) == now.astimezone().strftime("%H:%M")
    old = now - timedelta(days=3)
    assert data.local_stamp(old.isoformat(), now).startswith(old.astimezone().strftime("%b"))
    assert data.local_stamp(None) == ""


def _result(sync=None, reason=None, new=0, resolved=0, notes=()):
    return SimpleNamespace(sync=sync or {"issues": "unchanged"},
                           briefing=SimpleNamespace(fallback_reason=reason),
                           cards_new=new, cards_resolved=resolved, notes=list(notes))


def test_refresh_message():
    level, text = data.refresh_message(_result(new=2, resolved=1))
    assert level == "ok" and "2 new suggestions" in text and "1 resolved" in text
    level, text = data.refresh_message(_result(reason="model unavailable: cannot reach"))
    assert level == "warn" and text == "Ollama unavailable, showing template briefing"
    level, text = data.refresh_message(_result(sync={"issues": "error: GitHub 401: Bad"}))
    assert level == "warn" and "issues" in text
    level, text = data.refresh_message(_result(reason="model off", notes=["hardware.yaml missing"]))
    assert level == "ok" and text.endswith("hardware.yaml missing")


def test_describe_error():
    import httpx

    assert "Could not reach GitHub" in data.describe_error(httpx.ConnectError("boom"))
    assert data.describe_error(RuntimeError("x" * 300)).endswith("...")


def _hb(**kw):
    row = {"pid": 42, "interval_s": 180, "last_tick_at": "2026-10-08T15:58:00+00:00",
           "last_change_at": None, "next_tick_at": None, "last_error": None}
    row.update(kw)
    return row


def test_watch_status():
    assert data.watch_status(None, NOW) is None
    assert data.watch_status(_hb(last_tick_at=None), NOW) is None
    w = data.watch_status(_hb(), NOW)
    assert (w.watching, w.text) == (True, "watching · checked 2 min ago")
    assert data.watch_status(_hb(last_tick_at="2026-10-08T15:59:40+00:00"), NOW).text \
        == "watching · checked just now"
    # stale: older than 2 x interval
    stale = data.watch_status(_hb(last_tick_at="2026-10-08T15:53:00+00:00"), NOW)
    assert (stale.watching, stale.text) == (False, "not watching")
    # stopped daemon (pid cleared) or a --once run
    assert data.watch_status(_hb(pid=None), NOW).text == "not watching"
    err = data.watch_status(_hb(last_error="issues: GitHub 502"), NOW)
    assert "Last error: issues: GitHub 502" in err.tooltip


def test_window_data_includes_heartbeat():
    conn = db.connect(":memory:")
    assert data.load_window_data(conn, REPO, NOW).watch is None
    db.update_daemon_state(conn, pid=7, interval_s=180,
                           last_tick_at="2026-10-08T15:57:00+00:00")
    assert data.load_window_data(conn, REPO, NOW).watch.text == "watching · checked 3 min ago"


# ================================================================ dashboard view models

from datetime import date  # noqa: E402

from jarvis.checks import run_all  # noqa: E402
from jarvis.facts import Fact  # noqa: E402
from jarvis.hardware import load_hardware  # noqa: E402

TODAY = date(2026, 10, 8)


def _deadline(n=1, title="Core demo", days=10, closed=0, total=9, pace=0.9):
    due = TODAY + timedelta(days=days)
    return Fact(f"milestone_deadline:{n}", "milestone_deadline", 1, "t",
                {"milestone": title, "due": due.isoformat(), "days_left": days,
                 "closed": closed, "total": total, "open": total - closed,
                 "pace_per_day": pace if days > 0 else None})


def test_kpis_from_facts():
    facts = [
        _deadline(),
        _deadline(2, "v1.0", 23, 0, 7, 0.3),
        Fact("adjusted_progress:1", "adjusted_progress", 2, "t",
             {"milestone": "Core demo", "closed": 0, "total": 9, "maybe_done": [7, 28],
              "adjusted_closed": 2}, certain=False),
        Fact("quiet_gap", "quiet_gap", 3, "t",
             {"days_since": 3, "last_commit": "02fb2d2", "last_date": "2026-10-05"}),
    ]
    k = {x.key: x for x in data.build_kpis(facts, TODAY)}
    assert (k["due"].label, k["due"].value, k["due"].sub) == ("Core demo due", "10 days", "Oct 18")
    assert (k["closed"].value, k["closed"].sub) == ("0 / 9", "2 more may be done")
    assert "#7, #28" in k["closed"].tooltip
    assert (k["pace"].value, k["pace"].sub) == ("0.9/day", "to close on time")
    assert (k["last_commit"].value, k["last_commit"].sub) == ("3 days", "Oct 5 · 02fb2d2")


def test_kpis_overdue_and_no_milestones_and_recent_commit():
    k = {x.key: x for x in data.build_kpis([_deadline(days=-2, closed=3, total=5)], TODAY)}
    assert (k["due"].value, k["due"].tone) == ("overdue", "bad")
    assert k["due"].sub.startswith("by 2 days")
    assert (k["pace"].label, k["pace"].value) == ("Still open", "2")
    assert k["closed"].sub == "60% closed"
    act = Fact("activity", "activity", 3, "t", {"days": 7, "count": 4, "first": "2026-10-06",
                                                 "last": "2026-10-07", "theme": []})
    k = {x.key: x for x in data.build_kpis([act], TODAY)}
    assert k["due"].value == "none" and k["due"].tone == "muted"
    assert (k["last_commit"].value, k["last_commit"].sub) == ("yesterday", "4 commits in 7 days")
    k = {x.key: x for x in data.build_kpis([], TODAY)}
    assert k["last_commit"].value == "none"


def _ms(number, title, due, open_, closed, url=None):
    return {"number": number, "title": title, "due_on": due, "open_issues": open_,
            "closed_issues": closed, "html_url": url}


def test_milestone_timeline():
    tl = data.build_timeline([
        _ms(2, "v1.0", "2026-10-31T00:00:00Z", 7, 0),
        _ms(3, "v1.1", None, 3, 0),
        _ms(1, "Core demo", "2026-10-18T00:00:00Z", 9, 0, "u1"),
    ], {1: 2, 2: 0}, TODAY)
    assert [b.title for b in tl.bars] == ["Core demo", "v1.0", "v1.1"]  # due first, soonest first
    core, v10, v11 = tl.bars
    assert core.label == "Oct 18 · 0/9 (+2 maybe)"
    assert v10.label == "Oct 31 · 0/7"
    assert v11.label == "no due date · 0/3" and v11.due is None
    assert tl.start == date(2026, 10, 1)  # a week before today
    assert tl.end > date(2026, 10, 31)  # margin after the latest due date
    assert 0 < tl.frac(TODAY) < tl.frac(date(2026, 10, 18)) < tl.frac(date(2026, 10, 31)) < 1
    assert all(d.weekday() == 0 for d in tl.ticks)


def test_milestone_timeline_overdue_extends_start_and_caps_maybe():
    tl = data.build_timeline([_ms(1, "Late", "2026-09-20T00:00:00Z", 1, 4)], {1: 5}, TODAY)
    assert tl.start <= date(2026, 9, 20)
    assert tl.bars[0].maybe == 1  # never more than the open issues
    empty = data.build_timeline([], {}, TODAY)
    assert empty.bars == () and empty.end >= TODAY + timedelta(days=14)


def test_maybe_counts_from_facts():
    f = Fact("adjusted_progress:4", "adjusted_progress", 2, "t",
             {"closed": 1, "total": 5, "maybe_done": [3], "adjusted_closed": 2})
    assert data.maybe_counts([f]) == {4: 1}


def test_grid_levels():
    assert [data.grid_level(n) for n in (0, 1, 2, 3, 4, 5, 6, 40)] == [0, 1, 2, 2, 3, 3, 4, 4]


def test_contribution_grid_bucketing():
    days = ([TODAY] * 4 + [TODAY - timedelta(days=1)] * 6 + [TODAY - timedelta(days=2)]
            + [date(2025, 1, 1)])  # outside the 26 weeks: ignored
    g = data.build_grid(days, TODAY)
    assert g.weeks == 26
    assert g.start.weekday() == 0
    assert (TODAY - g.start).days == 25 * 7 + TODAY.weekday()
    assert g.days[-1].day == TODAY and g.days[-1].count == 4 and g.days[-1].level == 3
    assert g.days[-2].level == 4 and g.days[-3].level == 1
    assert len(g.days) == 25 * 7 + TODAY.weekday() + 1  # no future days
    assert {d.row for d in g.days if d.col == 0} == set(range(7))
    assert g.days[-1].col == 25 and g.days[-1].row == TODAY.weekday()
    assert g.total == 11 and g.active_days == 3
    assert g.longest_streak == 3 and g.current_streak == 3
    assert g.caption == ("11 commits in the last 6 months · active on 3 days · "
                         "longest streak 3 days")
    assert g.tooltip(g.days[-1]) == "4 commits on Thu, Oct 8"
    assert sum(1 for d in g.days if d.count == 0) == len(g.days) - 3


def test_contribution_grid_month_labels_and_empty():
    g = data.build_grid([], TODAY)
    assert g.total == 0 and g.caption == "0 commits in the last 6 months"
    cols = [c for c, _ in g.months]
    assert cols == sorted(cols)
    assert all(b - a >= 3 for a, b in zip(cols, cols[1:], strict=False))  # labels never crowd
    names = [n for _, n in g.months]
    assert names[-1] == "Oct" and "Sep" in names
    for col, name in g.months:
        assert (g.start + timedelta(weeks=col)).strftime("%b") == name
    # yesterday's streak survives an empty today
    g = data.build_grid([TODAY - timedelta(days=1), TODAY - timedelta(days=2)], TODAY)
    assert g.current_streak == 2


def test_commit_days_use_local_dates():
    conn = db.connect(":memory:")
    ts = "2026-10-06T01:52:25Z"
    db.upsert_commit(conn, {"sha": "a", "message": "m", "author": "t", "committed_at": ts,
                            "html_url": None})
    expect = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().date()
    assert data.commit_days(conn) == [expect]


def _issue(n, state, labels):
    return {"number": n, "state": state, "labels": json.dumps(labels)}


def test_phase_bars():
    pb = data.build_phase_bars([
        _issue(1, "open", ["phase-2"]), _issue(2, "closed", ["phase-1"]),
        _issue(3, "open", ["phase-1"]), _issue(4, "open", ["someday"]),
        _issue(5, "open", []), _issue(6, "open", ["phase-3", "phase-2"]),
        _issue(7, "open", ["bug"]), _issue(8, "closed", ["phase-10"]),
    ])
    assert [(b.label, b.open, b.closed) for b in pb.bars] == [
        ("phase-1", 1, 1), ("phase-2", 2, 0), ("phase-10", 0, 1), ("other labels", 1, 0),
        ("someday", 1, 0), ("unlabeled", 1, 0)]
    assert pb.bars[1].open_issues == (1, 6)  # lowest phase label wins
    assert pb.total_open == 6 and pb.max_total == 2
    assert data.build_phase_bars([]).bars == ()


HW_YAML = """
version: 1
rails:
  5V: {nominal_v: 5.0, tolerance_pct: null, max_current_a: null, ground_common: true}
  3V3: {nominal_v: 3.3, tolerance_pct: 2, max_current_a: 0.5, ground_common: true}
  USB: {nominal_v: 5.0, max_current_a: 0.5}
parts:
  MG90S: {rail: 5V, count: 2, v_min: 4.8, v_max: 6.0, current_typ_a: 0.25,
          current_peak_a: 0.7, notes: servo}
  TF-Luna: {rail: 5V, v_min: 3.7, v_max: 5.2, current_peak_a: 0.15}
  OLED: {rail: 3V3, planned: true, v_min: 3.0, v_max: 5.0, current_peak_a: 0.025}
  Sensor: {rail: 3V3, v_min: 3.0, v_max: 3.6, current_peak_a: 0.45}
"""


def _power():
    hw = load_hardware(HW_YAML)
    return {r.rail: r for r in data.build_power(hw, run_all(hw))}


def test_power_unknown_rating_and_tolerance():
    p5 = _power()["5V"]
    b = p5.budget
    assert [(s.part, s.amps, s.state) for s in b.segments] == [("MG90S", 1.4, "stall"),
                                                               ("TF-Luna", 0.15, "peak")]
    assert b.worst_case_a == 1.55 and b.rating_a is None and b.need_80_a == 1.94
    assert b.status == "unknown" and b.load_pct is None
    assert "needs ≥ 1.94 A" in b.caption and "MG90S ×2 stall 1.4 A" in b.caption
    w = p5.window
    assert w.example and (w.band_lo, w.band_hi) == (4.75, 5.25)
    by = {r.part: r for r in w.parts}
    assert by["MG90S"].low_gap == 0.05 and by["MG90S"].high_gap < 0  # band dips below 4.8 V
    assert by["TF-Luna"].high_gap == 0.05  # band goes over 5.2 V
    assert by["TF-Luna"].status == "unknown"
    assert "would dip below MG90S's 4.8 V min" in w.caption
    assert "exceed TF-Luna's 5.2 V max" in w.caption
    assert w.axis_lo <= 3.7 and w.axis_hi >= 6.0 and w.ticks[0] >= w.axis_lo


def test_power_known_rating_tolerance_and_planned():
    p = _power()
    assert list(p) == ["3V3", "5V"]  # USB has no parts: not shown
    b = p["3V3"].budget
    assert b.rating_a == 0.5 and b.worst_case_a == 0.45 and b.planned_a == 0.025
    assert b.load_pct == 90.0 and b.status == "warn"
    assert "90% of the 0.5 A rating, above the 80% target" in b.caption
    w = p["3V3"].window
    assert not w.example and (w.band_lo, w.band_hi) == (3.234, 3.366)
    assert {r.part: r.planned for r in w.parts} == {"OLED": True, "Sensor": False}
    assert "stays inside every part's range" in w.caption


def test_load_power_notes():
    conn = db.connect(":memory:")
    rails, note = data.load_power(conn, None)
    assert rails == () and "No hardware.yaml" in note
    db.upsert_repo_file(conn, "hardware.yaml", "s", "rails: {5V: {nominal_v: five}}")
    rails, note = data.load_power(conn, None)
    assert rails == () and "invalid" in note
    db.upsert_repo_file(conn, "hardware.yaml", "s", HW_YAML)
    rails, note = data.load_power(conn, None)
    assert note is None and {r.rail for r in rails} == {"5V", "3V3"}


def test_table_rows_status_tags_and_order():
    conn = db.connect(":memory:")
    ids = seed(conn, n_cards=3)  # rail_5v (elec), research, uart_level (elec)
    conn.execute("INSERT INTO electrical_findings (check_id, status) VALUES ('rail_5v', 'unknown')")
    conn.execute("INSERT INTO electrical_findings (check_id, status) VALUES ('uart_level', 'fail')")
    rows = data.build_rows(data.load_card_groups(conn), data.finding_statuses(conn))
    assert [r.id for r in rows] == [ids[2], ids[0], ids[1]]  # fail, needs value, research
    assert [(r.status_label, r.status_color) for r in rows] == [
        ("fail", "red"), ("needs value", "yellow"), ("new", "blue")]
    assert rows[0].type_label == "⚡ Electrical" and rows[2].type_label == "📄 Research"
    later = datetime.now(UTC) + timedelta(days=3)
    rows = data.build_rows(data.load_card_groups(conn, now=later), {})
    assert {r.status_label for r in rows} == {"open"}


def test_window_data_has_dashboard_parts():
    conn = db.connect(":memory:")
    seed(conn)
    db.upsert_repo_file(conn, "hardware.yaml", "s", HW_YAML)
    wd = data.load_window_data(conn, REPO, NOW)
    assert len(wd.kpis) == 4
    assert wd.timeline is not None and wd.grid is not None and wd.phases is not None
    assert {r.rail for r in wd.power} == {"5V", "3V3"} and wd.power_note is None
    assert len(wd.rows) == 2
    assert wd == data.load_window_data(conn, REPO, NOW)
