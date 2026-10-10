"""Course modules: what a course covers, how to start, and (later) its visuals.

A course from Canvas ("COMSC-230") maps to a module by the module's course
codes; the user can pick another one in School (kept in the UI settings). A
course no module claims gets ``GENERAL``: the tutor without course knowledge.
"""
from __future__ import annotations

from jarvis.school.modules.base import Module, Scenario, WebTool
from jarvis.school.modules.calc import CALC
from jarvis.school.modules.discrete import DISCRETE

GENERAL = Module(
    key="general", name="General",
    summary="Any subject: explanations, worked examples and check questions.",
    starters=("Explain a concept", "Walk me through a problem", "Quiz me"),
)

MODULES: dict[str, Module] = {m.key: m for m in (DISCRETE, CALC, GENERAL)}


def get(key: str | None) -> Module:
    """The module ``key``, or GENERAL for an unknown or empty key."""
    return MODULES.get(key or "", GENERAL)


def for_course(course: str | None) -> Module:
    """The module that claims ``course`` (by its course codes), else GENERAL."""
    code = (course or "").strip().upper()
    for m in MODULES.values():
        if code and code in (c.upper() for c in m.courses):
            return m
    return GENERAL


__all__ = ["GENERAL", "MODULES", "Module", "Scenario", "WebTool", "for_course", "get"]
