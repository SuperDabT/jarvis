"""View models for the brain view: the project graph, the thought stream, the
vitals and the pulse. No Qt here, so it is unit-testable.

Everything comes from data that is already synced; nothing is fetched. Links in
the graph are only drawn where the data says so: an issue's milestone, a commit
message naming "#N", a "may be done" match (``facts.maybe_done``), a card's
check, the parts a check names, a paper judged against an issue.
"""
from __future__ import annotations

import hashlib
import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

GITHUB = "https://github.com"
COMMITS_MAX = 60
PAPERS_MAX = 40
LIVE_HOURS = 48
STREAM_MAX = 50
PULSE_DAYS = 60

KINDS = ("core", "hub", "milestone", "issue", "commit", "card", "check", "part", "paper")
# legend / filter order (core and hubs are always shown)
FILTER_KINDS = ("issue", "commit", "card", "check", "paper")

_ISSUE_REF = re.compile(r"(?<![\w/])#(\d+)\b")


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def ago(when: datetime | None, now: datetime) -> str:
    """"20s ago", "9m ago", "2h ago", "3d ago"."""
    if when is None:
        return ""
    s = max(0, int((now - when).total_seconds()))
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _short(text: str | None, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


# ---------------------------------------------------------------- graph


@dataclass(frozen=True)
class Node:
    id: str
    kind: str          # one of KINDS
    label: str         # drawn next to big nodes ("#7", "Core demo", "02fb2d2")
    title: str         # inspector heading
    detail: str = ""   # inspector body
    url: str | None = None
    when: str | None = None    # ISO time of its latest activity
    live: bool = False         # activity in the last LIVE_HOURS
    status: str | None = None  # open / closed / pass / fail / unknown / active
    card_id: int | None = None


@dataclass(frozen=True)
class Edge:
    a: str
    b: str
    kind: str          # hub / milestone / mentions / maybe / history / card / check / judged
    dashed: bool = False


@dataclass(frozen=True)
class Graph:
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]

    def node(self, node_id: str) -> Node | None:
        return next((n for n in self.nodes if n.id == node_id), None)

    def neighbours(self, node_id: str) -> list[tuple[Edge, Node]]:
        by_id = {n.id: n for n in self.nodes}
        out = []
        for e in self.edges:
            if e.a == node_id and e.b in by_id:
                out.append((e, by_id[e.b]))
            elif e.b == node_id and e.a in by_id:
                out.append((e, by_id[e.a]))
        return out

    def count(self, kind: str) -> int:
        return sum(1 for n in self.nodes if n.kind == kind)


EDGE_LABELS = {
    "hub": "part of", "milestone": "in milestone", "mentions": "commit names the issue",
    "maybe": "may have closed it", "history": "previous commit", "card": "suggestion for",
    "check": "check involves", "judged": "research judged for",
}


def _parts_of(check_id: str) -> list[str]:
    """Part names a check id refers to: "voltage_window:MG90S" -> ["MG90S"],
    "logic_level:NUCLEO-F446RE.PA0->MG90S.SIG" -> both boards, rails -> "5V rail"."""
    kind, _, rest = check_id.partition(":")
    if kind == "logic_level":
        return [p.split(".")[0] for p in rest.split("->") if p]
    if kind == "voltage_window":
        return [rest]
    if kind in ("rail_budget", "common_ground", "planned_parts"):
        return [f"{rest} rail"]
    if kind == "missing":  # missing:rails.5V.max_current_a
        bits = rest.split(".")
        if len(bits) >= 2 and bits[0] == "rails":
            return [f"{bits[1]} rail"]
    return []


def build_graph(conn: sqlite3.Connection, repo: str, now: datetime | None = None) -> Graph:
    now = now or datetime.now(UTC)
    live_after = now - timedelta(hours=LIVE_HOURS)
    nodes: dict[str, Node] = {}
    edges: list[Edge] = []

    def add(n: Node) -> None:
        nodes.setdefault(n.id, n)

    def link(a: str, b: str, kind: str, dashed: bool = False) -> None:
        if a != b:
            edges.append(Edge(a, b, kind, dashed))

    def live(iso: str | None) -> bool:
        dt = _parse(iso)
        return bool(dt and dt >= live_after)

    name = repo.split("/")[-1]
    add(Node("jarvis", "core", "jarvis", name, f"Everything Jarvis knows about {repo}.",
             f"{GITHUB}/{repo}"))

    # milestones
    for m in conn.execute("SELECT * FROM milestones ORDER BY due_on IS NULL, due_on, number"):
        total = (m["open_issues"] or 0) + (m["closed_issues"] or 0)
        due = (m["due_on"] or "")[:10]
        add(Node(f"ms:{m['number']}", "milestone", m["title"], f"Milestone {m['title']}",
                 f"{m['closed_issues'] or 0} of {total} issues closed"
                 + (f" · due {due}" if due else " · no due date"),
                 m["html_url"], m["updated_at"], live(m["updated_at"]), m["state"]))
        link(f"ms:{m['number']}", "jarvis", "hub")

    # issues
    issue_numbers = set()
    for i in conn.execute("SELECT * FROM issues ORDER BY number"):
        n = i["number"]
        issue_numbers.add(n)
        when = i["closed_at"] if i["state"] == "closed" and i["closed_at"] else i["updated_at"]
        add(Node(f"issue:{n}", "issue", f"#{n}", f"#{n} {i['title']}",
                 _short(i["body"], 280) or "No description.", i["html_url"], when,
                 live(when), i["state"]))
        ms = i["milestone_number"]
        if ms is not None and f"ms:{ms}" in nodes:
            link(f"issue:{n}", f"ms:{ms}", "milestone")
        else:
            link(f"issue:{n}", "jarvis", "hub")

    # commits: newest COMMITS_MAX as a history strand from the "commits" hub
    rows = conn.execute("SELECT * FROM commits ORDER BY committed_at DESC LIMIT ?",
                        (COMMITS_MAX,)).fetchall()
    if rows:
        add(Node("hub:commits", "hub", "commits", "Commits",
                 f"The newest {len(rows)} commits, newest closest to the centre."))
        link("hub:commits", "jarvis", "hub")
    prev = "hub:commits"
    short_to_id: dict[str, str] = {}
    for c in rows:
        sha = c["sha"]
        cid = f"commit:{sha[:7]}"
        short_to_id[sha[:7]] = cid
        first = (c["message"] or "").strip().splitlines()[0] if c["message"] else ""
        add(Node(cid, "commit", sha[:7], _short(first, 90) or sha[:7],
                 f"{(c['committed_at'] or '')[:10]} · {c['author'] or 'unknown'}\n\n"
                 + _short(c["message"], 400), c["html_url"], c["committed_at"],
                 live(c["committed_at"])))
        link(cid, prev, "history")
        prev = cid
        for num in {int(x) for x in _ISSUE_REF.findall(c["message"] or "")}:
            if num in issue_numbers:
                link(cid, f"issue:{num}", "mentions")

    # "may be done" matches from the facts
    try:
        from jarvis.facts import compute_facts

        facts = compute_facts(conn, now=now)
    except Exception:  # noqa: BLE001 - links are a bonus, never block the view
        facts = []
    for f in facts:
        if f.kind != "maybe_done":
            continue
        num = f.data.get("issue")
        for ev in f.evidence:
            if ev.startswith("commit:") and ev[7:14] in short_to_id and num in issue_numbers:
                link(short_to_id[ev[7:14]], f"issue:{num}", "maybe", dashed=True)

    # hardware: checks and the parts they name
    checks = conn.execute("SELECT * FROM electrical_findings ORDER BY check_id").fetchall()
    if checks:
        add(Node("hub:hardware", "hub", "hardware", "Hardware",
                 "Electrical checks run on hardware.yaml, and the parts they involve."))
        link("hub:hardware", "jarvis", "hub")
    for ch in checks:
        cid = f"check:{ch['check_id']}"
        add(Node(cid, "check", ch["check_id"].split(":")[0], ch["title"] or ch["check_id"],
                 _short(ch["why"], 320) or ch["check_id"], None, ch["updated_at"],
                 live(ch["updated_at"]) and ch["status"] in ("fail", "warn"), ch["status"]))
        link(cid, "hub:hardware", "hub")
        for part in _parts_of(ch["check_id"]):
            pid = f"part:{part}"
            add(Node(pid, "part", part, part, "A part or rail named in hardware.yaml."))
            link(pid, "hub:hardware", "hub")
            link(cid, pid, "check")

    # cards (active suggestions)
    for c in conn.execute("SELECT * FROM cards WHERE status = 'active' ORDER BY id"):
        kid = f"card:{c['id']}"
        add(Node(kid, "card", "card", c["title"], c["why"] or "", c["source_url"],
                 c["created_at"], live(c["created_at"]), "active", c["id"]))
        target = None
        if c["check_id"] and f"check:{c['check_id']}" in nodes:
            target = f"check:{c['check_id']}"
        else:
            m = _ISSUE_REF.search(c["source_ref"] or "")
            if m and int(m.group(1)) in issue_numbers:
                target = f"issue:{m.group(1)}"
        link(kid, target or "jarvis", "card" if target else "hub")

    # research papers: the ones judged against an issue first, newest first
    judged: dict[str, list[tuple[int, str]]] = {}
    for r in conn.execute("SELECT paper_id, issue_number, verdict FROM research_seen"):
        judged.setdefault(r["paper_id"], []).append((r["issue_number"], r["verdict"] or ""))
    papers = conn.execute("SELECT paper_id, title, abstract, url, published, fetched_at "
                          "FROM research_papers").fetchall()
    papers.sort(key=lambda p: (p["paper_id"] not in judged, _neg_iso(p["fetched_at"]),
                               p["paper_id"]))
    papers = papers[:PAPERS_MAX]
    if papers:
        add(Node("hub:research", "hub", "research", "Research",
                 f"{len(papers)} arXiv papers found while searching for the open issues."))
        link("hub:research", "jarvis", "hub")
    for p in papers:
        pid = f"paper:{p['paper_id']}"
        verdicts = judged.get(p["paper_id"], [])
        relevant = any(v not in ("not_relevant", "irrelevant", "no") for _, v in verdicts)
        add(Node(pid, "paper", "paper", _short(p["title"], 120),
                 _short(p["abstract"], 360), p["url"], p["fetched_at"],
                 live(p["fetched_at"]) and relevant,
                 "relevant" if relevant else ("judged" if verdicts else None)))
        link(pid, "hub:research", "hub")
        for num, verdict in verdicts:
            if num in issue_numbers:
                link(pid, f"issue:{num}", "judged",
                     dashed=verdict in ("not_relevant", "irrelevant", "no"))

    return Graph(tuple(nodes.values()), tuple(edges))


def _neg_iso(iso: str | None) -> float:
    dt = _parse(iso)
    return -dt.timestamp() if dt else 0.0


# ---------------------------------------------------------------- layout


def _hash01(s: str, salt: str = "") -> float:
    """A stable pseudo-random number in [0, 1) for ``s`` (same on every run)."""
    h = hashlib.blake2s((salt + s).encode(), digest_size=8).digest()
    return int.from_bytes(h, "big") / 2**64


GOLDEN = math.pi * (3 - math.sqrt(5))


def layout(graph: Graph) -> dict[str, tuple[float, float]]:
    """Positions in a unit disc (radius ~1), stable across refreshes.

    Deterministic, no simulation: the core at the centre; hubs and milestones on
    a ring, each given an arc sized by its cluster; each hub's nodes form a
    sunflower cluster around it (the newest commits nearest the commits hub),
    with a little jitter hashed from the node id. Cross links (a commit naming
    an issue, a paper judged for an issue) then draw the web between clusters.
    """
    pos: dict[str, tuple[float, float]] = {}
    by_id = {n.id: n for n in graph.nodes}
    if "jarvis" in by_id:
        pos["jarvis"] = (0.0, 0.0)
    hubs = [n.id for n in graph.nodes if n.kind in ("hub", "milestone")]
    hub_set = set(hubs)
    parent: dict[str, str] = {}
    for e in graph.edges:
        for a, b in ((e.a, e.b), (e.b, e.a)):
            if a in parent or a in hub_set or a == "jarvis" or a not in by_id:
                continue
            if b in hub_set and e.kind in ("hub", "milestone"):
                parent[a] = b
    if "hub:commits" in hub_set:
        for k in _strand(graph):
            parent[k] = "hub:commits"
    for e in graph.edges:  # cards sit with the check or issue they are about
        if e.kind == "card" and e.a not in parent and e.b in parent:
            parent[e.a] = parent[e.b]
    groups: dict[str, list[str]] = {h: [] for h in hubs}
    for nid, h in parent.items():
        groups[h].append(nid)

    def cluster_r(n: int) -> float:
        return 0.07 + 0.05 * math.sqrt(n)

    sizes = {h: cluster_r(len(groups[h])) for h in hubs}
    total = sum(2 * r + 0.06 for r in sizes.values()) or 1.0
    ring = max(0.45, total / (2 * math.pi))
    angle = -math.pi / 2
    for h in hubs:
        share = 2 * math.pi * (2 * sizes[h] + 0.06) / total
        mid = angle + share / 2
        hx, hy = ring * math.cos(mid), ring * math.sin(mid)
        pos[h] = (hx, hy)
        if h == "hub:commits":
            kids = [k for k in _strand(graph) if parent.get(k) == h]  # newest first
        else:
            kids = sorted(groups[h], key=lambda k: (by_id[k].kind, k))
        n = len(kids)
        big = sizes[h]
        for i, k in enumerate(kids):
            r = big * math.sqrt((i + 0.8) / (n + 0.8))
            a = mid + i * GOLDEN + 0.35 * (_hash01(k, "a") - 0.5)
            r *= 0.9 + 0.2 * _hash01(k, "r")
            pos[k] = (hx + r * math.cos(a), hy + r * math.sin(a))
        angle += share
    for n in graph.nodes:  # anything not under a hub floats near the core
        if n.id not in pos:
            a = 2 * math.pi * _hash01(n.id, "a")
            r = 0.12 + 0.12 * _hash01(n.id, "r")
            pos[n.id] = (r * math.cos(a), r * math.sin(a))
    far = max((math.hypot(x, y) for x, y in pos.values()), default=1.0) or 1.0
    return {k: (x / far, y / far) for k, (x, y) in pos.items()}


def _strand(graph: Graph) -> list[str]:
    """Commit ids in strand order (newest first)."""
    nxt = {e.b: e.a for e in graph.edges if e.kind == "history"}
    out, cur = [], "hub:commits"
    while cur in nxt and len(out) <= len(graph.nodes):
        cur = nxt[cur]
        out.append(cur)
    return out


# ---------------------------------------------------------------- stream


@dataclass(frozen=True)
class StreamItem:
    when: str          # ISO
    kind: str          # commit / closed / opened / card / done / briefing / research / chat / check
    text: str
    origin: str        # "github", "jarvis", "chat", "research", "checks"
    node_id: str | None = None
    live: bool = False


def load_stream(conn: sqlite3.Connection, now: datetime | None = None,
                limit: int = STREAM_MAX) -> tuple[StreamItem, ...]:
    """Everything Jarvis noticed or said, newest first."""
    now = now or datetime.now(UTC)
    live_after = now - timedelta(hours=LIVE_HOURS)
    out: list[StreamItem] = []

    def add(when: str | None, kind: str, text: str, origin: str, node: str | None = None):
        dt = _parse(when)
        if dt is not None:
            out.append(StreamItem(dt.isoformat(), kind, text, origin, node, dt >= live_after))

    for c in conn.execute("SELECT sha, message, committed_at FROM commits "
                          "ORDER BY committed_at DESC LIMIT ?", (limit,)):
        first = (c["message"] or "").strip().splitlines()[0] if c["message"] else ""
        add(c["committed_at"], "commit", f"commit {c['sha'][:7]} · {_short(first, 90)}",
            "github", f"commit:{c['sha'][:7]}")
    for i in conn.execute("SELECT number, title, state, created_at, closed_at FROM issues"):
        add(i["created_at"], "opened", f"opened #{i['number']} {_short(i['title'], 80)}",
            "github", f"issue:{i['number']}")
        if i["state"] == "closed" and i["closed_at"]:
            add(i["closed_at"], "closed", f"closed #{i['number']} {_short(i['title'], 80)}",
                "github", f"issue:{i['number']}")
    for c in conn.execute("SELECT id, title, status, created_at, updated_at FROM cards"):
        add(c["created_at"], "card", f"new suggestion: {_short(c['title'], 90)}", "checks",
            f"card:{c['id']}" if c["status"] == "active" else None)
        if c["status"] != "active":
            add(c["updated_at"], "done",
                f"{'done' if c['status'] == 'done' else 'resolved'}: {_short(c['title'], 90)}",
                "jarvis")
    for b in conn.execute("SELECT headline, created_at, source, model FROM briefings "
                          "ORDER BY id DESC LIMIT 10"):
        who = b["model"] if b["source"] == "model" else "template"
        add(b["created_at"], "briefing", f"briefing rewritten ({who}): {_short(b['headline'], 90)}",
            "jarvis", "jarvis")
    for r in conn.execute("SELECT substr(judged_at, 1, 10) AS day, MAX(judged_at) AS last, "
                          "COUNT(*) AS n, SUM(verdict NOT IN ('not_relevant', 'irrelevant', "
                          "'no')) AS rel FROM research_seen GROUP BY day"):
        add(r["last"], "research",
            f"research judged {r['n']} paper{'s' if r['n'] != 1 else ''} · "
            f"{r['rel'] or 0} relevant", "research", "hub:research")
    for r in conn.execute("SELECT repo, tag, updated_at FROM research_releases"):
        add(r["updated_at"], "research", f"release tracked: {r['repo']} {r['tag']}", "research")
    msgs = conn.execute("SELECT session_id, role, content, created_at FROM chat_messages "
                        "ORDER BY id").fetchall()
    last_q: dict[str, str] = {}
    for m in msgs:
        if m["role"] == "user":
            last_q[m["session_id"]] = m["content"] or ""
        else:
            q = last_q.get(m["session_id"], "")
            if not (m["content"] or "").strip():
                continue
            add(m["created_at"], "chat",
                f"answered “{_short(q, 60)}”: {_short(m['content'], 110)}", "chat")
    for ch in conn.execute("SELECT check_id, status, title, updated_at FROM electrical_findings "
                           "WHERE status IN ('fail', 'warn')"):
        add(ch["updated_at"], "check", f"check {ch['status']}: {_short(ch['title'], 90)}",
            "checks", f"check:{ch['check_id']}")
    out.sort(key=lambda s: s.when, reverse=True)
    return tuple(out[:limit])


# ---------------------------------------------------------------- vitals / pulse


@dataclass(frozen=True)
class Vital:
    key: str
    value: str
    live: bool = False
    level: str | None = None   # "fail" for a red value
    tip: str = ""


def vitals(conn: sqlite3.Connection, now: datetime | None = None) -> tuple[Vital, ...]:
    now = now or datetime.now(UTC)
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0] or 0  # noqa: E731
    open_i = one("SELECT COUNT(*) FROM issues WHERE state = 'open'")
    closed_i = one("SELECT COUNT(*) FROM issues WHERE state = 'closed'")
    commits_w = sum(1 for r in conn.execute("SELECT committed_at FROM commits")
                    if (_parse(r[0]) or now) >= now - timedelta(days=7))
    cards = one("SELECT COUNT(*) FROM cards WHERE status = 'active'")
    fails = one("SELECT COUNT(*) FROM electrical_findings WHERE status = 'fail'")
    unknown = one("SELECT COUNT(*) FROM electrical_findings WHERE status IN ('unknown', 'warn')")
    passed = one("SELECT COUNT(*) FROM electrical_findings WHERE status = 'pass'")
    papers = one("SELECT COUNT(*) FROM research_papers")
    turns = one("SELECT COUNT(*) FROM chat_messages WHERE role = 'assistant'")
    out = [
        Vital("issues", f"{open_i} open · {closed_i} closed"),
        Vital("commits 7d", str(commits_w), live=commits_w > 0),
        Vital("suggestions", str(cards), live=cards > 0),
        Vital("checks", f"{passed} pass · {unknown} unsure" + (f" · {fails} fail" if fails else ""),
              level="fail" if fails else None),
        Vital("papers", str(papers)),
        Vital("chat turns", str(turns)),
    ]
    return tuple(out)


@dataclass(frozen=True)
class Pulse:
    counts: tuple[int, ...]   # per day, oldest first, PULSE_DAYS long
    total: int
    caption: str


def pulse(conn: sqlite3.Connection, today: date | None = None,
          days: int = PULSE_DAYS) -> Pulse:
    """Commits plus issues opened / closed, per local day."""
    today = today or datetime.now().astimezone().date()
    start = today - timedelta(days=days - 1)
    counts = [0] * days

    def bump(iso: str | None) -> None:
        dt = _parse(iso)
        if dt is None:
            return
        d = dt.astimezone().date()
        if start <= d <= today:
            counts[(d - start).days] += 1

    for (iso,) in conn.execute("SELECT committed_at FROM commits"):
        bump(iso)
    for r in conn.execute("SELECT created_at, closed_at FROM issues"):
        bump(r[0])
        bump(r[1])
    total = sum(counts)
    return Pulse(tuple(counts), total, f"last {days} days · {total} events")


# ---------------------------------------------------------------- everything


@dataclass(frozen=True)
class BrainData:
    graph: Graph
    stream: tuple[StreamItem, ...]
    vitals: tuple[Vital, ...]
    pulse: Pulse
    errors: tuple[str, ...] = field(default_factory=tuple)


def load_brain(conn: sqlite3.Connection, repo: str, now: datetime | None = None) -> BrainData:
    """Each part fails on its own: a broken one becomes an error line."""
    now = now or datetime.now(UTC)
    errors: list[str] = []

    def safe(fn, default: Any, what: str) -> Any:
        try:
            return fn()
        except sqlite3.Error as e:
            errors.append(f"Could not read {what}: {e}")
            return default

    graph = safe(lambda: build_graph(conn, repo, now), Graph((), ()), "the project graph")
    stream = safe(lambda: load_stream(conn, now), (), "the stream")
    vit = safe(lambda: vitals(conn, now), (), "the vitals")
    pul = safe(lambda: pulse(conn, now.astimezone().date()),
               Pulse((0,) * PULSE_DAYS, 0, ""), "the pulse")
    return BrainData(graph, stream, vit, pul, tuple(errors))


__all__ = ["Node", "Edge", "Graph", "build_graph", "layout", "StreamItem", "load_stream",
           "Vital", "vitals", "Pulse", "pulse", "BrainData", "load_brain", "ago",
           "EDGE_LABELS", "KINDS", "FILTER_KINDS"]
