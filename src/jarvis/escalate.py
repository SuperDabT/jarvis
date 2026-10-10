"""Thinking mode: escalate weak answers from a fast model to a reasoning model.

Two tiers:

- fast: the voice model (qwen3:14b) for voice, or the normal chat model
  (gemma4:26b without reasoning) for typed chat;
- deep: gemma4:26b with its native reasoning on (Ollama ``think: true``). The
  reasoning arrives in a separate stream field and is never shown or spoken; only
  its duration is reported (``Answer.thinking_s``). ``deep_think=False`` runs the
  deep tier without reasoning (voice auto mode: measured on the grounded prompt,
  gemma's reasoning never finished inside the 20 s voice cap, while plain gemma
  answered planning questions well in ~10 s).

``answer_with_escalation`` picks the tier:

- ``mode="think"``: straight to deep.
- ``mode="auto"`` with a fast tier (voice): planning questions ("what should I do
  today", "what's next", "prioritize") go straight to deep, because the small model
  fails at them and a fast pass would only add latency; anything else runs fast
  first and escalates when ``weak_answer`` says the answer is weak.
- ``mode="auto"`` without a fast tier (``fast_client=None``, typed chat): the deep
  model runs without reasoning first, and with reasoning only if that is weak.

Out-of-scope questions (weather, news, ...) never escalate: the one-sentence
decline is the right answer. If the deep answer is weak too (empty, failed, or a
planning answer that ignores the suggested next steps), the final answer is a
template built from ``facts.compute_next_steps`` — never an empty answer.

Streaming: the fast pass streams to ``on_token``. When it gets escalated,
``on_phase("discard_fast", ...)`` tells the UI to clear the streamed fast text;
the deep pass then streams to the same ``on_token``. The final, authoritative
text is ``answer.display_text`` (as with ``chat.ask``).

``on_phase(name, info)`` events, in order of appearance (info is a dict):

- ``"fast"``      {"model"}: the fast pass starts.
- ``"escalating"`` {"reasons": [...], "spoken": bool}: the fast answer was weak.
  ("spoken" is filled in by voice_answer; False here.)
- ``"discard_fast"`` {}: clear the streamed fast text.
- ``"deep"``      {"model", "reason"}: the deep pass starts; reason is
  "think_mode", "planning" or "escalated" (show the deep-thinking orb).
- ``"thinking"``  {}: the first reasoning chunk arrived (model loaded, reasoning).
- ``"answering"`` {"thinking_s"}: reasoning done, the answer starts streaming.
- ``"thinking_cut"`` {"thinking_s"}: the reasoning hit its time cap and was
  stopped; the deep model now answers without reasoning (no new "deep" event).
- ``"fallback"``  {"reasons": [...]}: the deep answer was weak too; the template
  answer is used.
- ``"done"``      {"tier", "escalated"}.
"""
from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from jarvis import assistant, chat, intent
from jarvis.chat import Answer, Brevity, Context
from jarvis.config import Config
from jarvis.facts import NextStep, compute_next_steps
from jarvis.ollama import THINKING_BUDGET_MSG

Mode = Literal["auto", "think"]
QuestionClass = Literal["planning", "out_of_scope", "project"]
PhaseFn = Callable[[str, dict[str, Any]], None]

DEEP_KEEP_ALIVE = "10m"
# Reasoning cap per deep pass. gemma4:26b reasons at ~40 tokens/s here and, on the
# rule-heavy grounded prompt, often keeps re-checking its draft for thousands of
# tokens (measured: 5000+ tokens / 140 s, sometimes never finishing). Past the cap
# the reasoning is stopped and the same model answers without it (prompt cached).
DEEP_THINK_BUDGET_S = {"voice": 20.0, "text": 45.0}

# ---------------------------------------------------------------- question classes

_PLANNING_RE = re.compile("|".join([
    r"\bwhat\s+should\s+(?:i|we)\b",
    r"\bwhat\s+(?:do|can|could)\s+(?:i|we)\s+(?:do|work\s+on|tackle|start)\b",
    r"\bwhat\s+(?:to|is\s+there\s+to)\s+(?:do|work\s+on)\b",
    r"\btasks?\s+(?:for|of)\s+(?:today|the\s+day|tomorrow|this\s+week|the\s+week)\b",
    r"\b(?:today'?s|our|my)\s+tasks?\b",
    r"\bwhat'?s\s+next\b", r"\bwhat\s+is\s+next\b", r"\bwhat\s+comes\s+next\b",
    r"\bnext\s+steps?\b", r"\bnext\s+task\b",
    r"\bprioriti[sz]", r"\bpriorit(?:y|ies)\b",
    r"\bwhat'?s\s+the\s+plan\b", r"\bmake\s+a\s+plan\b",
    r"\bplan\s+(?:for|my|our)\s+(?:the\s+)?(?:day|today|week|tomorrow|next)\b",
    r"\brecommend", r"\bsuggest(?:ion)?s?\b",
    r"\bfocus\s+on\b", r"\bwhere\s+(?:do|should|can)\s+(?:i|we)\s+start\b",
    r"\bwork\s+on\s+(?:today|next|this\s+week|tomorrow)\b",
    r"\bto-?do\b",
]), re.I)

_OUT_OF_SCOPE_RE = re.compile(
    r"\b(?:weather|forecast|raining|snow(?:ing)?|news|headlines?|stock\s+price|stocks?|"
    r"bitcoin|sports?|football|soccer|basketball|recipe|cook|joke|movie|film|song|music|"
    r"capital\s+of|president|prime\s+minister|what\s+time\s+is\s+it|translate|horoscope|"
    r"traffic)\b", re.I)
_PROJECT_RE = re.compile(
    r"#\d+|\b(?:issues?|commits?|milestones?|demo|v\d|rail|servo|hardware|project|repo|"
    r"pcb|lidar|sensor|board|firmware|code|track|uart|oled|pid|kalman|electrical|card|"
    r"check|jarvis)\b", re.I)


def classify_question(question: str) -> QuestionClass:
    """"planning" (what to do / next / prioritize), "out_of_scope" (weather, news,
    trivia, with no project word in it) or "project" (everything else)."""
    q = question.strip()
    if _OUT_OF_SCOPE_RE.search(q) and not _PROJECT_RE.search(q):
        return "out_of_scope"
    if _PLANNING_RE.search(q):
        return "planning"
    return "project"


# ---------------------------------------------------------------- weak answers

_UNKNOWN_RE = re.compile("|".join([
    r"\bi\s+(?:do\s+not|don'?t)\s+know\b",
    r"\b(?:i|jarvis)\s+(?:do\s+not|don'?t|does\s+not|doesn'?t)\s+have\s+"
    r"(?:that|this|enough|any|the)\b",
    r"\bnot\s+(?:in|part\s+of)\s+(?:the|my|jarvis'?s?|your)\s+(?:synced\s+)?(?:data|context)\b",
    r"\b(?:the\s+)?context\s+(?:does\s+not|doesn'?t)\s+(?:contain|include|say|mention|show)\b",
    r"\bdata\s+(?:does\s+not|doesn'?t)\s+(?:contain|include|say|mention|show)\b",
    r"\bno\s+(?:information|data|record|details?)\s+(?:about|on|for|in)\b",
    r"\bthere\s+is\s+no\s+(?:information|data|record)\b",
    r"\b(?:isn'?t|is\s+not|aren'?t|are\s+not)\s+(?:recorded|in\s+the\s+(?:data|context))\b",
    r"\b(?:cannot|can'?t|unable\s+to)\s+(?:determine|tell|answer|find|say)\b",
    r"\bonly\s+knows?\s+(?:about\s+)?(?:this|the|your)\s+project",
]), re.I)

HARD_REASONS = frozenset({"empty", "model_error", "planning_without_next_steps"})


def weak_answer(question: str, answer: Answer, next_step_refs: set[str] | frozenset[str],
                qclass: QuestionClass | None = None) -> tuple[bool, list[str]]:
    """(weak, reasons) for an answer; a pure function of its inputs.

    Reasons: "empty" (no text after removing SOURCES / thinking), "model_error",
    "says_unknown" (says it doesn't know / it isn't in the data, for a project
    question), "unverified_numbers", "unknown_refs" (an issue or sha that isn't in
    the data), "wrong_commit_date" (a commit-date claim contradicting the commits),
    "planning_without_next_steps" (a planning question whose answer cites none of
    the suggested next steps or their issues; citing only a milestone doesn't
    count). Out-of-scope questions are only
    weak when empty or failed: their decline is the expected answer.
    """
    qclass = qclass or classify_question(question)
    text = chat.strip_think(answer.text or "")
    text, _ = chat.split_sources(text)
    if answer.error and not text.strip():
        return True, ["model_error"]
    if not text.strip():
        return True, ["empty"]
    reasons: list[str] = []
    if answer.error:
        reasons.append("model_error")
    if qclass == "out_of_scope":
        return bool(reasons), reasons
    if _UNKNOWN_RE.search(text):
        reasons.append("says_unknown")
    if answer.unverified_numbers:
        reasons.append("unverified_numbers")
    if answer.invalid_refs:
        reasons.append("unknown_refs")
    if answer.claim_errors:
        reasons.append("wrong_commit_date")
    if qclass == "planning" and next_step_refs:
        used = {s.ref for s in answer.sources} | {s.ref for s in answer.cited_sources}
        used |= {f"#{n}" for n in chat.mentioned_issues(text)}
        # a milestone alone says nothing about which step: only issues, commits,
        # cards, checks and the next-steps fact itself count
        specific = {r for r in next_step_refs if not r.startswith("milestone:")}
        # the user's own items are options too (routed planning questions list them)
        if not used & specific and not any(r.startswith("item:") for r in used):
            reasons.append("planning_without_next_steps")
    return bool(reasons), reasons


# ---------------------------------------------------------------- fallback


def fallback_answer(steps: Sequence[NextStep], ctx: Context | None, brevity: Brevity,
                    prefix: str | None = None) -> Answer:
    """A template answer from the suggested next steps (no model)."""
    head = prefix or "I couldn't get a reliable answer to that."
    if not steps:
        text = f"{head} Jarvis has no suggested next steps from the data either."
    elif brevity == "voice":
        top = steps[:2]
        text = (f"{head} From the data, the top suggestions are: "
                + " Then: ".join(s.text for s in top))
    else:
        lines = [f"{s.rank}. {'(uncertain) ' if not s.certain else ''}{s.text}"
                 for s in steps]
        text = (f"{head} From the data, Jarvis's suggested next steps (computed from "
                "issues, milestones and checks, not a schedule):\n" + "\n".join(lines))
    refs = ctx.refs if ctx else {}
    wanted = list(dict.fromkeys(e for s in steps for e in s.evidence))
    sources = [refs[r] for r in wanted if r in refs]
    return Answer(text=text, sources=sources, unverified_numbers=[], notes=[], model=None,
                  duration=0.0, raw="", tier="fallback",
                  cited_sources=sources[:chat.CITED_SOURCES_MAX],
                  cited_more_count=max(0, len(sources) - chat.CITED_SOURCES_MAX),
                  context_tokens=ctx.tokens if ctx else 0)


# ---------------------------------------------------------------- orchestrator


def make_deep_client(cfg: Config) -> Any:
    """An OllamaClient for the deep tier (cfg.model, kept loaded 10 minutes)."""
    from jarvis.ollama import OllamaClient
    return OllamaClient(cfg.ollama_url, cfg.model, keep_alive=DEEP_KEEP_ALIVE)


def answer_with_escalation(
        conn: sqlite3.Connection, cfg: Config, question: str,
        history: Sequence[tuple[str, str]], *, mode: Mode = "auto",
        fast_client: Any | None, deep_client: Any | None,
        on_token: Callable[[str], None] | None = None, on_phase: PhaseFn | None = None,
        brevity: Brevity = "text", session_id: str | None = None,
        now: datetime | None = None, fast_budget: int = chat.CONTEXT_TOKEN_BUDGET,
        deep_budget: int = chat.CONTEXT_TOKEN_BUDGET, stable_prefix: bool = False,
        use_embeddings: bool = False, think_budget_s: float | None = None,
        deep_think: bool = True, focus: Sequence[str] = (), route: Any = None,
        route_client: Any = None) -> Answer:
    """Answer ``question`` on the fast tier, escalating to the reasoning model when
    needed (see the module docstring). Never raises for model failures.

    ``fast_client`` None = no fast tier (typed chat): the deep model without
    reasoning is the first pass. ``deep_client`` None = a client for cfg.model made
    (and closed) here. With ``session_id`` the question and the final answer are
    stored once in chat_messages (with tier / escalation in the meta).
    Returns the final chat.Answer with ``tier``, ``escalated``,
    ``escalation_reasons`` and ``thinking_s`` set; ``duration`` covers both passes
    and ``first_token_s`` is the final pass's first token, from the start.
    ``think_budget_s`` caps the reasoning (default DEEP_THINK_BUDGET_S[brevity]);
    when it runs out, ``on_phase("thinking_cut", {"thinking_s"})`` and the deep
    model answers again without reasoning. ``deep_think=False``: the deep pass
    runs without reasoning (no "thinking" / "answering" / "thinking_cut" events,
    ``thinking_s`` None); the voice auto mode uses this.

    First the question is routed (``intent.route``; ``route`` overrides it;
    unclear questions are classified by ``route_client``, default the fast
    client, else the deep one).
    Lookups code can answer (an agenda, a capture request, a recall with nothing
    found) return at once with ``tier="direct"``; the rest go to the model with
    only the data their route needs (``assistant.build_routed_context``).
    A selected graph item (``focus``) keeps the project route.
    """
    t0 = time.monotonic()
    question = question.strip()
    history = chat.trim_history(history)
    local = now if now is not None and now.tzinfo else (now or datetime.now(UTC)).astimezone()
    if route is None:
        if route_client is None:
            route_client = fast_client if fast_client is not None else deep_client
        route = (intent.Route("project", by="focus") if focus
                 else intent.route(question, local, conn, route_client))
    qclass: QuestionClass = ("planning" if route.intent == "planning"
                             else "out_of_scope" if route.intent == "general" else "project")
    direct = _direct_answer(conn, question, local, route)
    if direct is not None:
        if session_id:
            chat.save_message(conn, session_id, "user", question)
        direct.intent, direct.route_by = route.intent, route.by
        if on_token:
            on_token(direct.text)
        return _finish(conn, session_id, direct, t0, None,
                       lambda name, **info: on_phase(name, info) if on_phase else None)
    own_deep = deep_client is None
    if deep_client is None:
        deep_client = make_deep_client(cfg)

    def phase(name: str, **info: Any) -> None:
        if on_phase:
            on_phase(name, info)

    if think_budget_s is None:
        think_budget_s = DEEP_THINK_BUDGET_S.get(brevity, DEEP_THINK_BUDGET_S["text"])

    def run(client: Any, *, think: bool | None, budget: int,
            tier: str) -> tuple[Answer, Context | None, float]:
        start = time.monotonic() - t0
        ctxs: list[Context] = []
        think_t: list[float] = []
        answering = [False]

        def on_thinking(_chunk: str) -> None:
            if not think_t:
                think_t.append(time.monotonic())
                phase("thinking")

        def tok(t: str) -> None:
            if think_t and not answering[0]:
                answering[0] = True
                phase("answering", thinking_s=round(time.monotonic() - think_t[0], 2))
            if on_token:
                on_token(t)

        ans = chat.ask(conn, cfg, question, history, client, on_token=tok, now=now,
                       use_embeddings=use_embeddings, brevity=brevity, budget_tokens=budget,
                       stable_prefix=stable_prefix, think=think,
                       on_thinking=on_thinking if think else None,
                       max_thinking_s=think_budget_s if think else None, ctx_out=ctxs,
                       focus=focus, route=route)
        ans.tier = tier
        if think and ans.thinking_s is None and think_t:
            ans.thinking_s = time.monotonic() - think_t[0]
        return ans, (ctxs[0] if ctxs else None), start

    try:
        if session_id:
            chat.save_message(conn, session_id, "user", question)
        fast: Answer | None = None
        fast_ctx: Context | None = None
        reasons: list[str] = []
        has_fast_tier = fast_client is not None
        if mode == "think":
            deep_reason = "think_mode"
            reasons = ["think_mode"]
        elif has_fast_tier and qclass == "planning":
            deep_reason = "planning"
            reasons = ["planning"]
        else:
            first_client = fast_client if has_fast_tier else deep_client
            phase("fast", model=getattr(first_client, "model", None))
            fast, fast_ctx, fast_start = run(
                first_client, think=None if has_fast_tier else False,
                budget=fast_budget if has_fast_tier else deep_budget, tier="fast")
            refs = fast_ctx.next_step_refs if fast_ctx else set()
            weak, reasons = weak_answer(question, fast, refs, qclass)
            if qclass == "out_of_scope":
                # a decline cites nothing, whatever its SOURCES line lists
                fast.cited_sources, fast.cited_more_count = [], 0
            if not weak:
                fast.intent, fast.route_by = route.intent, route.by
                return _finish(conn, session_id, fast, t0, fast_start, phase)
            phase("escalating", reasons=list(reasons), spoken=False)
            phase("discard_fast")
            deep_reason = "escalated"

        phase("deep", model=getattr(deep_client, "model", None), reason=deep_reason)
        deep, deep_ctx, deep_start = run(deep_client, think=bool(deep_think),
                                         budget=deep_budget, tier="deep")
        if deep_think and deep.error and THINKING_BUDGET_MSG in deep.error:
            cut_s = deep.thinking_s
            phase("thinking_cut", thinking_s=round(cut_s, 1) if cut_s is not None else None)
            deep, deep_ctx, deep_start = run(deep_client, think=False, budget=deep_budget,
                                             tier="deep")
            deep.thinking_s = cut_s
        refs = deep_ctx.next_step_refs if deep_ctx else set()
        _, deep_reasons = weak_answer(question, deep, refs, qclass)
        final = deep
        final_start = deep_start
        if HARD_REASONS & set(deep_reasons):
            fast_ok = (fast is not None and fast_ctx is not None
                       and not HARD_REASONS & set(weak_answer(
                           question, fast, fast_ctx.next_step_refs, qclass)[1]))
            if fast_ok:
                final, final_start = fast, fast_start
            else:
                phase("fallback", reasons=list(deep_reasons))
                ctx = deep_ctx or fast_ctx
                steps = ctx.next_steps if ctx else compute_next_steps(conn, now)
                prefix = deep.text if deep.error and deep.text else None
                if not route.project_data:
                    steps = []
                week = (assistant.build_agenda(conn, local.date(),
                                               local.date() + timedelta(days=7),
                                               "in the next 7 days", local)
                        if route.personal else None)
                if week is not None and (week.due or week.overdue):
                    # the user's own dated items come first, as options
                    mine = assistant.agenda_answer(week).text
                    head = prefix or "I couldn't get a reliable answer to that."
                    prefix = f"{head}\n{mine}"
                final = fallback_answer(steps, ctx, brevity, prefix=prefix)
                final.model = deep.model
                final.error = deep.error
                final.thinking_s = deep.thinking_s
                final.first_token_s = None
                final_start = None
        final.intent, final.route_by = route.intent, route.by
        final.escalated = True
        final.escalation_reasons = list(reasons)
        if final is not deep and final.thinking_s is None:
            final.thinking_s = deep.thinking_s
        return _finish(conn, session_id, final, t0, final_start, phase)
    finally:
        if own_deep:
            deep_client.close()


def _direct_answer(conn: sqlite3.Connection, question: str, now: datetime,
                   route: Any) -> Answer | None:
    """The answer code gives without a model, or None (the model answers)."""
    if route.intent == "agenda":
        return assistant.agenda_answer(assistant.build_agenda(
            conn, route.start, route.end, route.span, now, project=route.project))
    if route.intent == "capture":
        return assistant.capture_answer(route.capture_text)
    if route.intent == "recall" and not assistant.search_memory(conn, question, now):
        return assistant.recall_gap_answer(question)
    return None


def _finish(conn: sqlite3.Connection, session_id: str | None, ans: Answer, t0: float,
            start: float | None, phase: Callable[..., None]) -> Answer:
    if ans.first_token_s is not None and start is not None:
        ans.first_token_s = start + ans.first_token_s
    ans.duration = time.monotonic() - t0
    if session_id and (ans.error is None or ans.tier == "fallback"):
        chat.save_message(conn, session_id, "assistant", ans.text, {
            "sources": [asdict(s) for s in ans.sources],
            "cited_sources": [asdict(s) for s in ans.cited_sources],
            "notes": ans.notes, "unverified_numbers": ans.unverified_numbers,
            "model": ans.model, "duration": round(ans.duration, 2), "tier": ans.tier,
            "escalated": ans.escalated, "escalation_reasons": ans.escalation_reasons,
            "thinking_s": round(ans.thinking_s, 2) if ans.thinking_s is not None else None})
    phase("done", tier=ans.tier, escalated=ans.escalated)
    return ans

