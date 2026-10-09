"""Schema version 2: projects, items and links (jarvis.db step 2).

Temporary databases only (conftest.py refuses the real one).
"""
from __future__ import annotations

import sqlite3

import pytest
from test_migrations import backups, dump, make_v0, version_of

from jarvis import db

NOW = "2026-10-09T22:00:00+00:00"
REPO = "SuperDabT/stm32-lidar-scanner"
NEW_TABLES = {"projects", "items", "links"}


@pytest.fixture
def conn():
    return db.connect(":memory:")


def make_v1(path, monkeypatch) -> None:
    """A database as step 1's code left it: v0 data, upgraded to version 1."""
    make_v0(path)
    with monkeypatch.context() as m:
        m.setattr(db, "MIGRATIONS", db.MIGRATIONS[:1])
        db.connect(path).close()
    assert version_of(path) == 1


def add_item(conn, **kw):
    row = {"kind": "task", "title": "Solder the header", "created_at": NOW, "updated_at": NOW,
           **kw}
    cols = ", ".join(row)
    cur = conn.execute(f"INSERT INTO items ({cols}) VALUES ({', '.join('?' * len(row))})",
                       tuple(row.values()))
    return cur.lastrowid


def add_link(conn, src_type, src_id, dst_type, dst_id, kind="about"):
    conn.execute("INSERT INTO links (src_type, src_id, dst_type, dst_id, kind, created_at) "
                 "VALUES (?, ?, ?, ?, ?, ?)", (src_type, src_id, dst_type, dst_id, kind, NOW))


def lidar_id(conn) -> int:
    return conn.execute("SELECT id FROM projects WHERE slug = 'lidar'").fetchone()[0]


# ---------------------------------------------------------------- migration


def test_version_1_database_upgrades_to_2_keeping_every_row(tmp_path, monkeypatch):
    path = tmp_path / "jarvis.db"
    make_v1(path, monkeypatch)
    rows_before, schema_before = dump(path)
    conn = db.connect(path)
    assert db.schema_version(conn) == 2 == db.latest_version()
    conn.close()
    rows_after, schema_after = dump(path)
    assert {t: rows_after[t] for t in rows_before} == rows_before  # old data untouched
    old = {(t, n) for t, n, _ in schema_before}
    assert {(t, n, s) for t, n, s in schema_after if (t, n) in old} == set(schema_before)
    assert NEW_TABLES <= set(rows_after)
    # a verified backup of the version 1 file
    bak = [b for b in backups(path) if ".bak-v1-" in b.name]
    assert len(bak) == 1 and dump(bak[0]) == (rows_before, schema_before)


def test_version_0_database_goes_straight_to_2(tmp_path):
    path = tmp_path / "jarvis.db"
    make_v0(path)
    rows_before, _ = dump(path)
    db.connect(path).close()
    rows_after, _ = dump(path)
    assert version_of(path) == 2
    assert {t: rows_after[t] for t in rows_before} == rows_before
    assert [b.name.split(".bak-")[1][:2] for b in backups(path)] == ["v0"]


def test_first_project_is_registered_once(tmp_path):
    path = tmp_path / "jarvis.db"
    for _ in range(3):
        db.connect(path).close()
    c = db.connect(path)
    rows = c.execute("SELECT slug, name, github_repo, status FROM projects").fetchall()
    assert [tuple(r) for r in rows] == [("lidar", "LiDAR Scanner", REPO, "active")]
    assert db.migrate(c, path) is None  # the runner again: nothing to do


def test_rerunning_the_runner_changes_nothing(tmp_path, monkeypatch):
    path = tmp_path / "jarvis.db"
    make_v1(path, monkeypatch)
    db.connect(path).close()
    state, made = dump(path), backups(path)
    for _ in range(2):
        c = db.connect(path)
        assert db.migrate(c, path) is None
        c.close()
    assert dump(path) == state and backups(path) == made


def test_seed_is_idempotent_by_itself(conn):
    """The seed insert never duplicates (also matched case-insensitively by repo)."""
    for repo in (REPO, REPO.lower()):
        conn.execute("INSERT INTO projects (slug, name, github_repo, created_at, updated_at) "
                     "VALUES ('lidar', 'x', ?, ?, ?) ON CONFLICT DO NOTHING", (repo, NOW, NOW))
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1


def test_a_clashing_table_fails_step_2_cleanly(tmp_path, monkeypatch):
    """A stray table named "items" stops the upgrade; nothing is half made."""
    path = tmp_path / "jarvis.db"
    make_v1(path, monkeypatch)
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE items (x)")
    raw.commit()
    raw.close()
    state = dump(path)
    with pytest.raises(db.MigrationError, match="step 2"):
        db.connect(path)
    assert version_of(path) == 1 and dump(path) == state


def test_new_tables_indexes_and_triggers_exist(conn):
    names = {(r[0], r[1]) for r in conn.execute("SELECT type, name FROM sqlite_master")}
    assert {("table", t) for t in NEW_TABLES} <= names
    assert {("index", i) for i in ("items_project", "items_open_due", "items_open_remind",
                                   "links_dst")} <= names
    assert {("trigger", t) for t in ("links_ends_exist", "links_immutable", "items_keep_id",
                                     "projects_keep_id", "items_drop_links",
                                     "projects_drop_links")} <= names
    plan = " ".join(r[3] for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM items WHERE status = 'open' AND deleted_at IS NULL "
        "AND due_at < '2026-10-10' ORDER BY due_at"))
    assert "items_open_due" in plan
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


# ---------------------------------------------------------------- items


def test_a_full_item_round_trips(conn):
    pid = lidar_id(conn)
    iid = add_item(conn, kind="reminder", title="Order servos", body="two MG90S",
                   priority=2, due_at="2026-10-10T17:00", remind_at="2026-10-10T16:30",
                   project_id=pid)
    row = conn.execute("SELECT * FROM items WHERE id = ?", (iid,)).fetchone()
    assert (row["kind"], row["status"], row["priority"], row["project_id"]) == (
        "reminder", "open", 2, pid)
    assert row["completed_at"] is None and row["deleted_at"] is None
    conn.execute("UPDATE items SET status = 'done', completed_at = ? WHERE id = ?", (NOW, iid))
    conn.execute("UPDATE items SET deleted_at = ? WHERE id = ?", (NOW, iid))  # soft delete


@pytest.mark.parametrize("bad", [
    {"kind": "note"}, {"kind": "Task"}, {"kind": None},
    {"title": ""}, {"title": "   "}, {"title": None},
    {"status": "archived"},
    {"status": "done"},                          # done needs completed_at
    {"completed_at": NOW},                       # an open item has no completed_at
    {"priority": 4}, {"priority": -1}, {"priority": "high"},
    {"project_id": 999},                         # no such project (foreign key)
    {"project_id": "lidar"},                     # STRICT: not an integer
])
def test_invalid_items_are_rejected(conn, bad):
    with pytest.raises(sqlite3.IntegrityError):
        add_item(conn, **bad)


@pytest.mark.parametrize("value, ok", [
    ("2026-10-10", True),
    ("2026-10-10T17:00", True),
    ("2028-02-29", True),
    ("2026-10-10T00:00", True),
    ("2026-10-10T23:59", True),
    ("2026-02-29", False),        # not a leap year
    ("2026-02-30", False),
    ("2026-13-01", False),
    ("2026-10-10T24:00", False),
    ("2026-10-10T17:60", False),
    ("2026-10-10 17:00", False),   # T separator only
    ("2026-10-10T17:00:00", False),
    ("2026-10-10T17:00+00:00", False),  # wall-clock, no offset
    ("tomorrow", False),
    ("", False),
])
def test_due_times(conn, value, ok):
    if ok:
        add_item(conn, due_at=value)
    else:
        with pytest.raises(sqlite3.IntegrityError):
            add_item(conn, due_at=value)


@pytest.mark.parametrize("value, ok", [
    ("2026-10-10T08:15", True),
    ("2026-10-10", False),  # a reminder needs a time
    ("2026-10-10T25:00", False),
])
def test_reminder_times(conn, value, ok):
    if ok:
        add_item(conn, remind_at=value)
    else:
        with pytest.raises(sqlite3.IntegrityError):
            add_item(conn, remind_at=value)


def test_date_only_due_sorts_before_that_days_times(conn):
    for due in ("2026-10-10T09:00", "2026-10-11", "2026-10-10"):
        add_item(conn, due_at=due)
    got = [r[0] for r in conn.execute("SELECT due_at FROM items ORDER BY due_at")]
    assert got == ["2026-10-10", "2026-10-10T09:00", "2026-10-11"]


def test_a_project_with_items_cannot_be_deleted(conn):
    pid = lidar_id(conn)
    add_item(conn, project_id=pid)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM projects WHERE id = ?", (pid,))


def test_ids_cannot_change(conn):
    iid = add_item(conn)
    with pytest.raises(sqlite3.IntegrityError, match="id cannot change"):
        conn.execute("UPDATE items SET id = 500 WHERE id = ?", (iid,))
    with pytest.raises(sqlite3.IntegrityError, match="id cannot change"):
        conn.execute("UPDATE projects SET id = 500")


@pytest.mark.parametrize("bad", [
    {"slug": "LiDAR"}, {"slug": "-x"}, {"slug": "a b"}, {"slug": ""},
    {"name": " "}, {"status": "done"},
    {"github_repo": "no-slash"}, {"github_repo": "a/b/c"}, {"github_repo": "a/b c"},
])
def test_invalid_projects_are_rejected(conn, bad):
    row = {"slug": "school", "name": "School", "github_repo": None, **bad}
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO projects (slug, name, github_repo, status, created_at, "
                     "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (row["slug"], row["name"], row["github_repo"],
                      row.get("status", "active"), NOW, NOW))


def test_projects_need_unique_slugs_and_repos(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO projects (slug, name, created_at, updated_at) "
                     "VALUES ('lidar', 'Other', ?, ?)", (NOW, NOW))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO projects (slug, name, github_repo, created_at, updated_at) "
                     "VALUES ('other', 'Other', 'superdabt/STM32-lidar-scanner', ?, ?)",
                     (NOW, NOW))
    conn.execute("INSERT INTO projects (slug, name, created_at, updated_at) "
                 "VALUES ('school', 'School', ?, ?)", (NOW, NOW))  # no repo is fine, twice
    conn.execute("INSERT INTO projects (slug, name, created_at, updated_at) "
                 "VALUES ('home', 'Home', ?, ?)", (NOW, NOW))


# ---------------------------------------------------------------- links


def test_a_task_about_a_github_issue_copies_nothing(conn):
    db.upsert_issue(conn, {"number": 7, "title": "Track filter", "state": "open"})
    issues_before = conn.execute("SELECT * FROM issues").fetchall()
    iid = add_item(conn, title="Finish the track filter", project_id=lidar_id(conn))
    add_link(conn, "item", iid, "issue", f"{REPO}#7")
    add_link(conn, "item", iid, "issue", f"{REPO}#99")  # not synced yet: still fine
    with pytest.raises(sqlite3.IntegrityError):         # the same link twice
        add_link(conn, "item", iid, "issue", f"{REPO}#7")
    assert conn.execute("SELECT * FROM issues").fetchall() == issues_before
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    # and the issue's side can be found
    got = conn.execute("SELECT src_id FROM links WHERE dst_type = 'issue' AND dst_id = ?",
                       (f"{REPO}#7",)).fetchall()
    assert [r[0] for r in got] == [str(iid)]


def test_links_between_records(conn):
    a, b = add_item(conn), add_item(conn, kind="idea", title="Use a median filter")
    add_link(conn, "item", a, "item", b, "related")
    add_link(conn, "item", a, "project", lidar_id(conn))
    add_link(conn, "project", lidar_id(conn), "commit", f"{REPO}@02fb2d2", "related")
    assert conn.execute("SELECT COUNT(*) FROM links").fetchone()[0] == 3
    # stored as decimal text whatever was passed
    assert {r[0] for r in conn.execute("SELECT src_id FROM links")} <= {str(a), str(lidar_id(conn))}


@pytest.mark.parametrize("dst_type, dst_id", [
    ("issue", "#7"), ("issue", "7"), ("issue", f"{REPO}#0"), ("issue", f"{REPO}#7a"),
    ("issue", "a/b/c#7"), ("issue", f"{REPO}#7#8"), ("issue", "a b/c#1"),
    ("commit", f"{REPO}@02FB2D2"), ("commit", f"{REPO}@02fb2d"), ("commit", f"{REPO}@" + "a" * 41),
    ("commit", "02fb2d2"), ("commit", f"{REPO}@02fb2d2@x"),
    ("item", "0"), ("item", "01"), ("item", "x"), ("project", "-1"),
    ("paper", "2401.00001"), ("milestone", "1"),
])
def test_malformed_targets_are_rejected(conn, dst_type, dst_id):
    iid = add_item(conn)
    with pytest.raises(sqlite3.IntegrityError):
        add_link(conn, "item", iid, dst_type, dst_id)


@pytest.mark.parametrize("src_type, src_id, dst_type, dst_id, kind, why", [
    ("item", "999", "issue", f"{REPO}#7", "about", "source record does not exist"),
    ("item", "1", "item", "999", "about", "target record does not exist"),
    ("item", "1", "project", "999", "about", "target record does not exist"),
    ("issue", f"{REPO}#7", "item", "1", "about", "CHECK"),  # external ends are targets only
    ("item", "1", "item", "1", "related", "CHECK"),         # not to itself
    ("item", "1", "issue", f"{REPO}#7", "blocks", "CHECK"),
])
def test_invalid_links_are_rejected(conn, src_type, src_id, dst_type, dst_id, kind, why):
    assert add_item(conn) == 1
    with pytest.raises(sqlite3.IntegrityError, match=why):
        add_link(conn, src_type, src_id, dst_type, dst_id, kind)


def test_links_are_immutable_and_follow_deletes(conn):
    a, b = add_item(conn), add_item(conn)
    add_link(conn, "item", a, "item", b, "related")
    add_link(conn, "item", b, "issue", f"{REPO}#7")
    with pytest.raises(sqlite3.IntegrityError, match="cannot be changed"):
        conn.execute("UPDATE links SET dst_id = '1'")
    conn.execute("UPDATE items SET deleted_at = ? WHERE id = ?", (NOW, b))  # soft: kept
    assert conn.execute("SELECT COUNT(*) FROM links").fetchone()[0] == 2
    conn.execute("DELETE FROM items WHERE id = ?", (b,))                   # hard: gone
    assert conn.execute("SELECT COUNT(*) FROM links").fetchone()[0] == 0


def test_foreign_keys_are_enforced_on_every_connection(tmp_path):
    path = tmp_path / "jarvis.db"
    for _ in range(2):
        c = db.connect(path)
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            add_item(c, project_id=12345)
        c.close()
