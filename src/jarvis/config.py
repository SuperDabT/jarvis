from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_REPO = "SuperDabT/stm32-lidar-scanner"
DEFAULT_DB_PATH = Path("~/.local/share/jarvis/jarvis.db")
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "gemma4:26b"
DEFAULT_HARDWARE_PATH = Path("~/.config/jarvis/hardware.yaml")
DEFAULT_RESEARCH_PATH = Path("~/.config/jarvis/research.yaml")
# Voice mode: a smaller model that fits fully on the GPU, plus local STT/TTS services.
DEFAULT_VOICE_MODEL = "qwen3:14b"
DEFAULT_STT_URL = "http://127.0.0.1:5092"   # Parakeet (parakeet-stt.service)
DEFAULT_TTS_URL = "http://127.0.0.1:8880"   # Kokoro (kokoro-tts.service)
DEFAULT_VOICE = "bm_george"


ENV_FILE = Path("~/.config/jarvis/env")


def env_value(key: str, env: dict[str, str] | None = None,
              env_file: str | Path | None = None) -> str | None:
    """``$key``, else its line in the env file (the service loads that file, but the
    window and a terminal don't). None when it isn't set. Never logged."""
    value = (env if env is not None else os.environ).get(key, "").strip()
    if value:
        return value
    try:
        text = Path(env_file or ENV_FILE).expanduser().read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        k, sep, v = line.strip().removeprefix("export ").partition("=")
        if sep and k.strip() == key:
            return v.strip().strip("'\"") or None
    return None


@dataclass(frozen=True)
class Config:
    repo: str = DEFAULT_REPO
    token: str | None = None
    db_path: Path = DEFAULT_DB_PATH.expanduser()
    ollama_url: str = DEFAULT_OLLAMA_URL
    model: str = DEFAULT_MODEL
    # Local hardware.yaml, used when the repo has none (Jarvis never writes to the repo).
    hardware_path: Path = DEFAULT_HARDWARE_PATH.expanduser()
    # Optional research loop settings (phase 6); defaults apply when the file is absent.
    research_path: Path = DEFAULT_RESEARCH_PATH.expanduser()
    voice_model: str = DEFAULT_VOICE_MODEL
    stt_url: str = DEFAULT_STT_URL
    tts_url: str = DEFAULT_TTS_URL
    voice: str = DEFAULT_VOICE
    # The tool-using assistant's model (jarvis.agent); None = ``model``.
    agent_model: str | None = None

    @classmethod
    def from_env(cls) -> Config:
        db = os.environ.get("JARVIS_DB")
        hw = os.environ.get("JARVIS_HARDWARE")
        rs = os.environ.get("JARVIS_RESEARCH")
        return cls(
            repo=os.environ.get("JARVIS_REPO") or DEFAULT_REPO,
            token=os.environ.get("GITHUB_TOKEN") or None,
            db_path=Path(db).expanduser() if db else DEFAULT_DB_PATH.expanduser(),
            ollama_url=os.environ.get("JARVIS_OLLAMA_URL") or DEFAULT_OLLAMA_URL,
            model=os.environ.get("JARVIS_MODEL") or DEFAULT_MODEL,
            hardware_path=Path(hw).expanduser() if hw else DEFAULT_HARDWARE_PATH.expanduser(),
            research_path=Path(rs).expanduser() if rs else DEFAULT_RESEARCH_PATH.expanduser(),
            voice_model=os.environ.get("JARVIS_VOICE_MODEL") or DEFAULT_VOICE_MODEL,
            stt_url=os.environ.get("JARVIS_STT_URL") or DEFAULT_STT_URL,
            tts_url=os.environ.get("JARVIS_TTS_URL") or DEFAULT_TTS_URL,
            voice=os.environ.get("JARVIS_VOICE") or DEFAULT_VOICE,
            agent_model=os.environ.get("JARVIS_AGENT_MODEL") or None,
        )
