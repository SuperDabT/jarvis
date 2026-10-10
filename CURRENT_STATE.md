# Current state

Updated after Phase 1, step 9: Phase 1 is complete (see ROADMAP.md).

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

## The window (shell)

- `gui/window.py` `MainWindow`: top bar, then rail | workspaces (`QStackedWidget`) | chat,
  then the ticker. `gui/shell.py` has the rail (`NavRail`, painted icons) and the placeholder
  pages. Workspaces in order, Ctrl+1 .. Ctrl+5: Home, Tasks, School, Projects, Knowledge;
  `show_workspace(key)`, `workspace`. It opens on Home (`start=` to choose another).
- Projects holds the whole engineering cockpit unchanged (status line, vitals / milestones /
  pulse / power, graph + briefing, inspector + stream). Its widgets are still attributes of
  the window (`win.graph`, `win.hud`, `win.inspector`, ...). Picking a graph reference from
  anywhere switches to Projects.
- The cockpit's left column (`gui/dashboard_data.py`, tiles in `gui/cockpit.py`) answers
  four questions about the active milestone. Active = the one picked in the Milestones tile
  (UI setting `projects/active_milestone`, only the number), else the nearest-due open one.
  Vitals: deadline and days left, closed / total, and risk: issues of that milestone closed
  in the last 14 days give the pace; on track when the open ones fit before the due date at
  that pace, at risk when not; none closed and due within 7 days is at risk, otherwise "too
  early to tell"; no due date, no issues, done and overdue are said as such. Issues: the
  repo's open count and the milestone's top 4 by their labels only (blocker, then P0..P4 /
  critical / high / medium / low, then phase-N, then number; "blocked" last); a row inspects
  the issue in the graph (else opens it), "view all" opens the milestone on GitHub.
  Milestones: the active one, the others folded; any can be made active. Commit activity:
  commits per local day from the synced history (`commits`, `commits_since`), 26 weeks,
  days before the synced range hollow (no data), hover for the day's commits; no history
  shows an empty state. Power and the old pulse are gone from the column; hardware.yaml,
  checks and findings are unchanged (a "no hardware checks" pill says why when the file is
  missing or invalid).
- Type and motion are shared: `theme.TYPE` is the scale (sans for headings, labels, values
  and text; mono only for ids, hashes, paths, label tags, the ticker and keys), applied with
  `theme.typeset(widget, role)`. Green marks live, active or positive things only. Rows that
  can be clicked are `widgets.HoverRow` (hover fades in over `motion.HOVER` 140 ms, press,
  a mint edge when selected); sections open with `motion.reveal` (`EXPAND` 200 ms); a new
  graph lock gets a mint ring that settles in and the inspector content fades in
  (`SELECT` 180 ms; hovering doesn't fade). With `JARVIS_REDUCE_MOTION=1` (or
  `--no-animations`) all of these are instant.
- Startup entrance, once (`MainWindow._enter`, after the first show; nothing waits for it):
  the top bar fades in, the rail settles in from the left, then the workspace fades up
  (~500 ms, `motion.ENTER_*`). The chat slides and fades in from the right (`PANEL` 200 ms)
  and fades out (`FAST`); the capture dialog fades up; reopening either mid-fade settles
  open. Qt's own menu / tooltip / combo fades follow the motion setting (the platform may
  ignore them). Surfaces separate by tone: panels have only a faint hairline, pills are soft
  fills, no green outlines.
- The inspector (the right column, full height; the thought stream is no longer in
  Projects, its events stay in Home's "recent activity" and the ticker): a header (kind,
  status, title, a priority tag only when the issue's labels give one, a short summary), the
  primary action "Ask Jarvis about this issue / commit / milestone / finding / ..." (none
  for the core and hubs), related items grouped (milestone, issues, commits, linked) as
  clickable rows that say how certain the link is ("the commit names the issue" vs "may have
  closed it · unconfirmed"), and everything else (full text, more links, the node id, the
  raw prompt) under "details". A check, or a card about one, also gets
  `gui/finding_view.py` (verified result, suggested next step, source file / field / line,
  open source, copy fix prompt). Jarvis edits no files.
- Ask Jarvis (`jarvis/focus.py`): the chat opens and asks the item's starter question with a
  SELECTED block in its context (`chat.build_context(focus=...)`, required, right after
  PROJECT): repository, the item's own data from the synced tables, its graph relationships
  each with its certainty, the file names its text mentions, and a line saying Jarvis has no
  copy of the source code. The chat shows "About <item>" over the composer; follow-ups keep
  the context until the chip's ✕ or "New"; clicking the chip shows the item in the graph.
  Spoken questions don't carry it (voice builds its own cache-friendly context).
- Narrow windows (800 px, a tiled half screen): below 980 px the chat floats over the
  right of the workspace instead of squeezing it; when the graph can't keep 300 px beside
  both cockpit columns, Projects shows one at a time (dashboard / inspector toggle in the
  status row; selecting a node shows the inspector); the briefing folds to its headline on a
  graph under 480 px; the ticker hint shortens with "…". The window's minimum is 800×560.
- The chat is at shell level: Ctrl+K / Ctrl+J / Ctrl+Space work in every workspace; in
  Projects it still takes the inspector column. Esc order: capture, voice, graph lock, chat.
- With the chat open every workspace fits in what is left (524 px at the 980 px minimum, so
  the window's own minimum never exceeds 980): in Projects the left column steps aside when
  the graph would get under 420 px; Tasks moves its filters under the title below 700 px and
  an open editor takes the list's place below 660 px; Home goes to one column below 640 px.
- Chat messages are `chat_panel.MessageText` (a read-only QTextBrowser), not QLabels: they
  wrap long words, paths and inline code inside the panel (copying gives the text as written)
  and never widen the conversation. A streamed token is appended to the document, so only
  the last paragraph is laid out again: ~0.6 ms a token at any reply length (re-setting a
  label's whole text cost 26 ms a token at 5,500 characters and starved typing). Long source
  chips are elided (tooltip has the full title).
- School and Knowledge are labelled placeholders ("not built yet", what they will hold),
  with only real actions (quick capture, open Projects).
- Tasks (`gui/tasks_view.py`, data in `gui/tasks_data.py`): open items in Overdue / Today /
  Upcoming / Someday (`items.grouped_items`: every open item in exactly one), filters by
  type and project, a side editor (due / remind accept dates or phrases like "tomorrow 5pm",
  worked out without a model), done / reopen, delete with Undo, and Done / Deleted sections to
  reopen or restore. New items come from quick capture. GitHub issues never appear here.
- Home (`gui/home_view.py`, data in `gui/home_data.py`): greeting and what needs attention,
  today (overdue first, tick off in place), upcoming deadlines (items and open milestones,
  14 days), the project's briefing headline and vitals, the computed next steps (top 3, from
  `WindowData.next_steps`, never the model) and recent activity. Three columns, two below
  1000 px, one below 640 px. Each section fails on its own.
- The window's `reload()` (file watcher, 30 s poll, after capture and after every write)
  refreshes Tasks and Home; each rebuilds only when its data changed. A `clock=` argument
  fixes "now" for tests and screenshots.
- Below 1320 px wide the rail is compact (icon over caption); below 1200 px the cockpit's side
  columns narrow (272 / 256) so the graph keeps about 330 px at the 980x600 minimum.
- Palette (`theme.py`): forest-green near-black `#031211`, grey-green ink `#DCE8E2`, teal mint
  `#52E3A4` (the Life OS mockup); every other colour is mixed from these.

## Items in the project graph (`gui/brain_data.py` build_graph)

Open items only; done and deleted items leave the graph (and come back when reopened or
restored). Rules, in order:

1. Items in a project hang off that project's hub (`project:<slug>`, linked to the core), at
   most 40 per project (`ITEMS_PER_PROJECT`), most urgent first: overdue, today, upcoming,
   someday, then by when and priority.
2. Items with an explicit `links` row to something in the graph (an issue or commit of this
   repo, a project, or another shown item, followed through chains) are always shown, past
   the cap. Relations are never hidden to save space.
3. Everything else (no project, no link) is left out and counted in `Graph.loose`; the legend's
   "+ unfiled" toggle shows up to 30 of them under an "unfiled" hub.

Edges are drawn once (same a, b, kind dropped; "related" either way round). Items are rounded
squares (task filled, idea an outlined diamond, reminder an outlined square with a dot): red
when overdue, mint when due today, ink otherwise. At most 12 urgent items carry a label
without hovering. The inspector shows an item's dates, project and links, with "edit in Tasks"
(clears Tasks filters that would hide it) and "done". The legend's "item" filter hides them.

Performance (498 nodes, 581 edges: GitHub data plus 333 items over 8 projects, this machine):
build_graph 8 ms, layout 0.6 ms, every node's neighbours 0.1 ms (indexed once per graph),
window reload 88 ms, paint 14 ms, hover hit test 0.2 ms.

## Ask Jarvis: routing and evidence (`jarvis/intent.py`, `jarvis/assistant.py`, no Qt)

Every chat and voice question is routed first (`escalate.answer_with_escalation`, and the
plain voice pass): `intent.route` recognises clear requests by sentence structure (agenda,
capture, planning, project, recall, general), asks the model's schema-constrained classifier
only for unclear ones, and falls back to "mixed" (the user's items plus the project data).
Lookups are answered by code with `tier="direct"` and no model: the agenda for a timeframe
(the user's items dated in it, overdue ones when it starts today, GitHub milestones due in it;
undated items and the project backlog are counted, never listed as due), capture requests
(handed to the capture form, which the user confirms; nothing is saved by the chat), and
recall questions with nothing found. Model answers get only their route's data
(`assistant.build_routed_context`: YOUR ITEMS, MEMORY, project data) and the routed prompt
(facts vs suggestions, no deciding for the user). Memory is the existing chat history and
items, searched by the question's words; the user's own words are labelled apart from
Jarvis's earlier answers. Selected graph items (focus) keep the project route; `chat.ask`
without a route is unchanged. Regression cases: `tests/test_intelligence.py`.

## The tool-using assistant (`jarvis/agent.py`, `tools.py`, `actions.py`, no Qt)

Open-ended requests (route "planning" or "mixed": "help me organize my weekend", "schedule
these", "what should I work on", unclear asks, and answers to a question Jarvis just asked)
run a bounded loop when the model can call tools (`OllamaClient.chat_tools`, Ollama's native
tool calling): at most 5 model calls (the last without tools), 8 tool calls and 150 s. The
tools (`tools.TOOLS`) wrap existing services: `get_agenda`, `list_items`, `search_memory`,
`project_status` (labelled backlog, not commitments), `list_projects`, `propose_changes` and
`ask_user`. None writes. `propose_changes` validates with the item service's own checks and
returns an `actions.Proposal` (create / set_due / set_remind, with the target's snapshot);
GitHub backlog is refused unless the user's request asked for project work. The chat shows it
with Apply and Dismiss; Apply runs `actions.apply_proposal`: one `items.transaction` (BEGIN
IMMEDIATE; the service functions don't commit inside it), stale targets / past dates / any
validation or database error roll everything back, and the rows are read back before and after
the commit. Proposals are stored in the assistant message's `chat_messages.sources_json`
(`proposal`, `proposal_status`) and applied at most once. `cfg.agent_model`
(`JARVIS_AGENT_MODEL`) changes the loop's model; default gemma4:26b. Agenda, capture, recall
and project questions keep their single-pass paths; voice doesn't use the loop. Each turn
first fixes its goal (`agent.Goal`: schedule / plan / answer, the days, and what the user said
for it; one `chat_json` call, or carried over from Jarvis's previous loop turn via
`pending_goal` in that message's metadata, so a reply continues the same request). Plans and
schedules are structured (`jarvis/planning.py`): code picks the items in scope (overdue, dated
up to the horizon, which is the later of 7 days after the period and 14 days from today, the
same 14 days as the agenda; undated) and gives them as a brief. The brief lists fixed
commitments (timed items, exam-like dated items) with the days of the period before them, and
each item's allowed days. The model answers with `submit_plan` (a decision per item id,
availability quoted from the user, suggestions that aren't tasks). `check_plan` refuses:
dropped items; work after a deadline; moved deadlines; any time not inside a window the user
stated, or within an hour of a commitment on record; a window whose quote names another
weekday; and free text stating a time, a duration, a wrong deadline weekday or a save nobody
made. The model fixes it: valid decisions (and valid windows) are kept, so a correction resends only what was wrong, and the merged plan is checked again. After the first call a plan turn is offered only submit_plan (lookups can't use up the calls a correction needs); the brief gives every date with its weekday. The accepted plan ends the turn and is rendered by code; important
items left without time are listed first ("Needs attention, not fitted"). A schedule's set_due /
timed work become a proposal. Plans never ask; schedules may ask once per goal, but never what
the user already said or the brief holds (`planning.question_problem`). Without a valid plan
the answer lists what is on record. `set_due` only dates undated items. The backlog guard
matches an existing item to an issue only by an explicit #N (fuzzy title matching is kept only
for new items). Tests: `tests/test_agent.py`.

## The real database

The real `~/.local/share/jarvis/jarvis.db` is at version 1 (upgraded on 2026-10-09 during
step 1; pre-upgrade backup `jarvis.db.bak-v0-20261009T221204Z`). The installed `jarvis`
commands run this checkout (editable install), so the next start of the window, the CLI or the
service upgrades it to version 2, after a verified backup. The running service keeps its old
code until restarted and works with either version.

## Tests

`QT_QPA_PLATFORM=offscreen JARVIS_REDUCE_MOTION=1 uv run pytest -q`: 978 pass.
`tests/conftest.py` refuses any SQLite connection inside `~/.local/share/jarvis` during tests
and fails the run if one was attempted. `tests/test_migrations.py` covers the upgrade machinery,
`tests/test_life_os_schema.py` the version 2 tables, `tests/test_items.py` the item service,
`tests/test_capture.py` capture and `jarvis add`, `tests/test_gui_capture.py` the overlay, `tests/test_gui_shell.py` the shell,
`tests/test_tasks_data.py` and `tests/test_gui_life.py` Tasks and Home,
`tests/test_graph_items.py` and `tests/test_gui_graph_items.py` items in the graph.
