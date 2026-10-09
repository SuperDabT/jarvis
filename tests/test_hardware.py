from __future__ import annotations

from pathlib import Path

import pytest

from jarvis import db
from jarvis.hardware import HardwareError, find_hardware, load_hardware

EXAMPLE = Path(__file__).resolve().parents[1] / "docs" / "hardware.example.yaml"

MINI = """
rails:
  5V: {nominal_v: 5.0, tolerance_pct: 5, max_current_a: 2.0, ground_common: true}
parts:
  MCU: {rail: null}
  Servo: {rail: 5V, count: 2, v_min: 4.8, v_max: 6.0, current_typ_a: 0.2, current_peak_a: 0.7}
signals:
  - {from: MCU.PA1, to: Servo.SIG, logic_v: 3.3, to_max_v: 5.0}
"""


def test_example_file_loads():
    hw = load_hardware(EXAMPLE.read_text())
    assert set(hw.rails) == {"5V", "USB_5V", "3V3"}
    r = hw.rails["5V"]
    assert r.max_current_a is None and r.tolerance_pct is None and r.ground_common is None
    assert "max_current_a" in r.todo
    assert hw.parts["MG90S"].count == 2 and hw.parts["MG90S"].verify
    assert hw.parts["SSD1306-OLED"].planned
    assert [s.label for s in hw.signals][0] == "NUCLEO-F446RE.PA9->TF-Luna.RX"
    assert "STM32F446RE" in hw.project_description


def test_config_copy_matches_example():
    cfg = Path("~/.config/jarvis/hardware.yaml").expanduser()
    if cfg.exists():
        assert cfg.read_text() == EXAMPLE.read_text()


def test_minimal_file():
    hw = load_hardware(MINI)
    assert hw.parts_on("5V")[0].name == "Servo"
    assert "Servo" in hw.excerpt(parts=["Servo"])


@pytest.mark.parametrize("text,path", [
    ("rails:\n  5V: {nominal_v: five}\n", "rails.5V.nominal_v"),
    ("rails:\n  5V: {tolerance_pct: 5}\n", "rails.5V.nominal_v"),
    ("rails:\n  5V: {nominal_v: 5, max_curent_a: 2}\n", "rails.5V.max_curent_a"),
    ("rails:\n  5V: {nominal_v: 5, max_current_a: -1}\n", "rails.5V.max_current_a"),
    ("parts:\n  X: {rail: 12V}\n", "parts.X.rail"),
    ("rails:\n  5V: {nominal_v: 5}\nparts:\n  X: {rail: 5V, v_min: 6, v_max: 5}\n",
     "parts.X.v_min"),
    ("parts:\n  X: {count: 0}\n", "parts.X.count"),
    ("parts:\n  X: {planned: maybe}\n", "parts.X.planned"),
    ("parts:\n  X: {}\nsignals:\n  - {from: X.A, to: Y.B}\n", "signals[0].to"),
    ("parts:\n  X: {}\nsignals:\n  - {from: X, to: X.B}\n", "signals[0].from"),
    ("parts:\n  X: {}\nsignals:\n  - {from: X.A, to: X.B, logic_v: '3v3'}\n",
     "signals[0].logic_v"),
    ("bogus: 1\n", "bogus"),
])
def test_errors_name_the_field(text, path):
    with pytest.raises(HardwareError) as e:
        load_hardware(text)
    assert e.value.path == path
    assert str(e.value).startswith(path)


def test_bad_yaml():
    with pytest.raises(HardwareError, match="not valid YAML"):
        load_hardware("rails: [\n")


def test_find_hardware_order(tmp_path):
    conn = db.connect(":memory:")
    local = tmp_path / "hw.yaml"
    assert find_hardware(conn, local) is None
    local.write_text(MINI)
    src = find_hardware(conn, local)
    assert src.kind == "file" and src.label == str(local)
    db.upsert_repo_file(conn, "hardware.yaml", "abc", MINI)
    src = find_hardware(conn, local)
    assert src.kind == "repo" and src.label == "repo hardware.yaml"
