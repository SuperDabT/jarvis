from __future__ import annotations

from datetime import datetime

from jarvis import db
from jarvis.briefing import (
    BANNED_PATTERNS, build_briefing, check_numbers, render_briefing, validate,
)
from jarvis.facts import Fact, compute_facts
from jarvis.ollama import OllamaError
from test_facts import NOW, seeded


class FakeClient:
    model = "fake:1b"

    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, 0

    def chat_json(self, system, user, schema):
        self.calls += 1
        if self.error:
            raise self.error
        return self.reply


DEADLINE = Fact("milestone_deadline:1", "milestone_deadline", 1,
                "Milestone Core demo is due Oct 18, in 10 days, with 0 of 9 issues closed; "
                "finishing the 9 open needs 0.9 issues per day.",
                {"milestone": "Core demo", "due": "2026-10-18", "days_left": 10, "closed": 0,
                 "total": 9, "open": 9, "pace_per_day": 0.9}, ["milestone:Core demo"])
MAYBE = Fact("maybe_done:28", "maybe_done", 2,
             '#28 "Show detections on the live radar plot" may already be done: commit '
             'b455d42 ("Radar plot: draw DET detections as red blips") describes similar work.',
             {"issue": 28, "commits": [{"sha": "b455d42", "date": "2026-10-05"}]},
             ["#28", "commit:b455d42"], certain=False)
GAP = Fact("quiet_gap", "quiet_gap", 3, "The last commit was 3 days ago, on Oct 5.",
           {"days_since": 3, "last_commit": "02fb2d2", "last_date": "2026-10-05"},
           ["commit:02fb2d2"])
FACTS = [DEADLINE, MAYBE, GAP]

GOOD_HEAD = {"text": "Core demo is due Oct 18 with 0 of 9 closed",
             "fact_ids": ["milestone_deadline:1"]}


def s(text, *ids):
    return {"text": text, "fact_ids": list(ids)}


def test_validation_keeps_good_and_drops_bad():
    raw = {"headline": GOOD_HEAD, "sentences": [
        s("Core demo is due Oct 18, in 10 days, with 0 of 9 issues closed.",
          "milestone_deadline:1"),
        s("#28 may already be done, based on commit b455d42.", "maybe_done:28"),
        s("The last commit was 3 days ago, on Oct 5.", "quiet_gap"),
        s("Work has slowed down."),                                     # uncited
        s("The last commit was 3 days ago.", "quiet_gap", "made_up"),   # unknown id
        s("Core demo is due Oct 18 and is 40% done.", "milestone_deadline:1"),  # invented number
        s("Core demo is due Oct 20.", "milestone_deadline:1"),          # invented date
        s("#28 is done.", "maybe_done:28"),                             # unhedged guess
        s("#28 may be done via commit abc1234f.", "maybe_done:28"),     # invented sha
        s("Core demo is due in 10 days, so you should hurry.", "milestone_deadline:1"),
        s("Great progress: the last commit was 3 days ago!", "quiet_gap"),
        s("Core demo has nine open issues and eleven closed.", "milestone_deadline:1"),
    ]}
    head, kept, dropped = validate(raw, FACTS)
    assert head is not None and head.fact_ids == ["milestone_deadline:1"]
    assert [k.text[:20] for k in kept] == ["Core demo is due Oct", "#28 may already be d",
                                          "The last commit was "]
    assert [k.uncertain for k in kept] == [False, True, False]
    reasons = [why for _, why in dropped]
    assert reasons[0] == "cites no facts"
    assert reasons[1].startswith("unknown fact id: made_up")
    assert reasons[2] == "number not in cited facts: 40"
    assert reasons[3] == "date not in cited facts: Oct 20"
    assert reasons[4] == "cites an uncertain fact without hedging"
    assert reasons[5].startswith("commit id not in cited facts")
    assert reasons[6] == "banned wording (advice: should)"
    assert reasons[7].startswith("banned wording")
    assert reasons[8] == "number not in cited facts: eleven"
    assert len(dropped) == 9


def test_numbers_must_come_from_cited_facts_only():
    # 3 is in quiet_gap, not in the deadline fact.
    assert check_numbers("Core demo is due in 3 days.", [DEADLINE]) is not None
    assert check_numbers("Core demo is due in 3 days.", [DEADLINE, GAP]) is None
    # ISO dates in data allow the short form; decimals and issue numbers are checked.
    assert check_numbers("#28 changed on Oct 5 (b455d42).", [MAYBE]) is None
    assert check_numbers("It needs 0.9 per day.", [DEADLINE]) is None
    assert check_numbers("It needs 1.5 per day.", [DEADLINE]) is not None
    assert check_numbers("#29 may be done.", [MAYBE]) is not None


def test_banned_list_is_a_constant():
    words = " ".join(p for p, _ in BANNED_PATTERNS)
    for w in ("great", "awesome", "excellent", "unfortunately", "behind schedule",
              "falling behind", "urgent", "must", "should", "!"):
        assert w in words


def test_headline_failure_or_too_few_sentences_falls_back():
    conn = seeded()
    facts = compute_facts(conn, now=NOW)
    head = {"text": "Status", "fact_ids": [facts[0].id]}
    one_good = {"headline": head, "sentences": [s("Status of things.", facts[0].id),
                                                s("Uncited.")]}
    b = build_briefing(conn, FakeClient(one_good), now=NOW)
    assert b.source == "template" and "only 1 sentence" in b.fallback_reason
    assert b.dropped == [("Uncited.", "cites no facts")]

    bad_head = {"headline": {"text": "Huge week!", "fact_ids": [facts[0].id]},
                "sentences": [s("A.", facts[0].id), s("B.", facts[0].id)]}
    b = build_briefing(conn, FakeClient(bad_head), now=NOW, force=True)
    assert b.source == "template" and "headline" in b.fallback_reason


def test_model_briefing_is_stored_and_cached():
    conn = seeded()
    facts = {f.id: f for f in compute_facts(conn, now=NOW)}
    assert "milestone_deadline:1" in facts and "maybe_done:28" in facts
    reply = {"headline": {"text": "Core demo is due Oct 18", "fact_ids": ["milestone_deadline:1"]},
             "sentences": [
                 s("Core demo is due Oct 18 with 0 of 9 issues closed.", "milestone_deadline:1"),
                 s("#28 may already be done.", "maybe_done:28"),
                 s("Nothing else!", "milestone_deadline:1")]}
    client = FakeClient(reply)
    b = build_briefing(conn, client, now=NOW)
    assert client.calls == 1 and b.source == "model" and b.model == "fake:1b"
    assert len(b.sentences) == 2 and b.sentences[1].uncertain
    assert b.dropped == [("Nothing else!", "banned wording (exclamation mark)")]

    again = build_briefing(conn, client, now=NOW)
    assert client.calls == 1 and again.from_cache
    assert (again.headline, again.sentences, again.source, again.model, again.facts_hash) == (
        b.headline, b.sentences, b.source, b.model, b.facts_hash)

    forced = build_briefing(conn, client, now=NOW, force=True)
    assert client.calls == 2 and not forced.from_cache
    assert conn.execute("SELECT COUNT(*) FROM briefings").fetchone()[0] == 2  # history kept

    # A fact change (new commit) changes the hash, so the cache misses.
    db.upsert_commit(conn, {"sha": "fff0000aaaa", "message": "Tune servo",
                            "committed_at": "2026-10-07T12:00:00Z"})
    build_briefing(conn, client, now=NOW)
    assert client.calls == 3


def test_fallback_on_model_error():
    conn = seeded()
    client = FakeClient(error=OllamaError("timed out"))
    b = build_briefing(conn, client, now=NOW)
    assert b.source == "template" and "timed out" in b.fallback_reason
    facts = compute_facts(conn, now=NOW)
    expected = [f.text for f in facts if f.priority <= 3][:6]
    assert [x.text for x in b.sentences] == expected
    assert b.headline == "Milestone Core demo is due Oct 18"
    # A cached template is not reused while a model client is offered...
    build_briefing(conn, client, now=NOW)
    assert client.calls == 2
    # ...but it is when running without a model.
    assert build_briefing(conn, None, now=NOW).from_cache


def test_no_model_template_and_render():
    conn = seeded()
    b = build_briefing(conn, None, now=NOW)
    text = render_briefing(b, now=NOW)
    lines = text.splitlines()
    assert lines[0] == b.headline and lines[1] == ""
    assert any(line.startswith("~ ") for line in lines)  # uncertain marker
    assert "source: template (model off)" in lines[-1]
    assert f"based on {b.fact_count} facts" in lines[-1]
    local = datetime.fromisoformat(b.created_at).astimezone().strftime("%H:%M")
    assert f"updated {local}" in lines[-1]


def test_empty_database_gives_template():
    conn = db.connect(":memory:")
    client = FakeClient(reply={})
    b = build_briefing(conn, client, now=NOW)
    assert client.calls == 0 and b.source == "template" and b.sentences == []
    assert "Nothing to report" in render_briefing(b, now=NOW)


def test_trailing_fact_ids_in_text_are_stripped_before_checks():
    raw = {"headline": {"text": "Core demo due Oct 18 (milestone_deadline:1)",
                        "fact_ids": ["milestone_deadline:1"]},
           "sentences": [s("The last commit was on Oct 5 (quiet_gap, milestone_deadline:1).",
                           "quiet_gap", "milestone_deadline:1"),
                         s("Core demo is due Oct 18 {milestone_deadline:1}.",
                           "milestone_deadline:1"),
                         s("Core demo is due Oct 18 (see 1).", "milestone_deadline:1")]}
    head, kept, dropped = validate(raw, FACTS)
    assert head.text == "Core demo due Oct 18"
    assert kept[0].text == "The last commit was on Oct 5."
    assert kept[1].text == "Core demo is due Oct 18."
    assert dropped == [("Core demo is due Oct 18 (see 1).", "number not in cited facts: 1")]
