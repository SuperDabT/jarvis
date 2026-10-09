"""One research run: issues -> queries -> arXiv -> prefilter -> judge -> few cards.

Also library releases (baseline first, cards only for later tags) and the
auto-resolve rule: a research card whose issue is closed is auto-resolved.

Card rules
----------
- type research, ``source_ref`` "#N", ``paper_id`` the arXiv id without version
  (or ``release:owner/repo@tag``), so the fingerprint is research + issue + paper.
- A paper never gets a second card, for any issue (checked against every card
  with that paper_id, whatever its status).
- At most one active paper card per issue (the next waits until it is done or
  resolved), ``max_cards_per_run`` per run and
  ``max_active`` active research cards overall. Ranking: non-someday issues
  first, then confidence, similarity, recency. Someday issues are only searched
  when the other issues left room in the run's budget.
- Release cards are exempt from the caps (rare, and a newer tag of the same repo
  auto-resolves the older card).
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from jarvis.cards import auto_resolve, upsert_card
from jarvis.db import now_iso
from jarvis.ollama import OllamaError
from jarvis.research.queries import IssuePlan, excerpt, plan_issues
from jarvis.research.relevance import (
    Embedder,
    Scored,
    Verdict,
    cached_verdict,
    confirm,
    judge,
    prefilter,
    store_verdict,
)
from jarvis.research.settings import ResearchSettings
from jarvis.research.sources import (
    ArxivClient,
    Paper,
    Release,
    SourceError,
    is_newer,
    newest_release,
    newest_tag,
    search_query,
)

CARD_TYPE = "research"
MAX_TITLE_WORDS = 12
NOTES_CHARS = 1500
CONF_RANK = {"high": 0, "medium": 1}


# ---------------------------------------------------------------- results


@dataclass
class Judged:
    scored: Scored
    verdict: Verdict


@dataclass
class IssueReport:
    plan: IssuePlan
    search: str = ""  # outcome of the issue's one arXiv request
    candidates: int = 0
    scored: list[Scored] = field(default_factory=list)
    kept: list[Scored] = field(default_factory=list)
    judged: list[Judged] = field(default_factory=list)


@dataclass
class PlannedCard:
    title: str
    why: str
    source_ref: str
    source_url: str
    paper_id: str
    claude_prompt: str
    kind: str  # "paper" | "release"
    card_id: int | None = None
    detail: str = ""  # similarity/confidence, for --verbose


@dataclass
class ResearchResult:
    dry_run: bool = False
    plans: list[IssuePlan] = field(default_factory=list)
    reports: list[IssueReport] = field(default_factory=list)
    cards: list[PlannedCard] = field(default_factory=list)  # created (or would be)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    resolved: int = 0
    queries: int = 0
    candidates: int = 0
    judge_calls: int = 0
    cached_verdicts: int = 0
    t_arxiv: float = 0.0
    t_embed: float = 0.0
    t_judge: float = 0.0
    duration: float = 0.0

    @property
    def new(self) -> int:
        return 0 if self.dry_run else sum(1 for c in self.cards if c.card_id is not None)

    @property
    def skipped(self) -> list[IssuePlan]:
        return [p for p in self.plans if p.skip]

    @property
    def error(self) -> str | None:
        return "; ".join(self.errors) if self.errors else None

    def summary(self) -> str:
        researched = len(self.plans) - len(self.skipped)
        made = len(self.cards)
        verb = "would create" if self.dry_run else "new"
        return (f"{researched} issues ({len(self.skipped)} skipped), {self.queries} queries, "
                f"{self.candidates} candidates, {self.judge_calls} judged "
                f"({self.cached_verdicts} cached), {made} {verb}, {self.resolved} resolved "
                f"(arXiv {self.t_arxiv:.0f}s, embed {self.t_embed:.0f}s, "
                f"judge {self.t_judge:.0f}s, total {self.duration:.0f}s)")


# ---------------------------------------------------------------- auto-resolve


def resolve_closed(conn: sqlite3.Connection) -> int:
    """Auto-resolve active research cards whose issue ("#N") is closed. Local, no network."""
    rows = conn.execute(
        """SELECT c.fingerprint FROM cards c JOIN issues i
               ON c.source_ref = '#' || i.number
           WHERE c.type = 'research' AND c.status = 'active' AND i.state = 'closed'"""
    ).fetchall()
    return auto_resolve(conn, [r["fingerprint"] for r in rows])


def active_research(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM cards WHERE type = 'research' "
                        "AND status = 'active'").fetchone()[0]


def paper_carded(conn: sqlite3.Connection, paper_id: str) -> bool:
    return conn.execute("SELECT 1 FROM cards WHERE type = 'research' AND paper_id = ? LIMIT 1",
                        (paper_id,)).fetchone() is not None


# ---------------------------------------------------------------- card text

_SMALL = {"a", "an", "the", "of", "for", "and", "or", "with", "in", "on", "to", "via", "by"}


def short_title(title: str, max_words: int = MAX_TITLE_WORDS) -> str:
    """Deterministic shortening to at most ``max_words`` words.

    Long titles keep the part before a colon when that part says something (2+
    words), else the part after it; otherwise they are cut at a word boundary
    with an ellipsis. Never invents words.
    """
    title = " ".join(title.split())
    words = title.split()
    if len(words) <= max_words:
        return title
    if ":" in title:
        head, tail = (x.strip() for x in title.split(":", 1))
        if 2 <= len(head.split()) <= max_words:
            return head
        if len(head.split()) < 2:  # "KLIO: ..." -> the descriptive part
            words = tail.split()
            if len(words) <= max_words:
                return tail
    cut = words[:max_words]
    while cut and cut[-1].lower().strip(",;:-") in _SMALL:
        cut.pop()
    return " ".join(cut).rstrip(",;:-") + " …"


def _project_lines(s: ResearchSettings, repo: str) -> list[str]:
    return [f"I'm working on a hobby embedded project: {s.project}",
            f"Setup: {s.setup}. Repo: https://github.com/{repo}"]


def paper_prompt(s: ResearchSettings, repo: str, issue: IssuePlan, p: Paper,
                 v: Verdict) -> str:
    out = _project_lines(s, repo) + [
        "",
        f"My open issue #{issue.number} \"{issue.title}\""
        + (f" ({issue.url})" if issue.url else "") + ":",
        excerpt(issue.body, 800) or "(no description)",
        "",
        "My project assistant found this paper and a local model flagged it as possibly "
        "relevant; that is a guess, so judge for yourself:",
        f"Title: {p.title}",
        f"Authors: {p.authors_short()}",
        f"Year: {p.year or 'unknown'}",
        f"Link: {p.url}",
        "Abstract:",
        p.abstract,
        "",
        f"Why it was flagged (the local model's words): {v.why}",
        "",
        f"Question: Would this approach help with #{issue.number} in my setup ({s.setup})? "
        "What would I need to change, and what's the cheapest experiment to test it?",
    ]
    return "\n".join(out) + "\n"


def paper_card(s: ResearchSettings, repo: str, issue: IssuePlan, j: Judged) -> PlannedCard:
    p, v = j.scored.paper, j.verdict
    return PlannedCard(
        title=short_title(p.title),
        why=f"Related to #{issue.number} {issue.title}: {v.why}",
        source_ref=f"#{issue.number}", source_url=p.url, paper_id=p.id,
        claude_prompt=paper_prompt(s, repo, issue, p, v), kind="paper",
        detail=f"similarity {j.scored.similarity:.3f}, confidence {v.confidence}")


def release_card(s: ResearchSettings, repo: str, rel: Release, old_tag: str,
                 issue: sqlite3.Row | None) -> PlannedCard:
    name = rel.repo.split("/", 1)[1]
    when = f" on {rel.published}" if rel.published else ""
    if issue is not None:
        why = (f"Related to #{issue['number']} {issue['title']}: {rel.repo} published "
               f"{rel.tag}{when}; its changes may matter for this issue.")
        ref = f"#{issue['number']}"
        q_issue = f"#{issue['number']} \"{issue['title']}\""
    else:
        why = (f"{rel.repo} published {rel.tag}{when}, a library this project builds on; "
               "its changes may matter for the firmware.")
        ref = rel.repo
        q_issue = "my project"
    notes = rel.notes.strip()
    if len(notes) > NOTES_CHARS:
        notes = notes[:NOTES_CHARS].rsplit("\n", 1)[0] + "\n[... truncated, see the link]"
    lines = _project_lines(s, repo) + [""]
    if issue is not None:
        body = issue["body"] if "body" in issue.keys() else ""
        lines += [f"My open issue {q_issue}"
                  + (f" ({issue['html_url']})" if issue["html_url"] else "") + ":",
                  excerpt(body, 800) or "(no description)", ""]
    lines += [f"{rel.repo} has a new version: {rel.tag} (previous one I saw: {old_tag}).",
              f"Link: {rel.url}",
              f"Changes since then: https://github.com/{rel.repo}/compare/{old_tag}...{rel.tag}"]
    if notes:
        lines += ["Release notes:", notes]
    else:
        lines += ["(Tag only, no release notes; see the compare link.)"]
    lines += ["", f"Question: Does anything in this release matter for {q_issue} in my setup "
                  f"({s.setup})? What would I need to change, and what's the cheapest way "
                  "to test it?"]
    return PlannedCard(title=f"{name} {rel.tag} released", why=why, source_ref=ref,
                       source_url=rel.url, paper_id=f"release:{rel.repo}@{rel.tag}",
                       claude_prompt="\n".join(lines) + "\n", kind="release",
                       detail=f"previous {old_tag}")


# ---------------------------------------------------------------- releases


def check_releases(conn: sqlite3.Connection, gh: Any, s: ResearchSettings, repo: str,
                   res: ResearchResult, write: bool) -> list[tuple[PlannedCard, str]]:
    """New library releases -> planned cards. First sight records a baseline only."""
    out: list[tuple[PlannedCard, str]] = []
    for lib, issue_no in s.libraries.items():
        row = conn.execute("SELECT * FROM research_releases WHERE repo = ?", (lib,)).fetchone()
        first = row is None
        fetch = gh.get_unconditional if (first or not write) else gh.get
        try:
            rel: Release | None = None
            r = fetch(f"/repos/{lib}/releases", {"per_page": 10})
            if r.status == 200:
                rel = newest_release(lib, r.data)
            elif r.status == 304 and row is not None and row["source"] == "releases":
                res.notes.append(f"release {lib}: unchanged ({row['tag']})")
                continue
            elif r.status == 404:
                res.notes.append(f"release {lib}: repo not found")
                continue
            if rel is None:
                t = fetch(f"/repos/{lib}/tags", {"per_page": 100})
                if t.status == 304 and row is not None:
                    res.notes.append(f"release {lib}: unchanged ({row['tag']})")
                    continue
                rel = newest_tag(lib, t.data) if t.status == 200 else None
        except Exception as e:  # noqa: BLE001 - GitHubError, httpx errors
            res.errors.append(f"release {lib}: {type(e).__name__}: {e}")
            continue
        if rel is None:
            res.notes.append(f"release {lib}: no releases or version tags")
            continue
        if first:
            res.notes.append(f"release {lib}: baseline {rel.tag} recorded (no card)")
            if write:
                conn.execute(
                    "INSERT INTO research_releases (repo, tag, url, source, baseline_at, "
                    "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (lib, rel.tag, rel.url, rel.source, now_iso(), now_iso()))
                conn.commit()
            continue
        if not is_newer(rel.tag, row["tag"]):
            res.notes.append(f"release {lib}: unchanged ({row['tag']})")
            continue
        issue = None
        carded = True
        if issue_no is not None:
            issue = conn.execute("SELECT * FROM issues WHERE number = ?", (issue_no,)).fetchone()
            if issue is None or issue["state"] != "open":
                res.notes.append(f"release {lib}: {rel.tag} is new, but #{issue_no} is not "
                                 "open; no card")
                carded = False
        if carded:
            out.append((release_card(s, repo, rel, row["tag"], issue), lib))
        res.notes.append(f"release {lib}: new {rel.tag} (was {row['tag']})")
        if write:
            conn.execute("UPDATE research_releases SET tag = ?, url = ?, source = ?, "
                         "updated_at = ? WHERE repo = ?",
                         (rel.tag, rel.url, rel.source, now_iso(), lib))
            conn.commit()
    return out


# ---------------------------------------------------------------- papers


def _store_papers(conn: sqlite3.Connection, papers: list[Paper]) -> None:
    now = now_iso()
    for p in papers:
        conn.execute(
            """INSERT INTO research_papers (paper_id, title, authors_json, abstract, published,
                   url, categories, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(paper_id) DO UPDATE SET title=excluded.title,
                   authors_json=excluded.authors_json, abstract=excluded.abstract,
                   published=excluded.published, url=excluded.url,
                   categories=excluded.categories, fetched_at=excluded.fetched_at""",
            (p.id, p.title, json.dumps(list(p.authors)), p.abstract, p.published, p.url,
             " ".join(p.categories), now))
    conn.commit()


SEARCH_CACHE_HOURS = 20  # arXiv answers are reused this long (new papers appear daily)
ARXIV_MAX_FAILURES = 2  # consecutive failed searches before arXiv is left alone this run


def _cached_search(conn: sqlite3.Connection, key: str, now: datetime) -> list[Paper] | None:
    row = conn.execute("SELECT * FROM research_searches WHERE query = ?", (key,)).fetchone()
    if row is None:
        return None
    try:
        age = now - datetime.fromisoformat(row["fetched_at"])
    except (TypeError, ValueError):
        return None
    if age > timedelta(hours=SEARCH_CACHE_HOURS) or age < timedelta(0):
        return None
    ids = json.loads(row["paper_ids"] or "[]")
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = {r["paper_id"]: r for r in conn.execute(
        f"SELECT * FROM research_papers WHERE paper_id IN ({marks})", ids)}
    if len(rows) != len(ids):
        return None
    return [Paper(r["paper_id"], r["title"], r["abstract"] or "",
                  tuple(json.loads(r["authors_json"] or "[]")), r["published"] or "",
                  r["url"], tuple((r["categories"] or "").split())) for i in ids
            for r in [rows[i]]]


def _search_issue(conn, plan: IssuePlan, arxiv: ArxivClient, s: ResearchSettings,
                  now: datetime, res: ResearchResult, write: bool, state: dict
                  ) -> tuple[IssueReport, list[Paper]]:
    """One arXiv request per issue: its queries ORed (cached for SEARCH_CACHE_HOURS)."""
    rep = IssueReport(plan)
    res.queries += len(plan.queries)
    try:
        key = search_query(plan.queries, now.date(), s.years) + f"|n={s.results_per_query}"
    except ValueError:
        rep.search = "no searchable words"
        return rep, []
    papers = _cached_search(conn, key, now)
    if papers is not None:
        rep.search = f"{len(papers)} results (cached)"
    elif state.get("arxiv_down"):
        rep.search = "not searched (arXiv failing this run)"
        return rep, []
    else:
        t0 = time.monotonic()
        try:
            papers = arxiv.search(plan.queries, now.date(), s.years, s.results_per_query)
        except SourceError as e:
            rep.search = f"error: {e}"
            state["failures"] = state.get("failures", 0) + 1
            if state["failures"] >= ARXIV_MAX_FAILURES:
                state["arxiv_down"] = True
                res.errors.append(f"arXiv: {e}; stopped asking for this run")
            return rep, []
        finally:
            res.t_arxiv += time.monotonic() - t0
        state["failures"] = 0
        rep.search = f"{len(papers)} results"
        if write:
            _store_papers(conn, papers)
            conn.execute(
                "INSERT INTO research_searches (query, paper_ids, fetched_at) VALUES (?, ?, ?) "
                "ON CONFLICT(query) DO UPDATE SET paper_ids=excluded.paper_ids, "
                "fetched_at=excluded.fetched_at",
                (key, json.dumps([p.id for p in papers]), now.isoformat(timespec="seconds")))
            conn.commit()
    seen: dict[str, Paper] = {}
    for p in papers:
        seen.setdefault(p.id, p)
    papers = list(seen.values())
    rep.candidates = len(papers)
    res.candidates += len(papers)
    return rep, papers


def run_research(
    conn: sqlite3.Connection,
    s: ResearchSettings,
    *,
    repo: str,
    ollama: Any | None,
    arxiv: ArxivClient | Any,
    gh: Any | None = None,
    dry_run: bool = False,
    only_issue: int | None = None,
    now: datetime | None = None,
    notes: list[str] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> ResearchResult:
    """The whole research pass. ``dry_run`` writes nothing at all."""
    t_start = clock()
    now = now or datetime.now(UTC)
    write = not dry_run
    res = ResearchResult(dry_run=dry_run, notes=[n for n in (notes or []) if n])
    if write:
        res.resolved = resolve_closed(conn)

    res.plans = plan_issues(conn, s, ollama, only=only_issue, write=write)
    open_numbers = {p.number for p in res.plans}

    # Library releases (exempt from the caps).
    release_cards: list[tuple[PlannedCard, str]] = []
    if gh is not None and only_issue is None:
        release_cards = check_releases(conn, gh, s, repo, res, write)

    active = active_research(conn)
    budget = max(0, min(s.max_cards_per_run, s.max_active - active))
    if budget == 0:
        res.notes.append(f"paper search skipped: {active} active research cards "
                         f"(max_active {s.max_active})")
    emb = Embedder(conn, ollama, s.embed_model, write=write)
    accepted: list[tuple[int, IssuePlan, Judged]] = []  # (tier, plan, judged)
    judge_down = ollama is None
    if judge_down:
        res.notes.append("no model: papers are searched but not judged, so no cards")

    researched = [p for p in res.plans if not p.skip]
    arxiv_state: dict = {}
    for tier in (0, 1):
        tier_plans = [p for p in researched if int(p.someday) == tier]
        if not tier_plans or budget == 0:
            continue
        if tier == 1:
            usable = {j.scored.paper.id for _, _, j in accepted
                      if not paper_carded(conn, j.scored.paper.id)}
            if len(usable) >= budget:
                res.notes.append(f"someday issues not searched: the run's budget of {budget} "
                                 "is already filled")
                for p in tier_plans:
                    p.skip = "someday: budget already filled"
                continue
        # Two phases per tier, so Ollama swaps between the embed model and the judge
        # model once, not once per issue: search + prefilter everything, then judge.
        staged: list[IssueReport] = []
        for plan in tier_plans:
            rep, papers = _search_issue(conn, plan, arxiv, s, now, res, write, arxiv_state)
            res.reports.append(rep)
            if not papers:
                continue
            t0 = clock()
            rep.kept, rep.scored = prefilter(plan, papers, emb, s)
            res.t_embed += clock() - t0
            staged.append(rep)
        for rep in staged:
            plan = rep.plan
            for sc in rep.kept:
                v = cached_verdict(conn, sc.paper.id, plan.number)
                if v is not None:
                    res.cached_verdicts += 1
                elif judge_down or res.judge_calls >= s.max_judge_per_run:
                    continue
                else:
                    t0 = clock()
                    try:
                        v = judge(ollama, plan, sc.paper, s, sc.similarity)
                        if v.accepted:
                            # Contrastive second look: the paper must fit this issue
                            # best among all researched issues.
                            best = confirm(ollama, researched, sc.paper, s)
                            res.judge_calls += 1
                            if best != plan.number:
                                v.verdict = "not_relevant"
                                v.reason = ("confirm: fits no issue clearly" if best == 0
                                            else f"confirm: fits #{best} better")
                    except OllamaError as e:
                        judge_down = True
                        res.errors.append(f"judge: model unavailable: {e}")
                        continue
                    finally:
                        res.t_judge += clock() - t0
                    res.judge_calls += 1
                    if write:
                        store_verdict(conn, sc.paper.id, plan.number, v)
                rep.judged.append(Judged(sc, v))
                if v.accepted:
                    accepted.append((tier, plan, Judged(sc, v)))
    if emb.error and emb.available is False and ollama is not None:
        res.notes.append(f"embeddings unavailable ({emb.error}); keyword overlap used")
    if res.judge_calls >= s.max_judge_per_run:
        res.notes.append(f"judge limit reached ({s.max_judge_per_run} calls); "
                         "the rest is judged next run")

    # Rank and pick.
    accepted.sort(key=lambda t: (t[0], CONF_RANK.get(t[2].verdict.confidence or "", 9),
                                 -t[2].scored.similarity, -(t[2].scored.paper.year or 0),
                                 t[1].number))
    picked: list[PlannedCard] = []
    used_papers: set[str] = set()
    # One active paper card per issue: a second paper waits until the first is handled.
    used_issues: set[int] = {
        int(r[0][1:]) for r in conn.execute(
            "SELECT source_ref FROM cards WHERE type = 'research' AND status = 'active' "
            "AND source_ref LIKE '#%' AND paper_id NOT LIKE 'release:%'")
        if r[0][1:].isdigit()}
    for _tier, plan, j in accepted:
        if len(picked) >= budget:
            break
        pid = j.scored.paper.id
        if pid in used_papers or plan.number in used_issues or paper_carded(conn, pid):
            continue
        if plan.number not in open_numbers:
            continue
        picked.append(paper_card(s, repo, plan, j))
        used_papers.add(pid)
        used_issues.add(plan.number)

    for card, lib in release_cards:
        res.cards.append(card)
        if write:
            card.card_id, created = _save(conn, card)
            if created:
                # A newer tag supersedes this library's older release cards.
                old = [r["fingerprint"] for r in conn.execute(
                    "SELECT fingerprint FROM cards WHERE type = 'research' AND status = 'active' "
                    "AND paper_id LIKE ? AND id != ?", (f"release:{lib}@%", card.card_id))]
                res.resolved += auto_resolve(conn, old)
            else:
                card.card_id = None
    for card in picked:
        res.cards.append(card)
        if write:
            card_id, created = _save(conn, card)
            card.card_id = card_id if created else None
    res.duration = clock() - t_start
    return res


def _save(conn: sqlite3.Connection, c: PlannedCard) -> tuple[int, bool]:
    return upsert_card(conn, type_=CARD_TYPE, title=c.title, why=c.why, source_ref=c.source_ref,
                       source_url=c.source_url, claude_prompt=c.claude_prompt,
                       paper_id=c.paper_id)


# ---------------------------------------------------------------- rendering


def _clip(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def render_result(res: ResearchResult, s: ResearchSettings, verbose: bool = False,
                  show_scored: int = 8) -> str:
    out: list[str] = []
    if verbose:
        out.append(f"settings: {s.source}; threshold {s.min_similarity:.2f}, top {s.top_k}, "
                   f"max {s.max_cards_per_run}/run, {s.max_active} active, "
                   f"embed {s.embed_model}")
        skipped = res.skipped
        if skipped:
            out.append(f"\nskipped issues ({len(skipped)}):")
            out += [f"  #{p.number} {_clip(p.title, 50)}: {p.skip}" for p in skipped]
        for rep in res.reports:
            p = rep.plan
            tag = " [someday]" if p.someday else ""
            out.append(f"\n#{p.number} {p.title}{tag}")
            out += [f"  note: {n}" for n in p.notes]
            for q in p.queries:
                out.append(f"  query: {q.label()}")
            out.append(f"  arXiv: {rep.search}")
            if not rep.scored:
                continue
            method = rep.scored[0].method
            out.append(f"  {rep.candidates} candidates by {method} similarity "
                       f"(kept {len(rep.kept)}):")
            verdicts = {j.scored.paper.id: j.verdict for j in rep.judged}
            for sc in rep.scored[:max(show_scored, len(rep.kept))]:
                v = verdicts.get(sc.paper.id)
                kept = sc in rep.kept
                if v is not None:
                    mark = f"-> {v.verdict}" + (f" ({v.confidence})" if v.confidence else "") \
                        + (" [cached]" if v.cached else "")
                    detail = v.why if v.accepted else v.reason
                elif kept:
                    mark, detail = "-> not judged", ""
                else:
                    mark, detail = "", ""
                out.append(f"    {sc.similarity:.3f} {sc.paper.id:<12} {sc.paper.year or '????'} "
                           f"{_clip(sc.paper.title, 70)} {mark}".rstrip())
                if detail:
                    out.append(f"           {_clip(detail, 150)}")
    if verbose or res.cards:
        head = "would create" if res.dry_run else "cards"
        out.append(f"\n{head} ({len(res.cards)}):" if res.cards else f"\n{head}: none")
        for c in res.cards:
            state = "" if res.dry_run else (" (new)" if c.card_id is not None else " (exists)")
            out.append(f"  {c.title}{state}")
            out.append(f"    {c.why}")
            out.append(f"    {c.source_url}  [{c.detail}]")
    notes = res.notes + ([n for p in res.plans for n in p.notes] if not verbose else [])
    if notes:
        out.append("")
        out += [f"note: {n}" for n in notes]
    out += [f"error: {e}" for e in res.errors]
    out.append(("dry run: " if res.dry_run else "research: ") + res.summary())
    return "\n".join(out).lstrip("\n") + "\n"
