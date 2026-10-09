"""Relevance: an embedding prefilter, then a strict model judge, then validation.

1. Prefilter. Cosine similarity between the issue (title + body excerpt, with the
   retrieval instruction qwen3-embedding expects on the query side) and each paper
   (title + abstract), via Ollama ``/api/embed``. Only the top ``top_k`` papers at or
   above ``min_similarity`` go to the judge. Paper vectors are cached per embed model.
   If the embed model is unavailable, a keyword overlap score stands in.
2. Judge. Gemma, schema-constrained, sees the project, the issue and one paper and
   fills a checklist: the paper's setting and technique, four yes/no checks
   (same_problem, comparable_data, transfers, new_to_issue), a confidence, applies_to
   and why. Code accepts only when all four checks are yes.
3. Validation, in code: only medium/high confidence survives, and ``why`` must be one
   or two hedged sentences ("may", "could", "might") with no numbers absent from the
   paper, the issue or the project text, no issue references and no hype or advice
   words. A reply that fails is recorded as ``invalid`` and makes no card.
4. Confirm. An accepted paper gets a second, contrastive look: given all researched
   issues, the model must pick this one as the best fit, or the paper is dropped.

Every judged (paper, issue) pair is stored in ``research_seen`` with its verdict and
the prompt version, so it is never judged twice (a new PROMPT_VERSION re-judges); a
model error stores nothing and is retried next run.
"""
from __future__ import annotations

import math
import re
import sqlite3
import time
from array import array
from dataclasses import dataclass
from typing import Any

from jarvis.briefing import _BANNED_RE, check_numbers
from jarvis.db import now_iso
from jarvis.facts import Fact, tokens
from jarvis.ollama import OllamaError
from jarvis.research.queries import IssuePlan, excerpt
from jarvis.research.settings import ResearchSettings
from jarvis.research.sources import Paper

ISSUE_INSTRUCT = ("Instruct: Given an engineering issue from a small embedded LiDAR "
                  "tracking project, retrieve research papers whose method could help "
                  "solve it\nQuery: ")
ABSTRACT_CHARS = 1800
KEYWORD_MIN = 0.5  # share of the issue title's words found in the paper (fallback)
MAX_WHY_SENTENCES = 2
MAX_WHY_CHARS = 360
MAX_APPLIES_WORDS = 8
PROMPT_VERSION = "5"  # stored with each verdict; a new version re-judges

HEDGE_RE = re.compile(r"\b(may|might|could)\b", re.I)
HYPE_RE = re.compile(
    r"\b(state[- ]of[- ]the[- ]art|cutting[- ]edge|groundbreaking|revolutionary|"
    r"breakthrough|game[- ]chang\w*|significantly|dramatically|drastically|"
    r"remarkabl\w*|perfect(ly)?|superior|novel|innovative|powerful|seamless\w*|"
    r"outperform\w*|best)\b", re.I)
ISSUE_REF_RE = re.compile(r"#\s*\d+")
CONFIDENCE = ("low", "medium", "high")


# ---------------------------------------------------------------- similarity


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def keyword_similarity(issue_title: str, paper_text: str) -> float:
    """Share of the issue title's meaningful words that appear in the paper."""
    want = set(tokens(issue_title))
    if not want:
        return 0.0
    have = set(tokens(paper_text))
    return round(len(want & have) / len(want), 3)


def paper_text(p: Paper) -> str:
    return f"{p.title}\n{p.abstract}"


def _pack(vec: list[float]) -> bytes:
    return array("f", vec).tobytes()


def _unpack(blob: bytes) -> list[float]:
    a = array("f")
    a.frombytes(blob)
    return a.tolist()


class Embedder:
    """Embeddings with a per-paper cache; ``available`` turns False on the first error."""

    def __init__(self, conn: sqlite3.Connection, ollama: Any | None, model: str,
                 write: bool = True) -> None:
        self.conn, self.ollama, self.model, self.write = conn, ollama, model, write
        self.available = ollama is not None and hasattr(ollama, "embed")
        self.error: str | None = None if self.available else "no embed client"
        self.seconds = 0.0
        self.calls = 0
        self._mem: dict[str, list[float]] = {}

    def _embed(self, texts: list[str]) -> list[list[float]] | None:
        if not self.available or not texts:
            return None
        t0 = time.monotonic()
        try:
            out = self.ollama.embed(texts, model=self.model)
        except OllamaError as e:
            self.available, self.error = False, str(e)
            return None
        finally:
            self.seconds += time.monotonic() - t0
            self.calls += 1
        return out

    def issue_vector(self, issue: IssuePlan) -> list[float] | None:
        vecs = self._embed([ISSUE_INSTRUCT + issue.text()])
        return vecs[0] if vecs else None

    def paper_vectors(self, papers: list[Paper]) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        missing: list[Paper] = []
        for p in papers:
            if p.id in self._mem:
                out[p.id] = self._mem[p.id]
                continue
            row = self.conn.execute(
                "SELECT embedding FROM research_papers WHERE paper_id = ? AND embed_model = ?",
                (p.id, self.model)).fetchone()
            if row is not None and row["embedding"]:
                out[p.id] = self._mem[p.id] = _unpack(row["embedding"])
            else:
                missing.append(p)
        for i in range(0, len(missing), 32):
            chunk = missing[i:i + 32]
            vecs = self._embed([paper_text(p) for p in chunk])
            if vecs is None:
                break
            for p, v in zip(chunk, vecs):
                out[p.id] = self._mem[p.id] = v
                if self.write:
                    self.conn.execute(
                        "UPDATE research_papers SET embedding = ?, embed_model = ? "
                        "WHERE paper_id = ?", (_pack(v), self.model, p.id))
        if self.write:
            self.conn.commit()
        return out


@dataclass
class Scored:
    paper: Paper
    similarity: float
    method: str  # "embedding" | "keywords"


def prefilter(issue: IssuePlan, papers: list[Paper], emb: Embedder, s: ResearchSettings
              ) -> tuple[list[Scored], list[Scored]]:
    """(kept top-k above the threshold, all scored, best first)."""
    scored: list[Scored] = []
    ivec = emb.issue_vector(issue) if emb.available else None
    pvecs = emb.paper_vectors(papers) if ivec is not None else {}
    for p in papers:
        if ivec is not None and p.id in pvecs:
            scored.append(Scored(p, round(cosine(ivec, pvecs[p.id]), 4), "embedding"))
        else:
            scored.append(Scored(p, keyword_similarity(issue.title, paper_text(p)), "keywords"))
    scored.sort(key=lambda x: (-x.similarity, x.paper.id))
    kept = [x for x in scored
            if x.similarity >= (s.min_similarity if x.method == "embedding" else KEYWORD_MIN)]
    return kept[: s.top_k], scored


# ---------------------------------------------------------------- judge

JUDGE_SYSTEM = """\
You screen research papers for one engineer's hobby embedded project and you are \
strict: a wrong "relevant" wastes the engineer's evening, a missed paper costs little. \
Most papers you see are NOT relevant.

Project: {project}
Hardware: {setup}. {constraints}

You get one OPEN issue from the project and one paper. Fill in, in this order:
- paper_setting: the paper's sensor, platform and data in a few words (for example \
"thermal camera on drones", "3D LiDAR on cars", "2D laser scanner on a mobile robot").
- technique: the paper's core technique in a few words.
- same_problem: true only if the paper's main contribution addresses the issue's main \
problem as written, not just the same field. "Uses LiDAR", "uses a Kalman filter" or \
"does tracking" alone is false.
- comparable_data: true only if the paper works on data like this project's: range or \
distance readings, a planar scan, or target positions. Cameras, thermal or event \
images, 3D point clouds, ECG, audio, radio or text are false, unless the technique \
is independent of the data and the issue's problem is identical.
- transfers: true only if the technique could realistically run on this hardware, or \
after dropping heavy parts. Deep networks, GPUs, many sensors, odometry/SLAM, or \
methods built for a car, drone, underwater or legged platform are usually false.
- new_to_issue: true only if the technique is something the issue does not already \
plan (for example coasting on the last velocity, or a size filter) and it serves \
the issue's own goal.
- relevant: true only if all four checks are true.
- confidence: "high" = directly about this problem in a similar low-cost, \
low-point-count setup; "medium" = same problem and the technique clearly transfers \
with some adaptation; "low" = anything else.
- applies_to: 2 to 6 words naming the part of the issue the paper helps with.
- why: 1 or 2 short plain sentences naming the technique and how it could help with \
this issue (or, if not relevant, which check failed). Always hedge with "may" or \
"could", because applicability is a judgement. Use no numbers unless copied from the \
abstract. No praise or hype words (novel, state-of-the-art, significantly, best, \
outperforms), no advice words (should, must), no issue numbers.

Surveys, datasets, benchmarks and papers that only use the topic as a small step are \
not relevant.

Example of a good why: "Its innovation-based tuning of the noise covariances may help \
the x/y Kalman filter cope with missed detections between sweeps."
"""

CHECKS = ("same_problem", "comparable_data", "transfers", "new_to_issue")

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "paper_setting": {"type": "string"},
        "technique": {"type": "string"},
        **{c: {"type": "boolean"} for c in CHECKS},
        "relevant": {"type": "boolean"},
        "confidence": {"type": "string", "enum": list(CONFIDENCE)},
        "applies_to": {"type": "string"},
        "why": {"type": "string"},
    },
    "required": ["paper_setting", "technique", *CHECKS, "relevant", "confidence",
                 "applies_to", "why"],
}


@dataclass
class Verdict:
    verdict: str  # "relevant" | "not_relevant" | "low_confidence" | "invalid"
    confidence: str | None
    applies_to: str
    why: str
    reason: str  # what decided it (validation failure, model's why for rejections)
    similarity: float
    cached: bool = False
    model: str | None = None

    @property
    def accepted(self) -> bool:
        return self.verdict == "relevant"


def judge_user_prompt(issue: IssuePlan, p: Paper) -> str:
    abstract = p.abstract if len(p.abstract) <= ABSTRACT_CHARS else \
        p.abstract[:ABSTRACT_CHARS].rsplit(" ", 1)[0] + " ..."
    return (f"OPEN ISSUE #{issue.number}: {issue.title}\n"
            f"Issue body: {excerpt(issue.body) or '(empty)'}\n\n"
            f"PAPER ({p.year or 'year unknown'}): {p.title}\n"
            f"Abstract: {abstract}\n\n"
            "Return relevant, applies_to, why and confidence.")


def _sentences(text: str) -> list[str]:
    return [x for x in re.split(r"(?<=[.?!])\s+", text) if x.strip()]


def text_problem(text: str, issue: IssuePlan, p: Paper, s: ResearchSettings,
                 hedge: bool = True) -> str | None:
    """Why ``text`` (the model's why) is not acceptable, or None."""
    text = text.strip()
    if not text:
        return "empty"
    if "\n" in text:
        return "multiple lines"
    if len(_sentences(text)) > MAX_WHY_SENTENCES:
        return f"more than {MAX_WHY_SENTENCES} sentences"
    if len(text) > MAX_WHY_CHARS:
        return f"longer than {MAX_WHY_CHARS} characters"
    if ISSUE_REF_RE.search(text):
        return "mentions an issue number"
    m = HYPE_RE.search(text)
    if m:
        return f"hype word: {m.group(0)}"
    for rx, why in _BANNED_RE:
        if rx.search(text):
            return f"banned wording ({why})"
    if hedge and not HEDGE_RE.search(text):
        return "not hedged (needs may/could/might)"
    ref = Fact("research", "research", 0,
               " ".join([p.title, p.abstract, issue.title, issue.body, s.project, s.setup]),
               {})
    return check_numbers(text, [ref])


def validate_judgment(raw: Any, issue: IssuePlan, p: Paper, s: ResearchSettings,
                      similarity: float) -> Verdict:
    if not isinstance(raw, dict):
        return Verdict("invalid", None, "", "", "reply is not an object", similarity)
    conf = str(raw.get("confidence") or "").strip().lower()
    conf = conf if conf in CONFIDENCE else None
    why = " ".join(str(raw.get("why") or "").split())
    applies = " ".join(str(raw.get("applies_to") or "").split()).strip(" .")
    setting = " ".join(str(raw.get("paper_setting") or "").split())[:80]
    # The checklist decides, not the model's own "relevant" flag alone.
    failed = [c for c in CHECKS if raw.get(c) is not True]
    if failed or raw.get("relevant") is not True:
        what = f"fails {', '.join(failed)}" if failed else "model said not relevant"
        detail = f"{what} ({setting})" if setting else what
        return Verdict("not_relevant", conf, applies, why, f"{detail}: {why}"[:300],
                       similarity)
    if conf in (None, "low"):
        return Verdict("low_confidence", conf, applies, why,
                       f"confidence {conf or 'missing'}: {why[:240]}", similarity)
    problem = text_problem(why, issue, p, s)
    if problem:
        return Verdict("invalid", conf, applies, why, f"why: {problem}", similarity)
    if applies and (len(applies.split()) > MAX_APPLIES_WORDS
                    or text_problem(applies, issue, p, s, hedge=False)):
        applies = ""  # optional; dropped rather than rejecting the paper
    return Verdict("relevant", conf, applies, why, "", similarity)


def cached_verdict(conn: sqlite3.Connection, paper_id: str, issue: int) -> Verdict | None:
    row = conn.execute("SELECT * FROM research_seen WHERE paper_id = ? AND issue_number = ?",
                       (paper_id, issue)).fetchone()
    if row is None or not str(row["model"] or "").endswith(f"@p{PROMPT_VERSION}"):
        return None  # never judged, or judged with an older prompt
    return Verdict(row["verdict"], row["confidence"], row["applies_to"] or "",
                   row["why"] or "", row["reason"] or "", row["similarity"] or 0.0,
                   cached=True, model=row["model"])


def store_verdict(conn: sqlite3.Connection, paper_id: str, issue: int, v: Verdict) -> None:
    conn.execute(
        """INSERT INTO research_seen (paper_id, issue_number, verdict, confidence, applies_to,
               why, reason, similarity, judged_at, model) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(paper_id, issue_number) DO UPDATE SET verdict=excluded.verdict,
               confidence=excluded.confidence, applies_to=excluded.applies_to,
               why=excluded.why, reason=excluded.reason, similarity=excluded.similarity,
               judged_at=excluded.judged_at, model=excluded.model""",
        (paper_id, issue, v.verdict, v.confidence, v.applies_to, v.why, v.reason,
         v.similarity, now_iso(), f"{v.model or 'model'}@p{PROMPT_VERSION}"))
    conn.commit()


def judge(ollama: Any, issue: IssuePlan, p: Paper, s: ResearchSettings,
          similarity: float) -> Verdict:
    """One model call (raises OllamaError), validated."""
    raw = ollama.chat_json(JUDGE_SYSTEM.format(project=s.project, setup=s.setup,
                                               constraints=s.constraints),
                           judge_user_prompt(issue, p), JUDGE_SCHEMA)
    v = validate_judgment(raw, issue, p, s, similarity)
    v.model = getattr(ollama, "model", None)
    return v


# ---------------------------------------------------------------- confirm

CONFIRM_SYSTEM = """\
You match one research paper to the open issues of a hobby embedded project.
Project: {project}
Hardware: {setup}. {constraints}

Pick the single issue whose main problem the paper's core technique helps with most \
directly on this hardware. Answer 0 if it does not clearly help any of them; that is \
a normal answer. Return the issue number and one short sentence of reason.
"""

CONFIRM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"issue": {"type": "integer"}, "reason": {"type": "string"}},
    "required": ["issue", "reason"],
}


def confirm(ollama: Any, issues: list[IssuePlan], p: Paper, s: ResearchSettings) -> int:
    """Second, contrastive look at an accepted paper: which issue does it fit best?
    Returns that issue number (0 for none). Raises OllamaError."""
    listed = "\n".join(f"#{i.number}: {i.title}. {excerpt(i.body, 200)}" for i in issues)
    abstract = p.abstract if len(p.abstract) <= ABSTRACT_CHARS else \
        p.abstract[:ABSTRACT_CHARS].rsplit(" ", 1)[0] + " ..."
    user = (f"OPEN ISSUES:\n{listed}\n\nPAPER ({p.year or 'year unknown'}): {p.title}\n"
            f"Abstract: {abstract}\n\nReturn issue and reason.")
    raw = ollama.chat_json(CONFIRM_SYSTEM.format(project=s.project, setup=s.setup,
                                                 constraints=s.constraints), user,
                           CONFIRM_SCHEMA)
    n = raw.get("issue") if isinstance(raw, dict) else None
    if isinstance(n, str) and n.strip().lstrip("#").isdigit():
        n = int(n.strip().lstrip("#"))
    return n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 0
