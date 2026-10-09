"""Phase 6: the research loop (arXiv + library releases -> a few research cards)."""
from jarvis.research.runner import ResearchResult, resolve_closed, run_research
from jarvis.research.settings import ResearchSettings, load_settings

__all__ = ["ResearchResult", "ResearchSettings", "load_settings", "resolve_closed",
           "run_research"]
