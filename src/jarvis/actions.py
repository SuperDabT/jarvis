"""Proposed changes to the user's items, and applying them once approved (no Qt).

The assistant never writes. When a plan needs dates or new items, the model
calls the ``propose_changes`` tool; ``build_proposal`` checks every change with
the item service's own validation and returns a ``Proposal``: data, nothing
saved. The chat shows it with Apply and Dismiss buttons; only Apply (the user's
click) runs ``apply_proposal``.

A proposal is one logical change. ``apply_proposal`` runs all of it inside one
``items.transaction``: each target must still be as it was when the change was
proposed (same ``updated_at``, open, not deleted), every date must still be
valid and not in the past, the service functions validate and write, and the
stored rows are read back and compared with what was intended before the
commit. Anything wrong rolls back everything; nothing is ever half applied. The
result lists what the database holds afterwards (read again after the commit).

An existing deadline is never moved: ``set_due`` only dates undated items;
planning work on a dated item (or an overdue one) is a reminder.

Engineering backlog is context, not a commitment: a change whose item is a
GitHub issue (named "#N", or titled like an open issue) is refused unless the
user's own request asked to include project work (``allow_backlog``); allowed
ones are marked ``backlog_ref`` so the card says where they came from.

Proposals are kept in the assistant message's metadata (``chat_messages.
sources_json``, under "proposal"), as plain JSON with a version; loading one
back re-checks its shape (``Proposal.from_dict``) and its status, so a stored
proposal is applied at most once.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, tzinfo
from typing import Any

from jarvis import items as it
from jarvis.facts import tokens

VERSION = 1
OPS = ("create", "set_due", "set_remind")
MAX_CHANGES = 12
STATUSES = ("pending", "applied", "dismissed", "failed")
_ISSUE_RE = re.compile(r"#(\d+)\b")


class ProposalError(ValueError):
    """A proposal that can't be built, loaded or applied (nothing was changed)."""


def human_when(stored: str | None) -> str:
    """"2026-10-10T13:00" -> "Sat Oct 10, 1:00 PM"; "2026-10-10" -> "Sat Oct 10"."""
    if not stored:
        return ""
    try:
        if len(stored) == 10:
            d = datetime.strptime(stored, it.DATE_FMT)
            return f"{d:%a %b} {d.day}"
        d = datetime.strptime(stored, it.MINUTE_FMT)
    except ValueError:
        return stored
    return f"{d:%a %b} {d.day}, {(d.hour % 12) or 12}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"


@dataclass(frozen=True)
class Change:
    op: str                          # create / set_due / set_remind
    title: str                       # the item's title (create: the new one)
    reason: str = ""
    item_id: int | None = None       # set_*: the target
    kind: str = "task"               # create
    project: str | None = None       # create: a project slug
    due: str | None = None           # stored form: "YYYY-MM-DD" / "YYYY-MM-DDTHH:MM"
    remind: str | None = None        # "YYYY-MM-DDTHH:MM"
    before: dict[str, Any] | None = None   # set_*: updated_at, due_at, remind_at
    backlog_ref: str | None = None   # "#31" when it comes from the GitHub backlog

    def describe(self) -> str:
        when = human_when(self.due or self.remind)
        if self.op == "create":
            return f"Add “{self.title}”" + (f", due {when}" if self.due else
                                            f", reminder {when}" if self.remind else "")
        what = "due date" if self.op == "set_due" else "reminder"
        old = (self.before or {}).get("due_at" if self.op == "set_due" else "remind_at")
        return (f"Set the {what} of “{self.title}” to {when}"
                + (f" (was {human_when(old)})" if old else ""))


@dataclass
class Proposal:
    id: str
    created_at: str
    question: str
    summary: str
    changes: list[Change]
    unscheduled: list[dict[str, Any]] = field(default_factory=list)  # item_id, title, reason
    assumptions: list[str] = field(default_factory=list)
    version: int = VERSION

    def to_dict(self) -> dict[str, Any]:
        return {"version": self.version, "id": self.id, "created_at": self.created_at,
                "question": self.question, "summary": self.summary,
                "changes": [asdict(c) for c in self.changes],
                "unscheduled": list(self.unscheduled), "assumptions": list(self.assumptions)}

    @classmethod
    def from_dict(cls, d: Any) -> Proposal:
        """A stored proposal, its shape checked (ProposalError otherwise)."""
        if not isinstance(d, dict) or d.get("version") != VERSION:
            raise ProposalError("unknown proposal format")
        try:
            changes = []
            for c in d["changes"]:
                if not isinstance(c, dict) or c.get("op") not in OPS:
                    raise ProposalError("a change has an unknown operation")
                ch = Change(**{k: c.get(k) for k in Change.__dataclass_fields__
                               if k in c})
                if not isinstance(ch.title, str) or (
                        ch.op != "create" and not isinstance(ch.item_id, int)):
                    raise ProposalError("a change is missing its item")
                changes.append(ch)
            p = cls(id=str(d["id"]), created_at=str(d["created_at"]),
                    question=str(d.get("question") or ""), summary=str(d.get("summary") or ""),
                    changes=changes, unscheduled=list(d.get("unscheduled") or []),
                    assumptions=[str(a) for a in d.get("assumptions") or []])
        except (KeyError, TypeError) as e:
            raise ProposalError(f"malformed proposal: {e}") from e
        if not p.changes or len(p.changes) > MAX_CHANGES:
            raise ProposalError("a proposal needs 1 to 12 changes")
        return p


# ---------------------------------------------------------------- building


def _norm(text: str) -> set[str]:
    return set(tokens(text))


def backlog_ref(conn: sqlite3.Connection, title: str, reason: str = "", *,
                new: bool = True) -> str | None:
    """"#N" when a change is about a GitHub issue: it names one, or (a new item
    only) its title is (nearly) an open issue's title. An existing item is the
    user's own record, identified by its id: similar wording to an issue doesn't
    make it backlog."""
    for m in _ISSUE_RE.finditer(f"{title} {reason}"):
        if conn.execute("SELECT 1 FROM issues WHERE number = ?", (int(m[1]),)).fetchone():
            return f"#{m[1]}"
    words = _norm(title)
    if not new:
        return None
    if len(words) < 2:
        return None
    for r in conn.execute("SELECT number, title FROM issues WHERE state = 'open'"):
        other = _norm(r["title"])
        if other and len(words & other) / len(words | other) >= 0.6:
            return f"#{r['number']}"
    return None


def _when(value: Any, field_name: str, now: datetime, tz: tzinfo | None,
          need_time: bool) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:00", value.strip()):
        value = value.strip()[:16]          # models often add ":00" seconds
    stored = it.wall_clock(value, field_name, tz, need_time=need_time)
    if stored is None:
        return None
    today = now.strftime(it.DATE_FMT)
    if len(stored) == 10 and stored < today or len(stored) > 10 and stored < now.strftime(
            it.MINUTE_FMT):
        raise it.ItemError(f"{field_name} {stored} is in the past", field_name)
    return stored


def check_change(conn: sqlite3.Connection, raw: Any, now: datetime, tz: tzinfo | None,
                 allow_backlog: bool) -> Change:
    """One proposed change, validated like the item service would (it.ItemError
    or ProposalError with a message the model can act on)."""
    if not isinstance(raw, dict):
        raise ProposalError("each change must be an object")
    op = raw.get("op")
    reason = str(raw.get("reason") or "")[:300]
    if op not in OPS:
        raise ProposalError(f"op must be one of {', '.join(OPS)} (got {op!r})")
    if op == "create":
        title = it._title(raw.get("title"))
        project = raw.get("project") or None
        if project is not None:
            project = it.get_project(conn, project).slug
        due = _when(raw.get("due"), "due", now, tz, need_time=False)
        remind = _when(raw.get("remind"), "remind", now, tz, need_time=True)
        ref = backlog_ref(conn, title, reason)
        if ref and not allow_backlog:
            raise ProposalError(f"“{title}” is engineering backlog ({ref}); it can only be "
                                "planned when the user asks to include project work")
        return Change("create", title, reason, kind=it._kind(raw.get("kind") or "task"),
                      project=project, due=due, remind=remind, backlog_ref=ref)
    try:
        item_id = int(raw.get("item_id"))
    except (TypeError, ValueError):
        raise ProposalError(f"{op} needs the item_id of one of the user's items") from None
    item = it.get_item(conn, item_id)          # ItemNotFound for unknown / deleted
    if item.done:
        raise ProposalError(f"“{item.title}” is already done")
    if op == "set_due":
        if item.due_at:
            # a deadline is a commitment: the assistant plans work on it with a
            # reminder, it never moves the deadline itself
            raise ProposalError(f"“{item.title}” already has a deadline ({item.due_at}); "
                                "use set_remind to plan work on it, don't move the deadline")
        due = _when(raw.get("due"), "due", now, tz, need_time=False)
        if due is None:
            raise ProposalError("set_due needs a due date")
        remind = None
    else:
        remind = _when(raw.get("remind"), "remind", now, tz, need_time=True)
        if remind is None:
            raise ProposalError("set_remind needs a date and time")
        due = None
    ref = backlog_ref(conn, item.title, new=False)
    if ref and not allow_backlog:
        raise ProposalError(f"“{item.title}” is engineering backlog ({ref}); it can only be "
                            "planned when the user asks to include project work")
    return Change(op, item.title, reason, item_id=item.id, due=due, remind=remind,
                  before=_snapshot(item), backlog_ref=ref)


def _snapshot(item: it.Item) -> dict[str, Any]:
    """What a set_* change was proposed against (checked again before applying)."""
    return {"updated_at": item.updated_at, "title": item.title, "due_at": item.due_at,
            "remind_at": item.remind_at}


def build_proposal(conn: sqlite3.Connection, raw_changes: Any, now: datetime,
                   tz: tzinfo | None, *, question: str = "", summary: str = "",
                   unscheduled: Any = (), assumptions: Any = (),
                   allow_backlog: bool = False) -> tuple[Proposal | None, list[str]]:
    """(the proposal, errors). Every change is checked; the proposal holds only
    the valid ones (None when none are), the errors say what was refused."""
    errors: list[str] = []
    changes: list[Change] = []
    seen: set[tuple[int | None, str]] = set()
    if not isinstance(raw_changes, list) or not raw_changes:
        return None, ["changes must be a non-empty list"]
    for k, raw in enumerate(raw_changes[:MAX_CHANGES], 1):
        try:
            ch = check_change(conn, raw, now, tz, allow_backlog)
        except (ProposalError, it.ItemError) as e:
            errors.append(f"change {k}: {e}")
            continue
        key = (ch.item_id, ch.op) if ch.item_id is not None else (None, ch.title.lower())
        if key in seen:
            errors.append(f"change {k}: the same change twice")
            continue
        seen.add(key)
        changes.append(ch)
    if len(raw_changes) > MAX_CHANGES:
        errors.append(f"only the first {MAX_CHANGES} changes were considered")
    if not changes:
        return None, errors
    keep = []
    for u in unscheduled or ():
        if isinstance(u, dict) and isinstance(u.get("item_id"), int):
            try:
                item = it.get_item(conn, u["item_id"])
            except it.ItemError:
                continue
            keep.append({"item_id": item.id, "title": item.title,
                         "reason": str(u.get("reason") or "")[:200]})
    return Proposal(uuid.uuid4().hex, now.isoformat(timespec="seconds"), question,
                    str(summary or "")[:400], changes, keep,
                    [str(a)[:200] for a in assumptions or ()][:6]), errors


# ---------------------------------------------------------------- applying


@dataclass
class ApplyResult:
    ok: bool
    message: str
    stored: list[dict[str, Any]] = field(default_factory=list)  # rows as read back


def _check_target(conn: sqlite3.Connection, ch: Change) -> it.Item:
    try:
        cur = it.get_item(conn, ch.item_id, include_deleted=True)
    except it.ItemError:
        raise ProposalError(f"“{ch.title}” no longer exists") from None
    if cur.deleted or cur.done:
        raise ProposalError(f"“{ch.title}” was {'deleted' if cur.deleted else 'completed'} "
                            "since this was proposed")
    if _snapshot(cur) != (ch.before or {}):
        raise ProposalError(f"“{ch.title}” was edited since this was proposed")
    return cur


def apply_proposal(conn: sqlite3.Connection, proposal: Proposal, now: datetime,
                   tz: tzinfo | None = None) -> ApplyResult:
    """All of ``proposal`` in one transaction, verified, or none of it."""
    done: list[tuple[Change, int]] = []
    try:
        with it.transaction(conn):
            for ch in proposal.changes:
                # still valid now (a proposal from yesterday may point at the past)
                due = _when(ch.due, "due", now, tz, need_time=False)
                remind = _when(ch.remind, "remind", now, tz, need_time=True)
                if ch.op == "create":
                    item = it.create_item(conn, ch.title, kind=ch.kind, project=ch.project,
                                          due=due, remind=remind, now=now, tz=tz)
                else:
                    _check_target(conn, ch)
                    kw = {"due": due} if ch.op == "set_due" else {"remind": remind}
                    item = it.update_item(conn, ch.item_id, now=now, tz=tz, **kw)
                done.append((ch, item.id))
            for ch, item_id in done:          # read back before committing
                _verify(it.get_item(conn, item_id), ch)
    except (ProposalError, it.ItemError, sqlite3.Error) as e:
        return ApplyResult(False, f"Nothing was changed: {e}")
    stored = []
    for ch, item_id in done:                  # and again, as committed
        row = it.get_item(conn, item_id)
        try:
            _verify(row, ch)
        except ProposalError as e:
            return ApplyResult(False, f"The saved data doesn't match the plan: {e}", stored)
        stored.append({"item_id": row.id, "title": row.title, "due_at": row.due_at,
                       "remind_at": row.remind_at, "status": row.status})
    return ApplyResult(True, f"Saved and checked {len(stored)} change"
                             f"{'s' if len(stored) != 1 else ''}.", stored)


def _verify(row: it.Item, ch: Change) -> None:
    if ch.op in ("create", "set_due") and ch.due is not None and row.due_at != ch.due:
        raise ProposalError(f"“{row.title}” has due {row.due_at}, not {ch.due}")
    if ch.op in ("create", "set_remind") and ch.remind is not None and row.remind_at != ch.remind:
        raise ProposalError(f"“{row.title}” has reminder {row.remind_at}, not {ch.remind}")
    if ch.op == "create" and row.title != ch.title:
        raise ProposalError(f"the new item is titled “{row.title}”")
    if row.status != "open" or row.deleted:
        raise ProposalError(f"“{row.title}” isn't open")


# ---------------------------------------------------------------- storage


def load_proposal(conn: sqlite3.Connection, message_id: int) -> tuple[Proposal, str]:
    """(the proposal stored with an assistant message, its status)."""
    row = conn.execute("SELECT role, sources_json FROM chat_messages WHERE id = ?",
                       (message_id,)).fetchone()
    if row is None or row["role"] != "assistant":
        raise ProposalError("that message has no proposal")
    try:
        meta = json.loads(row["sources_json"] or "{}")
    except ValueError as e:
        raise ProposalError("the stored proposal is unreadable") from e
    status = meta.get("proposal_status", "pending")
    if status not in STATUSES:
        raise ProposalError("the stored proposal has an unknown status")
    return Proposal.from_dict(meta.get("proposal")), status


def set_status(conn: sqlite3.Connection, message_id: int, status: str,
               result: str = "") -> None:
    """Record what happened to a stored proposal (applied / dismissed / failed)."""
    row = conn.execute("SELECT sources_json FROM chat_messages WHERE id = ?",
                       (message_id,)).fetchone()
    if row is None:
        return
    meta = json.loads(row["sources_json"] or "{}")
    meta["proposal_status"] = status
    meta["proposal_result"] = result
    conn.execute("UPDATE chat_messages SET sources_json = ? WHERE id = ?",
                 (json.dumps(meta), message_id))
    conn.commit()


def decide(conn: sqlite3.Connection, proposal: Proposal, message_id: int | None,
           apply: bool, now: datetime, tz: tzinfo | None = None) -> ApplyResult:
    """The user's Apply or Dismiss for a proposal. With ``message_id`` the stored
    copy is the authority (and must still be pending)."""
    if message_id is not None:
        stored, status = load_proposal(conn, message_id)
        if stored.id != proposal.id:
            raise ProposalError("the proposal doesn't match the stored one")
        if status != "pending":
            return ApplyResult(False, f"This proposal was already {status}.")
        proposal = stored
    if not apply:
        if message_id is not None:
            set_status(conn, message_id, "dismissed")
        return ApplyResult(True, "Dismissed. Nothing was changed.")
    result = apply_proposal(conn, proposal, now, tz)
    if message_id is not None:
        set_status(conn, message_id, "applied" if result.ok else "pending", result.message)
    return result
