"""What the inspector shows for an electrical finding (no Qt).

Built from what Jarvis already has: the checks over hardware.yaml
(``pipeline.load_checks``, Python does every number), the stored model
phrasing of the card (``electrical_findings.model_why``), and the Claude prompt
(``electrical.claude_prompt``). Three kinds of text, kept apart in the UI:

- verified: the check's own title and why, computed from hardware.yaml;
- rule: what this kind of check means, why it matters and the usual next step,
  fixed text per check (never about values it doesn't have);
- AI: the model's rewording of the finding, shown only when it differs.

The source is the file the checks read (the synced repo copy, else the local
file), the field path the finding needs, and its line when the line is
actually in the file. Jarvis never edits hardware.yaml: the view says so and
gives the next step instead.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from jarvis.config import Config
from jarvis.hardware import REPO_PATH

EXPLAIN = {
    "voltage_window": "Compares the voltage a part can get from its rail (nominal ± tolerance) "
                      "with the range the part accepts (v_min to v_max).",
    "rail_budget": "Adds up the worst-case current of everything on a rail and compares it "
                   "with what the rail's supply is rated for.",
    "planned_parts": "The same power budget with the planned, not yet wired parts added.",
    "logic_level": "Compares the voltage a pin drives with what the receiving pin tolerates "
                   "and needs to read a high.",
    "common_ground": "Checks that a rail with its own supply shares ground with the MCU, since "
                     "signals cross between them.",
    "missing": "A value the checks need is empty in hardware.yaml.",
}
MATTERS = {
    "voltage_window": "Outside its range a part can brown out, reset or be damaged.",
    "rail_budget": "Over its rating a supply sags or shuts down when the loads peak, e.g. "
                   "when servos stall.",
    "planned_parts": "Wiring the planned parts could push the rail over its rating.",
    "logic_level": "Overdriving a pin can damage it; too low a level reads as noise.",
    "common_ground": "Without a shared ground, PWM and UART levels have no reference and "
                     "behave erratically.",
    "missing": "Until it is filled in, the checks that need it can't confirm a margin.",
}
NEXT_FOR_STATUS = {
    ("rail_budget", "fail"): "Lower the worst-case load on the rail or use a supply rated "
                             "higher, then update max_current_a.",
    ("rail_budget", "warn"): "Leave more headroom: a higher-rated supply, or fewer loads "
                             "peaking at once.",
    ("planned_parts", "fail"): "Plan a bigger supply (or a separate rail) before wiring "
                               "the planned parts.",
    ("planned_parts", "warn"): "Check the rail's headroom before wiring the planned parts.",
    ("voltage_window", "fail"): "Move the part to a rail inside its range, or regulate its "
                                "supply.",
    ("voltage_window", "warn"): "Confirm the rail's tolerance and the part's datasheet limits.",
    ("logic_level", "fail"): "Add a level shifter or divider on this signal.",
    ("common_ground", "fail"): "Tie the rail's supply ground to the MCU ground.",
}
STATUS_WORDS = {"fail": "over a limit", "warn": "close to a limit",
                "unknown": "can't be confirmed", "pass": "passes"}


@dataclass(frozen=True)
class Source:
    label: str                 # "repo hardware.yaml" or the local file path
    kind: str                  # "repo" | "file"
    path: str                  # file path (local) or path in the repo
    url: str | None            # what "open source" opens
    field: str | None = None   # e.g. "rails.5V.ground_common"
    line: int | None = None    # 1-based, only when found in the file
    present: bool = False      # the field's key is written in the file (maybe null)

    @property
    def edit_note(self) -> str:
        """Why there is no "apply fix", and what to do instead."""
        if self.kind == "repo":
            return ("Jarvis doesn't edit files. Change hardware.yaml in the repo and push; "
                    "the next Refresh (Ctrl+R) syncs it and reruns the checks.")
        return ("Jarvis doesn't edit files. Change it in your editor; Refresh (Ctrl+R) "
                "reruns the checks.")


@dataclass(frozen=True)
class FindingView:
    check_id: str
    check: str
    status: str
    title: str                 # verified
    why: str                   # verified
    explain: str               # rule
    matters: str               # rule
    next_step: str             # rule, filled with the finding's own field and file
    ai_why: str | None         # the model's wording, when it differs from the verified text
    ai_model: str | None
    fields: tuple[str, ...]    # null fields that made it "unknown"
    todo: str                  # the note hardware.yaml itself has for the field
    source: Source | None
    prompt: str                # the Claude prompt, for "copy fix prompt"
    explain_question: str      # what "explain" asks the chat

    @property
    def status_words(self) -> str:
        return STATUS_WORDS.get(self.status, self.status)


# ---------------------------------------------------------------- locating a field


def _key_re(key: str) -> re.Pattern[str]:
    k = re.escape(key)
    return re.compile(rf"""^(\s*)(?:-\s+)?(?:{k}|"{k}"|'{k}')\s*:""")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def locate(text: str, field: str) -> tuple[int | None, bool]:
    """(1-based line, the key itself is written) for a "rails.5V.ground_common" or
    "parts.MG90S.v_min" path; the line of its rail/part when the key isn't
    written; (None, False) when the rail/part isn't in the file either."""
    bits = field.split(".")
    if len(bits) != 3 or bits[0] not in ("rails", "parts"):
        return None, False
    lines = text.splitlines()
    top = _key_re(bits[0])
    i = next((n for n, ln in enumerate(lines) if top.match(ln) and _indent(ln) == 0), None)
    if i is None:
        return None, False
    sub = _key_re(bits[1])
    j = None
    for n in range(i + 1, len(lines)):
        ln = lines[n]
        if ln.strip() and not ln.lstrip().startswith("#") and _indent(ln) == 0:
            break
        if sub.match(ln):
            j = n
            break
    if j is None:
        return None, False
    # flow style: "5V: {nominal_v: 5.0, ground_common: true}"
    if re.search(rf"[{{,]\s*{re.escape(bits[2])}\s*:", lines[j]):
        return j + 1, True
    leaf = _key_re(bits[2])
    base = _indent(lines[j])
    for n in range(j + 1, len(lines)):
        ln = lines[n]
        if ln.strip() and not ln.lstrip().startswith("#") and _indent(ln) <= base:
            break
        if leaf.match(ln):
            return n + 1, True
    return j + 1, False


# ---------------------------------------------------------------- building


def _todo(hw, path: str) -> str:
    from jarvis.checks import _todo_note

    try:
        return _todo_note(hw, path)
    except (IndexError, KeyError, ValueError):
        return ""


def _source(cfg: Config, src, field: str | None) -> Source:
    line, present = locate(src.text, field) if field else (None, False)
    if src.kind == "repo":
        path = REPO_PATH
        url = f"https://github.com/{cfg.repo}/blob/HEAD/{REPO_PATH}" + (f"#L{line}" if line else "")
    else:
        path = src.label
        url = Path(path).resolve().as_uri()      # a desktop opener can't take a line
    return Source(src.label, src.kind, path, url, field, line, present)


def _next_step(f, fields: tuple[str, ...], src: Source | None, todo: str) -> str:
    where = f" in {Path(src.path).name}" if src else ""
    if fields:
        names = ", ".join(fields)
        if f.check == "common_ground" and f.status == "unknown":
            rail = fields[0].split(".")[1]
            return (f"Check with a multimeter that the {rail} supply ground is tied to the MCU "
                    f"ground, then set {names}{where} (true or false).")
        step = f"Fill in {names}{where}"
        return step + (f" ({todo})." if todo else " from the datasheet or a measurement.")
    return NEXT_FOR_STATUS.get((f.check, f.status), "Review it against the datasheets, then "
                                                    "update hardware.yaml if something changed.")


def load_finding(conn: sqlite3.Connection, cfg: Config, check_id: str) -> FindingView | None:
    """The view for one finding, from the current hardware.yaml; None when no
    finding has that id. Checks run again (pure, a few ms), so the source and
    field are those of the file as it is now."""
    from jarvis.electrical import claude_prompt
    from jarvis.pipeline import load_checks

    loaded = load_checks(conn, cfg)
    if isinstance(loaded, str):
        return None
    src, hw, findings = loaded
    f = next((x for x in findings if x.check_id == check_id), None)
    if f is None:
        return None
    fields = tuple(f.missing) if f.status != "pass" else ()
    field = fields[0] if fields else None
    source = _source(cfg, src, field)
    todo = _todo(hw, field) if field else ""
    ai_why = ai_model = None
    try:
        row = conn.execute("SELECT model_result, model_why, model FROM electrical_findings "
                           "WHERE check_id = ?", (check_id,)).fetchone()
    except sqlite3.Error:
        row = None
    if row is not None and row["model_result"] == "model" and row["model_why"] \
            and row["model_why"].strip() != f.why.strip():
        ai_why, ai_model = row["model_why"], row["model"]
    question = (f"Explain the hardware finding \"{f.title}\" ({check_id}) in plain words: what "
                f"it means, why it matters for this project, and how to resolve it.")
    return FindingView(
        check_id=check_id, check=f.check, status=f.status, title=f.title, why=f.why,
        explain=EXPLAIN.get(f.check, ""), matters=MATTERS.get(f.check, ""),
        next_step=_next_step(f, fields, source, todo), ai_why=ai_why, ai_model=ai_model,
        fields=fields, todo=todo, source=source, prompt=claude_prompt(hw, f),
        explain_question=question)
