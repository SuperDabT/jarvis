"""The assistant's tools: existing services behind explicit schemas (no Qt).

Each tool is a JSON-schema description the model sees and a Python function
that runs it; the model only chooses tools and arguments, ``run_tool`` executes
them. Reading tools wrap the services the rest of Jarvis uses (items, agenda,
memory, project facts) and tag every record with its reference ("item:12",
"#31", "milestone:Core demo", "chat:40") and where it comes from. There is no
writing tool: ``propose_changes`` only builds a ``actions.Proposal`` for the
user to Apply or Dismiss, and ``ask_user`` ends the turn with a question.

Engineering data comes back labelled as backlog: an open issue or a suggested
next step is never something the user committed to.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from jarvis import actions, assistant
from jarvis import items as it
from jarvis.chat import Source
from jarvis.config import Config

LIST_LIMIT = 30
RESULT_CHARS = 6000        # a tool result the model sees is cut to this


@dataclass
class ToolContext:
    conn: sqlite3.Connection
    cfg: Config
    now: datetime                     # aware, local
    question: str
    allow_backlog: bool = False       # the user's request asked to include project work
    refs: dict[str, Source] = field(default_factory=dict)   # every record a tool returned
    evidence: list[str] = field(default_factory=list)       # tool results, for validation
    proposal: actions.Proposal | None = None
    clarification: dict[str, Any] | None = None
    refused: list[str] = field(default_factory=list)        # propose_changes' last refusals
    calls: list[str] = field(default_factory=list)          # tool names, in order


def _ref(ctx: ToolContext, ref: str, label: str, url: str | None = None) -> str:
    ctx.refs[ref] = Source(ref, label, url)
    return ref


def _date(value: Any, name: str) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise ValueError(f"{name} must be a date like 2026-10-10") from None


def _item_row(ctx: ToolContext, i: it.Item, projects: dict[int, it.Project]) -> dict:
    p = projects.get(i.project_id) if i.project_id is not None else None
    row = {"ref": _ref(ctx, f"item:{i.id}", i.title), "item_id": i.id, "title": i.title,
           "kind": i.kind, "status": i.status, "due": i.due_at, "remind": i.remind_at,
           "priority": i.priority, "project": p.slug if p else None}
    if i.body:
        row["notes"] = i.body[:200]
    return row


# ---------------------------------------------------------------- tools


def get_agenda(ctx: ToolContext, start: str, end: str | None = None) -> dict:
    s = _date(start, "start")
    e = _date(end or start, "end")
    if e < s or (e - s).days > 31:
        raise ValueError("end must be on or after start, at most 31 days later")
    ag = assistant.build_agenda(ctx.conn, s, e, f"{s} to {e}", ctx.now)

    def row(x: assistant.Entry) -> dict:
        return {"ref": _ref(ctx, x.ref, x.title, x.url), "title": x.title,
                "date": x.day.isoformat() if x.day else None, "time": x.time or None,
                "kind": x.kind, "project": x.project}

    return {"source": "the user's items (dated) and GitHub milestones",
            "due": [row(x) for x in ag.due],
            "overdue": [row(x) | {"days_late": x.late_days} for x in ag.overdue],
            "milestones_due": [row(x) for x in ag.milestones],
            "undated_open_items": ag.undated,
            "note": "Only these dates are known. Jarvis has no calendar: free time and "
                    "appointments not listed here are unknown."}


def list_items(ctx: ToolContext, status: str = "open", project: str | None = None,
               undated_only: bool = False, limit: int = 20) -> dict:
    if status not in ("open", "done"):
        raise ValueError("status must be open or done")
    projects = {p.id: p for p in it.list_projects(ctx.conn, include_archived=True)}
    rows = it.list_items(ctx.conn, status=status, project=project or None)
    if undated_only:
        rows = [i for i in rows if not i.when]
    n = max(1, min(int(limit or 20), LIST_LIMIT))
    return {"source": "the user's own items in Jarvis", "total": len(rows),
            "items": [_item_row(ctx, i, projects) for i in rows[:n]]}


def search_memory(ctx: ToolContext, query: str) -> dict:
    hits = assistant.search_memory(ctx.conn, str(query), ctx.now)
    return {"source": "earlier chat messages and items (keyword match)",
            "hits": [{"ref": _ref(ctx, m.ref, m.text[:60]),
                      "date": m.day.isoformat() if m.day else None,
                      "who": m.who, "text": m.text} for m in hits],
            "note": "'you said' is the user's own words; 'Jarvis answered' is an earlier "
                    "answer, not a confirmed fact." if hits else "Nothing stored matches."}


def project_status(ctx: ToolContext) -> dict:
    from jarvis.facts import compute_next_steps

    ms = []
    for r in ctx.conn.execute("SELECT title, due_on, open_issues, closed_issues, html_url "
                              "FROM milestones WHERE state = 'open' "
                              "ORDER BY due_on IS NULL, due_on"):
        ms.append({"ref": _ref(ctx, f"milestone:{r['title']}", r["title"], r["html_url"]),
                   "title": r["title"], "due": (r["due_on"] or "")[:10] or None,
                   "open_issues": r["open_issues"], "closed_issues": r["closed_issues"]})
    issues = [{"ref": _ref(ctx, f"#{r['number']}", r["title"], r["html_url"]),
               "number": r["number"], "title": r["title"]}
              for r in ctx.conn.execute("SELECT number, title, html_url FROM issues "
                                        "WHERE state = 'open' ORDER BY number LIMIT 25")]
    try:
        steps = compute_next_steps(ctx.conn, ctx.now)
    except sqlite3.Error:
        steps = []
    return {"source": f"GitHub ({ctx.cfg.repo}) synced data and Jarvis's computed suggestions",
            "note": "This is engineering BACKLOG, not the user's commitments: nothing here "
                    "is due for the user unless they choose it.",
            "milestones": ms, "open_issues": issues,
            "suggested_next_steps": [{"text": s.text, "certain": s.certain,
                                      "evidence": s.evidence} for s in steps[:5]]}


def list_projects(ctx: ToolContext) -> dict:
    out = []
    for p in it.list_projects(ctx.conn):
        n_open = len(it.open_items(ctx.conn, project=p.id))
        out.append({"slug": p.slug, "name": p.name, "status": p.status,
                    "github_repo": p.github_repo, "open_items": n_open})
    return {"source": "Jarvis projects", "projects": out}


def propose_changes(ctx: ToolContext, changes: list, summary: str = "",
                    unscheduled: list | None = None, assumptions: list | None = None) -> dict:
    tz = ctx.now.tzinfo
    proposal, errors = actions.build_proposal(
        ctx.conn, changes, ctx.now, tz, question=ctx.question, summary=summary,
        unscheduled=unscheduled or [], assumptions=assumptions or [],
        allow_backlog=ctx.allow_backlog)
    if proposal is not None:
        ctx.proposal = proposal
    ctx.refused = list(errors)
    return {"proposed": [c.describe() for c in proposal.changes] if proposal else [],
            "refused": errors,
            "saved": False,
            "note": "Nothing is saved: the user sees these as a proposal and decides with "
                    "Apply or Dismiss. Tell them it is a proposal." if proposal
                    else "No valid change; fix the refused ones or answer without changes."}


def ask_user(ctx: ToolContext, question: str, missing: list | None = None) -> dict:
    ctx.clarification = {"question": str(question)[:400],
                         "missing": [str(m)[:80] for m in missing or []][:5]}
    return {"asked": True}


# ---------------------------------------------------------------- registry


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required}}}


_CHANGE = {"type": "object", "properties": {
    "op": {"type": "string", "enum": list(actions.OPS),
           "description": "create a new item, or set the due date / reminder of an existing one"},
    "item_id": {"type": "integer", "description": "set_due / set_remind: the item's item_id"},
    "title": {"type": "string", "description": "create: the new item's title"},
    "kind": {"type": "string", "enum": list(it.KINDS)},
    "project": {"type": "string", "description": "create: a project slug, optional"},
    "due": {"type": "string", "description": "YYYY-MM-DD or YYYY-MM-DDTHH:MM"},
    "remind": {"type": "string", "description": "YYYY-MM-DDTHH:MM"},
    "reason": {"type": "string", "description": "why, from the evidence"}},
    "required": ["op", "reason"]}

TOOLS: dict[str, tuple[dict, Callable[..., dict]]] = {
    "get_agenda": (_fn(
        "get_agenda", "The user's items dated between start and end (and overdue ones), "
        "plus GitHub milestones due then. Dates only: Jarvis has no calendar.",
        {"start": {"type": "string", "description": "YYYY-MM-DD"},
         "end": {"type": "string", "description": "YYYY-MM-DD, default start"}},
        ["start"]), get_agenda),
    "list_items": (_fn(
        "list_items", "The user's own items (tasks, ideas, reminders) with ids, dates, "
        "priority and project; optionally one project's or only undated ones.",
        {"status": {"type": "string", "enum": ["open", "done"]},
         "project": {"type": "string", "description": "a project slug"},
         "undated_only": {"type": "boolean"},
         "limit": {"type": "integer"}}, []), list_items),
    "search_memory": (_fn(
        "search_memory", "Earlier conversations and items that mention the query's words: "
        "decisions, preferences, things the user said before.",
        {"query": {"type": "string"}}, ["query"]), search_memory),
    "project_status": (_fn(
        "project_status", "The engineering project's open milestones, open GitHub issues "
        "and Jarvis's computed next steps. Backlog, not commitments.", {}, []),
        project_status),
    "list_projects": (_fn(
        "list_projects", "The user's projects (slug, name, open items).", {}, []),
        list_projects),
    "propose_changes": (_fn(
        "propose_changes", "Propose dates, reminders or new items for the user to approve. "
        "Saves nothing. Use only when the user wants things scheduled or added; a plan in "
        "words needs no changes.",
        {"changes": {"type": "array", "items": _CHANGE},
         "summary": {"type": "string"},
         "unscheduled": {"type": "array", "items": {"type": "object", "properties": {
             "item_id": {"type": "integer"}, "reason": {"type": "string"}}},
             "description": "items deliberately left without a date, and why"},
         "assumptions": {"type": "array", "items": {"type": "string"},
                         "description": "every assumption the dates rest on"}},
        ["changes"]), propose_changes),
    "ask_user": (_fn(
        "ask_user", "Ask the user one short question when missing information (free time, "
        "a constraint, which item they mean) would change the result. Ends your turn.",
        {"question": {"type": "string"},
         "missing": {"type": "array", "items": {"type": "string"}}}, ["question"]),
        ask_user),
}


def schemas() -> list[dict]:
    return [spec for spec, _ in TOOLS.values()]


def run_tool(ctx: ToolContext, name: str, arguments: Any) -> str:
    """Run one tool call; always returns JSON text for the model (errors included,
    so it can correct itself). Never raises for a bad call."""
    ctx.calls.append(name)
    entry = TOOLS.get(name)
    if entry is None:
        result: dict = {"error": f"unknown tool {name!r}; use one of {', '.join(TOOLS)}"}
    elif not isinstance(arguments, dict):
        result = {"error": "arguments must be an object"}
    else:
        try:
            result = entry[1](ctx, **arguments)
        except TypeError as e:
            result = {"error": f"bad arguments for {name}: {e}"}
        except (ValueError, it.ItemError, actions.ProposalError, sqlite3.Error) as e:
            result = {"error": f"{name} failed: {e}"}
    text = json.dumps(result, ensure_ascii=False, default=str)[:RESULT_CHARS]
    ctx.evidence.append(text)
    return text


def week_dates(now: datetime) -> str:
    """Today and the coming days with weekdays, so the model never computes dates."""
    days = [now.date() + timedelta(days=k) for k in range(9)]
    return ", ".join(f"{d:%a} {d.isoformat()}" for d in days)
