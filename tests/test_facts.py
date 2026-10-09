from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jarvis import db
from jarvis.facts import Fact, compute_facts, facts_hash, match_issue, stem, tokens

# Fixed "now": Oct 8 2026, 10:00 at UTC-7 (a local evening/morning offset that matters).
LOCAL = timezone(timedelta(hours=-7))
NOW = datetime(2026, 10, 8, 10, 0, tzinfo=LOCAL)


def by_id(facts):
    return {f.id: f for f in facts}


def ms(conn, number, title, open_, closed, due=None):
    db.upsert_milestone(conn, {"number": number, "title": title, "state": "open",
                               "open_issues": open_, "closed_issues": closed, "due_on": due})


def issue(conn, number, title, state="open", milestone=None, labels=(), created="2026-09-01T00:00:00Z",
          updated="2026-10-07T00:00:00Z", closed=None):
    db.upsert_issue(conn, {"number": number, "title": title, "state": state, "labels": list(labels),
                           "milestone_number": milestone, "created_at": created,
                           "updated_at": updated, "closed_at": closed})


def commit(conn, sha, message, when):
    db.upsert_commit(conn, {"sha": sha, "message": message, "committed_at": when})


def seeded():
    conn = db.connect(":memory:")
    ms(conn, 1, "Core demo", 9, 0, "2026-10-18T07:00:00Z")
    ms(conn, 2, "v1.0", 7, 3, "2026-10-31T00:00:00Z")
    ms(conn, 3, "v1.1", 3, 1)
    issue(conn, 31, "Timestamp each detection when its run closes", milestone=1)
    issue(conn, 28, "Show detections on the live radar plot", milestone=1)
    issue(conn, 7, "Create track.c with a person-size filter", milestone=1)
    issue(conn, 13, "Watch for PID hunting caused by the direction error", milestone=1,
          updated="2026-09-01T00:00:00Z")
    issue(conn, 40, "UART command interface", milestone=2)
    issue(conn, 20, "TinyML classifier", labels=["someday"])
    issue(conn, 21, "Nimble analyst", labels=["someday"])
    issue(conn, 22, "Loose end")
    issue(conn, 27, "Custom shield PCB", state="closed", closed="2026-10-04T18:09:00Z")
    issue(conn, 5, "Old thing", state="closed", closed="2026-09-01T00:00:00Z")
    commit(conn, "02fb2d2aaaa", "Report detections as their run closes, with timestamps\n\nbody",
           "2026-10-06T01:52:25Z")
    commit(conn, "b455d42bbbb", "Radar plot: draw DET detections as red blips",
           "2026-10-05T00:21:39Z")
    commit(conn, "457810fcccc", "Fix typo in count_hits so track.c compiles", "2026-10-04T22:28:10Z")
    commit(conn, "e9256c6dddd", "Start track.c: tracker state enum", "2026-10-01T00:14:19Z")
    commit(conn, "0ld0000eeee", "Ancient sweep work", "2026-08-01T00:00:00Z")
    return conn


def test_text_normalizer():
    assert stem("closes") == stem("closed") == stem("closing") == stem("close") == "clos"
    assert stem("detections") == "detection"
    assert stem("run") == "run"
    assert tokens("Fix the track.c bug when its run closes") == ["bug", "run", "clos"]


def test_sorted_by_priority_then_id():
    facts = compute_facts(seeded(), now=NOW)
    keys = [(f.priority, f.id) for f in facts]
    assert keys == sorted(keys)
    assert facts[0].kind == "milestone_deadline"


def test_milestone_deadline_and_pace():
    f = by_id(compute_facts(seeded(), now=NOW))
    core = f["milestone_deadline:1"]
    # due_on 2026-10-18T07:00Z -> Oct 18 (UTC date), today Oct 8 local -> 10 days
    assert core.data["days_left"] == 10 and core.data["due"] == "2026-10-18"
    assert core.data["pace_per_day"] == 0.9 and core.priority == 1
    assert "Oct 18" in core.text and "0 of 9" in core.text and "0.9" in core.text
    v1 = f["milestone_deadline:2"]
    assert v1.data["days_left"] == 23 and v1.priority == 3
    assert v1.data["pace_per_day"] == 0.3  # 7 / 23 = 0.304
    assert "3 of 10" in v1.text
    assert "milestone_deadline:3" not in f
    assert f["milestone_progress:3"].priority == 4
    assert "1 of 4" in f["milestone_progress:3"].text


def test_due_date_uses_utc_date_not_local():
    conn = db.connect(":memory:")
    # 03:00Z on Oct 10 is still Oct 9 in UTC-7, but GitHub's due date is Oct 10.
    ms(conn, 1, "M", 2, 0, "2026-10-10T03:00:00Z")
    fact = by_id(compute_facts(conn, now=NOW))["milestone_deadline:1"]
    assert fact.data["due"] == "2026-10-10" and fact.data["days_left"] == 2
    assert fact.data["pace_per_day"] == 1.0


def test_overdue_and_due_today():
    conn = db.connect(":memory:")
    ms(conn, 1, "Late", 4, 1, "2026-10-05T00:00:00Z")
    ms(conn, 2, "Today", 1, 1, "2026-10-08T00:00:00Z")
    f = by_id(compute_facts(conn, now=NOW))
    late = f["milestone_deadline:1"]
    assert late.data["days_left"] == -3 and late.priority == 1
    assert "overdue by 3 days" in late.text and late.data["pace_per_day"] is None
    assert "due today" in f["milestone_deadline:2"].text


def test_pace_rounding():
    conn = db.connect(":memory:")
    ms(conn, 1, "M", 5, 0, "2026-10-11T00:00:00Z")  # 5 / 3 = 1.666 -> 1.7
    assert by_id(compute_facts(conn, now=NOW))["milestone_deadline:1"].data["pace_per_day"] == 1.7


def test_maybe_done_true_positives_and_negative():
    f = by_id(compute_facts(seeded(), now=NOW))
    m31 = f["maybe_done:31"]
    assert not m31.certain and m31.priority == 2
    assert "may already be done" in m31.text and "commit:02fb2d2" in m31.evidence
    assert m31.data["method"] == "tokens" and m31.data["commits"][0]["score"] == 1.0
    m28 = f["maybe_done:28"]
    assert "commit:b455d42" in m28.evidence and m28.data["commits"][0]["score"] == 0.6
    m7 = f["maybe_done:7"]
    assert m7.data["method"] == "file" and m7.data["file"] == "track.c"
    assert "may already exist" in m7.data["note"] and len(m7.evidence) <= 3
    assert "maybe_done:13" not in f
    assert "maybe_done:40" not in f
    for fact in f.values():
        if fact.kind == "maybe_done":
            assert " is done" not in fact.text


def test_maybe_done_literal_reference():
    conn = db.connect(":memory:")
    issue(conn, 12, "servo_write returns a status")
    commit(conn, "aaa1111", "Unrelated wording entirely\n\ncloses #12", "2026-10-07T00:00:00Z")
    commit(conn, "bbb2222", "See #120 and PR/#12 and &#12", "2026-10-07T00:00:00Z")
    fact = by_id(compute_facts(conn, now=NOW))["maybe_done:12"]
    assert fact.data["method"] == "reference" and fact.evidence == ["#12", "commit:aaa1111"]
    assert not fact.certain


def test_match_ignores_commits_before_issue_and_partial_starts():
    rows = [
        {"sha": "old1234", "message": "Radar plot shows live detections", "committed_at": "2026-09-01T00:00:00Z"},
        {"sha": "new1234", "message": "Start the live radar plot of detections", "committed_at": "2026-10-07T00:00:00Z"},
    ]
    iss = {"number": 1, "title": "Show detections on the live radar plot", "created_at": "2026-09-15T00:00:00Z"}
    assert match_issue(iss, rows) == []


def test_adjusted_progress():
    f = by_id(compute_facts(seeded(), now=NOW))
    adj = f["adjusted_progress:1"]
    assert adj.data["maybe_done"] == [7, 28, 31] and adj.data["adjusted_closed"] == 3
    assert "3 of 9" in adj.text and not adj.certain and adj.priority == 2
    assert "adjusted_progress:2" not in f


def test_activity_quiet_gap_recently_closed():
    f = by_id(compute_facts(seeded(), now=NOW))
    act = f["activity"]
    # window is local dates >= Oct 1; e9256c6 (Oct 1 00:14Z) is Sep 30 local, so excluded
    assert act.data["count"] == 3
    assert act.data["first"] == "2026-10-04" and act.data["last"] == "2026-10-05"
    assert "Oct 4 to Oct 5" in act.text
    assert act.data["theme"][0] == "detection"
    # last commit 2026-10-06T01:52Z is Oct 5 local -> 3 days before Oct 8
    gap = f["quiet_gap"]
    assert gap.data["days_since"] == 3 and "Oct 5" in gap.text
    rc = f["recently_closed"]
    assert rc.data["count"] == 1 and rc.evidence == ["#27"] and "Oct 4" in rc.text


def test_no_quiet_gap_or_activity_when_appropriate():
    conn = db.connect(":memory:")
    commit(conn, "abc1234", "Recent work", "2026-10-07T20:00:00Z")
    f = by_id(compute_facts(conn, now=NOW))
    assert "quiet_gap" not in f and "activity" in f
    conn2 = db.connect(":memory:")
    commit(conn2, "abc1234", "Old work", "2026-09-01T20:00:00Z")
    f2 = by_id(compute_facts(conn2, now=NOW))
    assert "activity" not in f2 and f2["quiet_gap"].data["days_since"] == 37
    assert compute_facts(db.connect(":memory:"), now=NOW) == []


def test_stale_issue():
    f = by_id(compute_facts(seeded(), now=NOW))
    stale = [x for x in f.values() if x.kind == "stale_issue"]
    assert [x.id for x in stale] == ["stale_issue:13"]
    assert stale[0].data["days_since_update"] == 38  # Aug 31 local and stale[0].priority == 3


def test_stale_issue_only_in_due_soon_milestones():
    conn = db.connect(":memory:")
    ms(conn, 1, "Far", 1, 0, "2026-12-31T00:00:00Z")
    issue(conn, 1, "Old", milestone=1, updated="2026-01-01T00:00:00Z")
    assert not [x for x in compute_facts(conn, now=NOW) if x.kind == "stale_issue"]


def test_unassigned_work():
    u = by_id(compute_facts(seeded(), now=NOW))["unassigned_work"]
    assert u.data["count"] == 3 and u.data["labels"] == {"someday": 2} and u.data["unlabeled"] == 1
    assert u.text == "3 open issues have no milestone; 2 are labeled someday, 1 has no label."
    assert u.priority == 5


def test_facts_hash():
    a = compute_facts(seeded(), now=NOW)
    b = compute_facts(seeded(), now=NOW)
    assert facts_hash(a) == facts_hash(b)
    assert facts_hash(list(reversed(a))) == facts_hash(a)
    # a later time on the same local day changes nothing
    assert facts_hash(compute_facts(seeded(), now=NOW + timedelta(hours=1))) == facts_hash(a)
    conn = seeded()
    issue(conn, 50, "Brand new loose issue")
    assert facts_hash(compute_facts(conn, now=NOW)) != facts_hash(a)
    changed = [Fact(**{**a[0].__dict__, "text": a[0].text + " x"})] + a[1:]
    assert facts_hash(changed) != facts_hash(a)


def test_default_now_is_local_aware():
    facts = compute_facts(seeded())
    assert facts and all(isinstance(f, Fact) for f in facts)


# ---------------------------------------------------------------- next steps


def steps_seed(*, electrical=True, quiet=True):
    from jarvis.facts import compute_next_steps  # noqa: F401 (import check)
    conn = db.connect(":memory:")
    ms(conn, 1, "Core demo", 6, 1, "2026-10-18T07:00:00Z")
    ms(conn, 2, "v1.0", 2, 0, "2026-10-31T00:00:00Z")
    issue(conn, 31, "Timestamp each detection when its run closes", milestone=1,
          labels=["phase-2"])
    issue(conn, 12, "servo_write returns a status", milestone=1, labels=["phase-2"])
    issue(conn, 9, "State machine: survey / track / lost", milestone=1, labels=["phase-2"])
    issue(conn, 15, "Calibrate the sweep", milestone=1, labels=["phase-1"])
    issue(conn, 26, "Core demo plan", milestone=1)
    issue(conn, 30, "Someday driver", milestone=1, labels=["phase-3"])
    issue(conn, 2, "Down sweeps read wider", milestone=2, labels=["phase-1"])
    issue(conn, 40, "UART command interface", milestone=2, labels=["phase-1"])
    when = "2026-10-06T01:52:25Z" if quiet else "2026-10-08T15:00:00Z"
    commit(conn, "02fb2d2aaaa", "Report detections as their run closes, with timestamps", when)
    if electrical:
        for k, (cid, st) in enumerate([("rail_budget:5V", "unknown"),
                                       ("voltage_window:MG90S", "unknown"),
                                       ("missing:rails.5V.max_current_a", "unknown"),
                                       ("missing:rails.5V.tolerance_pct", "unknown")]):
            conn.execute("INSERT INTO electrical_findings (check_id, status, title) "
                         "VALUES (?, ?, ?)", (cid, st, cid))
            if not cid.startswith("missing:"):
                conn.execute("INSERT INTO cards (type, title, why, check_id, status, "
                             "fingerprint) VALUES ('electrical', ?, 'w', ?, 'active', ?)",
                             (cid, cid, f"fp{k}"))
        conn.commit()
    return conn


def test_next_steps_ranking_maybe_done_first_then_phase_order_blockers_and_gap():
    from jarvis.facts import NEXT_STEPS_MAX, compute_next_steps
    steps = compute_next_steps(steps_seed(), now=NOW)
    assert [s.rank for s in steps] == list(range(1, len(steps) + 1))
    assert len(steps) == NEXT_STEPS_MAX
    kinds = [s.kind for s in steps]
    assert kinds == ["verify_close", "milestone_issue", "milestone_issue", "blocker",
                     "quiet_gap"]
    v = steps[0]
    assert not v.certain and "#31" in v.text and "may already be done" in v.text
    assert "#31" in v.evidence and "commit:02fb2d2" in v.evidence
    # nearest-due milestone (Core demo), phase-1 before phase-2, then by number;
    # #31 is skipped (maybe done)
    assert "#15" in steps[1].text and "#9 " in steps[2].text
    assert "milestone Core demo" in steps[1].text and "Oct 18" in steps[1].text
    b = steps[3]
    assert "2 hardware.yaml values" in b.text and "rails.5V.max_current_a" in b.text
    assert "2 electrical check cards" in b.text
    assert "check:missing:rails.5V.tolerance_pct" in b.evidence
    assert steps[4].text == "No commits for 3 days; the last was 02fb2d2 on Oct 5."


def test_next_steps_fill_with_milestone_issues_when_no_blockers_or_gap():
    from jarvis.facts import compute_next_steps
    steps = compute_next_steps(steps_seed(electrical=False, quiet=False), now=NOW)
    assert [s.kind for s in steps] == ["verify_close"] + ["milestone_issue"] * 4
    nums = [s.evidence[0] for s in steps[1:]]
    assert nums == ["#15", "#9", "#12", "#30"]  # phase-1, phase-2 by number, phase-3


def test_next_steps_fact_is_low_priority_and_carries_evidence():
    from jarvis.facts import NEXT_STEPS_PRIORITY
    facts = compute_facts(steps_seed(), now=NOW)
    f = by_id(facts)["next_steps"]
    assert f.priority == NEXT_STEPS_PRIORITY and facts[-1].id == "next_steps"
    assert f.text.startswith("Suggested next steps (computed by Jarvis, not a schedule): 1.")
    assert "#15" in f.evidence and len(f.data["steps"]) == 5
    assert not f.certain


def test_no_next_steps_on_empty_db():
    from jarvis.facts import compute_next_steps
    conn = db.connect(":memory:")
    assert compute_next_steps(conn, now=NOW) == []
    assert "next_steps" not in by_id(compute_facts(conn, now=NOW))
