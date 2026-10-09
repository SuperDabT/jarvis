from __future__ import annotations

from dataclasses import replace

from jarvis import cards as cardlib
from jarvis import db
from jarvis.checks import run_all
from jarvis.config import Config
from jarvis.electrical import (
    active_counts, carded_findings, claude_prompt, sync_cards, validate_rewrite,
)
from jarvis.facts import compute_facts
from jarvis.hardware import load_hardware
from jarvis.ollama import OllamaError
from jarvis.pipeline import run_checks
from test_hardware import EXAMPLE

FIXED = EXAMPLE.read_text().replace(
    "    tolerance_pct: null\n    max_current_a: null\n    ground_common: null",
    "    tolerance_pct: 2\n    max_current_a: 3.0\n    ground_common: true", 1)


class FakeClient:
    model = "fake:1b"

    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, 0

    def chat_json(self, system, user, schema):
        self.calls += 1
        if self.error:
            raise self.error
        return self.reply(user) if callable(self.reply) else self.reply


def setup(text=None):
    conn = db.connect(":memory:")
    db.upsert_issue(conn, {"number": 29, "title": "Add SSD1306 OLED status display",
                           "state": "open", "html_url": "https://x/issues/29"})
    db.upsert_issue(conn, {"number": 12, "title": "Old closed thing", "state": "closed"})
    hw = load_hardware(text or EXAMPLE.read_text())
    return conn, hw, run_all(hw)


def active(conn):
    return {c.check_id: c for c in cardlib.list_cards(conn, "active") if c.type == "electrical"}


def test_cards_from_real_draft_without_model():
    conn, hw, findings = setup()
    r = sync_cards(conn, hw, findings)
    cards = active(conn)
    assert set(cards) == {"rail_budget:5V", "voltage_window:TF-Luna", "voltage_window:MG90S",
                          "common_ground:5V"}
    # missing:* findings are covered by the cards that need them.
    assert not any(k.startswith("missing:") for k in cards)
    assert r.new == 4 and r.resolved == 0
    c = cards["rail_budget:5V"]
    assert c.title == "Power check: 5V rail rating unknown, worst case 1.55 A"
    assert c.source_ref == "hardware.yaml" and c.fingerprint == "electrical|hardware.yaml|rail_budget:5V"
    assert c.source_url is None


def test_rerun_does_not_duplicate_and_fix_auto_resolves():
    conn, hw, findings = setup()
    sync_cards(conn, hw, findings)
    r = sync_cards(conn, hw, findings)
    assert r.new == 0 and r.resolved == 0
    assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 4
    hw2 = load_hardware(FIXED)
    r = sync_cards(conn, hw2, run_all(hw2))
    assert r.resolved == 4 and active(conn) == {}
    # The problem comes back: same card reopens, no new row.
    r = sync_cards(conn, hw, findings)
    assert r.new == 4
    assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 4


def test_done_card_never_returns():
    conn, hw, findings = setup()
    sync_cards(conn, hw, findings)
    cid = active(conn)["rail_budget:5V"].id
    cardlib.mark_done(conn, cid)
    sync_cards(conn, hw, findings)
    assert "rail_budget:5V" not in active(conn)
    assert cardlib.get_card(conn, cid).status == "done"


def test_uncovered_missing_value_gets_a_card():
    conn, hw, findings = setup()
    budget = next(f for f in findings if f.check_id == "rail_budget:5V")
    miss = next(f for f in findings if f.check_id == "missing:rails.5V.max_current_a")
    picked = {f.check_id for f in carded_findings([replace(budget, status="pass"), miss])}
    assert picked == {"missing:rails.5V.max_current_a"}


def test_claude_prompt_is_self_contained():
    conn, hw, findings = setup()
    f = next(f for f in findings if f.check_id == "voltage_window:TF-Luna")
    issue = conn.execute("SELECT * FROM issues WHERE number = 29").fetchone()
    p = claude_prompt(hw, f, issue)
    assert "STM32F446RE" in p and "pan-tilt" in p
    assert "2 x MG90S" in p and "1 x TF-Luna" in p
    assert "headroom_high_v: 0.2" in p and "5.25" in p
    assert "```yaml" in p and "v_max: 5.2" in p and "no over-voltage protection" in p
    assert "#29" in p and "https://x/issues/29" in p
    assert "Is this margin safe" in p and "shield" in p


# ---------------------------------------------------------------- model pass


def _luna(findings):
    return next(f for f in findings if f.check_id == "voltage_window:TF-Luna")


def test_validate_rewrite_accepts_good_text_and_open_issue():
    _, _, findings = setup()
    f = _luna(findings)
    t, w, i, why = validate_rewrite(
        {"issue": 29, "title": "TF-Luna has only 0.2 V of headroom",
         "why": "The 5V rail sits 0.2 V under the TF-Luna's 5.2 V limit. Its tolerance is "
                "unknown."}, f, {29})
    assert (t is not None, w is not None, i, why) == (True, True, 29, [])


def test_validate_rewrite_rejects_bad_parts_independently():
    _, _, findings = setup()
    f = _luna(findings)
    t, w, i, why = validate_rewrite(
        {"issue": 12, "title": "TF-Luna is safe at 5 V",
         "why": "It has 0.3 V of margin."}, f, {29})
    assert (t, w, i) == (None, None, None)
    assert any("not an open issue" in x for x in why)
    assert any("softens" in x for x in why)
    assert any("number not in cited facts: 0.3" in x for x in why)
    t, _, _, why = validate_rewrite(
        {"issue": 0, "title": "one two three four five six seven eight nine ten eleven",
         "why": "A. B. C."}, f, {29})
    assert t is None and any("10 words" in x for x in why) and any("sentences" in x for x in why)


def test_model_text_used_cached_and_linked():
    conn, hw, findings = setup()
    reply = {"issue": 0, "title": "Supply limit check", "why": "A value is missing."}

    def answer(user):
        if '"voltage_window:TF-Luna"' in user:
            return {"issue": 29, "title": "TF-Luna has only 0.2 V headroom",
                    "why": "At 5 V the TF-Luna's 5.2 V ceiling is 0.2 V away."}
        return reply

    client = FakeClient(answer)
    sync_cards(conn, hw, findings, client)
    assert client.calls == 4
    c = active(conn)["voltage_window:TF-Luna"]
    assert c.title == "TF-Luna has only 0.2 V headroom"
    assert c.why.endswith("Related: #29 Add SSD1306 OLED status display.")
    assert c.source_url == "https://x/issues/29"
    assert c.fingerprint == "electrical|hardware.yaml|voltage_window:TF-Luna"
    assert "#29" in c.claude_prompt
    # Unchanged findings: no new model calls, same text.
    sync_cards(conn, hw, findings, client)
    assert client.calls == 4
    assert active(conn)["voltage_window:TF-Luna"].title == c.title
    # Without a client the cached wording is still used.
    sync_cards(conn, hw, findings, None)
    assert active(conn)["voltage_window:TF-Luna"].title == c.title


def test_model_rejection_keeps_template_and_unavailable_is_not_cached():
    conn, hw, findings = setup()
    bad = FakeClient({"issue": 99, "title": "Rail is fine", "why": "All good at 9 V."})
    r = sync_cards(conn, hw, findings, bad)
    assert active(conn)["rail_budget:5V"].title.startswith("Power check: 5V rail rating unknown")
    assert r.notes and bad.calls == 4
    sync_cards(conn, hw, findings, bad)
    assert bad.calls == 4  # rejection is cached per finding hash
    down = FakeClient(error=OllamaError("down"))
    conn2, hw2, f2 = setup()
    sync_cards(conn2, hw2, f2, down)
    sync_cards(conn2, hw2, f2, down)
    assert down.calls == 8


# ---------------------------------------------------------------- pipeline + facts


def test_run_checks_sources(tmp_path):
    conn = db.connect(":memory:")
    cfg = Config(hardware_path=tmp_path / "none.yaml")
    new, resolved, notes = run_checks(conn, cfg, None)
    assert (new, resolved) == (0, 0) and "no hardware.yaml" in notes[0]
    assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0

    path = tmp_path / "hw.yaml"
    path.write_text(EXAMPLE.read_text())
    new, _, notes = run_checks(conn, Config(hardware_path=path), None)
    assert new == 4 and notes[0] == f"hardware: {path}"

    path.write_text("rails:\n  5V: {nominal_v: five}\n")
    new, resolved, notes = run_checks(conn, Config(hardware_path=path), None)
    assert (new, resolved) == (0, 0) and "rails.5V.nominal_v" in notes[0]
    assert len(active(conn)) == 4  # a broken file never resolves cards

    db.upsert_repo_file(conn, "hardware.yaml", "sha", FIXED)
    new, resolved, notes = run_checks(conn, Config(hardware_path=path), None)
    assert notes[0] == "hardware: repo hardware.yaml" and resolved == 4


def test_electrical_fact():
    conn, hw, findings = setup()
    assert not any(f.id == "electrical" for f in compute_facts(conn))
    sync_cards(conn, hw, findings)
    f = next(f for f in compute_facts(conn) if f.id == "electrical")
    assert f.priority == 3 and f.certain
    assert f.data == {"active": 4, "by_status": {"unknown": 4}}
    assert f.text.startswith("4 electrical check cards are active: 4 unconfirmed")
    assert active_counts(conn) == {"unknown": 4}
    conn.execute("UPDATE electrical_findings SET status = 'fail' "
                 "WHERE check_id = 'rail_budget:5V'")
    f = next(f for f in compute_facts(conn) if f.id == "electrical")
    assert f.priority == 2 and f.data["by_status"] == {"fail": 1, "unknown": 3}
