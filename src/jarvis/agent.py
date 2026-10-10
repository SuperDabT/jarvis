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
from datetime import datetime
from typing import Any

from jarvis import chat, intent, tools
from jarvis.chat import Answer, Context
from jarvis.config import Config
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
- For anything about a stretch of time (today, a weekend, a week), check get_agenda for those dates: it has the deadlines and overdue items.
- Planning is reasoning: compare options, point out deadlines, conflicts and trade-offs, and \
say what you would suggest and why. A plan in words saves nothing.
- Scheduling means giving items dates or reminders. Only when the user asks for that, call \
propose_changes. It saves nothing either: the user approves it with Apply. Leave an item \
undated (list it under unscheduled, with the reason) rather than forcing it into a slot.
- If missing information (free time, a constraint, which item is meant) would change the \
result, call ask_user with one short question instead of guessing. If it would not, give a \
draft and state your assumptions.
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
              time_budget_s: float = TIME_BUDGET_S) -> Answer:
    """One assistant turn with tools. Never raises for model or tool failures."""
    t0 = time.monotonic()
    question = question.strip()
    ctx = tools.ToolContext(conn, cfg, now, question,
                            allow_backlog=wants_backlog(conn, question))
    model = getattr(client, "model", None)

    def phase(event: str, **info: Any) -> None:
        if on_phase:
            on_phase(event, info)

    msgs: list[dict[str, Any]] = [{"role": "system", "content": AGENT_PROMPT}]
    msgs += [{"role": r, "content": c} for r, c in chat.trim_history(history)]
    msgs.append({"role": "user", "content": (
        f"{question}\n\n(Now: {now:%A} {now.date().isoformat()} {now:%H:%M}. "
        f"Dates: {tools.week_dates(now)}.)")})
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

    if ctx.clarification is not None:
        ans = Answer(text=ctx.clarification["question"], sources=[], unverified_numbers=[],
                     notes=[], model=model, duration=duration, tier="agent",
                     action={"kind": "clarify", **ctx.clarification})
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
    ans.intent = route.intent if route is not None else "planning"
    ans.route_by = route.by if route is not None else ""
    ans.steps = list(ctx.calls)
    return ans


def agent_client(cfg: Config, deep_client: Any = None) -> Any:
    """The client for the loop: ``cfg.agent_model`` if set (a new client), else the
    deep tier's (gemma4:26b by default)."""
    if cfg.agent_model and cfg.agent_model != getattr(deep_client, "model", None):
        from jarvis.ollama import OllamaClient
        return OllamaClient(cfg.ollama_url, cfg.agent_model, keep_alive="10m")
    return deep_client
