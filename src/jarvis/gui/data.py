"""View models for the window, loaded from SQLite. No Qt here, so it is unit-testable.

The window only reads: the newest stored briefing, the active cards, and the
evidence behind each briefing sentence (from ``facts.compute_facts``). Writes go
through ``cards.mark_done`` / ``cards.set_feedback`` in the window itself.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from jarvis.cards import Card, list_cards

GITHUB = "https://github.com"
MAX_LINKS_PER_SENTENCE = 6
NEW_CARD_HOURS = 24

# Display order and labels for card groups. Unknown types are appended after these.
CARD_TYPES: tuple[tuple[str, str], ...] = (
    ("electrical", "Electrical"),
    ("research", "Research"),
)


@dataclass(frozen=True)
class EvidenceLink:
    label: str  # "#28", "a1b2c3d", "Milestone Beta"
    url: str


@dataclass(frozen=True)
class SentenceView:
    text: str
    uncertain: bool
    fact_ids: tuple[str, ...]
    links: tuple[EvidenceLink, ...] = ()


@dataclass(frozen=True)
class BriefingView:
    headline: str
    sentences: tuple[SentenceView, ...]
    source: str  # "model" | "template"
    model: str | None
    created_at: str  # ISO 8601 UTC

    @property
    def source_label(self) -> str:
        return (self.model or "model") if self.source == "model" else "template"


@dataclass(frozen=True)
class CardView:
    card: Card
    is_new: bool = False

    @property
    def id(self) -> int:
        return self.card.id


@dataclass(frozen=True)
class CardGroup:
    type: str
    label: str
    cards: tuple[CardView, ...]


@dataclass(frozen=True)
class WindowData:
    repo: str
    briefing: BriefingView | None
    groups: tuple[CardGroup, ...]
    last_sync: str | None  # ISO 8601 UTC of the newest sync_log row
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def card_count(self) -> int:
        return sum(len(g.cards) for g in self.groups)

    @property
    def updated_at(self) -> str | None:
        """Newest of last sync and briefing time: when the shown data was last checked."""
        stamps = [s for s in (self.last_sync, self.briefing and self.briefing.created_at) if s]
        return max(stamps, key=_parse) if stamps else None


# ---------------------------------------------------------------- time


def _parse(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def local_stamp(iso: str | None, now: datetime | None = None) -> str:
    """"18:42" today, "Oct 7 18:42" otherwise, in local time. Empty for None."""
    if not iso:
        return ""
    try:
        local = _parse(iso).astimezone()
    except ValueError:
        return iso
    today = (now or datetime.now(UTC)).astimezone().date()
    hm = local.strftime("%H:%M")
    return hm if local.date() == today else f"{local.strftime('%b')} {local.day} {hm}"


# ---------------------------------------------------------------- evidence


def evidence_link(item: str, repo: str, conn: sqlite3.Connection | None = None) -> EvidenceLink | None:
    """Turn one fact evidence string ("#12", "commit:abc1234", "milestone:Beta") into a link."""
    base = f"{GITHUB}/{repo}"
    if item.startswith("#") and item[1:].isdigit():
        return EvidenceLink(item, f"{base}/issues/{item[1:]}")
    if item.startswith("commit:"):
        sha = item.split(":", 1)[1].strip()
        if not sha:
            return None
        return EvidenceLink(sha[:7], f"{base}/commit/{sha}")
    if item.startswith("milestone:"):
        title = item.split(":", 1)[1].strip()
        url = None
        if conn is not None:
            try:
                row = conn.execute(
                    "SELECT html_url FROM milestones WHERE title = ? ORDER BY number DESC LIMIT 1",
                    (title,),
                ).fetchone()
                url = row[0] if row else None
            except sqlite3.Error:
                url = None
        return EvidenceLink(f"{title} milestone", url or f"{base}/milestones")
    return None


def _evidence_by_fact(conn: sqlite3.Connection) -> dict[str, list[str]]:
    from jarvis.facts import compute_facts  # local: facts is heavier and optional here

    return {f.id: list(f.evidence) for f in compute_facts(conn)}


def sentence_links(
    fact_ids: list[str], evidence: dict[str, list[str]], repo: str,
    conn: sqlite3.Connection | None = None,
) -> tuple[EvidenceLink, ...]:
    """Deduplicated links for the facts a sentence cites, issues and commits first."""
    seen: dict[str, EvidenceLink] = {}
    for fid in fact_ids:
        for item in evidence.get(fid, []):
            link = evidence_link(item, repo, conn)
            if link and link.url not in seen:
                seen[link.url] = link
    order = {"issues": 0, "commit": 1}
    links = sorted(seen.values(),
                   key=lambda lk: next((v for k, v in order.items() if f"/{k}/" in lk.url), 2))
    return tuple(links[:MAX_LINKS_PER_SENTENCE])


# ---------------------------------------------------------------- loaders


def latest_briefing_row(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM briefings ORDER BY id DESC LIMIT 1").fetchone()


def load_briefing(
    conn: sqlite3.Connection, repo: str, evidence: dict[str, list[str]] | None = None
) -> BriefingView | None:
    """The newest stored briefing, with evidence links per sentence.

    ``evidence`` maps fact id -> evidence strings; computed from the DB when None.
    Facts that no longer exist (the data moved on since the briefing) simply give
    no links.
    """
    row = latest_briefing_row(conn)
    if row is None:
        return None
    if evidence is None:
        try:
            evidence = _evidence_by_fact(conn)
        except Exception:  # noqa: BLE001 - links are optional; never block the briefing
            evidence = {}
    try:
        raw = json.loads(row["sentences_json"] or "[]")
    except json.JSONDecodeError:
        raw = []
    sentences = []
    for s in raw if isinstance(raw, list) else []:
        if not isinstance(s, dict) or not s.get("text"):
            continue
        ids = [str(i) for i in s.get("fact_ids") or []]
        sentences.append(SentenceView(
            text=str(s["text"]), uncertain=bool(s.get("uncertain")), fact_ids=tuple(ids),
            links=sentence_links(ids, evidence, repo, conn),
        ))
    return BriefingView(
        headline=row["headline"] or "", sentences=tuple(sentences), source=row["source"],
        model=row["model"], created_at=row["created_at"],
    )


def load_card_groups(conn: sqlite3.Connection, now: datetime | None = None) -> tuple[CardGroup, ...]:
    """Active cards grouped by type, known types first, empty groups left out."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=NEW_CARD_HOURS)
    by_type: dict[str, list[CardView]] = {}
    for c in list_cards(conn, "active"):
        try:
            is_new = _parse(c.created_at) >= cutoff
        except (TypeError, ValueError):
            is_new = False
        by_type.setdefault(c.type, []).append(CardView(c, is_new))
    labels = dict(CARD_TYPES)
    order = [t for t, _ in CARD_TYPES] + sorted(t for t in by_type if t not in labels)
    return tuple(
        CardGroup(t, labels.get(t, t.capitalize()), tuple(by_type[t]))
        for t in order if by_type.get(t)
    )


def last_sync(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(ran_at) FROM sync_log").fetchone()
    return row[0] if row else None


def load_window_data(conn: sqlite3.Connection, repo: str,
                     now: datetime | None = None) -> WindowData:
    errors: list[str] = []
    try:
        briefing = load_briefing(conn, repo)
    except sqlite3.Error as e:
        briefing = None
        errors.append(f"Could not read the briefing: {e}")
    try:
        groups = load_card_groups(conn, now)
    except sqlite3.Error as e:
        groups = ()
        errors.append(f"Could not read suggestions: {e}")
    try:
        synced = last_sync(conn)
    except sqlite3.Error:
        synced = None
    return WindowData(repo, briefing, groups, synced, tuple(errors))


# ---------------------------------------------------------------- refresh outcome


def refresh_message(result, now: datetime | None = None) -> tuple[str, str]:
    """(level, text) for the status line after a refresh. level: "ok" or "warn".

    ``result`` is a ``pipeline.RefreshResult`` (duck-typed: .sync, .briefing,
    .cards_new, .cards_resolved, .notes).
    """
    problems: list[str] = []
    failed = [name for name, outcome in (getattr(result, "sync", None) or {}).items()
              if str(outcome).startswith("error")]
    if failed:
        problems.append(f"GitHub sync failed for {', '.join(failed)}; showing what was cached")
    reason = getattr(getattr(result, "briefing", None), "fallback_reason", None) or ""
    if reason.startswith("model unavailable"):
        problems.append("Ollama unavailable, showing template briefing")
    elif reason.startswith("model output rejected"):
        problems.append("Model reply did not pass the checks, showing template briefing")
    notes = [str(n) for n in (getattr(result, "notes", None) or []) if n]

    stamp = local_stamp((now or datetime.now(UTC)).isoformat())
    parts = [f"Refreshed at {stamp}"]
    new = getattr(result, "cards_new", 0) or 0
    resolved = getattr(result, "cards_resolved", 0) or 0
    if new:
        parts.append(f"{new} new suggestion{'s' if new != 1 else ''}")
    if resolved:
        parts.append(f"{resolved} resolved on its own" if resolved == 1
                     else f"{resolved} resolved on their own")
    if problems:
        return "warn", " · ".join(problems + notes)
    return "ok", " · ".join(parts + notes)


def describe_error(e: BaseException) -> str:
    """A short, human sentence for an exception raised by a refresh."""
    name = type(e).__name__
    module = type(e).__module__ or ""
    if module.startswith("httpx") and ("Connect" in name or "Timeout" in name
                                       or "Network" in name):
        return "Could not reach GitHub (offline?). Showing cached data."
    if name == "GitHubError":
        return f"{e}. Showing cached data."
    if isinstance(e, sqlite3.OperationalError):
        return f"Database busy or unreadable: {e}"
    text = str(e).strip() or name
    if len(text) > 160:
        text = text[:157] + "..."
    return f"Refresh failed: {text}"
