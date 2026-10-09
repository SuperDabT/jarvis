# Current state

Updated after Phase 1, step 1 (see ROADMAP.md).

## Database

- Schema version 1 (`db.latest_version()`). Version 1 is the baseline: every table that existed
  before versioning (`db.BASELINE_SCHEMA` plus `db.BASELINE_COLUMNS`). A version 0 database
  (made before versioning) upgrades to it with no data change.
- `db.connect()` upgrades under `<db>.migrate.lock`, after a verified backup
  (`<db>.bak-v<N>-<time>`), in one transaction. Failure raises `db.MigrationError` and leaves the
  old version. `db.restore_backup()` puts a backup back (see README).
- `db.py` owns the whole schema; `chat.py` no longer creates `chat_messages` itself.
- To add a table or column: append `Migration(N + 1, "...", fn)` to `db.MIGRATIONS`, and add a
  test that upgrades `tests/fixtures/schema_v0.sql` through it. Never edit a released step.

## The real database

On 2026-10-09 the real `~/.local/share/jarvis/jarvis.db` was upgraded to version 1 by a
`jarvis watch --once` run during step 1's verification. Its pre-upgrade backup is
`jarvis.db.bak-v0-20261009T221204Z` (verified, 800 rows). Nothing but routine tick bookkeeping
changed since. The running `jarvis watch` service (older code) works with version 1 unchanged.

## Tests

`QT_QPA_PLATFORM=offscreen JARVIS_REDUCE_MOTION=1 uv run pytest -q`: 460 pass.
`tests/test_migrations.py` covers upgrade, idempotence, backup, restore, failure rollback, a
newer database, lock timeout, and six processes racing a writer.
