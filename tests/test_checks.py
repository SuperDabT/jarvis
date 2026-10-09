from __future__ import annotations

from jarvis.checks import (
    common_ground, logic_level, missing_values, planned_parts, rail_budget, run_all,
    voltage_window,
)
from jarvis.hardware import load_hardware
from test_hardware import EXAMPLE


def hw_with(rail="nominal_v: 5.0, tolerance_pct: 2, max_current_a: 3.0, ground_common: true",
            extra_parts="", signals=""):
    return load_hardware(f"""
rails:
  5V: {{{rail}}}
  3V3: {{nominal_v: 3.3, max_current_a: 0.5, ground_common: true}}
parts:
  MCU: {{rail: null}}
  Servo: {{rail: 5V, count: 2, v_min: 4.8, v_max: 6.0, current_typ_a: 0.25,
          current_peak_a: 0.7, source: stall current}}
  Luna: {{rail: 5V, v_min: 3.7, v_max: 5.2, current_typ_a: 0.07, current_peak_a: 0.15}}
{extra_parts}
signals:
  - {{from: MCU.PA9, to: Luna.RX, logic_v: 3.3, to_max_v: 3.3}}
{signals}
""")


def by_id(findings):
    return {f.check_id: f for f in findings}


# ---------------------------------------------------------------- voltage window


def test_voltage_window_pass_with_known_tolerance():
    hw = hw_with()
    f = voltage_window(hw, hw.parts["Luna"])
    assert f.status == "pass"
    assert f.numbers["rail_max_v"] == 5.1 and f.numbers["headroom_high_v"] == 0.1


def test_voltage_window_fail_when_worst_case_over():
    hw = hw_with(rail="nominal_v: 5.0, tolerance_pct: 5, max_current_a: 3.0")
    f = voltage_window(hw, hw.parts["Luna"])
    assert f.status == "fail"
    assert "5.25" in f.why and "0.05" in f.why
    assert voltage_window(hw, hw.parts["Servo"]).status == "fail"  # 4.75 < 4.8


def test_voltage_window_warn_when_tight():
    hw = hw_with(rail="nominal_v: 5.0, tolerance_pct: 3.5, max_current_a: 3.0")
    f = voltage_window(hw, hw.parts["Luna"])  # 5.175 V vs 5.2 V: 0.025 V = 0.5 %
    assert f.status == "warn"
    assert f.numbers["headroom_high_v"] == 0.025


def test_voltage_window_unknown_tolerance_states_the_margin():
    hw = hw_with(rail="nominal_v: 5.0, max_current_a: 3.0")
    f = voltage_window(hw, hw.parts["Luna"])
    assert f.status == "unknown"
    assert f.numbers["headroom_high_v"] == 0.2 and f.numbers["headroom_high_pct"] == 4.0
    assert f.numbers["example_rail_v"] == 5.25
    assert "0.2 V (4%)" in f.why and "5.25 V, past the limit" in f.why
    assert f.missing == ["rails.5V.tolerance_pct"]


def test_voltage_window_unknown_tolerance_but_wide_margin_passes():
    hw = hw_with(rail="nominal_v: 3.3, max_current_a: 3.0",
                 extra_parts="  Wide: {rail: 5V, v_min: 1.8, v_max: 5.5}")
    assert voltage_window(hw, hw.parts["Wide"]).status == "pass"


def test_voltage_window_nominal_outside_fails_even_without_tolerance():
    hw = hw_with(rail="nominal_v: 5.5, max_current_a: 3.0")
    assert voltage_window(hw, hw.parts["Luna"]).status == "fail"


# ---------------------------------------------------------------- budget


def test_rail_budget_pass_warn_fail():
    assert rail_budget(hw_with(), hw_with().rails["5V"]).status == "pass"
    hw = hw_with(rail="nominal_v: 5.0, tolerance_pct: 2, max_current_a: 1.8")
    f = rail_budget(hw, hw.rails["5V"])
    assert f.status == "warn" and f.numbers["load_pct"] == 86.1
    hw = hw_with(rail="nominal_v: 5.0, tolerance_pct: 2, max_current_a: 1.0")
    f = rail_budget(hw, hw.rails["5V"])
    assert f.status == "fail" and "1.55 A on 1 A" in f.title


def test_rail_budget_unknown_rating_reports_demand_and_stall_scenario():
    hw = hw_with(rail="nominal_v: 5.0, tolerance_pct: 2")
    f = rail_budget(hw, hw.rails["5V"])
    assert f.status == "unknown"
    assert f.check_id == "rail_budget:5V"
    assert f.numbers["worst_case_a"] == 1.55
    assert f.numbers["Servo_all_peak_a"] == 1.4
    assert f.numbers["typical_a"] == 0.57
    assert f.numbers["rating_for_80pct_a"] == 1.94
    assert "both Servo stalled, 2 × 0.7 A = 1.4 A" in f.why
    assert "rating unknown, worst case 1.55 A" in f.title


def test_rail_budget_missing_part_current_is_unknown():
    hw = hw_with(extra_parts="  Mystery: {rail: 5V}")
    f = rail_budget(hw, hw.rails["5V"])
    assert f.status == "unknown" and "parts.Mystery.current_peak_a" in f.missing


def test_rail_budget_ignores_planned_parts_and_empty_rails():
    hw = hw_with(extra_parts="  Big: {rail: 5V, planned: true, current_peak_a: 5}")
    assert rail_budget(hw, hw.rails["5V"]).numbers["worst_case_a"] == 1.55
    assert rail_budget(hw, hw.rails["5V"], include_planned=True).status == "fail"
    assert rail_budget(hw, hw.rails["3V3"]) is None


# ---------------------------------------------------------------- planned parts


def test_planned_parts_shows_effect_on_budget():
    hw = hw_with(extra_parts="  OLED: {rail: 5V, planned: true, current_peak_a: 0.025}")
    f = planned_parts(hw, hw.rails["5V"])
    assert f.check_id == "planned_parts:5V"
    assert (f.numbers["before_a"], f.numbers["after_a"], f.numbers["added_a"]) == (
        1.55, 1.575, 0.025)
    assert f.status == "pass" and f.numbers["load_pct_after"] == 52.5
    hw = hw_with(extra_parts="  Big: {rail: 5V, planned: true, current_peak_a: 1.5}")
    assert planned_parts(hw, hw.rails["5V"]).status == "fail"
    assert planned_parts(hw, hw.rails["3V3"]) is None


# ---------------------------------------------------------------- logic level


def test_logic_level():
    hw = hw_with(signals="""
  - {from: MCU.PA1, to: Servo.SIG, logic_v: 5.0, to_max_v: 3.6}
  - {from: MCU.PA2, to: Servo.SIG, logic_v: 3.3, to_max_v: 5.0, to_min_high_v: 3.5}
  - {from: MCU.PA3, to: Servo.SIG}
""")
    f = [logic_level(hw, s) for s in hw.signals]
    assert [x.status for x in f] == ["pass", "fail", "fail", "unknown"]
    assert f[0].check_id == "logic_level:MCU.PA9->Luna.RX"
    assert "1.4 V over" in f[1].why
    assert f[3].missing == ["signals[3].logic_v", "signals[3].to_max_v"]


# ---------------------------------------------------------------- ground


def test_common_ground():
    sig = "  - {from: MCU.PA1, to: Servo.SIG, logic_v: 3.3, to_max_v: 5}"
    assert common_ground(hw_with(signals=sig), hw_with().rails["5V"]).status == "pass"
    hw = hw_with(rail="nominal_v: 5.0, ground_common: false", signals=sig)
    assert common_ground(hw, hw.rails["5V"]).status == "fail"
    hw = hw_with(rail="nominal_v: 5.0", signals=sig)
    f = common_ground(hw, hw.rails["5V"])
    assert f.status == "unknown" and f.numbers["signals"] == 2
    assert common_ground(hw, hw.rails["3V3"]) is None  # no loads receiving signals


# ---------------------------------------------------------------- missing values


def test_missing_values_one_per_needed_null_field():
    hw = hw_with(rail="nominal_v: 5.0")
    findings = run_all(hw)
    miss = by_id(missing_values(hw, [f for f in findings if not f.check_id.startswith("missing")]))
    assert set(miss) == {"missing:rails.5V.tolerance_pct", "missing:rails.5V.max_current_a",
                         "missing:rails.5V.ground_common"}
    assert miss["missing:rails.5V.tolerance_pct"].numbers["needed_by"] == [
        "voltage_window:Luna", "voltage_window:Servo"]
    # A null nobody needs (3V3 tolerance, all margins wide) is not reported.
    assert not any("3V3" in k for k in miss)


# ---------------------------------------------------------------- the real draft


def test_real_draft_file():
    hw = load_hardware(EXAMPLE.read_text())
    f = by_id(run_all(hw))
    luna = f["voltage_window:TF-Luna"]
    assert luna.status in ("unknown", "warn")
    assert luna.numbers["headroom_high_v"] == 0.2 and luna.numbers["v_max"] == 5.2
    budget = f["rail_budget:5V"]
    assert budget.status == "unknown" and budget.numbers["worst_case_a"] == 1.55
    assert budget.numbers["MG90S_all_peak_a"] == 1.4
    logic = [x for k, x in f.items() if k.startswith("logic_level:")]
    assert len(logic) == 4 and all(x.status == "pass" for x in logic)
    assert f["common_ground:5V"].status == "unknown"
    assert f["planned_parts:3V3"].status == "pass"
    assert f["planned_parts:3V3"].numbers["added_a"] == 0.025
    assert "missing:rails.5V.max_current_a" in f
    assert "voltage_window:SSD1306-OLED" not in f  # planned parts skipped
    # Stable ordering: fail, warn, unknown, pass.
    order = [x.status for x in run_all(hw)]
    assert order == sorted(order, key=["fail", "warn", "unknown", "pass"].index)


def test_real_draft_with_known_supply_passes():
    text = EXAMPLE.read_text().replace(
        "    tolerance_pct: null\n    max_current_a: null\n    ground_common: null",
        "    tolerance_pct: 2\n    max_current_a: 3.0\n    ground_common: true", 1)
    f = by_id(run_all(load_hardware(text)))
    for cid in ("rail_budget:5V", "voltage_window:TF-Luna", "voltage_window:MG90S",
                "common_ground:5V"):
        assert f[cid].status == "pass", cid
    assert not any(k.startswith("missing:") for k in f)
