"""Phase 6: the research loop. No network: arXiv, GitHub and Ollama are fakes."""
from __future__ import annotations

import os
import signal
import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

from jarvis import cards as cardlib
from jarvis import db
from jarvis.config import Config
from jarvis.daemon import Daemon, research_interval_from_env, tick
from jarvis.facts import compute_facts, research_fact
from jarvis.github import FetchResult
from jarvis.lock import refresh_running
from jarvis.ollama import OllamaError
from jarvis.research.queries import (
    IssuePlan,
    plan_issues,
    rule_skip,
    title_query,
    validate_query,
)
from jarvis.research.relevance import Embedder, prefilter, validate_judgment
from jarvis.research.runner import render_result, resolve_closed, run_research, short_title
from jarvis.research.settings import ResearchSettings, load_settings, parse_settings
from jarvis.research.sources import (
    Paper,
    Query,
    SourceError,
    is_newer,
    newest_release,
    newest_tag,
    parse_atom,
    search_query,
)

NOW = datetime(2026, 10, 8, 16, 0, tzinfo=UTC)

ATOM = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"
      xmlns:arxiv="http://arxiv.org/schemas/atom" xmlns="http://www.w3.org/2005/Atom">
  <id>https://arxiv.org/api/x</id>
  <title>arXiv Query</title>
  <opensearch:totalResults>2</opensearch:totalResults>
  <entry>
    <id>http://arxiv.org/abs/2603.09783v2</id>
    <title>Lightweight 3D LiDAR-Based UAV Tracking:
      An Adaptive Extended Kalman Filtering Approach</title>
    <summary>  Accurate relative positioning is crucial.
      We use an adaptive EKF with 12 states.  </summary>
    <published>2026-03-10T15:19:34Z</published>
    <category term="cs.RO" scheme="http://arxiv.org/schemas/atom"/>
    <category term="eess.SY" scheme="http://arxiv.org/schemas/atom"/>
    <author><name>Nivand Khosravi</name></author>
    <author><name>Meysam Basiri</name></author>
    <author><name>Rodrigo Ventura</name></author>
    <author><name>Fourth Person</name></author>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/math/0101001v1</id>
    <title>Old style id</title>
    <summary>Text.</summary>
    <published>2024-01-02T00:00:00Z</published>
    <author><name>A B</name></author>
  </entry>
</feed>"""

ERROR_FEED = """<?xml version='1.0'?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><id>http://arxiv.org/api/errors#incorrect_id_format</id><title>Error</title>
<summary>incorrect id format</summary></entry></feed>"""


# ---------------------------------------------------------------- fakes


TITLES: dict[str, str] = {}  # paper id -> title, so the fake judge can tell papers apart


def paper(pid, title, abstract="", year=2025):
    TITLES[pid] = title
    return Paper(pid, title, abstract or f"{title}. A method.", ("A One", "B Two"),
                 f"{year}-05-01", f"https://arxiv.org/abs/{pid}", ("cs.RO",))


class FakeArxiv:
    """Returns papers for queries whose text contains a key (case-insensitive)."""

    def __init__(self, by_key: dict[str, list[Paper]] | None = None):
        self.by_key = by_key or {}
        self.requests = 0
        self.error: Exception | None = None

    def search(self, q, today, years=3, max_results=25):
        self.requests += 1
        if self.error:
            raise self.error
        qs = q if isinstance(q, list) else [q]
        out = []
        for key, papers in self.by_key.items():
            if any(key.lower() in x.text.lower() for x in qs):
                out += papers
        return out


# Topic axes for fake embeddings: a text's vector counts topic words.
AXES = ("kalman", "ransac", "pid", "uav", "classifier")


def vec(text: str) -> list[float]:
    t = text.lower()
    v = [float(t.count(a)) for a in AXES]
    return v if any(v) else [0.0, 0.0, 0.0, 0.0, 0.0001]


class FakeOllama:
    """Triage: worthy unless the title is in ``unworthy``. Judge: per paper id."""

    model = "fake:1b"

    def __init__(self, verdicts=None, unworthy=(), embed_down=False, down=False):
        self.verdicts = verdicts or {}
        self.unworthy = set(unworthy)
        self.embed_down = embed_down
        self.down = down
        self.judge_calls = 0
        self.confirm_calls = 0
        self.triage_calls = 0
        self.embed_calls = 0

    def chat_json(self, system, user, schema):
        if self.down:
            raise OllamaError("cannot reach Ollama: refused")
        props = schema["properties"]
        if "research_worthy" in props:
            self.triage_calls += 1
            title = user.split("\n", 1)[0].split(": ", 1)[1]
            if title in self.unworthy:
                return {"research_worthy": False, "reason": "A wiring chore.", "queries": []}
            return {"research_worthy": True, "reason": "Algorithmic.", "queries": []}
        line = next(x for x in user.splitlines() if x.startswith("PAPER"))
        if "issue" in props:  # confirm: the first listed issue sharing a topic word
            self.confirm_calls += 1
            title = line.split(": ", 1)[1].lower()
            for x in user.splitlines():
                if x.startswith("#") and any(a in x.lower() and a in title for a in AXES):
                    return {"issue": int(x[1:].split(":")[0]), "reason": "fits"}
            return {"issue": 0, "reason": "none"}
        self.judge_calls += 1
        for pid, reply in self.verdicts.items():
            if line.endswith(": " + TITLES.get(pid, pid)):
                return reply
        return {"relevant": False, "applies_to": "", "why": "Different problem.",
                "confidence": "low"}

    def embed(self, texts, model=None):
        self.embed_calls += 1
        if self.embed_down:
            raise OllamaError("model not found")
        return [vec(t) for t in texts]


CHECKS_OK = {"paper_setting": "2D laser scanner", "technique": "adaptive Kalman filter",
             "same_problem": True, "comparable_data": True, "transfers": True,
             "new_to_issue": True}
GOOD = {**CHECKS_OK, "relevant": True, "applies_to": "x/y velocity estimate",
        "why": "Its adaptive noise tuning may help the Kalman filter cope with missed "
               "detections.", "confidence": "high"}


class FakeGH:
    def __init__(self):
        self.responses: dict[str, list[FetchResult]] = {}
        self.calls: list[tuple[str, bool]] = []

    def queue(self, path, *results):
        self.responses.setdefault(path, []).extend(results)

    def _next(self, path):
        q = self.responses.get(path)
        if not q:
            return FetchResult(False, 304, None)
        return q.pop(0) if len(q) > 1 else q[0]

    def get(self, path, params=None):
        self.calls.append((path, True))
        return self._next(path)

    def get_unconditional(self, path, params=None):
        self.calls.append((path, False))
        r = self._next(path)
        return r if r.status != 304 else FetchResult(True, 200, [])


def add_issue(conn, number, title, state="open", labels=(), body=""):
    db.upsert_issue(conn, {"number": number, "title": title, "state": state,
                           "labels": list(labels), "body": body,
                           "html_url": f"https://github.com/me/robot/issues/{number}",
                           "created_at": "2026-09-01T00:00:00Z",
                           "updated_at": "2026-10-01T00:00:00Z"})


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    add_issue(c, 10, "Kalman filter on x/y", body="Smooth the target position.")
    add_issue(c, 24, "RANSAC wall fitting", body="Fit walls as lines.")
    add_issue(c, 12, "servo_write returns a status")
    add_issue(c, 17, "Demo video and final README")
    add_issue(c, 18, "Migrate to FreeRTOS")
    add_issue(c, 20, "TinyML person classifier vs rule-based filter", labels=["someday"])
    add_issue(c, 5, "Old closed thing", state="closed")
    yield c
    c.close()


SETTINGS = ResearchSettings(min_similarity=0.5, libraries={})


def run(conn, ollama, arxiv, s=SETTINGS, **kw):
    return run_research(conn, s, repo="me/robot", ollama=ollama, arxiv=arxiv, now=NOW, **kw)


KALMAN = paper("2603.09783", "Adaptive Kalman filter for sparse LiDAR target tracking",
               "We track a target with an adaptive Kalman filter. Missed detections handled.")
UAV = paper("2401.00001", "UAV swarm with uav uav lidar")
RANSAC = paper("2502.11111", "RANSAC line extraction from 2D laser scans",
               "RANSAC fits lines to ransac scans.")


# ---------------------------------------------------------------- sources


def test_parse_atom_fixture():
    papers = parse_atom(ATOM)
    assert [p.id for p in papers] == ["2603.09783", "math/0101001"]
    p = papers[0]
    assert p.title == ("Lightweight 3D LiDAR-Based UAV Tracking: An Adaptive Extended "
                       "Kalman Filtering Approach")
    assert p.abstract == "Accurate relative positioning is crucial. We use an adaptive EKF with 12 states."
    assert p.year == 2026 and p.published == "2026-03-10"
    assert p.url == "https://arxiv.org/abs/2603.09783"
    assert p.categories == ("cs.RO", "eess.SY")
    assert p.authors_short() == "Nivand Khosravi, Meysam Basiri, Rodrigo Ventura et al."
    with pytest.raises(SourceError):
        parse_atom(ERROR_FEED)
    with pytest.raises(SourceError):
        parse_atom("<not xml")


def test_search_query_is_narrow_and_recent():
    q = Query("Kalman filter", ("LiDAR", "laser scanner"), ("cs.RO", "eess.SP"))
    text = search_query(q, date(2026, 10, 8), 3)
    assert text == ('all:kalman AND all:filter AND (all:lidar OR all:"laser scanner") AND '
                    "(cat:cs.RO OR cat:eess.SP) AND submittedDate:[202310090000 TO 202610082359]")
    assert 'all:"pan-tilt"' in search_query(Query("pan-tilt PID control"), date(2026, 1, 1), 1)


def test_release_picking():
    rels = [{"tag_name": "V11.3.0", "published_at": "2026-03-30T00:00:00Z"},
            {"tag_name": "V11.4.0-rc1", "prerelease": True, "published_at": "2026-09-01T00:00:00Z"},
            {"tag_name": "V11.3.1", "published_at": "2026-08-21T00:00:00Z", "body": "notes",
             "html_url": "https://x/rel"}]
    r = newest_release("FreeRTOS/FreeRTOS-Kernel", rels)
    assert (r.tag, r.url, r.notes, r.published) == ("V11.3.1", "https://x/rel", "notes",
                                                     "2026-08-21")
    tags = [{"name": "V202110.00-SMP"}, {"name": "v1.28.3"}, {"name": "v1.28.10"},
            {"name": "v1.9.0"}]
    t = newest_tag("STMicroelectronics/STM32CubeF4", tags)
    assert t.tag == "v1.28.10" and t.source == "tags"
    assert t.url == "https://github.com/STMicroelectronics/STM32CubeF4/releases/tag/v1.28.10"
    assert is_newer("v1.28.10", "v1.28.3") and not is_newer("v1.28.3", "v1.28.3")
    assert not is_newer("v1.27.0", "v1.28.3")


# ---------------------------------------------------------------- settings


def test_settings_defaults_and_file(tmp_path):
    s, note = load_settings(tmp_path / "missing.yaml")
    assert note is None and s.max_cards_per_run == 3 and s.max_active == 8
    assert s.libraries == {"STMicroelectronics/STM32CubeF4": None, "FreeRTOS/FreeRTOS-Kernel": 18}
    f = tmp_path / "research.yaml"
    f.write_text("libraries:\n  FreeRTOS/FreeRTOS-Kernel: '#18'\nqueries:\n  10: [Kalman "
                 "tracking 2D LiDAR]\nskip_issues: [24]\nmax_cards_per_run: 2\n")
    s, note = load_settings(f)
    assert note is None and s.libraries == {"FreeRTOS/FreeRTOS-Kernel": 18}
    assert s.queries == {10: ("Kalman tracking 2D LiDAR",)} and s.skip_issues == {24}
    assert s.max_cards_per_run == 2 and s.source == str(f)
    f.write_text("max_active: -1\n")
    s, note = load_settings(f)
    assert "invalid" in note and s.max_active == 8
    with pytest.raises(ValueError, match="unknown keys"):
        parse_settings({"bogus": 1})


# ---------------------------------------------------------------- queries


def plan(number, title, labels=(), body=""):
    return IssuePlan(number, title, body, tuple(labels), None)


def test_title_queries_are_narrow():
    s = ResearchSettings()
    q = title_query(plan(10, "Kalman filter on x/y"), s)
    assert q.text == "Kalman filter" and q.domain == s.domain_terms
    q = title_query(plan(24, "RANSAC wall fitting"), s)
    assert q.text == "RANSAC wall fitting" and "cs.RO" in q.categories
    q = title_query(plan(7, "Create track.c with a person-size filter"), s)
    assert q.text == "person-size filter"
    q = title_query(plan(3, "LiDAR frame dropout"), s)
    assert q.domain == ()  # the title already names the sensor
    assert "cs.LG" in title_query(plan(20, "TinyML person classifier"), s).categories


def test_skip_filter_is_explainable():
    s = ResearchSettings()
    assert rule_skip(plan(17, "Demo video and final README"), s) == \
        "not research: documentation or demo"
    assert rule_skip(plan(25, "Full code review before project 2"), s) == \
        "not research: code housekeeping"
    assert rule_skip(plan(12, "servo_write returns a status"), s) == \
        "not research: code housekeeping"
    assert rule_skip(plan(18, "Migrate to FreeRTOS"), s) == \
        "covered by FreeRTOS/FreeRTOS-Kernel releases"
    assert rule_skip(plan(10, "Kalman filter on x/y"), s) is None
    s2 = ResearchSettings(skip_issues=frozenset({10}), include_issues=frozenset({17}))
    assert rule_skip(plan(10, "Kalman filter on x/y"), s2) == "skipped in research.yaml"
    assert rule_skip(plan(17, "Demo video and final README"), s2) is None


def test_validate_model_queries():
    assert validate_query("Kalman filter 2D LiDAR tracking") == "Kalman filter 2D LiDAR tracking"
    assert validate_query('"RANSAC" line fitting.') == "RANSAC line fitting"
    assert validate_query("Kalman") is None
    assert validate_query("one two three four five six seven eight nine") is None
    assert validate_query("fix #12 servo") is None
    assert validate_query("query: (stuff)") is None
    assert validate_query(42) is None


def test_plan_issues_model_triage_cached_and_someday_last(conn):
    ol = FakeOllama(unworthy={"RANSAC wall fitting"})
    plans = plan_issues(conn, SETTINGS, ol)
    by = {p.number: p for p in plans}
    assert by[24].skip.startswith("not research (model): A wiring chore")
    assert by[12].skip and by[17].skip
    assert by[10].skip is None and by[10].queries[0].text == "Kalman filter"
    assert [p.number for p in plans][-1] == 20  # someday last
    calls = ol.triage_calls
    plan_issues(conn, SETTINGS, ol)
    assert ol.triage_calls == calls  # cached per issue text
    # model down: deterministic queries only, with a note, nothing cached
    c2 = db.connect(":memory:")
    add_issue(c2, 10, "Kalman filter on x/y")
    p = plan_issues(c2, SETTINGS, FakeOllama(down=True))[0]
    assert p.skip is None and p.queries and "model unavailable" in p.notes[0]
    assert c2.execute("SELECT COUNT(*) FROM research_queries").fetchone()[0] == 0


# ---------------------------------------------------------------- relevance


def test_embedding_prefilter_threshold_and_top_k(conn):
    issue = plan(10, "Kalman filter on x/y", body="kalman")
    papers = [KALMAN, UAV, paper("2501.2", "Kalman kalman uav mix")]
    emb = Embedder(conn, FakeOllama(), "fake-embed")
    kept, scored = prefilter(issue, papers, emb, ResearchSettings(min_similarity=0.5, top_k=5))
    assert [s.paper.id for s in kept] == ["2603.09783", "2501.2"]
    assert scored[-1].paper.id == UAV.id and scored[-1].similarity < 0.5
    kept, _ = prefilter(issue, papers, emb, ResearchSettings(min_similarity=0.5, top_k=1))
    assert [s.paper.id for s in kept] == ["2603.09783"]
    # embed model unavailable -> keyword overlap
    emb2 = Embedder(conn, FakeOllama(embed_down=True), "fake-embed")
    kept, scored = prefilter(issue, papers, emb2, ResearchSettings(min_similarity=0.5))
    assert scored[0].method == "keywords" and KALMAN.id in [s.paper.id for s in kept]
    assert not emb2.available


@pytest.mark.parametrize("why,verdict,reason", [
    ("Its adaptive noise tuning may help the filter cope with missed detections.",
     "relevant", ""),
    ("Its adaptive noise tuning helps the filter cope with missed detections.",
     "invalid", "not hedged"),
    ("Its 7-state model may cut the error by 40 percent.", "invalid", "number not in"),
    ("Its 12 states may be more than needed here.", "relevant", ""),  # 12 is in the abstract
    ("This novel filter may help a lot.", "invalid", "hype word"),
    ("This may help. It could also help. And it might help again.", "invalid",
     "more than 2 sentences"),
    ("It may help #10 directly.", "invalid", "issue number"),
    ("You should use its tuning, which may help.", "invalid", "banned wording"),
])
def test_judge_validation(why, verdict, reason):
    issue = plan(10, "Kalman filter on x/y")
    p = paper("2601.00077", "Adaptive EKF", "An adaptive EKF with 12 states.")
    s = ResearchSettings()
    v = validate_judgment({**CHECKS_OK, "relevant": True, "applies_to": "x/y filter",
                           "why": why, "confidence": "medium"}, issue, p, s, 0.7)
    assert v.verdict == verdict
    assert reason in v.reason


def test_judge_low_confidence_and_not_relevant_are_rejected():
    issue, p, s = plan(10, "Kalman"), paper("1", "T"), ResearchSettings()
    low = validate_judgment({**GOOD, "confidence": "low"}, issue, p, s, 0.7)
    assert low.verdict == "low_confidence" and not low.accepted
    no = validate_judgment({**GOOD, "relevant": False}, issue, p, s, 0.7)
    assert no.verdict == "not_relevant"
    assert validate_judgment("junk", issue, p, s, 0.7).verdict == "invalid"
    # the checklist decides: one failed check rejects even when "relevant" is true
    v = validate_judgment({**GOOD, "comparable_data": False, "paper_setting": "thermal camera"},
                          issue, p, s, 0.7)
    assert v.verdict == "not_relevant"
    assert v.reason.startswith("fails comparable_data (thermal camera)")


# ---------------------------------------------------------------- runs


def test_run_creates_one_good_card_and_second_run_judges_nothing(conn):
    ol = FakeOllama(verdicts={KALMAN.id: GOOD})
    ax = FakeArxiv({"kalman": [KALMAN, UAV]})
    r = run(conn, ol, ax)
    assert r.new == 1 and len(r.cards) == 1
    card = cardlib.list_cards(conn, "active")[0]
    assert (card.type, card.source_ref, card.paper_id) == ("research", "#10", KALMAN.id)
    assert card.source_url == "https://arxiv.org/abs/2603.09783"
    assert card.title == KALMAN.title
    assert card.why.startswith("Related to #10 Kalman filter on x/y: Its adaptive noise")
    pr = card.claude_prompt
    for part in ("STM32F446RE", "#10 \"Kalman filter on x/y\"", "Smooth the target position.",
                 KALMAN.title, "A One, B Two", "2025", KALMAN.abstract, KALMAN.url,
                 "Would this approach help with #10 in my setup (STM32F446RE, single-beam "
                 "TF-Luna on a pan-tilt, no RTOS)? What would I need to change, and what's "
                 "the cheapest experiment to test it?"):
        assert part in pr
    seen = conn.execute("SELECT verdict FROM research_seen WHERE paper_id = ?",
                        (KALMAN.id,)).fetchone()
    assert seen["verdict"] == "relevant"
    calls = ol.judge_calls
    r2 = run(conn, ol, ax)
    assert ol.judge_calls == calls and r2.judge_calls == 0 and r2.cached_verdicts >= 1
    assert r2.new == 0 and r2.cards == []
    assert "0 new" in r2.summary()


def test_rejected_verdicts_are_cached_and_model_errors_are_not(conn):
    ax = FakeArxiv({"kalman": [KALMAN]})
    down = FakeOllama(verdicts={KALMAN.id: GOOD})
    down.chat_json_orig = down.chat_json

    def flaky(system, user, schema):
        if "relevant" in schema["properties"]:
            raise OllamaError("timeout")
        return down.chat_json_orig(system, user, schema)

    down.chat_json = flaky
    r = run(conn, down, ax)
    assert r.new == 0 and any("judge: model unavailable" in e for e in r.errors)
    assert conn.execute("SELECT COUNT(*) FROM research_seen").fetchone()[0] == 0
    ol = FakeOllama(verdicts={KALMAN.id: {**GOOD, "relevant": False}})
    run(conn, ol, ax)
    assert ol.judge_calls == 1
    run(conn, ol, ax)
    assert ol.judge_calls == 1  # the rejection is cached too


def test_paper_is_never_carded_twice_across_issues(conn):
    shared = paper("2502.22222", "Kalman RANSAC kalman ransac combined tracker")
    ol = FakeOllama(verdicts={shared.id: GOOD})
    ax = FakeArxiv({"kalman": [shared], "ransac": [shared]})
    r = run(conn, ol, ax)
    assert r.new == 1
    assert conn.execute("SELECT COUNT(*) FROM cards WHERE paper_id = ?",
                        (shared.id,)).fetchone()[0] == 1
    cardlib.mark_done(conn, cardlib.list_cards(conn, "active")[0].id)
    r2 = run(conn, ol, ax)  # still relevant for the other issue, but already carded once
    assert r2.new == 0
    assert conn.execute("SELECT COUNT(*) FROM cards WHERE paper_id = ?",
                        (shared.id,)).fetchone()[0] == 1


def test_caps_per_run_and_active(conn):
    k2 = paper("2504.4", "Kalman kalman smoother")
    ol = FakeOllama(verdicts={KALMAN.id: GOOD, k2.id: GOOD, RANSAC.id: {
        **GOOD, "why": "Its line fitting may help find walls.", "confidence": "medium"}})
    ax = FakeArxiv({"kalman": [KALMAN, k2], "ransac": [RANSAC]})
    s = ResearchSettings(min_similarity=0.5, libraries={}, max_cards_per_run=1)
    r = run(conn, ol, ax, s)
    assert r.new == 1 and r.cards[0].source_ref == "#10"  # high confidence ranks first
    # one active paper card per issue: the second Kalman paper waits, the RANSAC one comes
    s2 = ResearchSettings(min_similarity=0.5, libraries={}, max_cards_per_run=3)
    r2 = run(conn, ol, ax, s2)
    assert [c.source_ref for c in r2.cards] == ["#24"]
    s3 = ResearchSettings(min_similarity=0.5, libraries={}, max_active=2)
    requests = ax.requests
    r3 = run(conn, ol, ax, s3)
    assert r3.new == 0 and ax.requests == requests  # no search when max_active is reached
    assert any("max_active 2" in n for n in r3.notes)


def test_someday_only_searched_when_budget_remains(conn):
    clf = paper("2505.5", "TinyML classifier classifier on a microcontroller")
    ol = FakeOllama(verdicts={KALMAN.id: GOOD, clf.id: GOOD})
    ax = FakeArxiv({"kalman": [KALMAN], "classifier": [clf]})
    s = ResearchSettings(min_similarity=0.5, libraries={}, max_cards_per_run=1)
    r = run(conn, ol, ax, s)
    assert [c.source_ref for c in r.cards] == ["#10"]
    assert any(p.number == 20 and "budget" in (p.skip or "") for p in r.plans)
    r2 = run(conn, ol, ax, s)  # #10 is carded now: room for the someday issue
    assert [c.source_ref for c in r2.cards] == ["#20"]


def test_dry_run_writes_nothing(conn):
    ol = FakeOllama(verdicts={KALMAN.id: GOOD})
    gh = FakeGH()
    gh.queue("/repos/FreeRTOS/FreeRTOS-Kernel/releases",
             FetchResult(True, 200, [{"tag_name": "V11.3.1", "published_at": "2026-08-21"}]))
    s = ResearchSettings(min_similarity=0.5, libraries={"FreeRTOS/FreeRTOS-Kernel": 18})
    before = conn.total_changes
    r = run(conn, ol, FakeArxiv({"kalman": [KALMAN]}), s, gh=gh, dry_run=True)
    assert len(r.cards) == 1 and r.new == 0
    assert conn.total_changes == before
    for table in ("cards", "research_seen", "research_papers", "research_queries",
                  "research_releases", "etags"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    out = render_result(r, s, verbose=True)
    assert "would create (1):" in out and "skipped issues" in out and "1.000" in out
    assert out.strip().endswith(")") and "dry run:" in out


def test_release_baseline_then_new_tag(conn):
    gh = FakeGH()
    path = "/repos/FreeRTOS/FreeRTOS-Kernel/releases"
    rel = lambda tag, day: {"tag_name": tag, "published_at": f"2026-{day}T00:00:00Z",  # noqa: E731
                             "html_url": f"https://github.com/FreeRTOS/FreeRTOS-Kernel/releases/tag/{tag}",
                             "body": "Kernel fixes."}
    gh.queue(path, FetchResult(True, 200, [rel("V11.3.1", "08-21"), rel("V11.3.0", "03-30")]))
    s = ResearchSettings(min_similarity=0.5, libraries={"FreeRTOS/FreeRTOS-Kernel": 18})
    ol = FakeOllama()
    r = run(conn, ol, FakeArxiv(), s, gh=gh)
    assert r.new == 0 and any("baseline V11.3.1" in n for n in r.notes)
    assert gh.calls[-1] == (path, False)  # first sight: unconditional
    # unchanged (304): nothing
    gh.responses[path] = [FetchResult(False, 304, None)]
    r = run(conn, ol, FakeArxiv(), s, gh=gh)
    assert r.new == 0 and gh.calls[-1] == (path, True)
    # a new tag: one card for #18
    gh.responses[path] = [FetchResult(True, 200, [rel("V11.4.0", "10-01"), rel("V11.3.1", "08-21")])]
    r = run(conn, ol, FakeArxiv(), s, gh=gh)
    assert r.new == 1
    card = cardlib.list_cards(conn, "active")[0]
    assert card.title == "FreeRTOS-Kernel V11.4.0 released" and card.source_ref == "#18"
    assert card.paper_id == "release:FreeRTOS/FreeRTOS-Kernel@V11.4.0"
    assert card.why.startswith("Related to #18 Migrate to FreeRTOS:")
    assert "compare/V11.3.1...V11.4.0" in card.claude_prompt
    assert "Kernel fixes." in card.claude_prompt
    # same again: no second card
    r = run(conn, ol, FakeArxiv(), s, gh=gh)
    assert r.new == 0
    # a later tag supersedes the older release card
    gh.responses[path] = [FetchResult(True, 200, [rel("V11.5.0", "11-01")])]
    r = run(conn, ol, FakeArxiv(), s, gh=gh)
    assert r.new == 1 and r.resolved == 1
    assert [c.paper_id for c in cardlib.list_cards(conn, "active")] == \
        ["release:FreeRTOS/FreeRTOS-Kernel@V11.5.0"]


def test_release_tags_fallback_and_closed_issue(conn):
    gh = FakeGH()
    lib = "STMicroelectronics/STM32CubeF4"
    gh.queue(f"/repos/{lib}/releases", FetchResult(True, 200, []))
    gh.queue(f"/repos/{lib}/tags", FetchResult(True, 200, [{"name": "v1.28.3"}]))
    s = ResearchSettings(min_similarity=0.5, libraries={lib: 5})  # #5 is closed
    run(conn, FakeOllama(), FakeArxiv(), s, gh=gh)
    row = conn.execute("SELECT * FROM research_releases").fetchone()
    assert (row["tag"], row["source"]) == ("v1.28.3", "tags")
    gh.responses[f"/repos/{lib}/tags"] = [FetchResult(True, 200, [{"name": "v1.28.4"},
                                                                 {"name": "v1.28.3"}])]
    r = run(conn, FakeOllama(), FakeArxiv(), s, gh=gh)
    assert r.new == 0 and any("#5 is not open" in n for n in r.notes)
    assert conn.execute("SELECT tag FROM research_releases").fetchone()[0] == "v1.28.4"


def test_short_title_is_deterministic():
    assert short_title("Short title") == "Short title"
    long = ("Lightweight LiDAR-Based Tracking of People in Rooms: An Adaptive Extended "
            "Kalman Filtering Approach for Small Robots")
    assert short_title(long) == "Lightweight LiDAR-Based Tracking of People in Rooms"
    words = " ".join(f"w{i}" for i in range(20))
    assert short_title(words) == " ".join(f"w{i}" for i in range(12)) + " …"
    assert short_title("KLIO: " + words) == " ".join(f"w{i}" for i in range(12)) + " …"


# ---------------------------------------------------------------- auto-resolve


def _research_card(conn, issue_no, pid):
    return cardlib.upsert_card(conn, type_="research", title="P", why="w",
                               source_ref=f"#{issue_no}", source_url="u", claude_prompt="p",
                               paper_id=pid)[0]


def test_resolve_closed_and_research_fact(conn):
    _research_card(conn, 10, "1111.1")
    _research_card(conn, 24, "2222.2")
    f = research_fact(conn)
    assert f.priority == 4 and f.data["issues"] == [10, 24]
    assert f.text == ("2 research cards are active, for #10 Kalman filter on x/y; "
                      "#24 RANSAC wall fitting.")
    assert "research" in {x.id for x in compute_facts(conn, now=NOW)}
    add_issue(conn, 24, "RANSAC wall fitting", state="closed")
    assert resolve_closed(conn) == 1
    assert [c.source_ref for c in cardlib.list_cards(conn, "active")] == ["#10"]
    assert resolve_closed(conn) == 0


def test_fast_tick_auto_resolves_research_cards(tmp_path):
    from test_daemon import FakeGH as TickGH
    from test_daemon import FakeOllama as TickOllama
    from test_daemon import issue as gh_issue

    cfg = Config(repo="me/robot", db_path=tmp_path / "j.db", hardware_path=tmp_path / "no.yaml")
    c = db.connect(cfg.db_path)
    db.upsert_issue(c, {**gh_issue(10, "Kalman filter on x/y"), "labels": []})
    _research_card(c, 10, "1111.1")
    gh = TickGH(c)
    tick(c, cfg, gh=gh, ollama=TickOllama(), now=NOW)
    assert len(cardlib.list_cards(c, "active")) == 1
    gh.queued["issues"] = FetchResult(True, 200, [gh_issue(10, "Kalman filter on x/y",
                                                           state="closed")])
    r = tick(c, cfg, gh=gh, ollama=TickOllama(), now=NOW + timedelta(minutes=3))
    assert r.cards_resolved == 1
    assert cardlib.list_cards(c, "auto_resolved")[0].paper_id == "1111.1"
    c.close()


# ---------------------------------------------------------------- daemon scheduling


class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []
        self.stop_at = None
        self.daemon = None

    def clock(self):
        return self.t

    def wall(self):
        return NOW + timedelta(seconds=self.t)

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s
        if self.stop_at is not None and self.t >= self.stop_at:
            self.daemon.stop.set()


class FakeResearchResult:
    def __init__(self, new=0, errors=()):
        self.new = new
        self.errors = list(errors)
        self.notes = []

    @property
    def error(self):
        return "; ".join(self.errors) or None

    def summary(self):
        return f"{self.new} new"


def research_daemon(tmp_path, fn, stop_at, **kw):
    from test_daemon import FakeGH as TickGH
    from test_daemon import FakeOllama as TickOllama

    cfg = Config(repo="me/robot", db_path=tmp_path / "d.db", hardware_path=tmp_path / "no.yaml")
    c = db.connect(cfg.db_path)
    ck = Clock()
    lines: list[str] = []
    sent: list = []
    d = Daemon(cfg, c, interval=600, gh=TickGH(c), ollama=TickOllama(), out=lines.append,
               clock=ck.clock, sleep=ck.sleep, wall=ck.wall, hw_poll_s=300,
               notify=lambda s, b: sent.append(s), research=fn, research_interval=3600,
               research_delay=120, **kw)
    ck.daemon, ck.stop_at = d, stop_at
    return d, c, ck, lines, sent


def test_daemon_runs_research_on_the_slow_interval(tmp_path):
    times = []

    def fn(now):
        times.append(now)
        if len(times) == 1:
            _research_card(d.conn, 10, "9999.1")  # make "new" cards real for the message
            return FakeResearchResult(new=1)
        return FakeResearchResult(new=0)

    d, c, _ck, lines, sent = research_daemon(tmp_path, fn, stop_at=2 * 3600 + 400)
    db.upsert_issue(c, {"number": 10, "title": "Kalman", "state": "open"})
    assert d.run() == 0
    offsets = [(t - NOW).total_seconds() for t in times]
    assert offsets == [120, 120 + 3600, 120 + 7200]
    assert len(sent) == 1 and sent[0] == "1 new suggestion"  # one notification per run
    row = db.get_daemon_state(c)
    assert row["last_research_at"] == (NOW + timedelta(seconds=7320)).isoformat(timespec="seconds")
    assert row["next_research_at"] == (NOW + timedelta(seconds=7320 + 3600)).isoformat(
        timespec="seconds")
    assert sum(" research: " in ln for ln in lines) == 3
    assert "research every 1h" in lines[0]
    # fast ticks still ran every 600 s in between
    assert len([r for r in d.ticks if r.kind == "github"]) >= 12


def test_daemon_research_respects_stored_schedule_and_retries_errors(tmp_path):
    calls = []

    def fn(now):
        calls.append(now)
        return FakeResearchResult(errors=["arXiv unreachable"])

    d, c, _ck, _lines, _ = research_daemon(tmp_path, fn, stop_at=3600)
    db.update_daemon_state(c, last_research_at=(NOW - timedelta(minutes=30)).isoformat())
    assert d.first_research_delay() == pytest.approx(1800)
    d.research_interval = 7200
    assert d.first_research_delay() == pytest.approx(5400)
    d.run()
    # with a 2 h interval and the last run 30 min ago, it is due in 90 min (> 1 h of fake time)
    assert calls == []
    assert db.get_daemon_state(c)["next_research_at"] == (
        NOW + timedelta(seconds=5400)).isoformat(timespec="seconds")
    # never sooner than the startup delay, even when overdue
    db.update_daemon_state(c, last_research_at=(NOW - timedelta(days=1)).isoformat())
    assert d.first_research_delay() == 120


def test_daemon_research_error_retries_sooner(tmp_path):
    calls = []

    def fn(now):
        calls.append(now)
        return FakeResearchResult(errors=["arXiv unreachable"])

    d, _c, _ck, lines, _ = research_daemon(tmp_path, fn, stop_at=2 * 3600)
    d.run()
    assert [(t - NOW).total_seconds() for t in calls] == [120, 120 + 1800, 120 + 3600,
                                                          120 + 5400]
    assert any("retry in 30m" in ln for ln in lines)


def test_daemon_research_aborts_on_sigterm(tmp_path):
    def fn(now):
        os.kill(os.getpid(), signal.SIGTERM)  # arrives mid-research
        raise AssertionError("not reached")

    d, c, _ck, lines, _ = research_daemon(tmp_path, fn, stop_at=None)
    old = signal.signal(signal.SIGTERM, d.request_stop)
    try:
        assert d.run() == 0
    finally:
        signal.signal(signal.SIGTERM, old)
    assert any("aborted the tick" in ln for ln in lines)
    assert not refresh_running(d.cfg.db_path)
    assert db.get_daemon_state(c)["last_research_at"] is None


def test_research_interval_from_env():
    assert research_interval_from_env({}) == 6 * 3600
    assert research_interval_from_env({"JARVIS_RESEARCH_INTERVAL": "12"}) == 12 * 3600
    assert research_interval_from_env({"JARVIS_RESEARCH_INTERVAL": "0.2"}) == 3600
    assert research_interval_from_env({"JARVIS_RESEARCH_INTERVAL": "x"}) == 6 * 3600


# ---------------------------------------------------------------- schema


def test_old_database_migrates_in_place(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE daemon_state (id INTEGER PRIMARY KEY CHECK (id = 1), pid INTEGER, "
                "started_at TEXT, last_tick_at TEXT, last_change_at TEXT, last_error TEXT, "
                "interval_s INTEGER, next_tick_at TEXT, hardware_hash TEXT, facts_hash TEXT, "
                "model_pending INTEGER DEFAULT 0)")
    raw.execute("INSERT INTO daemon_state (id, pid) VALUES (1, 42)")
    raw.commit()
    raw.close()
    for _ in range(2):  # idempotent
        c = db.connect(path)
        cols = {r[1] for r in c.execute("PRAGMA table_info(daemon_state)")}
        assert {"last_research_at", "next_research_at", "research_summary"} <= cols
        assert db.get_daemon_state(c)["pid"] == 42
        db.update_daemon_state(c, last_research_at="x")
        c.close()


def test_example_file_matches_the_defaults():
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "docs" / "research.example.yaml"
    s, note = load_settings(example)
    d = ResearchSettings()
    assert note is None
    for key in ("project", "setup", "constraints", "libraries", "max_cards_per_run", "max_active",
                "max_judge_per_run", "min_similarity", "top_k", "years", "results_per_query",
                "embed_model", "domain_terms"):
        assert getattr(s, key) == getattr(d, key), key


def test_second_paper_for_an_issue_waits_until_the_first_is_handled(conn):
    k2 = paper("2504.44", "Kalman kalman smoother for range data")
    ol = FakeOllama(verdicts={KALMAN.id: GOOD, k2.id: {**GOOD, "confidence": "medium"}})
    ax = FakeArxiv({"kalman": [KALMAN, k2]})
    assert [c.paper_id for c in run(conn, ol, ax).cards] == [KALMAN.id]
    assert run(conn, ol, ax).cards == []
    cardlib.mark_done(conn, cardlib.list_cards(conn, "active")[0].id)
    assert [c.paper_id for c in run(conn, ol, ax).cards] == [k2.id]
    assert ol.judge_calls == 2  # both judged once, in the first run


def test_arxiv_client_is_polite_and_retries_rate_limits():
    import httpx

    from jarvis.research.sources import ArxivClient

    replies = [httpx.Response(429, text="Rate exceeded.", headers={"Retry-After": "45"}),
               httpx.Response(200, text=ATOM), httpx.Response(200, text=ATOM)]
    seen = []

    def handler(request):
        seen.append(request)
        return replies.pop(0)

    t = [0.0]
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        t[0] += s

    ax = ArxivClient(transport=httpx.MockTransport(handler), sleep=sleep, clock=lambda: t[0])
    q = [Query("Kalman filter", ("LiDAR",), ("cs.RO",)), Query("RANSAC line fitting")]
    papers = ax.search(q, date(2026, 10, 8))
    assert len(papers) == 2 and sleeps == [45.0]  # Retry-After beats the 20 s default
    assert seen[0].headers["User-Agent"].startswith("jarvis-research/")
    sq = seen[0].url.params["search_query"]
    assert sq.startswith("((all:kalman AND all:filter AND all:lidar) OR "
                         "(all:ransac AND all:line AND all:fitting)) AND cat:cs.RO AND ")
    assert seen[0].url.params["sortBy"] == "relevance"
    ax.search(q, date(2026, 10, 8))
    assert sleeps[-1] == pytest.approx(3.0)  # one request per 3 s

    ax2 = ArxivClient(transport=httpx.MockTransport(lambda r: httpx.Response(429, text="x")),
                      sleep=sleep, clock=lambda: t[0])
    with pytest.raises(SourceError, match="429"):
        ax2.search(q, date(2026, 10, 8))
    assert ax2.requests == 3  # first try + 2 retries


def test_arxiv_failures_stop_the_searches_and_answers_are_cached(conn):
    ax = FakeArxiv({"kalman": [KALMAN]})
    ax.error = SourceError("arXiv returned 429 (Rate exceeded.)")
    r = run(conn, FakeOllama(), ax)
    assert ax.requests == 2  # two failures, then arXiv is left alone for this run
    assert any("stopped asking" in e for e in r.errors)
    ax.error = None
    ol = FakeOllama(verdicts={KALMAN.id: GOOD})
    r = run(conn, ol, ax)
    assert r.new == 1
    requests = ax.requests
    run(conn, ol, ax)  # within SEARCH_CACHE_HOURS: no new arXiv request
    assert ax.requests == requests
    run_research(conn, SETTINGS, repo="me/robot", ollama=ol, arxiv=ax,
                 now=NOW + timedelta(hours=21))
    assert ax.requests > requests


def test_confirm_step_drops_a_paper_that_fits_another_issue_or_none(conn):
    shared = paper("2502.33333", "Kalman ransac ransac fusion")  # confirm picks #10 (listed first)
    vague = paper("2502.44444", "Kalman kalman generic estimation")
    ol = FakeOllama(verdicts={shared.id: GOOD, vague.id: GOOD})
    ax = FakeArxiv({"ransac": [shared]})
    r = run(conn, ol, ax)
    assert r.new == 0 and ol.confirm_calls == 1
    row = conn.execute("SELECT verdict, reason FROM research_seen").fetchone()
    assert (row["verdict"], row["reason"]) == ("not_relevant", "confirm: fits #10 better")
    ol.chat_json_judge = ol.chat_json

    def no_fit(system, user, schema):
        if "issue" in schema["properties"]:
            return {"issue": 0, "reason": "none"}
        return ol.chat_json_judge(system, user, schema)

    ol.chat_json = no_fit
    r = run_research(conn, SETTINGS, repo="me/robot", ollama=ol,  # past the search cache
                     arxiv=FakeArxiv({"kalman": [vague]}), now=NOW + timedelta(days=1))
    assert r.new == 0
    assert conn.execute("SELECT reason FROM research_seen WHERE paper_id = ?",
                        (vague.id,)).fetchone()[0] == "confirm: fits no issue clearly"


def test_verdicts_from_an_older_prompt_are_judged_again(conn):
    ol = FakeOllama(verdicts={KALMAN.id: {**GOOD, "relevant": False}})
    ax = FakeArxiv({"kalman": [KALMAN]})
    run(conn, ol, ax)
    assert conn.execute("SELECT model FROM research_seen").fetchone()[0].startswith("fake:1b@p")
    conn.execute("UPDATE research_seen SET model = 'fake:1b@p0'")
    run(conn, ol, ax)
    assert ol.judge_calls == 2
    run(conn, ol, ax)
    assert ol.judge_calls == 2
