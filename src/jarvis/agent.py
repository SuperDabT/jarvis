"""The bounded tool-using assistant for open-ended requests (no Qt).

Jarvis is the user's race team: it watches their tasks, deadlines and projects,
says what deserves attention and why, and helps them use their time; the user
decides and does the deep work (specialist work, such as a study plan or code,
belongs to their other tools). Lookups keep their direct paths (jarvis.intent /
jarvis.assistant); this module handles requests that need reasoning.

A turn first fixes its goal (``Goal``): what the user wants back ("schedule":
dates or reminders for tasks; "plan": how to use a period; "answer") and the
days, from one schema-constrained call (``classify_goal``) or carried over from
Jarvis's previous turn in the conversation (``pending_goal`` in that message's
metadata), with everything the user said for it so far.

Plans and schedules are structured. Code decides which items the plan must
account for (``planning.build_brief``: overdue, due in the period or the week
after it, undated) and gives them to the model as a brief; the model answers by
calling ``submit_plan`` with a decision per item id, the availability the user
stated (quoted), suggestions that aren't tasks and assumptions;
``planning.check_plan`` rejects anything that drops an item, moves a deadline,
plans work after a deadline, or uses a time the user didn't give, and the model
fixes it. The accepted plan ends the turn and is rendered by code
(``planning.render``), so facts, proposals and suggestions are labelled by what
they are; a schedule's changes become an ``actions.Proposal`` for Apply. If no
valid plan comes back, the answer lists what is on record (``render_brief``).
``ask_user`` is offered only for schedules, once per goal; plans never ask,
they say what is provisional.

Other requests ("answer") get the read tools and answer in prose, checked like
any chat answer (``chat.validate_answer``) against what the tools returned.

The loop is bounded: at most ``MAX_MODEL_CALLS`` model calls (the last one
without tools), ``MAX_TOOL_CALLS`` tool calls and ``TIME_BUDGET_S``; a prose
reply to a plan or schedule request gets one reminder. Nothing here writes.
"""
from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any

from jarvis import chat, intent, planning, tools
from jarvis.chat import Answer, Context, Source
from jarvis.config import Config
from jarvis.ollama import OllamaError

MAX_MODEL_CALLS = 5
MAX_TOOL_CALLS = 8
TIME_BUDGET_S = 150.0
AGENT_INTENTS = frozenset({"planning", "mixed"})
SAID_KEPT = 6               # user messages kept with a goal

AGENT_PROMPT = """\
You are Jarvis, the user's personal operations assistant, like a race team for their life: \
you watch their tasks, deadlines and projects, notice what deserves attention, explain why, \
and help them use their time. They decide and do the deep work; detailed study plans, code \
and other specialist work belong to their other tools, not to you.

You read the user's data only through the tools; you cannot change anything.

What is true:
- The user's own items and their dates are their commitments; a due date is a deadline. \
Deadlines are fixed: never move one to make a plan easier.
- GitHub issues, milestones and suggested next steps are engineering BACKLOG: context, never \
commitments, and never planned unless the user asked for project work.
- Jarvis has no calendar. Free time, appointments and how long things take are unknown unless \
the user said them. Never invent availability, times or durations.
- Recommendations are yours, not facts: give each its reason (a deadline, something overdue, \
a conflict, what the user said) and leave the choice to the user.
"""

ANSWER_RULES = """
Answer the request: call the tools you need, then reply concisely (at most 10 lines), the \
useful result first; keep facts, assumptions and suggestions apart. Never say a change was \
made. Refer to items by title and issues as #N; no square-bracket references in the text. \
End with one last line: "SOURCES: " then the refs you used (item:12, #31, ...) or \
"SOURCES: none".
"""

PLAN_RULES = """
This is a plan or schedule request. Jarvis has put the items in scope in the brief below \
(the set is decided: don't ask which tasks or deadlines). Answer by calling submit_plan once, \
with a decision for EVERY item_id:
- work_on: a day to work on it, within the item's allowed days in the brief. Fixed \
commitments (exams, appointments) are facts: keep time before them (work_on the exam item \
means preparing for it) and never plan over them.
- If the important items can't all get time, say so: leave the less urgent ones with the \
reason; Jarvis lists them as not fitted.
- set_due: only when scheduling, to give an undated item a date.
- leave / exclude: with the reason (already handled, not this period, the user's words).
- availability: only windows the user stated, each with their exact words as quote. With \
none, plan days only (no times): the plan is provisional, say so in the summary. Never write \
a time of day or a duration the user didn't give, anywhere.
- suggestions: things worth reserving time for that aren't tasks (e.g. exam preparation), \
as one line each; no detailed study plans or task breakdowns.
- summary: 2-3 sentences on what matters most in the period and why.
Read other tools only if the brief is missing something you need. If submit_plan returns \
errors, fix them and call it again with the whole plan.
"""
ASK_RULE = """\
- If one fact you can't look up (usually when the user is free) is essential to the \
schedule, call ask_user with that one question instead of guessing; otherwise plan. Never \
ask for what the user already said in this conversation or what the brief shows.
"""

_CLAIMED_RE = re.compile(
    r"\b(?:i(?:'ve| have)|i)\s+(?:\w+\s+)?(?:scheduled|added|saved|set|updated|moved|created|"
    r"changed|put)\b|\b(?:has|have)\s+been\s+(?:scheduled|added|saved|set|updated|moved|"
    r"created)\b", re.I)
_INLINE_REF_RE = re.compile(r"\s*[\[(](?:item|chat|milestone|card|check|fact):[^\])]*[\])]")
# chat-template tokens a model sometimes leaks into its text ("<channel|>", "<|"|>")
_SPECIAL_TOKEN_RE = re.compile(r"<\|[^<>\n]{0,24}?\|?>|<[a-z_]{2,24}\|>")
# an answer that presents changes as ready to apply ("I've proposed ...", "click Apply"),
# as opposed to offering to propose some
_OFFERS_APPLY_RE = re.compile(
    r"\b(?:click|press|hit|say|use|tap)\s+[*\"“]*apply\b|\bapply\s+saves\b|"
    r"\bi(?:'ve|\s+have)\s+proposed\b|\bproposed\s+(?:changes?|dates?|schedule)\b|"
    r"\bhave\s+been\s+proposed\b", re.I)
NOT_SAVED_NOTE = "Note: nothing has been saved. Changes only happen when you press Apply."
STOPPED = ("I stopped before finishing: {why}. Here is what I could check so far; ask again "
           "with a narrower request if this isn't enough.")

OUTCOME_WORDS = {
    "schedule": "a schedule: dates or reminders for tasks, as a proposal I approve",
    "plan": "a plan for how to use the period; nothing saved",
    "answer": "an answer to my question",
}
PLAN_NUDGE = ("Answer with submit_plan: one decision for every item_id in the brief, with "
              "reasons. Don't answer in prose.")
PLAN_NUDGE_ASK = PLAN_NUDGE[:-1] + (", unless one fact you can't look up is essential: then "
                                    "call ask_user with that one question.")


@dataclass(frozen=True)
class Goal:
    """What the user wants back, the days it is about, and what they said for it."""
    outcome: str             # "schedule" | "plan" | "answer"
    start: date
    end: date
    request: str = ""        # the original request (a follow-up keeps it)
    asked: str = ""          # Jarvis's last reply in this goal (often a question)
    said: tuple[str, ...] = ()   # the user's messages for this goal, oldest first

    def to_dict(self) -> dict[str, Any]:
        return {"outcome": self.outcome, "start": self.start.isoformat(),
                "end": self.end.isoformat(), "request": self.request, "asked": self.asked,
                "said": list(self.said[-SAID_KEPT:])}

    @classmethod
    def from_dict(cls, d: Any) -> Goal | None:
        try:
            said = tuple(str(x) for x in d.get("said") or [] if isinstance(x, str))
            g = cls(str(d["outcome"]), date.fromisoformat(d["start"]),
                    date.fromisoformat(d["end"]), str(d.get("request") or ""),
                    str(d.get("asked") or ""), said)
        except (KeyError, TypeError, ValueError, AttributeError):
            return None
        return g if g.outcome in OUTCOME_WORDS and g.start <= g.end else None


GOAL_SYSTEM = """\
Read the user's latest message (with the conversation before it) and say what they want \
back from their personal assistant, and which days it is about. Reply with JSON.
outcome:
- schedule: dates, times or reminders assigned to their tasks (scheduling, fitting tasks \
into days, or answering a question about their availability for that)
- plan: organizing, advice, choosing what to work on, without setting dates
- answer: a question to answer
start, end: the days it is about, YYYY-MM-DD (a weekend: Saturday to Sunday). If no days \
are named, start today and end 6 days later.
"""
GOAL_SCHEMA = {"type": "object", "properties": {
    "outcome": {"type": "string", "enum": list(OUTCOME_WORDS)},
    "start": {"type": "string"}, "end": {"type": "string"}},
    "required": ["outcome", "start", "end"]}


def classify_goal(client: Any, question: str, history: Sequence[tuple[str, str]],
                  now: datetime) -> Goal:
    """The turn's goal from the model (``chat_json``); "plan" over the named days
    (or the coming week) when it can't say."""
    today = now.date()
    start, end, _ = intent.timeframe(question, today) or (
        today, today + timedelta(days=6), "")
    fallback = Goal("plan", start, end, question)
    if not hasattr(client, "chat_json"):
        return fallback
    convo = "\n".join(f"{r}: {c}" for r, c in list(history)[-4:])
    user = (f"Today: {now:%A} {today.isoformat()}. Dates: {tools.week_dates(now)}.\n"
            + (f"Conversation so far:\n{convo}\n" if convo else "")
            + f"Latest message: {question}")
    try:
        got = client.chat_json(GOAL_SYSTEM, user, GOAL_SCHEMA)
        g = Goal(str(got["outcome"]), date.fromisoformat(str(got["start"])[:10]),
                 date.fromisoformat(str(got["end"])[:10]), question)
    except Exception:  # noqa: BLE001 - any failure means the fallback goal
        return fallback
    if g.outcome not in OUTCOME_WORDS or g.end < g.start or g.start < today - timedelta(
            days=1) or (g.end - g.start).days > 31:
        return replace(fallback, outcome=g.outcome if g.outcome in OUTCOME_WORDS else "plan")
    return g


def _asks(text: str | None) -> bool:
    """Does a reply end by asking the user something?"""
    body, _ = chat.split_sources(text or "")
    return body.rstrip().rstrip("*_ ").endswith("?")


def _question_of(text: str) -> str:
    """The question a prose reply ends with (its last sentence or two)."""
    body, _ = chat.split_sources(_SPECIAL_TOKEN_RE.sub("", text or ""))
    body = _INLINE_REF_RE.sub("", body).strip()
    parts = re.split(r"(?<=[.!?])\s+", body)
    tail = " ".join(parts[-2:]) if len(parts) > 1 and len(parts[-1]) < 60 else parts[-1]
    return tail.strip().lstrip("*-• ").strip()


def wants_backlog(conn: sqlite3.Connection, question: str) -> bool:
    """Did the user's own request ask to include project work? (a project's name,
    an issue number, or the words project / issue / milestone)"""
    return bool(intent._project_slug(conn, question)) or bool(re.search(
        r"#\d+|\bprojects?\b|\bissues?\b|\bmilestones?\b|\bgithub\b", question, re.I))


def _validation_context(conn: sqlite3.Connection, ctx: tools.ToolContext) -> Context:
    issues = {r[0] for r in conn.execute("SELECT number FROM issues")}
    shas = [r[0] for r in conn.execute("SELECT sha FROM commits")]
    return Context(text="\n".join(ctx.evidence) + "\n" + tools.week_dates(ctx.now),
                   refs=dict(ctx.refs), issue_numbers=issues, commit_shas=shas,
                   today=ctx.now.date())


def run_agent(conn: sqlite3.Connection, cfg: Config, question: str,
              history: Sequence[tuple[str, str]], client: Any, now: datetime, *,
              route: Any = None, on_phase: Callable[[str, dict], None] | None = None,
              max_model_calls: int = MAX_MODEL_CALLS, max_tool_calls: int = MAX_TOOL_CALLS,
              time_budget_s: float = TIME_BUDGET_S, goal: Goal | None = None) -> Answer:
    """One assistant turn with tools. Never raises for model or tool failures.
    ``goal``: carried over from Jarvis's previous turn (None: classified here)."""
    t0 = time.monotonic()
    question = question.strip()
    followup = goal is not None
    if goal is None:
        goal = classify_goal(client, question, history, now)
    request = goal.request if followup else question
    said = (*(goal.said or (goal.request,)), question) if followup else (question,)
    goal = replace(goal, request=request, said=tuple(said)[-SAID_KEPT:])
    planning_turn = goal.outcome in ("plan", "schedule")
    ctx = tools.ToolContext(conn, cfg, now, request,
                            allow_backlog=wants_backlog(conn, " ".join(goal.said)))
    ctx.user_text = "\n".join(goal.said)
    ctx.scheduling = goal.outcome == "schedule"
    # one clarifying question per goal, and only for schedules: a plan says what's provisional
    can_ask = goal.outcome != "plan" and not (followup and _asks(goal.asked))
    names = [*tools.READ_TOOLS, "submit_plan" if planning_turn else "propose_changes"]
    if can_ask:
        names.append("ask_user")
    model = getattr(client, "model", None)

    def phase(event: str, **info: Any) -> None:
        if on_phase:
            on_phase(event, info)

    system = AGENT_PROMPT + (PLAN_RULES + (ASK_RULE if can_ask else "") if planning_turn
                             else ANSWER_RULES)
    msgs: list[dict[str, Any]] = [{"role": "system", "content": system}]
    msgs += [{"role": r, "content": c} for r, c in chat.trim_history(history)]
    text = (f"My request was: {goal.request}\nYou replied: {goal.asked}\nNow I say: {question}"
            if followup else question)
    extra = ""
    if planning_turn:
        ctx.brief = planning.build_brief(conn, goal.start, goal.end, now.date())
        for b in ctx.brief.items.values():
            ctx.refs[f"item:{b.id}"] = Source(f"item:{b.id}", b.title)
        brief_text = "\n".join(ctx.brief.lines())
        ctx.evidence.append(brief_text)
        ctx.calls.append("jarvis:brief")
        extra = f"\n\nBRIEF (from Jarvis's records):\n{brief_text}"
    msgs.append({"role": "user", "content": (
        f"{text}\n\n(Now: {now:%A} {now.date().isoformat()} {now:%H:%M}. "
        f"Dates: {tools.week_dates(now)}. What I want back: {OUTCOME_WORDS[goal.outcome]}){extra}")})
    if planning_turn and ctx.allow_backlog:
        result = tools.run_tool(ctx, "project_status", {}, required=True)
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "project_status", "arguments": {}}}]})
        msgs.append({"role": "tool", "tool_name": "project_status", "content": result})

    nudged = False
    final: str | None = None
    stopped: str | None = None
    tool_calls = 0
    error: str | None = None
    for step in range(max_model_calls):
        out_of_time = time.monotonic() - t0 > time_budget_s
        last = step == max_model_calls - 1 or tool_calls >= max_tool_calls or out_of_time
        if planning_turn and step == max_model_calls - 1:
            last = False          # a plan still needs submit_plan on the last step
            names = ["submit_plan"]
        phase("agent_step", step=step + 1, tools=not last)
        if last and step:
            msgs.append({"role": "user", "content": "Answer me now from what the tools "
                         "returned; no more tools. End with the SOURCES line."})
        try:
            resp = client.chat_tools(msgs, None if last else tools.schemas(names))
        except OllamaError as e:
            error = str(e)
            break
        calls = resp.get("tool_calls") or []
        if not calls or last:
            final = resp.get("content") or ""
            if last and calls:
                stopped = "the step limit"
            if planning_turn and not calls and not nudged and step < max_model_calls - 1:
                nudged = True
                problem = (planning.question_problem(ctx.brief, ctx.user_text,
                                                     _question_of(final))
                           if _asks(final) else None)
                msgs.append({"role": "assistant", "content": final})
                msgs.append({"role": "user", "content": f"Don't ask: {problem}. {PLAN_NUDGE}"
                             if problem else PLAN_NUDGE_ASK if can_ask else PLAN_NUDGE})
                final = None
                continue
            break
        msgs.append({"role": "assistant", "content": resp.get("content") or "",
                     "tool_calls": [{"function": {"name": c["name"],
                                                  "arguments": c["arguments"]}}
                                    for c in calls]})
        for c in calls:
            if c["name"] not in names:
                result = (f'{{"error": "{c["name"]} isn\'t available now; use one of '
                          f'{", ".join(names)}"}}')
            elif tool_calls >= max_tool_calls and c["name"] != "submit_plan":
                result = '{"error": "tool limit reached: answer with what you have"}'
            else:
                tool_calls += 1
                phase("tool", name=c["name"])
                result = tools.run_tool(ctx, c["name"], c["arguments"])
            msgs.append({"role": "tool", "tool_name": c["name"], "content": result})
            if ctx.clarification or ctx.plan:
                break
        if ctx.clarification or ctx.plan:
            break
        if time.monotonic() - t0 > time_budget_s and step < max_model_calls - 1:
            stopped = "the time limit"
    duration = time.monotonic() - t0
    if (ctx.clarification is None and ctx.plan is None and can_ask and final
            and _asks(final) and (planning_turn or goal.outcome == "schedule")):
        # asked in prose instead of through ask_user: it is still the clarification,
        # unless it asks for what the user already said or the records hold
        q = _question_of(final)
        problem = planning.question_problem(ctx.brief, ctx.user_text, q)
        if problem:
            ctx.refused = [f"ask_user: {problem}"]
        else:
            ctx.clarification = {"question": q, "missing": []}
    if ctx.clarification is not None:
        q = _INLINE_REF_RE.sub("", _SPECIAL_TOKEN_RE.sub("", ctx.clarification["question"]))
        ctx.clarification["question"] = q.strip()

    if ctx.clarification is not None:
        ans = Answer(text=ctx.clarification["question"], sources=[], unverified_numbers=[],
                     notes=[], model=model, duration=duration, tier="agent",
                     action={"kind": "clarify", **ctx.clarification})
    elif planning_turn:
        ans = _plan_answer(ctx, error, stopped, cfg)
        ans.model, ans.duration = model, duration
    elif final is None:
        why = f"the model failed ({error})" if error else "the step limit"
        msg = (chat.UNREACHABLE.format(url=cfg.ollama_url) if error and "cannot reach" in error
               else STOPPED.format(why=why))
        ans = Answer(text=msg, sources=[], unverified_numbers=[], notes=[], model=model,
                     duration=duration, tier="agent", error=error)
    else:
        ans = chat.validate_answer(chat.strip_think(final), _validation_context(conn, ctx),
                                   question, history)
        ans.text = _INLINE_REF_RE.sub("", ans.text)   # refs belong on the SOURCES line
        ans.text = _SPECIAL_TOKEN_RE.sub("", ans.text).strip()
        ans.model, ans.duration, ans.tier = model, duration, "agent"
        if stopped:
            ans.notes.insert(0, f"Note: stopped at {stopped}; this answer may be incomplete.")
        if not ans.text.strip():
            ans.text = STOPPED.format(why="the model gave no answer")
        if ctx.proposal is None and _OFFERS_APPLY_RE.search(ans.text):
            why = f" ({'; '.join(ctx.refused[:3])})" if ctx.refused else ""
            ans.notes.append(f"Note: no changes were proposed{why}, so there is nothing to "
                             "apply.")
    if ctx.proposal is not None and ctx.clarification is None:
        ans.action = {"kind": "proposal", "proposal": ctx.proposal.to_dict()}
    if _CLAIMED_RE.search(ans.text):
        ans.notes.append(NOT_SAVED_NOTE)
    ans.intent = route.intent if route is not None else "planning"
    ans.route_by = route.by if route is not None else ""
    ans.steps = list(ctx.calls)
    ans.goal = goal.outcome
    # the next reply continues this goal (an answer, a correction, a new constraint)
    ans.action = {**(ans.action or {"kind": "none"}),
                  "goal": replace(goal, asked=ans.text[:400]).to_dict()}
    return ans


def _plan_answer(ctx: tools.ToolContext, error: str | None, stopped: str | None,
                 cfg: Config) -> Answer:
    """A plan or schedule turn's answer, written by code from the accepted plan (or,
    without one, from the brief), so nothing in scope can go missing."""
    brief = ctx.brief
    assert brief is not None
    notes: list[str] = []
    if ctx.plan is not None:
        text = planning.render(brief, ctx.plan, ctx.scheduling)
        refs = [f"item:{d.item_id}" for d in ctx.plan.decisions
                if d.action in ("set_due", "work_on")]
        refs += [f"item:{b.id}" for b in brief.items.values() if b.due and
                 f"item:{b.id}" not in refs]
    else:
        if error and "cannot reach" in error:
            why = "the local model isn't reachable"
        elif error:
            why = f"the model failed: {error}"
        elif ctx.refused and all(r.startswith("ask_user:") for r in ctx.refused):
            why = "it asked for something already known instead of planning"
        elif ctx.refused:
            why = "the plan it made broke these rules: " + "; ".join(ctx.refused[:3])
        else:
            why = f"no plan came back{' before ' + stopped if stopped else ''}"
        text = planning.render_brief(brief, why)
        refs = [f"item:{b.id}" for b in brief.items.values()]
    sources = [ctx.refs[r] for r in refs if r in ctx.refs]
    return Answer(text=text, sources=sources, unverified_numbers=[], notes=notes, model=None,
                  duration=0.0, tier="agent", error=error if ctx.plan is None else None,
                  cited_sources=sources[:chat.CITED_SOURCES_MAX],
                  cited_more_count=max(0, len(sources) - chat.CITED_SOURCES_MAX))


def agent_client(cfg: Config, deep_client: Any = None) -> Any:
    """The client for the loop: ``cfg.agent_model`` if set (a new client), else the
    deep tier's (gemma4:26b by default)."""
    if cfg.agent_model and cfg.agent_model != getattr(deep_client, "model", None):
        from jarvis.ollama import OllamaClient
        return OllamaClient(cfg.ollama_url, cfg.agent_model, keep_alive="10m")
    return deep_client
