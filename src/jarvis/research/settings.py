"""Research loop settings: optional ``research.yaml`` on top of built-in defaults.

The file lives at ``$JARVIS_RESEARCH`` or ``~/.config/jarvis/research.yaml``. Every
key is optional; with no file at all the defaults below are used. See
``docs/research.example.yaml``.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from jarvis.ollama import DEFAULT_EMBED_MODEL

DEFAULT_PROJECT = (
    "An STM32F446RE (NUCLEO-F446RE) pan-tilt tracker in bare-metal C: two hobby servos "
    "sweep a single-beam Benewake TF-Luna LiDAR over UART to detect and follow a person "
    "in a room (a 2D scan of a few hundred points per sweep)."
)
DEFAULT_SETUP = "STM32F446RE, single-beam TF-Luna on a pan-tilt, no RTOS"
# What the judge treats as "transfers to this hardware".
DEFAULT_CONSTRAINTS = (
    "Firmware is bare-metal C on the microcontroller (no OS; tools that need another "
    "language or runtime on the device do not apply). One distance reading at a time, a "
    "few hundred points per sweep, no camera, no GPU."
)
DEFAULT_LIBRARIES: dict[str, int | None] = {
    "STMicroelectronics/STM32CubeF4": None,
    "FreeRTOS/FreeRTOS-Kernel": 18,
}
# Words that anchor a title-derived arXiv query to this kind of sensor.
DEFAULT_DOMAIN_TERMS = ("LiDAR", "laser scanner", "rangefinder")

# Calibrated on the real issues of the pan-tilt repo with qwen3-embedding:0.6b
# (README "Research loop"). Similarity separates poorly on this domain: unrelated
# 3D/odometry papers reached 0.60, while the one genuinely useful paper found
# (1D DBSCAN for laser range scan segmentation, for the person-size filter issue)
# scored 0.41. So the floor only drops clearly off-field work (most scored
# 0.20-0.39); top_k bounds the judge cost and the judge decides.
DEFAULT_MIN_SIMILARITY = 0.40
DEFAULT_TOP_K = 5


@dataclass(frozen=True)
class ResearchSettings:
    project: str = DEFAULT_PROJECT
    setup: str = DEFAULT_SETUP
    constraints: str = DEFAULT_CONSTRAINTS
    libraries: dict[str, int | None] = field(default_factory=lambda: dict(DEFAULT_LIBRARIES))
    queries: dict[int, tuple[str, ...]] = field(default_factory=dict)
    skip_issues: frozenset[int] = frozenset()
    include_issues: frozenset[int] = frozenset()
    max_cards_per_run: int = 3
    max_active: int = 8
    max_judge_per_run: int = 60
    min_similarity: float = DEFAULT_MIN_SIMILARITY
    top_k: int = DEFAULT_TOP_K
    years: int = 3
    results_per_query: int = 25
    embed_model: str = DEFAULT_EMBED_MODEL
    domain_terms: tuple[str, ...] = DEFAULT_DOMAIN_TERMS
    source: str = "defaults"  # where the settings came from, for --verbose


KNOWN_KEYS = {
    "project", "setup", "constraints", "libraries", "queries", "skip_issues", "include_issues",
    "max_cards_per_run", "max_active", "max_judge_per_run", "min_similarity", "top_k",
    "years", "results_per_query", "embed_model", "domain_terms",
}


def _issue_num(v: Any, where: str) -> int:
    if isinstance(v, str) and v.strip().lstrip("#").isdigit():
        v = int(v.strip().lstrip("#"))
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        raise ValueError(f"{where}: expected an issue number, got {v!r}")
    return v


def _pos_int(raw: dict, key: str, default: int) -> int:
    v = raw.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise ValueError(f"{key}: expected a non-negative integer, got {v!r}")
    return v


def parse_settings(raw: Any, source: str = "research.yaml") -> ResearchSettings:
    """Settings from a parsed YAML mapping. Raises ValueError naming the bad key."""
    base = ResearchSettings()
    if raw is None:
        return replace(base, source=source)
    if not isinstance(raw, dict):
        raise ValueError("expected a mapping at the top level")
    unknown = set(raw) - KNOWN_KEYS
    if unknown:
        raise ValueError(f"unknown keys: {', '.join(sorted(map(str, unknown)))}")
    out: dict[str, Any] = {"source": source}
    for key in ("project", "setup", "constraints", "embed_model"):
        if key in raw:
            if not isinstance(raw[key], str) or not raw[key].strip():
                raise ValueError(f"{key}: expected text")
            out[key] = " ".join(raw[key].split())
    if "libraries" in raw:
        libs = raw["libraries"] or {}
        if not isinstance(libs, dict):
            raise ValueError("libraries: expected a map of owner/repo -> issue number or null")
        parsed: dict[str, int | None] = {}
        for repo, issue in libs.items():
            if not isinstance(repo, str) or repo.count("/") != 1:
                raise ValueError(f"libraries: {repo!r} is not owner/repo")
            parsed[repo] = None if issue is None else _issue_num(issue, f"libraries.{repo}")
        out["libraries"] = parsed
    if "queries" in raw:
        q = raw["queries"] or {}
        if not isinstance(q, dict):
            raise ValueError("queries: expected a map of issue number -> list of queries")
        qs: dict[int, tuple[str, ...]] = {}
        for n, items in q.items():
            num = _issue_num(n, "queries")
            if isinstance(items, str):
                items = [items]
            if not isinstance(items, list) or not all(isinstance(x, str) and x.strip()
                                                      for x in items):
                raise ValueError(f"queries.{num}: expected a list of text")
            qs[num] = tuple(" ".join(x.split()) for x in items)
        out["queries"] = qs
    for key in ("skip_issues", "include_issues"):
        if key in raw:
            items = raw[key] or []
            if not isinstance(items, list):
                raise ValueError(f"{key}: expected a list of issue numbers")
            out[key] = frozenset(_issue_num(x, key) for x in items)
    for key in ("max_cards_per_run", "max_active", "max_judge_per_run", "top_k", "years",
                "results_per_query"):
        if key in raw:
            out[key] = _pos_int(raw, key, getattr(base, key))
    if "results_per_query" in out and not 1 <= out["results_per_query"] <= 100:
        raise ValueError("results_per_query: expected 1 to 100")
    if "years" in out and out["years"] < 1:
        raise ValueError("years: expected at least 1")
    if "min_similarity" in raw:
        v = raw["min_similarity"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1:
            raise ValueError(f"min_similarity: expected a number from 0 to 1, got {v!r}")
        out["min_similarity"] = float(v)
    if "domain_terms" in raw:
        terms = raw["domain_terms"] or []
        if not isinstance(terms, list) or not all(isinstance(t, str) and t.strip()
                                                  for t in terms):
            raise ValueError("domain_terms: expected a list of text")
        out["domain_terms"] = tuple(" ".join(t.split()) for t in terms)
    return replace(base, **out)


def load_settings(path: Path | None) -> tuple[ResearchSettings, str | None]:
    """(settings, note). A missing file gives the defaults and no note; a broken file
    gives the defaults plus a note saying what is wrong."""
    if path is None:
        return ResearchSettings(), None
    path = Path(path).expanduser()
    try:
        text = path.read_text()
    except FileNotFoundError:
        return ResearchSettings(), None
    except OSError as e:
        return ResearchSettings(), f"research: cannot read {path}: {e}; using defaults"
    try:
        return parse_settings(yaml.safe_load(text), str(path)), None
    except (yaml.YAMLError, ValueError) as e:
        return ResearchSettings(), f"research: {path} is invalid ({e}); using defaults"
