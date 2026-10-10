"""Finding details for the inspector (no Qt): the field's place in hardware.yaml,
verified text vs rule text vs AI wording, the source and the next step.
Databases are in memory; hardware.yaml lives under tmp_path."""
from __future__ import annotations

import pytest
from test_gui_data import REPO

from jarvis import db
from jarvis.config import Config
from jarvis.gui.finding_view import load_finding, locate
from jarvis.pipeline import run_checks

HW = """version: 1
rails:
  5V:
    nominal_v: 5.0
    tolerance_pct: null
    max_current_a: null
    ground_common: null
    todo: {ground_common: "check the buck converter's GND pin goes to the Nucleo GND"}
  3V3: {nominal_v: 3.3, tolerance_pct: 2, max_current_a: 0.5, ground_common: true}
parts:
  NUCLEO: {rail: 3V3, v_min: 3.0, v_max: 3.6, current_peak_a: 0.1}
  MG90S: {rail: 5V, count: 2, v_min: 4.8, v_max: 6.0, current_typ_a: 0.25, current_peak_a: 0.7}
  TF-Luna: {rail: 5V, v_min: 3.7, v_max: 5.2, current_peak_a: 0.15}
signals:
  - {from: NUCLEO.PA8, to: MG90S.signal, logic_v: 3.3, to_max_v: 5.5}
"""


@pytest.fixture
def env(tmp_path):
    hw = tmp_path / "hardware.yaml"
    hw.write_text(HW)
    cfg = Config(repo=REPO, db_path=tmp_path / "jarvis.db", hardware_path=hw)
    conn = db.connect(":memory:")
    run_checks(conn, cfg, None)          # stores the findings and cards, no model
    return conn, cfg, hw


@pytest.mark.parametrize("text, field, out", [
    ("rails:\n  5V:\n    nominal_v: 5\n    ground_common: null\n", "rails.5V.ground_common",
     (4, True)),
    ("rails:\n  5V: {nominal_v: 5, ground_common: null}\n", "rails.5V.ground_common", (2, True)),
    ("rails:\n  5V:\n    nominal_v: 5\nparts: {}\n", "rails.5V.ground_common", (2, False)),
    ("rails:\n  3V3: {nominal_v: 3.3}\n", "rails.5V.ground_common", (None, False)),
    ("parts:\n  # MG90S: commented out\n  MG90S:\n    v_min: 4.8\n", "parts.MG90S.v_min",
     (4, True)),
    ("rails:\n  5V:\n    nominal_v: 5\nparts:\n  X:\n    ground_common: 1\n",
     "rails.5V.ground_common", (2, False)),        # never matched in the next section
    ("rails: {}\n", "signals[0].logic_v", (None, False)),
])
def test_locate(text, field, out):
    assert locate(text, field) == out


def test_unknown_ground_has_its_field_line_todo_and_next_step(env):
    conn, cfg, hw = env
    v = load_finding(conn, cfg, "common_ground:5V")
    assert v.status == "unknown" and v.status_words == "can't be confirmed"
    assert v.why.startswith("MG90S on the 5V rail get 1 signal from NUCLEO")   # verified
    assert "shares ground with the MCU" in v.explain and "no reference" in v.matters
    assert v.fields == ("rails.5V.ground_common",)
    src = v.source
    assert (src.kind, src.path, src.field, src.line, src.present) == (
        "file", str(hw), "rails.5V.ground_common", 7, True)
    assert HW.splitlines()[src.line - 1].strip() == "ground_common: null"
    assert src.url == hw.resolve().as_uri()
    assert "multimeter" in v.next_step and "rails.5V.ground_common in hardware.yaml" in v.next_step
    assert "Jarvis doesn't edit files" in src.edit_note
    assert "common_ground:5V" in v.explain_question and v.prompt
    assert v.ai_why is None


def test_missing_value_uses_the_files_own_todo(env):
    conn, cfg, _ = env
    v = load_finding(conn, cfg, "missing:rails.5V.ground_common")
    assert "Nucleo GND" in v.todo and "Nucleo GND" in v.next_step


def test_failing_check_gets_the_rule_step_and_no_field(env, tmp_path):
    conn, cfg, hw = env
    hw.write_text(HW.replace("current_peak_a: 0.1}", "current_peak_a: 0.6}"))
    v = load_finding(conn, cfg, "rail_budget:3V3")
    assert v.status == "fail" and v.fields == () and v.source.field is None
    assert v.next_step.startswith("Lower the worst-case load")


def test_ai_wording_only_when_the_model_rewrote_it(env):
    conn, cfg, _ = env
    assert load_finding(conn, cfg, "rail_budget:5V").ai_why is None      # template only
    conn.execute("UPDATE electrical_findings SET model_result = 'model', model = 'gemma4:26b', "
                 "model_why = 'The 5V supply may be too small for both servos.' "
                 "WHERE check_id = 'rail_budget:5V'")
    v = load_finding(conn, cfg, "rail_budget:5V")
    assert v.ai_why == "The 5V supply may be too small for both servos."
    assert v.ai_model == "gemma4:26b" and v.why != v.ai_why       # verified text kept apart
    conn.execute("UPDATE electrical_findings SET model_result = 'rejected' "
                 "WHERE check_id = 'rail_budget:5V'")
    assert load_finding(conn, cfg, "rail_budget:5V").ai_why is None


def test_repo_copy_links_to_github_with_the_line(env):
    conn, cfg, _ = env
    db.upsert_repo_file(conn, "hardware.yaml", "sha1", HW)
    v = load_finding(conn, cfg, "common_ground:5V")
    assert v.source.kind == "repo" and v.source.path == "hardware.yaml"
    assert v.source.url == f"https://github.com/{REPO}/blob/HEAD/hardware.yaml#L7"
    assert "in the repo and push" in v.source.edit_note


def test_no_hardware_file_or_unknown_id(tmp_path):
    cfg = Config(repo=REPO, db_path=tmp_path / "j.db", hardware_path=tmp_path / "none.yaml")
    conn = db.connect(":memory:")
    assert load_finding(conn, cfg, "common_ground:5V") is None
    (tmp_path / "none.yaml").write_text(HW)
    assert load_finding(conn, cfg, "no_such:check") is None
