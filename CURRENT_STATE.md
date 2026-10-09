# Current state

Updated after Phase 1, steps 4 and 6 (see ROADMAP.md).

## Database

- Schema version 2 (`db.latest_version()`), built by `db.MIGRATIONS`:
  1. Baseline: every table that existed before versioning (`BASELINE_SCHEMA`,
     `BASELINE_COLUMNS`). A version 0 database upgrades to it with no data change.
  2. Life OS core (`LIFE_OS_SCHEMA`): `projects`, `items`, `links`, and the first project
     (`lidar`, "LiDAR Scanner", `SuperDabT/stm32-lidar-scanner`).
- `db.connect()` upgrades under `<db>.migrate.lock`, after a verified backup
  (`<db>.bak-v<N>-<time>`), in one transaction. Failure raises `db.MigrationError` and leaves the
  old version. `db.restore_backup()` puts a backup back (see README).
- `db.py` owns the whole schema. To change it: append `Migration(N + 1, ...)`, never edit a
  released step, and test the upgrade from `tests/fixtures/schema_v0.sql`.

### Life OS tables (version 2)

- `projects`: `slug` (lowercase, for `#slug` capture), `name`, optional unique `github_repo`
  (`owner/name`, case-insensitive), `status` active / paused / archived. A project with items
  cannot be deleted; archive it.
- `items`: `kind` task / idea / reminder, `title`, `body`, `status` open / done (done iff
  `completed_at`), `priority` 0-3, `due_at`, `remind_at`, optional `project_id`, `deleted_at`
  for soft delete.
- `links`: `src_type`/`src_id` (item or project) `--kind-->` `dst_type`/`dst_id` (item,
  project, `issue` "owner/name#N", `commit` "owner/name@sha"), kind `about` or `related`.
  Item and project ends must exist and their links go with them; GitHub ends are checked for
  format only and never copied into `issues`. Links are immutable.
- Times: bookkeeping columns are UTC instants (`now_iso()`); `due_at` ("YYYY-MM-DD" or
  "YYYY-MM-DDTHH:MM") and `remind_at` ("YYYY-MM-DDTHH:MM") are local wall-clock times, as typed.
- The new tables are `STRICT` (SQLite 3.37+) with CHECK constraints and triggers, so bad rows
  are refused by the database itself, not only by Python. Foreign keys are on for every
  `db.connect()` connection (a raw `sqlite3.connect` does not enforce them).
- Not yet: no UI. Courses, notes, recurrence and
  notifications are later phases; they would add tables and `links` types in a new step.

## Item service (`jarvis/items.py`, no Qt)

- Write: `create_item`, `update_item` (`UNSET` = leave, `None` = clear), `complete_item`,
  `reopen_item`, `delete_item` (soft), `restore_item`. Each validates first and raises
  `ItemError` (with `.field`) or `ItemNotFound`, then commits; repeating one is harmless.
- Read: `get_item`, `list_items(status=open|done|all)`, `open_items`, `today_items`,
  `overdue_items`, each filterable by `project` (id, slug, `#slug` or repo) and `kinds`;
  `get_project`, `list_projects`.
- An item's *when* is `due_at`, else `remind_at`. Overdue and today are computed from the
  clock in `tz` (default the system zone) and never overlap. Open lists sort by when (none
  last), then priority, then age.
- `related` links: `link_related`, `unlink_related`, `related_items`. Two-way, one row per pair
  (smaller id first); a reverse row written elsewhere still counts.
- External links (an item about a GitHub issue) have no helper yet; the table supports them.

## Capture (`jarvis/capture.py`, no Qt)

- `capture(conn, text, client, answers=(), now, tz)` -> `CaptureResult` with status `saved`,
  `clarify` (answer with `result.answered(reply)` and call again; the text is kept) or `error`
  (nothing saved). `capture_plain` saves the text as typed, without the model.
- The model (`cfg.model` through `OllamaClient.chat_json`, schema-constrained JSON, thinking
  off) fills a form: kind, title, body, due, remind, project, priority, and the exact words
  each came from, or a question. Python checks every field before `items.create_item`:
  - a date, project or priority whose words are not in the text is dropped (noted);
  - common date phrases are worked out by `resolve_phrase` from `now` in `tz`, overriding the
    model's arithmetic; other phrasings use the model's date if it is not past or 5+ years out;
  - "next Thursday", "at 5", past dates and unreadable dates become a clarification with
    concrete options; the model may ask once itself; two answers that settle nothing -> error;
  - a project needs `#slug` or words matching its slug or name (not its repo name);
  - a title must mostly use words from the text; malformed replies save nothing.
- A reminder with a day but no time is saved as due that day; one with no time at all is
  saved without one.
- Entry points: `jarvis add "..."` (asks in a terminal; exit 3 if it would need to ask but
  can't, `--plain` to skip the model) and the window's Ctrl+N overlay
  (`gui/capture_overlay.py`: worker thread, own connection, Try again / Save as typed, Esc
  closes it before anything else).
- About 2.5 s per capture with `gemma4:26b` loaded (about 14 s when it has to load first).

## The real database

The real `~/.local/share/jarvis/jarvis.db` is at version 1 (upgraded on 2026-10-09 during
step 1; pre-upgrade backup `jarvis.db.bak-v0-20261009T221204Z`). The installed `jarvis`
commands run this checkout (editable install), so the next start of the window, the CLI or the
service upgrades it to version 2, after a verified backup. The running service keeps its old
code until restarted and works with either version.

## Tests

`QT_QPA_PLATFORM=offscreen JARVIS_REDUCE_MOTION=1 uv run pytest -q`: 687 pass.
`tests/conftest.py` refuses any SQLite connection inside `~/.local/share/jarvis` during tests
and fails the run if one was attempted. `tests/test_migrations.py` covers the upgrade machinery,
`tests/test_life_os_schema.py` the version 2 tables, `tests/test_items.py` the item service,
`tests/test_capture.py` capture and `jarvis add`, `tests/test_gui_capture.py` the overlay.
