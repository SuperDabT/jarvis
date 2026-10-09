"""Load and validate hardware.yaml, the electrical facts the checks run on.

Schema (see docs/hardware.example.yaml for a filled-in example)::

    version: 1
    project: {name, description}
    rails:                       # map: rail name -> rail
      <name>:
        nominal_v: float         # required
        tolerance_pct: float|null
        max_current_a: float|null
        ground_common: bool|null # is this rail's ground tied to the MCU ground?
        source, notes: str
        todo: {field: note}      # why a value is null / what to look up
    parts:                       # map: part name -> part
      <name>:
        rail: str|null           # null = not a load on any listed rail
        count: int (default 1)
        planned: bool (default false)
        v_min, v_max, current_typ_a, current_peak_a: float|null
        source, notes: str; verify: bool; todo: {field: note}
    signals:                     # list
      - from: Part.PIN
        to: Part.PIN
        logic_v: float|null      # driver's high level
        to_max_v: float|null     # highest voltage the receiving pin tolerates
        to_min_high_v: float|null  # optional: receiver's minimum logic-high
        function, source, notes: str; verify, planned: bool; todo: {field: note}

Errors raise HardwareError naming the path of the bad field, e.g.
``rails.5V.max_current_a: expected a number or null, got 'two'``.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_PATH = "hardware.yaml"


class HardwareError(ValueError):
    """hardware.yaml is malformed. The message starts with the bad field's path."""

    def __init__(self, path: str, message: str) -> None:
        self.path = path
        super().__init__(f"{path}: {message}" if path else message)


@dataclass(frozen=True)
class Rail:
    name: str
    nominal_v: float
    tolerance_pct: float | None = None
    max_current_a: float | None = None
    ground_common: bool | None = None
    source: str = ""
    notes: str = ""
    todo: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Part:
    name: str
    rail: str | None = None
    count: int = 1
    planned: bool = False
    v_min: float | None = None
    v_max: float | None = None
    current_typ_a: float | None = None
    current_peak_a: float | None = None
    source: str = ""
    verify: bool = False
    notes: str = ""
    todo: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Signal:
    index: int
    from_part: str
    from_pin: str
    to_part: str
    to_pin: str
    logic_v: float | None = None
    to_max_v: float | None = None
    to_min_high_v: float | None = None
    function: str = ""
    planned: bool = False
    source: str = ""
    verify: bool = False
    notes: str = ""
    todo: dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.from_part}.{self.from_pin}->{self.to_part}.{self.to_pin}"

    @property
    def path(self) -> str:
        return f"signals[{self.index}]"


@dataclass(frozen=True)
class Hardware:
    project_name: str
    project_description: str
    rails: dict[str, Rail]
    parts: dict[str, Part]
    signals: list[Signal]
    raw: dict[str, Any]

    def parts_on(self, rail: str, include_planned: bool = False) -> list[Part]:
        return [p for p in self.parts.values()
                if p.rail == rail and (include_planned or not p.planned)]

    def excerpt(self, rails: list[str] = (), parts: list[str] = (),
                signals: list[int] = ()) -> str:
        """The raw YAML of just these entries, for a self-contained prompt."""
        out: dict[str, Any] = {}
        r = {k: self.raw["rails"][k] for k in rails if k in self.raw.get("rails", {})}
        p = {k: self.raw["parts"][k] for k in parts if k in self.raw.get("parts", {})}
        s = [self.raw["signals"][i] for i in signals if i < len(self.raw.get("signals", []))]
        if r:
            out["rails"] = r
        if p:
            out["parts"] = p
        if s:
            out["signals"] = s
        return yaml.safe_dump(out, sort_keys=False, allow_unicode=True, width=1000).strip()


# ---------------------------------------------------------------- validation

RAIL_FIELDS = {"nominal_v", "tolerance_pct", "max_current_a", "ground_common",
               "source", "notes", "todo"}
PART_FIELDS = {"rail", "count", "planned", "v_min", "v_max", "current_typ_a",
               "current_peak_a", "source", "verify", "notes", "todo", "datasheet"}
SIGNAL_FIELDS = {"from", "to", "logic_v", "to_max_v", "to_min_high_v", "function",
                 "planned", "source", "verify", "notes", "todo"}
TOP_FIELDS = {"version", "project", "rails", "parts", "signals"}


def _check_keys(obj: dict, allowed: set[str], path: str) -> None:
    for k in obj:
        if k not in allowed:
            raise HardwareError(f"{path}.{k}" if path else str(k),
                                f"unknown field (allowed: {', '.join(sorted(allowed))})")


def _num(obj: dict, key: str, path: str, *, required: bool = False,
         positive: bool = True) -> float | None:
    p = f"{path}.{key}"
    if key not in obj or obj[key] is None:
        if required:
            raise HardwareError(p, "required number is missing")
        return None
    v = obj[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise HardwareError(p, f"expected a number or null, got {v!r}")
    if positive and v <= 0:
        raise HardwareError(p, f"must be greater than 0, got {v!r}")
    if not positive and v < 0:
        raise HardwareError(p, f"must not be negative, got {v!r}")
    return float(v)


def _bool(obj: dict, key: str, path: str, default: bool | None) -> bool | None:
    v = obj.get(key, default)
    if v is None:
        return None
    if not isinstance(v, bool):
        raise HardwareError(f"{path}.{key}", f"expected true, false or null, got {v!r}")
    return v


def _str(obj: dict, key: str, path: str) -> str:
    v = obj.get(key)
    if v is None:
        return ""
    if not isinstance(v, (str, int, float)):
        raise HardwareError(f"{path}.{key}", f"expected text, got {type(v).__name__}")
    return str(v).strip()


def _todo(obj: dict, path: str) -> dict[str, str]:
    v = obj.get("todo")
    if v is None:
        return {}
    if isinstance(v, str):
        return {"*": v.strip()}
    if not isinstance(v, dict):
        raise HardwareError(f"{path}.todo", "expected a map of field: note, or one note")
    return {str(k): str(n).strip() for k, n in v.items()}


def _mapping(raw: Any, path: str) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise HardwareError(path, f"expected a map of name: entry, got {type(raw).__name__}")
    return raw


def _pin_ref(v: Any, path: str) -> tuple[str, str]:
    if not isinstance(v, str) or "." not in v:
        raise HardwareError(path, f"expected 'Part.PIN', got {v!r}")
    part, pin = v.split(".", 1)
    if not part or not pin:
        raise HardwareError(path, f"expected 'Part.PIN', got {v!r}")
    return part, pin


def load_hardware(text: str) -> Hardware:
    """Parse and validate hardware.yaml text."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise HardwareError("", f"not valid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise HardwareError("", "top level must be a map with rails, parts and signals")
    _check_keys(raw, TOP_FIELDS, "")
    raw = {k: raw[k] for k in raw}
    raw.setdefault("rails", {})
    raw.setdefault("parts", {})
    raw.setdefault("signals", [])

    project = raw.get("project") or {}
    if not isinstance(project, dict):
        raise HardwareError("project", "expected a map with name and description")
    _check_keys(project, {"name", "description"}, "project")

    rails: dict[str, Rail] = {}
    for name, r in _mapping(raw["rails"], "rails").items():
        path = f"rails.{name}"
        name = str(name)
        if not isinstance(r, dict):
            raise HardwareError(path, "expected a map of fields")
        _check_keys(r, RAIL_FIELDS, path)
        tol = _num(r, "tolerance_pct", path, positive=False)
        if tol is not None and tol > 50:
            raise HardwareError(f"{path}.tolerance_pct", f"looks wrong: {tol} %")
        rails[name] = Rail(
            name=name, nominal_v=_num(r, "nominal_v", path, required=True),
            tolerance_pct=tol, max_current_a=_num(r, "max_current_a", path),
            ground_common=_bool(r, "ground_common", path, None),
            source=_str(r, "source", path), notes=_str(r, "notes", path),
            todo=_todo(r, path),
        )

    parts: dict[str, Part] = {}
    for name, p in _mapping(raw["parts"], "parts").items():
        path = f"parts.{name}"
        name = str(name)
        if "." in name:
            raise HardwareError(path, "part names cannot contain '.' (used in Part.PIN)")
        if not isinstance(p, dict):
            raise HardwareError(path, "expected a map of fields")
        _check_keys(p, PART_FIELDS, path)
        rail = p.get("rail")
        if rail is not None:
            rail = str(rail)
            if rail not in rails:
                raise HardwareError(f"{path}.rail",
                                    f"unknown rail {rail!r} (rails: {', '.join(rails) or 'none'})")
        count = p.get("count", 1)
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise HardwareError(f"{path}.count", f"expected a whole number >= 1, got {count!r}")
        v_min, v_max = _num(p, "v_min", path), _num(p, "v_max", path)
        if v_min is not None and v_max is not None and v_min > v_max:
            raise HardwareError(f"{path}.v_min", f"{v_min} V is above v_max {v_max} V")
        typ, peak = _num(p, "current_typ_a", path), _num(p, "current_peak_a", path)
        if typ is not None and peak is not None and typ > peak:
            raise HardwareError(f"{path}.current_typ_a",
                                f"{typ} A is above current_peak_a {peak} A")
        parts[name] = Part(
            name=name, rail=rail, count=count,
            planned=bool(_bool(p, "planned", path, False)),
            v_min=v_min, v_max=v_max, current_typ_a=typ, current_peak_a=peak,
            source=_str(p, "source", path) or _str(p, "datasheet", path),
            verify=bool(_bool(p, "verify", path, False)), notes=_str(p, "notes", path),
            todo=_todo(p, path),
        )

    sigs_raw = raw["signals"]
    if not isinstance(sigs_raw, list):
        raise HardwareError("signals", "expected a list")
    signals: list[Signal] = []
    for i, s in enumerate(sigs_raw):
        path = f"signals[{i}]"
        if not isinstance(s, dict):
            raise HardwareError(path, "expected a map of fields")
        _check_keys(s, SIGNAL_FIELDS, path)
        ends = []
        for end in ("from", "to"):
            if end not in s:
                raise HardwareError(f"{path}.{end}", "required 'Part.PIN' is missing")
            part, pin = _pin_ref(s[end], f"{path}.{end}")
            if part not in parts:
                raise HardwareError(f"{path}.{end}", f"unknown part {part!r}")
            ends.append((part, pin))
        signals.append(Signal(
            index=i, from_part=ends[0][0], from_pin=ends[0][1],
            to_part=ends[1][0], to_pin=ends[1][1],
            logic_v=_num(s, "logic_v", path), to_max_v=_num(s, "to_max_v", path),
            to_min_high_v=_num(s, "to_min_high_v", path),
            function=_str(s, "function", path),
            planned=bool(_bool(s, "planned", path, False)),
            source=_str(s, "source", path), verify=bool(_bool(s, "verify", path, False)),
            notes=_str(s, "notes", path), todo=_todo(s, path),
        ))

    return Hardware(
        project_name=_str(project, "name", "project"),
        project_description=" ".join(_str(project, "description", "project").split()),
        rails=rails, parts=parts, signals=signals, raw=raw,
    )


# ---------------------------------------------------------------- locating


@dataclass(frozen=True)
class HardwareSource:
    text: str
    label: str  # e.g. "repo hardware.yaml" or "/home/me/.config/jarvis/hardware.yaml"
    kind: str   # "repo" | "file"


def find_hardware(conn: sqlite3.Connection, path: Path | None) -> HardwareSource | None:
    """Repo copy first (synced into repo_files), else the local file at ``path``.

    Jarvis never writes to the watched repo; the local file is the usual source.
    """
    row = conn.execute("SELECT content FROM repo_files WHERE path = ?", (REPO_PATH,)).fetchone()
    if row is not None and (row["content"] or "").strip():
        return HardwareSource(row["content"], f"repo {REPO_PATH}", "repo")
    if path is not None:
        p = Path(path).expanduser()
        if p.is_file():
            return HardwareSource(p.read_text(encoding="utf-8"), str(p), "file")
    return None
