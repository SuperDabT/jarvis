from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_REPO = "SuperDabT/stm32-lidar-scanner"
DEFAULT_DB_PATH = Path("~/.local/share/jarvis/jarvis.db")


@dataclass(frozen=True)
class Config:
    repo: str = DEFAULT_REPO
    token: str | None = None
    db_path: Path = DEFAULT_DB_PATH.expanduser()

    @classmethod
    def from_env(cls) -> Config:
        db = os.environ.get("JARVIS_DB")
        return cls(
            repo=os.environ.get("JARVIS_REPO") or DEFAULT_REPO,
            token=os.environ.get("GITHUB_TOKEN") or None,
            db_path=Path(db).expanduser() if db else DEFAULT_DB_PATH.expanduser(),
        )
