# Roadmap

Jarvis is growing from a one-project engineering assistant into a personal Life OS (school,
tasks, projects, knowledge, a local assistant), keeping the dark command-center look and the
existing project cockpit.

## Phase 1: foundation, navigation, quick capture

| Step | What | Status |
|---|---|---|
| 1 | Versioned migrations (`user_version`), verified backups, restore, one schema owner | done |
| 2 | Core data: `projects` (LiDAR repo as project 1), `items` (task / idea / reminder), `links` | done |
| 3 | `jarvis/items.py`: item service (CRUD, complete, soft delete, open / today / overdue) | done |
| 4 | `jarvis/capture.py`: natural-language capture (local model fills a form; Python checks every field) | done |
| 5 | App shell: navigation rail, Home / Tasks / School / Projects / Knowledge, chat at shell level | done |
| 6 | Quick capture: Ctrl+N overlay and `jarvis add "..."` | done |
| 7 | Tasks workspace v0: Overdue / Today / Upcoming / Someday | done |
| 8 | Home dashboard v0 from stored data | done |
| 9 | Task items in the graph | done |

Decided for Phase 1: GitHub issues stay in Projects (not copied into tasks); in-app capture only
(no global hotkey); reminder times are stored and shown, no notifications; School and Knowledge
are placeholders; the assistant keeps its project-only knowledge and moves to the shell.

## Later

Courses and notes data, School and Knowledge workspaces, the assistant answering about tasks and
items, a global capture hotkey, reminder notifications, phone access.
