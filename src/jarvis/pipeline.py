from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from jarvis.briefing import Briefing, build_briefing
from jarvis.checks import Finding, run_all
from jarvis.config import Config
from jarvis.electrical import sync_cards
from jarvis.github import GitHubClient
from jarvis.hardware import Hardware, HardwareError, HardwareSource, find_hardware, load_hardware
from jarvis.ollama import OllamaClient
from jarvis.sync import sync


@dataclass
class RefreshResult:
    sync: dict[str, str]
    briefing: Briefing
    cards_new: int = 0
    cards_resolved: int = 0
    notes: list[str] = field(default_factory=list)


def run_checks(conn: sqlite3.Connection, cfg: Config,
               ollama: OllamaClient | None) -> tuple[int, int, list[str]]:
    """Electrical checks -> cards. Returns (new cards, auto-resolved, notes).

    hardware.yaml comes from the synced repo copy if the repo has one, else
    ``cfg.hardware_path`` ($JARVIS_HARDWARE or ~/.config/jarvis/hardware.yaml).
    No file -> one note and no card changes. A file that fails validation ->
    a note naming the bad field, and existing cards are left as they are.
    """
    loaded = load_checks(conn, cfg)
    if isinstance(loaded, str):
        return 0, 0, [loaded]
    src, hw, findings = loaded
    result = sync_cards(conn, hw, findings, ollama)
    return result.new, result.resolved, [f"hardware: {src.label}", *result.notes]


def load_checks(conn: sqlite3.Connection, cfg: Config
                ) -> tuple[HardwareSource, Hardware, list[Finding]] | str:
    """(source, hardware, findings), or a note string explaining why not."""
    src = find_hardware(conn, cfg.hardware_path)
    if src is None:
        return (f"hardware: no hardware.yaml in the repo or at {cfg.hardware_path}; "
                "electrical checks skipped (see docs/hardware.example.yaml)")
    try:
        hw = load_hardware(src.text)
    except HardwareError as e:
        return f"hardware: {src.label} is invalid, checks skipped: {e}"
    return src, hw, run_all(hw)


def refresh(conn: sqlite3.Connection, cfg: Config, use_model: bool = True,
            force_briefing: bool = False) -> RefreshResult:
    """One full pass: sync GitHub, run checks, build the briefing.

    Shared by the CLI and the window's refresh button.
    """
    with GitHubClient(cfg.repo, cfg.token, conn) as gh:
        outcomes = sync(conn, gh)
    ollama = OllamaClient(cfg.ollama_url, cfg.model) if use_model else None
    try:
        new, resolved, notes = run_checks(conn, cfg, ollama)
        briefing = build_briefing(conn, ollama, force=force_briefing)
    finally:
        if ollama:
            ollama.close()
    return RefreshResult(outcomes, briefing, new, resolved, notes)
