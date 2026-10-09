"""Tasks, ideas and reminders: the item service over the ``items`` / ``links``
tables (schema version 2, see db.LIFE_OS_SCHEMA). No Qt.

Every write validates its input first and raises ``ItemError`` with a plain
message (the database's own CHECK constraints stay as the last line of
defence), then commits, like cards.py.

Times
-----
``due`` and ``remind`` are wall-clock times in the user's zone, stored as
"YYYY-MM-DD" (due that day, no time) or "YYYY-MM-DDTHH:MM". They accept a
``date``, a ``datetime`` (an aware one is converted to ``tz``; seconds are
dropped) or one of those strings ("YYYY-MM-DD HH:MM" too). A reminder needs a
time. Bookkeeping times are UTC instants, as everywhere else.

An item's *when* is its due time, or its reminder time if it has no due time.
Relative to ``now`` (converted to ``tz``, default the system zone):

- overdue: open, and its when is a date before today or a time before now
- today:   open, its when is today, and it is not overdue

so the two never overlap. Items without a when are in neither. Deleted items
(soft deleted, ``deleted_at`` set) are left out of every query unless asked.

Links
-----
``related`` links are two-way: each pair is stored once, smaller id first
(``src_id`` < ``dst_id``), and either direction counts when looking for a
duplicate or removing one.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, tzinfo
from typing import Any

KINDS = ("task", "idea", "reminder")
STATUSES = ("open", "done")
PRIORITY_MAX = 3
TITLE_MAX = 500
BODY_MAX = 20_000
DATE_FMT = "%Y-%m-%d"
MINUTE_FMT = "%Y-%m-%dT%H:%M"
ITEM_COLUMNS = ("id", "kind", "title", "body", "status", "priority", "due_at", "remind_at",
                "project_id", "created_at", "updated_at", "completed_at", "deleted_at")

# The "when" of an item, and the overdue / today tests on it, as SQL. Parameters
# :today ("YYYY-MM-DD") and :now ("YYYY-MM-DDTHH:MM"), both wall-clock in tz.
_WHEN = "COALESCE(due_at, remind_at)"
_OVERDUE = (f"(CASE WHEN length({_WHEN}) = 10 THEN {_WHEN} < :today "
            f"ELSE {_WHEN} < :now END)")
_TODAY = f"(substr({_WHEN}, 1, 10) = :today AND NOT {_OVERDUE})"
_ORDER = f"ORDER BY {_WHEN} IS NULL, {_WHEN}, priority DESC, id"


class ItemError(ValueError):
    """Bad input for an item operation. ``field`` names the argument, if one."""

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


class ItemNotFound(ItemError, LookupError):
    """No such item or project (or the item is deleted and that was not allowed)."""


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


UNSET: Any = _Unset()  # "leave as is" for update_item; None means "clear"


@dataclass(frozen=True)
class Item:
    id: int
    kind: str
    title: str
    body: str | None
    status: str
    priority: int
    due_at: str | None
    remind_at: str | None
    project_id: int | None
    created_at: str
    updated_at: str
    completed_at: str | None
    deleted_at: str | None

    @property
    def done(self) -> bool:
        return self.status == "done"

    @property
    def deleted(self) -> bool:
        return self.deleted_at is not None

    @property
    def when(self) -> str | None:
        return self.due_at or self.remind_at


@dataclass(frozen=True)
class Project:
    id: int
    slug: str
    name: str
    github_repo: str | None
    description: str | None
    status: str
    created_at: str
    updated_at: str


# ---------------------------------------------------------------- helpers


def _row_to_item(row: sqlite3.Row) -> Item:
    return Item(**{k: row[k] for k in ITEM_COLUMNS})


def _stamp(now: datetime | None) -> str:
    """A UTC bookkeeping time like db.now_iso(), from ``now`` if given."""
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.astimezone()
    return now.astimezone(UTC).isoformat(timespec="seconds")


def local_now(now: datetime | None = None, tz: tzinfo | None = None) -> datetime:
    """``now`` (default: the current time) as an aware datetime in ``tz`` (default:
    the system zone). A naive ``now`` is taken to be in ``tz`` already."""
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz) if tz is not None else now.astimezone()
    return now.astimezone(tz)


def _wall_clock(value: Any, field: str, tz: tzinfo | None, *, need_time: bool) -> str | None:
    """A due / remind argument as the stored text, or ItemError."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(tz)
        return value.strftime(MINUTE_FMT)
    if isinstance(value, date):
        if need_time:
            raise ItemError(f"{field} needs a time of day, not only a date", field)
        return value.strftime(DATE_FMT)
    if not isinstance(value, str):
        raise ItemError(f"{field} must be a date, a datetime or text, not "
                        f"{type(value).__name__}", field)
    text = value.strip()
    if len(text) == 10:
        try:
            d = datetime.strptime(text, DATE_FMT)
        except ValueError:
            raise ItemError(f"{field} {value!r} is not a valid date (YYYY-MM-DD)", field) from None
        if need_time:
            raise ItemError(f"{field} needs a time of day, not only a date", field)
        return d.strftime(DATE_FMT)
    try:
        dt = datetime.strptime(text.replace(" ", "T", 1), MINUTE_FMT)
    except ValueError:
        raise ItemError(f"{field} {value!r} is not a valid date and time "
                        "(YYYY-MM-DD or YYYY-MM-DDTHH:MM)", field) from None
    return dt.strftime(MINUTE_FMT)


def _title(value: Any) -> str:
    if not isinstance(value, str):
        raise ItemError("title must be text", "title")
    text = value.strip()
    if not text:
        raise ItemError("title can't be empty", "title")
    if len(text) > TITLE_MAX:
        raise ItemError(f"title is longer than {TITLE_MAX} characters", "title")
    return text


def _body(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ItemError("body must be text", "body")
    text = value.strip()
    if len(text) > BODY_MAX:
        raise ItemError(f"body is longer than {BODY_MAX} characters", "body")
    return text or None


def _kind(value: Any) -> str:
    kind = value.strip().lower() if isinstance(value, str) else value
    if kind not in KINDS:
        raise ItemError(f"kind must be one of {', '.join(KINDS)} (got {value!r})", "kind")
    return kind


def _priority(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= PRIORITY_MAX:
        raise ItemError(f"priority must be a whole number from 0 to {PRIORITY_MAX} "
                        f"(got {value!r})", "priority")
    return value


def _item_id(value: Any) -> int:
    if isinstance(value, Item):
        return value.id
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ItemError(f"an item id is a positive whole number (got {value!r})", "item")
    return value


def _write(conn: sqlite3.Connection, sql: str, params: Any) -> sqlite3.Cursor:
    """Run one write and commit; a constraint the checks above missed becomes an
    ItemError (and nothing is kept)."""
    try:
        cur = conn.execute(sql, params)
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        raise ItemError(f"the database refused the change: {e}") from e
    return cur


# ---------------------------------------------------------------- projects


def _row_to_project(row: sqlite3.Row) -> Project:
    return Project(**{k: row[k] for k in Project.__dataclass_fields__})


def list_projects(conn: sqlite3.Connection, include_archived: bool = False) -> list[Project]:
    sql = "SELECT * FROM projects"
    if not include_archived:
        sql += " WHERE status != 'archived'"
    return [_row_to_project(r) for r in conn.execute(sql + " ORDER BY name, id")]


def get_project(conn: sqlite3.Connection, ref: int | str | Project) -> Project:
    """A project by id, slug ("lidar", "#lidar") or GitHub repo ("owner/name").
    Raises ItemNotFound."""
    if isinstance(ref, Project):
        ref = ref.id
    row = None
    if isinstance(ref, int) and not isinstance(ref, bool):
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (ref,)).fetchone()
    elif isinstance(ref, str) and ref.strip():
        key = ref.strip().removeprefix("#")
        col = "github_repo" if "/" in key else "slug"
        row = conn.execute(f"SELECT * FROM projects WHERE {col} = ? COLLATE NOCASE",
                           (key,)).fetchone()
    if row is None:
        raise ItemNotFound(f"no project {ref!r}", "project")
    return _row_to_project(row)


# ---------------------------------------------------------------- items: write


def create_item(conn: sqlite3.Connection, title: str, *, kind: str = "task",
                body: str | None = None, priority: int = 0, due: Any = None,
                remind: Any = None, project: int | str | Project | None = None,
                now: datetime | None = None, tz: tzinfo | None = None) -> Item:
    """Add an open item and return it. ``project``: id, slug or repo, or None."""
    row = {
        "kind": _kind(kind), "title": _title(title), "body": _body(body),
        "priority": _priority(priority),
        "due_at": _wall_clock(due, "due", tz, need_time=False),
        "remind_at": _wall_clock(remind, "remind", tz, need_time=True),
        "project_id": get_project(conn, project).id if project is not None else None,
    }
    stamp = _stamp(now)
    cur = _write(conn, "INSERT INTO items (kind, title, body, priority, due_at, remind_at, "
                       "project_id, created_at, updated_at) VALUES (:kind, :title, :body, "
                       ":priority, :due_at, :remind_at, :project_id, :at, :at)",
                 {**row, "at": stamp})
    return get_item(conn, cur.lastrowid)


def update_item(conn: sqlite3.Connection, item: int | Item, *, title: Any = UNSET,
                kind: Any = UNSET, body: Any = UNSET, priority: Any = UNSET,
                due: Any = UNSET, remind: Any = UNSET, project: Any = UNSET,
                now: datetime | None = None, tz: tzinfo | None = None) -> Item:
    """Change the given fields (None clears body / due / remind / project). A
    deleted item must be restored first. Returns the item as stored."""
    current = get_item(conn, item)
    changes: dict[str, Any] = {}
    if title is not UNSET:
        changes["title"] = _title(title)
    if kind is not UNSET:
        changes["kind"] = _kind(kind)
    if body is not UNSET:
        changes["body"] = _body(body)
    if priority is not UNSET:
        changes["priority"] = _priority(priority)
    if due is not UNSET:
        changes["due_at"] = _wall_clock(due, "due", tz, need_time=False)
    if remind is not UNSET:
        changes["remind_at"] = _wall_clock(remind, "remind", tz, need_time=True)
    if project is not UNSET:
        changes["project_id"] = get_project(conn, project).id if project is not None else None
    changes = {k: v for k, v in changes.items() if getattr(current, k) != v}
    if not changes:
        return current
    sets = ", ".join(f"{k} = :{k}" for k in changes)
    _write(conn, f"UPDATE items SET {sets}, updated_at = :at WHERE id = :id",
           {**changes, "at": _stamp(now), "id": current.id})
    return get_item(conn, current.id)


def complete_item(conn: sqlite3.Connection, item: int | Item,
                  now: datetime | None = None) -> Item:
    """Mark done (already done: unchanged)."""
    current = get_item(conn, item)
    if current.done:
        return current
    stamp = _stamp(now)
    _write(conn, "UPDATE items SET status = 'done', completed_at = ?, updated_at = ? "
                 "WHERE id = ?", (stamp, stamp, current.id))
    return get_item(conn, current.id)


def reopen_item(conn: sqlite3.Connection, item: int | Item,
                now: datetime | None = None) -> Item:
    """Mark open again (already open: unchanged)."""
    current = get_item(conn, item)
    if not current.done:
        return current
    _write(conn, "UPDATE items SET status = 'open', completed_at = NULL, updated_at = ? "
                 "WHERE id = ?", (_stamp(now), current.id))
    return get_item(conn, current.id)


def delete_item(conn: sqlite3.Connection, item: int | Item,
                now: datetime | None = None) -> Item:
    """Soft delete: hidden everywhere, links kept, ``restore_item`` undoes it.
    Already deleted: unchanged."""
    current = get_item(conn, item, include_deleted=True)
    if current.deleted:
        return current
    stamp = _stamp(now)
    _write(conn, "UPDATE items SET deleted_at = ?, updated_at = ? WHERE id = ?",
           (stamp, stamp, current.id))
    return get_item(conn, current.id, include_deleted=True)


def restore_item(conn: sqlite3.Connection, item: int | Item,
                 now: datetime | None = None) -> Item:
    """Undo ``delete_item`` (not deleted: unchanged)."""
    current = get_item(conn, item, include_deleted=True)
    if not current.deleted:
        return current
    _write(conn, "UPDATE items SET deleted_at = NULL, updated_at = ? WHERE id = ?",
           (_stamp(now), current.id))
    return get_item(conn, current.id)


# ---------------------------------------------------------------- items: read


def get_item(conn: sqlite3.Connection, item: int | Item, *,
             include_deleted: bool = False) -> Item:
    """One item. Raises ItemNotFound if it does not exist, or is deleted and
    ``include_deleted`` is False."""
    item_id = _item_id(item)
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise ItemNotFound(f"no item {item_id}", "item")
    found = _row_to_item(row)
    if found.deleted and not include_deleted:
        raise ItemNotFound(f"item {item_id} is deleted", "item")
    return found


def _filters(conn: sqlite3.Connection, project: Any, kinds: Iterable[str] | None,
             include_deleted: bool) -> tuple[list[str], dict[str, Any]]:
    where: list[str] = []
    params: dict[str, Any] = {}
    if not include_deleted:
        where.append("deleted_at IS NULL")
    if project is not None:
        where.append("project_id = :project_id")
        params["project_id"] = get_project(conn, project).id
    if kinds is not None:
        wanted = sorted({_kind(k) for k in ([kinds] if isinstance(kinds, str) else kinds)})
        if not wanted:
            raise ItemError("kinds is empty", "kinds")
        where.append("kind IN (" + ", ".join(f":k{i}" for i in range(len(wanted))) + ")")
        params.update({f"k{i}": k for i, k in enumerate(wanted)})
    return where, params


def list_items(conn: sqlite3.Connection, *, status: str = "open",
               project: int | str | Project | None = None,
               kinds: Iterable[str] | str | None = None,
               include_deleted: bool = False) -> list[Item]:
    """Items by status ("open", "done" or "all"), optionally of one project and
    some kinds. Open: soonest when first (none last), then priority, then oldest.
    Done: most recently completed first."""
    if status not in (*STATUSES, "all"):
        raise ItemError(f"status must be open, done or all (got {status!r})", "status")
    where, params = _filters(conn, project, kinds, include_deleted)
    if status != "all":
        where.append("status = :status")
        params["status"] = status
    order = "ORDER BY completed_at DESC, id DESC" if status == "done" else _ORDER
    sql = "SELECT * FROM items" + (" WHERE " + " AND ".join(where) if where else "")
    return [_row_to_item(r) for r in conn.execute(f"{sql} {order}", params)]


def open_items(conn: sqlite3.Connection, *, project: int | str | Project | None = None,
               kinds: Iterable[str] | str | None = None) -> list[Item]:
    return list_items(conn, status="open", project=project, kinds=kinds)


def _dated(conn: sqlite3.Connection, test: str, now: datetime | None, tz: tzinfo | None,
           project: Any, kinds: Any) -> list[Item]:
    local = local_now(now, tz)
    where, params = _filters(conn, project, kinds, include_deleted=False)
    where += ["status = 'open'", test]
    params.update(today=local.strftime(DATE_FMT), now=local.strftime(MINUTE_FMT))
    sql = f"SELECT * FROM items WHERE {' AND '.join(where)} {_ORDER}"
    return [_row_to_item(r) for r in conn.execute(sql, params)]


def overdue_items(conn: sqlite3.Connection, now: datetime | None = None,
                  tz: tzinfo | None = None, *, project: int | str | Project | None = None,
                  kinds: Iterable[str] | str | None = None) -> list[Item]:
    """Open items whose when has passed (see the module docstring)."""
    return _dated(conn, _OVERDUE, now, tz, project, kinds)


def today_items(conn: sqlite3.Connection, now: datetime | None = None,
                tz: tzinfo | None = None, *, project: int | str | Project | None = None,
                kinds: Iterable[str] | str | None = None) -> list[Item]:
    """Open items whose when is later today (or today, with no time)."""
    return _dated(conn, _TODAY, now, tz, project, kinds)


# ---------------------------------------------------------------- related links


def _pair(conn: sqlite3.Connection, a: int | Item, b: int | Item) -> tuple[Item, Item]:
    x, y = get_item(conn, a), get_item(conn, b)
    if x.id == y.id:
        raise ItemError("an item can't be related to itself", "item")
    return (x, y) if x.id < y.id else (y, x)


_EITHER_WAY = ("kind = 'related' AND src_type = 'item' AND dst_type = 'item' AND "
               "((src_id = :a AND dst_id = :b) OR (src_id = :b AND dst_id = :a))")


def link_related(conn: sqlite3.Connection, a: int | Item, b: int | Item,
                 now: datetime | None = None) -> bool:
    """Relate two (not deleted) items. True if a link was added, False if they
    were already related, in either direction."""
    x, y = _pair(conn, a, b)
    ids = {"a": str(x.id), "b": str(y.id)}
    if conn.execute(f"SELECT 1 FROM links WHERE {_EITHER_WAY}", ids).fetchone():
        return False
    _write(conn, "INSERT INTO links (src_type, src_id, dst_type, dst_id, kind, created_at) "
                 "VALUES ('item', :a, 'item', :b, 'related', :at)", {**ids, "at": _stamp(now)})
    return True


def unlink_related(conn: sqlite3.Connection, a: int | Item, b: int | Item) -> bool:
    """Remove the relation between two items, whichever way it was stored. True
    if there was one. Works on deleted items too."""
    ids = sorted((_item_id(a), _item_id(b)))
    if ids[0] == ids[1]:
        raise ItemError("an item can't be related to itself", "item")
    cur = _write(conn, f"DELETE FROM links WHERE {_EITHER_WAY}",
                 {"a": str(ids[0]), "b": str(ids[1])})
    return cur.rowcount > 0


def related_items(conn: sqlite3.Connection, item: int | Item, *,
                  include_deleted: bool = False) -> list[Item]:
    """The items related to ``item``, in either direction, open first by when."""
    item_id = str(get_item(conn, item, include_deleted=True).id)
    sql = ("SELECT * FROM items WHERE id IN ("
           " SELECT CAST(dst_id AS INTEGER) FROM links WHERE kind = 'related' AND"
           "  src_type = 'item' AND dst_type = 'item' AND src_id = :id"
           " UNION SELECT CAST(src_id AS INTEGER) FROM links WHERE kind = 'related' AND"
           "  src_type = 'item' AND dst_type = 'item' AND dst_id = :id)")
    if not include_deleted:
        sql += " AND deleted_at IS NULL"
    return [_row_to_item(r) for r in conn.execute(f"{sql} ORDER BY status = 'done', "
                                                  f"{_WHEN} IS NULL, {_WHEN}, id",
                                                  {"id": item_id})]
