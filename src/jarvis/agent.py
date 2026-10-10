"""The bounded tool-using assistant for open-ended requests (no Qt).

Used for requests that need reasoning over several kinds of data (planning,
organizing, scheduling, unclear asks); lookups keep their direct paths
(jarvis.intent / jarvis.assistant). One turn is a short loop:

1. the model sees the request, today's dates and the tool schemas
   (jarvis.tools) and either calls tools or answers;
2. ``tools.run_tool`` executes each call (the model never touches the
   database) and the result, errors included, goes back to the model;
3. it stops on an answer, on ``ask_user`` (the turn ends with that question),
   or on a limit: at most ``MAX_MODEL_CALLS`` model calls (the last one has no
   tools, so the model must answer), ``MAX_TOOL_CALLS`` tool calls and
   ``TIME_BUDGET_S``. Hitting a limit is said in the answer, never hidden.

Before the loop the turn's goal is fixed (``Goal``): what the user wants back
("schedule": dates for tasks, "plan": advice, "answer") and the days it is
about, from a schema-constrained model call (``classify_goal``), or carried
over from the clarifying question Jarvis asked last (stored in that message's
metadata as ``pending_goal``). For schedules and plans the records every such
answer needs are fetched by code first (``required_context``: the agenda for
the period and the 7 days after it, the open items), so a deadline can't be
missed because the model looked at the wrong days. A "schedule" turn has to end
with a proposal or one question: prose gets one reminder; a question asked in
prose becomes the clarification; otherwise the answer says no schedule was
proposed. Plans and schedules that leave out a known deadline get a note.

The answer is checked like any chat answer (``chat.validate_answer``) against
what the tools returned: its references must be records a tool produced. A
plan is words; dates and new items only exist as a ``actions.Proposal``
(``Answer.action``), applied by the user's Apply. A reply that claims a change
was made gets a note saying nothing was saved.
"""
from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any

from jarvis import chat, intent, tools
from jarvis.chat import Answer, Context
from jarvis.config import Config
from jarvis.facts import tokens
from jarvis.ollama import OllamaError

MAX_MODEL_CALLS = 5
MAX_TOOL_CALLS = 8
TIME_BUDGET_S = 150.0
AGENT_INTENTS = frozenset({"planning", "mixed"})

AGENT_PROMPT = """\
You are Jarvis, the user's local personal assistant. You can read the user's data only \
through the tools; you cannot change anything. Work in short steps: call the tools you need \
for this request, read their results, call more only if something important is still \
missing, then answer.

What is true:
- The user's own items (tasks, ideas, reminders) and their dates are their commitments. \
Only an item's own date puts it on a day.
- GitHub issues, milestones and suggested next steps are engineering BACKLOG. They are \
context, never the user's commitments. Mention them as options only when the request \
is about that work, and never put them on a day unless the user asked to include it.
- Jarvis has no calendar. Free time, appointments and how long a task takes are unknown \
unless the user said them or an item records them. Never invent free time or durations.

How to help:
- The conversation already holds the agenda for the period (and the week after it) and the \
open items, fetched by Jarvis. Use them; call tools only for what is still missing.
- "These tasks" or "my tasks" without names means the open undated tasks plus those due in the \
period; say that you took them as the set (an assumption), don't ask which ones.
- Planning is reasoning: compare options, point out deadlines, conflicts and trade-offs, and \
say what you would suggest and why. A plan in words saves nothing.
- Scheduling means giving items dates or reminders. Only when the user asks for that, call \
propose_changes. It saves nothing either: the user approves it with Apply. Leave an item \
undated (list it under unscheduled, with the reason) rather than forcing it into a slot.
- If a fact you can't look up would change the dates (usually when the user is free), call \
ask_user with one specific question about it instead of guessing. Never ask about things the \
tools can tell you. If nothing essential is missing, propose and state your assumptions.
- When the user answers your question, continue the original request with their answer.
- If you suggest an order of work, give each step's reason from the records or from what the \
user said (a deadline, something overdue, a stated constraint). Call it a suggestion; don't \
call items priorities, and don't invent durations, dependencies or urgency.
- Never say a change was made, saved, scheduled or added. Say "proposed" and that Apply \
saves it.
- Never decide for the user; the choice is theirs.

Answer: the useful result first, concise (at most 10 lines; short lists are fine). Keep \
facts (from the tools), assumptions and suggestions visibly apart. Refer to items by title \
and issues as #N; never write the square-bracket references. Copy dates from the tool \
results or the date list; do no date arithmetic. End with one last line: "SOURCES: " then \
the refs you used (as the tools gave them, for example item:12, #31, milestone:Core demo), \
or "SOURCES: none".
"""

_CLAIMED_RE = re.compile(
    r"\b(?:i(?:'ve| have)|i)\s+(?:now\s+)?(?:scheduled|added|saved|set|updated|moved|created|"
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
    "schedule": "a schedule: dates or reminders for tasks, as a proposal I approve "
                "(or one question if something essential is missing)",
    "plan": "a plan or advice in words; dates only if I ask for them",
    "answer": "an answer to my question",
}
SCHEDULE_NUDGE = ("I asked for a schedule. Do one of these now: call propose_changes with "
                  "dates for the tasks (state your assumptions), or, if one fact you can't "
                  "look up would change the dates, call ask_user with that one question. "
                  "Don't answer with a plan in prose.")
REQUIRED_AHEAD_DAYS = 7     # deadlines after the period still shape a plan for it


@dataclass(frozen=True)
class Goal:
    """What the user wants back from this turn and the days it is about."""
    outcome: str             # "schedule" | "plan" | "answer"
    start: date
    end: date
    request: str = ""        # the original request (a follow-up keeps it)
    asked: str = ""          # Jarvis's last reply in this goal (often a question)

    def to_dict(self) -> dict[str, str]:
        return {"outcome": self.outcome, "start": self.start.isoformat(),
                "end": self.end.isoformat(), "request": self.request, "asked": self.asked}

    @classmethod
    def from_dict(cls, d: Any) -> Goal | None:
        try:
            g = cls(str(d["outcome"]), date.fromisoformat(d["start"]),
                    date.fromisoformat(d["end"]), str(d.get("request") or ""),
                    str(d.get("asked") or ""))
        except (KeyError, TypeError, ValueError):
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


def required_context(ctx: tools.ToolContext, goal: Goal, msgs: list[dict[str, Any]]) -> None:
    """Fetch what every plan or schedule needs before the model starts: the agenda
    for the period and the week after it (deadlines, overdue items) and the open
    items; project status too when the user asked for project work. Added to the
    conversation as tool results, so the model reads them like its own calls."""
    start = max(goal.start, ctx.now.date())
    ahead = min(goal.end + timedelta(days=REQUIRED_AHEAD_DAYS), start + timedelta(days=31))
    calls = [("get_agenda", {"start": start.isoformat(), "end": ahead.isoformat()}),
             ("list_items", {"limit": tools.LIST_LIMIT})]
    if ctx.allow_backlog:
        calls.append(("project_status", {}))
    msgs.append({"role": "assistant", "content": "",
                 "tool_calls": [{"function": {"name": n, "arguments": a}} for n, a in calls]})
    for name, args in calls:
        msgs.append({"role": "tool", "tool_name": name,
                     "content": tools.run_tool(ctx, name, args, required=True)})


def missed_deadlines(conn: sqlite3.Connection, goal: Goal, now: datetime, text: str,
                     proposal: Any) -> list[str]:
    """Known deadlines (due in the period or the week after it, or overdue) that
    the answer and its proposal don't mention, as "Title (due Mon Oct 12)"."""
    from jarvis import assistant

    start = max(goal.start, now.date())
    ag = assistant.build_agenda(conn, start, goal.end + timedelta(days=REQUIRED_AHEAD_DAYS),
                                "", now)
    said = set(tokens(text))
    if proposal is not None:
        for c in proposal.changes:
            said |= set(tokens(c.title))
        for u in proposal.unscheduled:
            said |= set(tokens(u.get("title") or ""))
    out = []
    for e in [*ag.overdue, *ag.due]:
        words = set(tokens(e.title))
        head = set(tokens(re.split(r"[:(\-–—]", e.title)[0]))   # "Lab report: op-amp ..."
        if words and len(words & said) / len(words) < 0.6 and not (head and head <= said):
            when = "overdue" if e.late_days else f"due {e.day:%a %b} {e.day.day}"
            out.append(f"{e.title} ({when})")
    return out


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
    ``goal``: carried over from Jarvis's clarifying question (None: classified here)."""
    t0 = time.monotonic()
    question = question.strip()
    followup = goal is not None
    if goal is None:
        goal = classify_goal(client, question, history, now)
    request = goal.request if followup else question
    ctx = tools.ToolContext(conn, cfg, now, request,
                            allow_backlog=wants_backlog(conn, f"{request} {question}"))
    model = getattr(client, "model", None)

    def phase(event: str, **info: Any) -> None:
        if on_phase:
            on_phase(event, info)

    msgs: list[dict[str, Any]] = [{"role": "system", "content": AGENT_PROMPT}]
    msgs += [{"role": r, "content": c} for r, c in chat.trim_history(history)]
    said = (f"My request was: {goal.request}\nYou replied: {goal.asked}\nNow I say: {question}"
            if followup else question)
    msgs.append({"role": "user", "content": (
        f"{said}\n\n(Now: {now:%A} {now.date().isoformat()} {now:%H:%M}. "
        f"Dates: {tools.week_dates(now)}. What I want back: {OUTCOME_WORDS[goal.outcome]})")})
    if goal.outcome != "answer":
        required_context(ctx, goal, msgs)
    nudged = False
    final: str | None = None
    stopped: str | None = None
    tool_calls = 0
    error: str | None = None
    for step in range(max_model_calls):
        out_of_time = time.monotonic() - t0 > time_budget_s
        last = step == max_model_calls - 1 or tool_calls >= max_tool_calls or out_of_time
        phase("agent_step", step=step + 1, tools=not last)
        if last and step:
            msgs.append({"role": "user", "content": "Answer me now from what the tools "
                         "returned; no more tools. End with the SOURCES line."})
        try:
            resp = client.chat_tools(msgs, None if last else tools.schemas())
        except OllamaError as e:
            error = str(e)
            break
        calls = resp.get("tool_calls") or []
        if not calls or last:
            final = resp.get("content") or ""
            if last and calls:
                stopped = "the step limit"
            if (goal.outcome == "schedule" and ctx.proposal is None and not calls
                    and not nudged and not last):
                # a schedule was asked for: one reminder to propose or ask, not prose
                nudged = True
                msgs.append({"role": "assistant", "content": final})
                msgs.append({"role": "user", "content": SCHEDULE_NUDGE})
                final = None
                continue
            break
        msgs.append({"role": "assistant", "content": resp.get("content") or "",
                     "tool_calls": [{"function": {"name": c["name"],
                                                  "arguments": c["arguments"]}}
                                    for c in calls]})
        for c in calls:
            if tool_calls >= max_tool_calls:
                result = '{"error": "tool limit reached: answer with what you have"}'
            else:
                tool_calls += 1
                phase("tool", name=c["name"])
                result = tools.run_tool(ctx, c["name"], c["arguments"])
            msgs.append({"role": "tool", "tool_name": c["name"], "content": result})
            if ctx.clarification:
                break
        if ctx.clarification:
            break
        if time.monotonic() - t0 > time_budget_s and step < max_model_calls - 1:
            stopped = "the time limit"   # the next step must answer without tools
    duration = time.monotonic() - t0
    if (ctx.clarification is None and ctx.proposal is None and goal.outcome == "schedule"
            and final and _asks(final)):
        # asked in prose instead of through ask_user: it is still the clarification
        ctx.clarification = {"question": _question_of(final), "missing": []}
    if ctx.clarification is not None:
        q = _INLINE_REF_RE.sub("", _SPECIAL_TOKEN_RE.sub("", ctx.clarification["question"]))
        ctx.clarification["question"] = q.strip()

    if ctx.clarification is not None:
        ans = Answer(text=ctx.clarification["question"], sources=[], unverified_numbers=[],
                     notes=[], model=model, duration=duration, tier="agent",
                     action={"kind": "clarify", **ctx.clarification,
                             "goal": replace(goal, request=request,
                                             asked=ctx.clarification["question"]).to_dict()})
    elif final is None:
        why = f"the model failed ({error})" if error else "the step limit"
        text = (chat.UNREACHABLE.format(url=cfg.ollama_url) if error and "cannot reach" in error
                else STOPPED.format(why=why))
        ans = Answer(text=text, sources=[], unverified_numbers=[], notes=[], model=model,
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
    if ctx.proposal is not None and ctx.clarification is None:
        ans.action = {"kind": "proposal", "proposal": ctx.proposal.to_dict()}
    if _CLAIMED_RE.search(ans.text):
        ans.notes.append(NOT_SAVED_NOTE)
    if ctx.proposal is None and _OFFERS_APPLY_RE.search(ans.text):
        why = f" ({'; '.join(ctx.refused[:3])})" if ctx.refused else ""
        ans.notes.append(f"Note: no changes were proposed{why}, so there is nothing to apply.")
    if (goal.outcome == "schedule" and ctx.proposal is None and ctx.clarification is None
            and final is not None and not any("nothing to apply" in n for n in ans.notes)):
        why = f" ({'; '.join(ctx.refused[:2])})" if ctx.refused else ""
        ans.notes.append(f"Note: no schedule was proposed{why}; nothing can be applied yet.")
    left_out = (missed_deadlines(conn, goal, now, ans.text, ctx.proposal)
                if goal.outcome != "answer" and ctx.clarification is None and final is not None
                else [])
    if left_out:
        ans.notes.append("Also on record and not mentioned: " + "; ".join(left_out) + ".")
    ans.intent = route.intent if route is not None else "planning"
    ans.route_by = route.by if route is not None else ""
    ans.steps = list(ctx.calls)
    ans.goal = goal.outcome
    if ans.action is None or ans.action.get("kind") != "clarify":
        # the next reply continues this goal (revising the plan or the proposal)
        ans.action = {**(ans.action or {"kind": "none"}),
                      "goal": replace(goal, request=request, asked=ans.text[:400]).to_dict()}
    return ans


def agent_client(cfg: Config, deep_client: Any = None) -> Any:
    """The client for the loop: ``cfg.agent_model`` if set (a new client), else the
    deep tier's (gemma4:26b by default)."""
    if cfg.agent_model and cfg.agent_model != getattr(deep_client, "model", None):
        from jarvis.ollama import OllamaClient
        return OllamaClient(cfg.ollama_url, cfg.agent_model, keep_alive="10m")
    return deep_client
