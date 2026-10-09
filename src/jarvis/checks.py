"""Electrical checks over hardware.yaml. Pure functions; Python does every number.

Each check returns Findings. ``check_id`` is stable per (check, subject), so a
card made from a finding keeps its fingerprint across runs:

    voltage_window:<part>      rail worst-case range vs the part's v_min/v_max
    rail_budget:<rail>         sum of peak currents vs the rail's rating
    planned_parts:<rail>       the same with planned (not yet wired) parts added
    logic_level:<from>-><to>   driver high level vs the receiving pin's tolerance
    common_ground:<rail>       is a separately supplied rail tied to the MCU ground
    missing:<field path>       a null value some check needed

Status: "fail" (over a limit), "warn" (close to one), "unknown" (a needed value
is null, so the check cannot confirm the margin), "pass".
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any

from jarvis.hardware import Hardware, Part, Rail, Signal

BUDGET_WARN_PCT = 80.0   # warn above this share of a rail's rating
HEADROOM_WARN_PCT = 1.0  # warn when worst-case voltage headroom is under this share of nominal
# With an unknown tolerance, a margin this wide (as % of nominal) on both sides
# still passes: ordinary supplies stay well inside +-10 %.
UNKNOWN_TOL_SAFE_PCT = 10.0
EXAMPLE_TOL_PCT = 5.0    # the "a +-5 % supply would reach ..." example

STATUS_ORDER = ("fail", "warn", "unknown", "pass")


@dataclass(frozen=True)
class Finding:
    check_id: str
    status: str  # "fail" | "warn" | "unknown" | "pass"
    title: str
    why: str
    numbers: dict[str, Any]
    parts: list[str]
    # Additive context, used for prompts and dedupe; not part of the contract.
    rails: list[str] = field(default_factory=list)
    signals: list[int] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # null field paths that made it unknown

    @property
    def check(self) -> str:
        return self.check_id.split(":", 1)[0]

    def content_hash(self) -> str:
        blob = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(blob.encode()).hexdigest()


def fmt(x: float) -> str:
    """3.0 -> "3", 1.5500000001 -> "1.55"."""
    return f"{round(x, 3):g}"


def r3(x: float) -> float:
    return round(x, 3)


def _ceil2(x: float) -> float:
    return math.ceil(round(x * 100, 6)) / 100


def _limit_str(p: Part) -> str:
    lo = fmt(p.v_min) if p.v_min is not None else "?"
    hi = fmt(p.v_max) if p.v_max is not None else "?"
    return f"{lo}-{hi} V"


# ---------------------------------------------------------------- voltage window


def voltage_window(hw: Hardware, part: Part) -> Finding | None:
    if part.rail is None or (part.v_min is None and part.v_max is None):
        if part.rail is not None:
            return Finding(
                f"voltage_window:{part.name}", "unknown",
                f"Voltage check: {part.name} has no voltage limits listed",
                f"{part.name} is on the {part.rail} rail but hardware.yaml lists no v_min or "
                f"v_max for it, so its supply range cannot be checked.",
                {}, [part.name], rails=[part.rail],
                missing=[f"parts.{part.name}.v_min", f"parts.{part.name}.v_max"])
        return None
    rail = hw.rails[part.rail]
    nom, tol = rail.nominal_v, rail.tolerance_pct
    cid = f"voltage_window:{part.name}"
    missing = [f"parts.{part.name}.{k}" for k in ("v_min", "v_max")
               if getattr(part, k) is None]
    nums: dict[str, Any] = {"rail": rail.name, "nominal_v": nom, "tolerance_pct": tol,
                            "v_min": part.v_min, "v_max": part.v_max}
    base = dict(parts=[part.name], rails=[rail.name])

    lo, hi = (nom, nom) if tol is None else (nom * (1 - tol / 100), nom * (1 + tol / 100))
    lo, hi = r3(lo), r3(hi)
    if tol is not None:
        nums.update(rail_min_v=lo, rail_max_v=hi)
    head_hi = r3(part.v_max - hi) if part.v_max is not None else None
    head_lo = r3(lo - part.v_min) if part.v_min is not None else None
    nums.update(headroom_high_v=head_hi, headroom_low_v=head_lo)
    pct = lambda v: round(v / nom * 100, 1)  # noqa: E731
    if head_hi is not None:
        nums["headroom_high_pct"] = pct(head_hi)
    if head_lo is not None:
        nums["headroom_low_pct"] = pct(head_lo)
    rng = f"{fmt(nom)} V nominal" if tol is None else (
        f"{fmt(lo)}-{fmt(hi)} V ({fmt(nom)} V ± {fmt(tol)}%)")

    # Over a limit (worst case if tolerance known, nominal otherwise): fail.
    over = head_hi is not None and head_hi < 0
    under = head_lo is not None and head_lo < 0
    if over or under:
        side = (f"above {part.name}'s {fmt(part.v_max)} V max by {fmt(-head_hi)} V" if over
                else f"below {part.name}'s {fmt(part.v_min)} V min by {fmt(-head_lo)} V")
        return Finding(cid, "fail",
                       f"Voltage check: {part.name} outside its {_limit_str(part)} range on "
                       f"{rail.name}",
                       f"The {rail.name} rail is {rng}, which is {side}.",
                       nums, **base, missing=missing)

    # The tighter side decides.
    sides = [(h, s) for h, s in ((head_hi, "high"), (head_lo, "low")) if h is not None]
    tight_v, tight_side = min(sides)
    tight_pct = pct(tight_v)
    limit = part.v_max if tight_side == "high" else part.v_min
    limit_word = "max" if tight_side == "high" else "min"

    if tol is None:
        if tight_pct >= UNKNOWN_TOL_SAFE_PCT and not missing:
            return Finding(cid, "pass", f"Voltage check: {part.name} fits the {rail.name} rail",
                           f"{part.name} ({_limit_str(part)}) has at least {fmt(tight_v)} V "
                           f"({fmt(tight_pct)}%) of headroom at the {rail.name} rail's "
                           f"{fmt(nom)} V nominal, wide enough without a known tolerance.",
                           nums, **base)
        ex = EXAMPLE_TOL_PCT
        ex_v = r3(nom * (1 + ex / 100)) if tight_side == "high" else r3(nom * (1 - ex / 100))
        nums.update(example_tolerance_pct=ex, example_rail_v=ex_v)
        crosses = (ex_v > limit) if tight_side == "high" else (ex_v < limit)
        example = (f"; a ±{fmt(ex)}% supply would reach {fmt(ex_v)} V, "
                   f"{'past' if crosses else 'inside'} the limit")
        return Finding(
            cid, "unknown",
            f"Voltage check: {part.name} margin on {rail.name} unconfirmed, "
            f"{fmt(tight_v)} V headroom",
            f"At the {rail.name} rail's {fmt(nom)} V nominal, {part.name}'s {fmt(limit)} V "
            f"{limit_word} leaves {fmt(tight_v)} V ({fmt(tight_pct)}%), and the rail's "
            f"tolerance is not in hardware.yaml{example}.",
            nums, **base, missing=[f"rails.{rail.name}.tolerance_pct", *missing])

    if tight_pct < HEADROOM_WARN_PCT:
        return Finding(cid, "warn",
                       f"Voltage check: {part.name} within {fmt(tight_v)} V of its "
                       f"{limit_word} on {rail.name}",
                       f"The {rail.name} rail is {rng}; its worst case leaves only "
                       f"{fmt(tight_v)} V ({fmt(tight_pct)}%) to {part.name}'s {fmt(limit)} V "
                       f"{limit_word}.", nums, **base, missing=missing)
    status = "unknown" if missing else "pass"
    return Finding(cid, status,
                   f"Voltage check: {part.name} fits the {rail.name} rail" if status == "pass"
                   else f"Voltage check: {part.name} has a voltage limit missing",
                   f"The {rail.name} rail is {rng}, leaving at least {fmt(tight_v)} V "
                   f"({fmt(tight_pct)}%) to {part.name}'s {fmt(limit)} V {limit_word}"
                   + ("; the other limit is not listed." if missing else "."),
                   nums, **base, missing=missing)


# ---------------------------------------------------------------- current budget


def _load_terms(loads: list[Part]) -> tuple[float, list[str], dict[str, Any], list[str]]:
    """(peak demand of known loads, text terms, numbers, missing paths)."""
    total = 0.0
    terms: list[str] = []
    nums: dict[str, Any] = {}
    missing: list[str] = []
    for p in loads:
        if p.current_peak_a is None:
            missing.append(f"parts.{p.name}.current_peak_a")
            continue
        sub = r3(p.count * p.current_peak_a)
        total += sub
        nums[f"{p.name}_peak_a"] = p.current_peak_a
        if p.count > 1:
            nums[f"{p.name}_count"] = p.count
            nums[f"{p.name}_all_peak_a"] = sub
            who = "both" if p.count == 2 else f"all {p.count}"
            state = "stalled" if _is_motor(p) else "at peak"
            terms.append(f"{who} {p.name} {state}, {p.count} × {fmt(p.current_peak_a)} A = "
                         f"{fmt(sub)} A")
        else:
            terms.append(f"{p.name} peak {fmt(p.current_peak_a)} A")
    return r3(total), terms, nums, missing


def _is_motor(p: Part) -> bool:
    blob = f"{p.name} {p.source} {p.notes}".lower()
    return "stall" in blob or "servo" in blob or "motor" in blob


def _typical(loads: list[Part]) -> float | None:
    if any(p.current_typ_a is None for p in loads):
        return None
    return r3(sum(p.count * p.current_typ_a for p in loads))


def _budget_status(demand: float, rating: float) -> tuple[str, float]:
    pct = round(demand / rating * 100, 1)
    if pct > 100:
        return "fail", pct
    if pct > BUDGET_WARN_PCT:
        return "warn", pct
    return "pass", pct


def rail_budget(hw: Hardware, rail: Rail, include_planned: bool = False) -> Finding | None:
    loads = hw.parts_on(rail.name, include_planned)
    if not loads:
        return None
    cid = f"rail_budget:{rail.name}"
    demand, terms, nums, missing = _load_terms(loads)
    nums = {"rail": rail.name, "worst_case_a": demand, **nums,
            "typical_a": _typical(loads), "max_current_a": rail.max_current_a}
    base = dict(parts=[p.name for p in loads], rails=[rail.name])
    breakdown = "; ".join(terms)
    partial = (" from the loads with a listed peak current (some are missing)"
               if missing else "")

    if rail.max_current_a is None:
        need = demand
        need80 = _ceil2(demand / (BUDGET_WARN_PCT / 100))
        nums.update(min_rating_a=need, rating_for_80pct_a=need80)
        return Finding(
            cid, "unknown",
            f"Power check: {rail.name} rail rating unknown, worst case {fmt(demand)} A",
            f"Worst case on the {rail.name} rail is {fmt(demand)} A{partial} ({breakdown}); "
            f"its supply rating is not in hardware.yaml, so it needs at least {fmt(need)} A, "
            f"or {fmt(need80)} A to stay under {fmt(BUDGET_WARN_PCT)}%.",
            nums, **base, missing=[f"rails.{rail.name}.max_current_a", *missing])

    status, pct = _budget_status(demand, rail.max_current_a)
    nums["load_pct"] = pct
    if missing:
        status = "fail" if status == "fail" else "unknown"
    rating = fmt(rail.max_current_a)
    title = {
        "fail": f"Power check: {rail.name} rail overloaded, {fmt(demand)} A on {rating} A",
        "warn": f"Power check: {rail.name} rail at {fmt(pct)}% of its {rating} A rating",
        "unknown": f"Power check: {rail.name} rail load incomplete, {fmt(demand)} A so far",
        "pass": f"Power check: {rail.name} rail at {fmt(pct)}% of its {rating} A rating",
    }[status]
    why = (f"Worst case on the {rail.name} rail is {fmt(demand)} A{partial} ({breakdown}), "
           f"{fmt(pct)}% of its {rating} A rating.")
    return Finding(cid, status, title, why, nums, **base, missing=missing)


def planned_parts(hw: Hardware, rail: Rail) -> Finding | None:
    planned = [p for p in hw.parts_on(rail.name, include_planned=True) if p.planned]
    if not planned:
        return None
    current = hw.parts_on(rail.name)
    cid = f"planned_parts:{rail.name}"
    before, _, _, miss_before = _load_terms(current)
    after, terms, nums, miss_after = _load_terms(current + planned)
    missing = list(dict.fromkeys(miss_before + miss_after))
    added = r3(after - before)
    names = ", ".join(p.name for p in planned)
    nums = {"rail": rail.name, "before_a": before, "after_a": after, "added_a": added,
            **{k: v for k, v in nums.items() if any(k.startswith(p.name) for p in planned)},
            "max_current_a": rail.max_current_a}
    base = dict(parts=[p.name for p in current + planned], rails=[rail.name])
    head = (f"Adding planned {names} ({fmt(added)} A peak) takes the {rail.name} rail's "
            f"listed loads from {fmt(before)} A to {fmt(after)} A")
    if rail.max_current_a is None:
        return Finding(cid, "unknown",
                       f"Planned parts: {names} on {rail.name}, rating unknown",
                       f"{head}; the rail's rating is not in hardware.yaml.",
                       nums, **base, missing=[f"rails.{rail.name}.max_current_a", *missing])
    status, pct = _budget_status(after, rail.max_current_a)
    nums["load_pct_after"] = pct
    if missing and status != "fail":
        status = "unknown"
    return Finding(cid, status,
                   f"Planned parts: {names} puts {rail.name} at {fmt(pct)}% of its rating",
                   f"{head}, {fmt(pct)}% of its {fmt(rail.max_current_a)} A rating "
                   f"(only loads listed in hardware.yaml are counted).",
                   nums, **base, missing=missing)


# ---------------------------------------------------------------- logic levels


def logic_level(hw: Hardware, sig: Signal) -> Finding:
    cid = f"logic_level:{sig.label}"
    frm, to = f"{sig.from_part} {sig.from_pin}", f"{sig.to_part} {sig.to_pin}"
    nums = {"logic_v": sig.logic_v, "to_max_v": sig.to_max_v,
            "to_min_high_v": sig.to_min_high_v}
    base = dict(parts=[sig.from_part, sig.to_part], signals=[sig.index])
    missing = [f"{sig.path}.{k}" for k in ("logic_v", "to_max_v") if getattr(sig, k) is None]
    if missing:
        return Finding(cid, "unknown", f"Logic check: {frm} to {to} levels not listed",
                       f"The {frm} to {to} signal is missing "
                       f"{' and '.join(m.rsplit('.', 1)[1] for m in missing)} in hardware.yaml, "
                       f"so its logic levels cannot be compared.",
                       nums, **base, missing=missing)
    if sig.logic_v > sig.to_max_v:
        return Finding(cid, "fail", f"Logic check: {frm} overdrives {to}",
                       f"{frm} drives {fmt(sig.logic_v)} V into {to}, which tolerates only "
                       f"{fmt(sig.to_max_v)} V, {fmt(r3(sig.logic_v - sig.to_max_v))} V over.",
                       nums, **base)
    if sig.to_min_high_v is not None and sig.logic_v < sig.to_min_high_v:
        return Finding(cid, "fail", f"Logic check: {frm} too low for {to}",
                       f"{frm} drives {fmt(sig.logic_v)} V, below the {fmt(sig.to_min_high_v)} V "
                       f"{to} needs to read a high.", nums, **base)
    return Finding(cid, "pass", f"Logic check: {frm} to {to} levels match",
                   f"{frm} drives {fmt(sig.logic_v)} V into {to}, which tolerates "
                   f"{fmt(sig.to_max_v)} V.", nums, **base)


# ---------------------------------------------------------------- grounds


def common_ground(hw: Hardware, rail: Rail, include_planned: bool = False) -> Finding | None:
    """Only for rails whose loads receive a signal from a part not on that rail."""
    on_rail = {p.name for p in hw.parts_on(rail.name, include_planned)}
    rail_of = {p.name: p.rail for p in hw.parts.values()}
    sigs = [s for s in hw.signals if (include_planned or not s.planned)
            and s.to_part in on_rail and rail_of.get(s.from_part) != rail.name]
    if not sigs:
        return None
    cid = f"common_ground:{rail.name}"
    receivers = sorted({s.to_part for s in sigs})
    drivers = sorted({s.from_part for s in sigs})
    nums = {"rail": rail.name, "signals": len(sigs), "ground_common": rail.ground_common}
    base = dict(parts=receivers + drivers, rails=[rail.name], signals=[s.index for s in sigs])
    n = len(sigs)
    who = (f"{', '.join(receivers)} on the {rail.name} rail get {n} "
           f"signal{'s' if n != 1 else ''} from {', '.join(drivers)}")
    if rail.ground_common is None:
        return Finding(cid, "unknown",
                       f"Wiring check: {rail.name} supply ground tie to the MCU unconfirmed",
                       f"{who}, but hardware.yaml does not say the {rail.name} supply ground is "
                       f"tied to the MCU ground; without it the PWM and UART levels have no "
                       f"shared reference.", nums, **base,
                       missing=[f"rails.{rail.name}.ground_common"])
    if rail.ground_common is False:
        return Finding(cid, "fail", f"Wiring check: {rail.name} ground not tied to the MCU",
                       f"{who}, and hardware.yaml says the {rail.name} supply ground is not "
                       f"tied to the MCU ground, so those signals have no shared reference.",
                       nums, **base)
    return Finding(cid, "pass", f"Wiring check: {rail.name} ground is common with the MCU",
                   f"{who}, and the {rail.name} supply ground is tied to the MCU ground.",
                   nums, **base)


# ---------------------------------------------------------------- missing values


def _todo_note(hw: Hardware, path: str) -> str:
    bits = path.split(".")
    if bits[0] == "rails" and bits[1] in hw.rails:
        todo = hw.rails[bits[1]].todo
    elif bits[0] == "parts" and bits[1] in hw.parts:
        todo = hw.parts[bits[1]].todo
    elif bits[0].startswith("signals["):
        idx = int(bits[0][8:-1])
        todo = hw.signals[idx].todo if idx < len(hw.signals) else {}
    else:
        todo = {}
    return todo.get(bits[-1]) or todo.get("*") or ""


def missing_values(hw: Hardware, findings: list[Finding]) -> list[Finding]:
    """One finding per null field that a non-passing check needed."""
    needed: dict[str, list[str]] = {}
    for f in findings:
        if f.status == "pass":
            continue
        for path in f.missing:
            needed.setdefault(path, []).append(f.check_id)
    out = []
    for path, by in needed.items():
        note = _todo_note(hw, path)
        bits = path.split(".")
        subject = bits[1] if bits[0] in ("rails", "parts") else bits[0]
        out.append(Finding(
            f"missing:{path}", "unknown", f"Missing value: {path}",
            f"hardware.yaml has no value for {path}, needed by {', '.join(by)}"
            + (f". To do: {note}" if note else "."),
            {"field": path, "needed_by": by},
            [subject] if bits[0] == "parts" else [],
            rails=[subject] if bits[0] == "rails" else [],
            missing=[path],
        ))
    return out


# ---------------------------------------------------------------- all


def run_all(hw: Hardware, include_planned: bool = False) -> list[Finding]:
    """Every check, sorted fail -> warn -> unknown -> pass, then by check_id."""
    out: list[Finding] = []
    for part in hw.parts.values():
        if part.planned and not include_planned:
            continue
        f = voltage_window(hw, part)
        if f:
            out.append(f)
    for rail in hw.rails.values():
        for f in (rail_budget(hw, rail, include_planned),
                  None if include_planned else planned_parts(hw, rail),
                  common_ground(hw, rail, include_planned)):
            if f:
                out.append(f)
    for sig in hw.signals:
        if sig.planned and not include_planned:
            continue
        out.append(logic_level(hw, sig))
    out.extend(missing_values(hw, out))
    return sorted(out, key=lambda f: (STATUS_ORDER.index(f.status), f.check_id))
