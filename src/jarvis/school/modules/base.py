"""The shapes of a module: its scenarios (visuals) and web tools."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


class SpecError(ValueError):
    """A visual's spec from the model is unusable (the message says why)."""


@dataclass(frozen=True)
class Scenario:
    """A visual the tutor may ask for. The model fills ``schema`` (shown to it with
    ``example``); ``validate`` cleans the spec or raises SpecError; ``compute``
    turns it into exact data for the stage to draw."""
    key: str
    title: str
    summary: str                       # one line, for the model: when to use it
    schema: str                        # the spec's fields, as the model sees them
    example: dict[str, Any]
    validate: Callable[[dict[str, Any]], dict[str, Any]]
    compute: Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class WebTool:
    """An existing simulation the tools panel may open (verified URLs only)."""
    key: str
    title: str
    url: str
    note: str = ""


@dataclass(frozen=True)
class Module:
    key: str
    name: str
    summary: str
    courses: tuple[str, ...] = ()            # Canvas course codes it claims
    topics: tuple[str, ...] = ()             # what the course covers, for the tutor
    starters: tuple[str, ...] = ()           # first-question chips
    scenarios: tuple[Scenario, ...] = field(default=())
    tools: tuple[WebTool, ...] = field(default=())

    def scenario(self, key: str) -> Scenario | None:
        return next((s for s in self.scenarios if s.key == key), None)
