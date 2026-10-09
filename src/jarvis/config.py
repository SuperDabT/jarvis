from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_REPO = "SuperDabT/stm32-lidar-scanner"
DEFAULT_DB_PATH = Path("~/.local/share/jarvis/jarvis.db")
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "gemma4:26b"
DEFAULT_HARDWARE_PATH = Path("~/.config/jarvis/hardware.yaml")


@dataclass(frozen=True)
class Config:
    repo: str = DEFAULT_REPO
    token: str | None = None
    db_path: Path = DEFAULT_DB_PATH.expanduser()
    ollama_url: str = DEFAULT_OLLAMA_URL
    model: str = DEFAULT_MODEL
    # Local hardware.yaml, used when the repo has none (Jarvis never writes to the repo).
    hardware_path: Path = DEFAULT_HARDWARE_PATH.expanduser()

    @classmethod
    def from_env(cls) -> Config:
        db = os.environ.get("JARVIS_DB")
        hw = os.environ.get("JARVIS_HARDWARE")
        return cls(
            repo=os.environ.get("JARVIS_REPO") or DEFAULT_REPO,
            token=os.environ.get("GITHUB_TOKEN") or None,
            db_path=Path(db).expanduser() if db else DEFAULT_DB_PATH.expanduser(),
            ollama_url=os.environ.get("JARVIS_OLLAMA_URL") or DEFAULT_OLLAMA_URL,
            model=os.environ.get("JARVIS_MODEL") or DEFAULT_MODEL,
            hardware_path=Path(hw).expanduser() if hw else DEFAULT_HARDWARE_PATH.expanduser(),
        )
