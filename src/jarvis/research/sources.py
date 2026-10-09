"""Research sources: the arXiv API and GitHub releases of libraries the project uses.

arXiv
-----
``export.arxiv.org/api/query`` returns Atom XML, parsed with the standard library.
The client keeps arXiv's guideline of at most one request every 3 seconds and sends
a descriptive User-Agent.

Sort order: ``relevance``, restricted to the last N years by a ``submittedDate``
range. A ``submittedDate`` sort returns whatever is newest among loosely matching
papers (arXiv ANDs the words anywhere in the record), which for narrow hobby topics
is mostly unrelated work that happens to be recent. Relevance inside a recent window
gives "recent and on topic"; newly published papers still surface once they rank,
and the seen cache makes re-reading the same top results cost no model call.

GitHub releases
---------------
Releases first (newest non-draft, non-prerelease); a repo that only pushes tags
(STM32CubeF4) falls back to its tags, highest version wins. Both use the existing
conditional requests, so an unchanged repo is a 304.
"""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

ARXIV_URL = "https://export.arxiv.org/api/query"
ARXIV_MIN_INTERVAL_S = 3.0
USER_AGENT = ("jarvis-research/0.1 (personal project assistant; a few dozen queries "
              "every few hours, 1 request per 3 s)")
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"

_VERSION_RE = re.compile(r"v\d+$")
_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+\-]*")
QUERY_STOPWORDS = frozenset(
    """a an the of on in for and or with without by at to from into via using based
    is are be as its it this that these how what when vs versus""".split())


class SourceError(RuntimeError):
    """A research source could not be read (network, bad status, bad XML)."""


@dataclass(frozen=True)
class Paper:
    id: str  # arXiv id without version, e.g. "2603.09783"
    title: str
    abstract: str
    authors: tuple[str, ...]
    published: str  # YYYY-MM-DD
    url: str  # abs page without version
    categories: tuple[str, ...] = ()

    @property
    def year(self) -> int | None:
        try:
            return int(self.published[:4])
        except (TypeError, ValueError):
            return None

    def authors_short(self) -> str:
        if not self.authors:
            return "unknown authors"
        head = ", ".join(self.authors[:3])
        return head + (" et al." if len(self.authors) > 3 else "")


# ---------------------------------------------------------------- arXiv parsing


def _text(el: ET.Element | None) -> str:
    return " ".join((el.text or "").split()) if el is not None else ""


def arxiv_id(raw: str) -> str:
    """'http://arxiv.org/abs/2603.09783v2' -> '2603.09783' (old ids keep their archive)."""
    raw = raw.strip()
    if "/abs/" in raw:
        raw = raw.split("/abs/", 1)[1]
    return _VERSION_RE.sub("", raw)


def parse_atom(xml_text: str) -> list[Paper]:
    """Papers in an arXiv Atom feed. An error feed (a single 'Error' entry) raises."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise SourceError(f"arXiv returned unparseable XML: {e}") from e
    papers: list[Paper] = []
    for entry in root.findall(f"{ATOM}entry"):
        raw_id = _text(entry.find(f"{ATOM}id"))
        title = _text(entry.find(f"{ATOM}title"))
        if "/api/errors" in raw_id or title == "Error":
            raise SourceError(f"arXiv query error: {_text(entry.find(f'{ATOM}summary'))}")
        if not raw_id or not title:
            continue
        pid = arxiv_id(raw_id)
        authors = tuple(_text(a.find(f"{ATOM}name")) for a in entry.findall(f"{ATOM}author"))
        cats = tuple(c.get("term", "") for c in entry.findall(f"{ATOM}category")
                     if c.get("term"))
        published = _text(entry.find(f"{ATOM}published"))[:10]
        papers.append(Paper(
            id=pid, title=title, abstract=_text(entry.find(f"{ATOM}summary")),
            authors=tuple(a for a in authors if a), published=published,
            url=f"https://arxiv.org/abs/{pid}", categories=cats))
    return papers


# ---------------------------------------------------------------- arXiv queries


@dataclass(frozen=True)
class Query:
    """One arXiv search: content words ANDed, optionally AND (any domain term)."""

    text: str
    domain: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    origin: str = "title"  # "title" | "model" | "config"

    def words(self) -> list[str]:
        seen: list[str] = []
        for w in _WORD_RE.findall(self.text):
            lw = w.lower()
            if lw in QUERY_STOPWORDS or len(lw) < 2 or lw in seen:
                continue
            seen.append(lw)
        return seen

    def label(self) -> str:
        dom = f" + [{' | '.join(self.domain)}]" if self.domain else ""
        return f"{self.text}{dom} ({self.origin})"


def _term(word: str) -> str:
    word = word.strip()
    return f'all:"{word}"' if (" " in word or "-" in word) else f"all:{word}"


def _clause(q: Query) -> str:
    parts = [_term(w) for w in q.words()]
    if q.domain:
        dom = [_term(t.lower()) for t in q.domain]
        parts.append(dom[0] if len(dom) == 1 else "(" + " OR ".join(dom) + ")")
    return " AND ".join(parts)


def search_query(queries: Query | list[Query], today: date, years: int) -> str:
    """One arXiv search_query for one or several queries (ORed), restricted to the
    union of their categories and to the last ``years`` years."""
    qs = [queries] if isinstance(queries, Query) else list(queries)
    clauses = [c for c in (_clause(q) for q in qs) if c]
    if not clauses:
        raise ValueError("no searchable words")
    body = clauses[0] if len(clauses) == 1 else " OR ".join(f"({c})" for c in clauses)
    parts = [body if len(clauses) == 1 else f"({body})"]
    cats: list[str] = []
    for q in qs:
        cats += [c for c in q.categories if c not in cats]
    if cats:
        cc = [f"cat:{c}" for c in cats]
        parts.append(cc[0] if len(cc) == 1 else "(" + " OR ".join(cc) + ")")
    start = today - timedelta(days=365 * years)
    parts.append(f"submittedDate:[{start:%Y%m%d}0000 TO {today:%Y%m%d}2359]")
    return " AND ".join(parts)


class ArxivClient:
    """Rate-limited arXiv API client. ``sleep``/``clock`` are injectable for tests.

    At most one request every ``min_interval`` seconds. A 429/503 or a timeout is
    retried after ``retry_waits`` (Retry-After wins when it is longer); then
    ``SourceError`` is raised and the caller stops asking arXiv for this run.
    """

    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], Any] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        min_interval: float = ARXIV_MIN_INTERVAL_S,
        timeout: float = 60,
        retry_waits: tuple[float, ...] = (20.0, 60.0),
    ) -> None:
        self._http = httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT},
                                  transport=transport, follow_redirects=True)
        self.sleep, self.clock = sleep, clock
        self.min_interval = min_interval
        self.retry_waits = retry_waits
        self._last: float | None = None
        self.requests = 0
        self.seconds = 0.0  # time spent waiting on arXiv, including the polite gaps

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> ArxivClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _once(self, params: dict) -> httpx.Response | str:
        """One polite request: the response, or a retryable failure text."""
        if self._last is not None:
            wait = self.min_interval - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)
        try:
            return self._http.get(ARXIV_URL, params=params)
        except httpx.TimeoutException as e:
            return f"arXiv timed out ({type(e).__name__})"
        except httpx.HTTPError as e:
            raise SourceError(f"arXiv unreachable: {type(e).__name__}: {e}") from e
        finally:
            self._last = self.clock()
            self.requests += 1

    def search(self, queries: Query | list[Query], today: date, years: int = 3,
               max_results: int = 25) -> list[Paper]:
        t0 = self.clock()
        params = {"search_query": search_query(queries, today, years), "start": 0,
                  "max_results": max_results, "sortBy": "relevance", "sortOrder": "descending"}
        try:
            for attempt in range(len(self.retry_waits) + 1):
                resp = self._once(params)
                if isinstance(resp, httpx.Response) and resp.status_code == 200:
                    return parse_atom(resp.text)
                if isinstance(resp, httpx.Response) and resp.status_code not in (429, 503):
                    raise SourceError(f"arXiv returned {resp.status_code}: {resp.text[:160]}")
                why = resp if isinstance(resp, str) else (
                    f"arXiv returned {resp.status_code} ({' '.join(resp.text.split())[:60]})")
                if attempt == len(self.retry_waits):
                    raise SourceError(why)
                wait = self.retry_waits[attempt]
                if isinstance(resp, httpx.Response):
                    try:
                        wait = max(wait, float(resp.headers.get("Retry-After", 0)))
                    except ValueError:
                        pass
                self.sleep(wait)
            raise SourceError("unreachable")  # pragma: no cover
        finally:
            self.seconds += self.clock() - t0


# ---------------------------------------------------------------- releases


@dataclass(frozen=True)
class Release:
    repo: str
    tag: str
    url: str
    name: str = ""
    published: str = ""  # YYYY-MM-DD, "" for a bare tag
    notes: str = ""
    source: str = "releases"  # "releases" | "tags"


_VERSION_NUM_RE = re.compile(r"^[vV]?(\d+(?:\.\d+)*)$")


def version_key(tag: str) -> tuple[int, ...] | None:
    """(1, 28, 3) for 'v1.28.3'; None for tags that are not plain versions
    ('V202110.00-SMP', 'rc1' builds), which are never picked as latest."""
    m = _VERSION_NUM_RE.match(tag.strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def newest_release(repo: str, data: Any) -> Release | None:
    """Newest published, non-draft, non-prerelease entry of a /releases payload."""
    best = None
    for r in data if isinstance(data, list) else []:
        if not isinstance(r, dict) or r.get("draft") or r.get("prerelease") or not r.get("tag_name"):
            continue
        when = r.get("published_at") or r.get("created_at") or ""
        if best is None or when > best[0]:
            best = (when, r)
    if best is None:
        return None
    r = best[1]
    return Release(repo=repo, tag=r["tag_name"], name=r.get("name") or r["tag_name"],
                   url=r.get("html_url") or f"https://github.com/{repo}/releases/tag/{r['tag_name']}",
                   published=(best[0] or "")[:10], notes=(r.get("body") or "").strip(),
                   source="releases")


def newest_tag(repo: str, data: Any) -> Release | None:
    """Highest plain-version tag of a /tags payload."""
    tags = [t.get("name") for t in data if isinstance(t, dict)] if isinstance(data, list) else []
    ranked = sorted((version_key(t), t) for t in tags if t and version_key(t) is not None)
    if not ranked:
        return None
    tag = ranked[-1][1]
    return Release(repo=repo, tag=tag, name=tag,
                   url=f"https://github.com/{repo}/releases/tag/{tag}", source="tags")


def is_newer(new: str, old: str) -> bool:
    """True when tag ``new`` should replace baseline ``old`` (version order when both
    parse, otherwise any different tag counts, since releases are already date-ordered)."""
    if new == old:
        return False
    a, b = version_key(new), version_key(old)
    if a is not None and b is not None:
        return a > b
    return True


def today_utc() -> date:
    return datetime.now(UTC).date()
