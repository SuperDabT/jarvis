# Jarvis

Jarvis is a local assistant for an embedded project. It mirrors a GitHub repo's issues, milestones, commits and `hardware.yaml` into SQLite and prints a status summary. Later phases add suggestion cards on top.

## Setup

```sh
uv sync
```

Create a read-only fine-grained GitHub token on the one repo with Issues, Metadata and Contents read access, then:

```sh
export GITHUB_TOKEN=...
uv run jarvis            # sync, then summary
uv run jarvis sync       # sync only
uv run jarvis summary --days 14
```

Without a token Jarvis runs unauthenticated (fine for a public repo, lower rate limit).

## Environment variables

- `JARVIS_REPO`: repo to watch as `owner/name` (default `SuperDabT/stm32-lidar-scanner`)
- `JARVIS_DB`: SQLite path (default `~/.local/share/jarvis/jarvis.db`)
- `GITHUB_TOKEN`: GitHub token
- `JARVIS_INTERVAL`: seconds between GitHub checks for `jarvis watch` (default 180; never below 60 or GitHub's `X-Poll-Interval`)
- `JARVIS_RESEARCH`: research loop settings (default `~/.config/jarvis/research.yaml`, optional; see `docs/research.example.yaml`)
- `JARVIS_RESEARCH_INTERVAL`: hours between research runs in `jarvis watch` (default 6, minimum 1)
- `JARVIS_HARDWARE`: local `hardware.yaml` (default `~/.config/jarvis/hardware.yaml`). A `hardware.yaml` in the repo root takes precedence; Jarvis never writes to the repo. See `docs/hardware.example.yaml` for the schema.

## Electrical checks

```sh
uv run jarvis checks [--no-model]   # run checks on hardware.yaml, print findings, update cards
uv run jarvis cards [--all]         # list cards
uv run jarvis card <id>             # print the ready-to-paste Claude prompt
uv run jarvis done <id>             # close a card for good
```

Python computes every number; the model (if reachable) only links an open issue and rewrites the card text, and its output is validated.

## Staying current: `jarvis watch`

```sh
uv run jarvis watch                 # run until SIGTERM / Ctrl+C
uv run jarvis watch --interval 300  # seconds between GitHub checks (or $JARVIS_INTERVAL)
uv run jarvis watch --once          # one tick, then exit (testing, cron)
uv run jarvis watch --verbose       # per-tick detail
```

Each tick asks GitHub for issues, milestones, commits and `hardware.yaml` with saved ETags (an unchanged repo answers 304). If nothing changed on GitHub, the hardware file is the same and the facts are the same (date-based facts move at midnight), the tick stops there with no model call. Otherwise it runs the electrical checks and rebuilds the briefing; both reuse cached model wording when their input is unchanged. The local `hardware.yaml` is also polled every 10 s, so an edit updates the cards within seconds without asking GitHub.

One line per tick goes to stdout:

```
18:40:02 tick: github unchanged, hardware unchanged -> skipped (0.4s)
18:43:02 tick: issues changed -> checks: 0 new 1 resolved, briefing regenerated (gemma4:26b, 11.2s)
```

Errors (offline, GitHub 5xx, rate limit, Ollama down) are logged and retried with backoff (60 s doubling to 15 min, or until a rate limit resets). While Ollama is down the template wording is shown and the model is asked again on the next tick. Only one refresh runs at a time (`<db>.lock`): the window's Refresh and `jarvis` say a refresh is already running instead of doubling model calls. A second `jarvis watch` refuses to start. The window header shows "watching · checked 2 min ago" while the daemon is alive.

## The window: `jarvis-window`

```sh
uv run jarvis-window                 # dashboard + chat; refreshes on open
uv run jarvis-window --no-refresh    # cached data only
uv run jarvis-window --theme dark    # or light; default follows the system
```

A Notion-style dashboard over the same database: the briefing as a callout at the top, four
headline numbers (nearest due milestone, its closed count, the pace needed, time since the last
commit), a milestone timeline with a "today" line, a commit grid of the last 26 weeks, open
issues by phase, the power charts per rail (worst-case current against the rating, and each
part's voltage range against the supply band), and the suggestions as a table. Click a row to
open the full card with the prompt for Claude, Done and Not useful. Every number is computed in
Python from the synced data and `hardware.yaml`; the model only writes the briefing sentences
and chat answers.

"Ask Jarvis" on the right (Ctrl+K to type, Ctrl+J to show or hide) answers questions from the
synced data with sources (issues, commits, checks), using the local model; answers stream in
and can take 10-25 s to start. Ctrl+R / F5 refresh, Esc closes the card drawer, Ctrl+Q quits.

Commit history: the first sync after upgrading fetches the last 183 days of commits once
(paginated, 100 per page); later syncs only fetch more when the newest page of commits changed.

## Research loop: `jarvis research`

```sh
uv run jarvis research --dry-run --verbose   # queries, candidates, verdicts, cards it would make; writes nothing
uv run jarvis research                       # make research cards (also runs inside jarvis watch)
uv run jarvis research --issue 10            # one issue only
```

Research cards point an open issue at outside work: arXiv papers and new releases of libraries the project uses. They are meant to be few and good; zero cards is a normal outcome.

1. **Which issues.** Open issues only. Skipped, with the reason shown in `--verbose`: issues listed in `skip_issues`, issues mapped to a library (covered by its releases), chores recognized from the title (docs, demo, code review, UI, drivers, test plumbing, planning), and issues the model marks "not research-worthy". Issues labeled `someday` are searched only when the others left room in the run's budget.
2. **Queries.** The issue title without filler or code identifiers, anchored to the sensor (`LiDAR | laser scanner | rangefinder`), plus up to two model-proposed queries (2-8 plain words, validated, cached per issue text). `queries:` in research.yaml replaces them. Each issue costs one arXiv request (its queries ORed), sorted by relevance within the last 3 years, in cs.RO / eess.SP / eess.SY / cs.CV (plus cs.LG for ML issues); at most one request every 3 s, a 429/503 is retried twice with a pause, and after two failed issues arXiv is left alone until the next run. Answers are reused for 20 hours.
3. **Prefilter.** Cosine similarity between the issue and each paper's title + abstract, using `qwen3-embedding:0.6b` through Ollama (`/api/embed`); the best 5 at or above `min_similarity` (0.40) go on. Without the embed model a keyword overlap score is used.
4. **Judge.** Gemma fills a checklist per (paper, issue): the paper's setting and technique, then four yes/no checks (same problem as the issue, comparable data such as range readings or positions, transfers to a bare-metal STM32, not already planned in the issue), a confidence, and a why. Code accepts only all-four-yes + medium/high confidence, and the why must be one or two hedged sentences ("may", "could") with no numbers absent from the paper or issue and no hype or advice words. An accepted paper then gets a second, contrastive look: the model sees all researched issues and must pick this one as the best fit (or it is dropped). Every verdict, accepted or not, is stored in `research_seen`, so a pair is never judged twice; a model error stores nothing.
5. **Cards.** At most `max_cards_per_run` (3) per run, `max_active` (8) at once, one active paper card per issue, and a paper never gets a second card for another issue. The title is the paper's own (shortened, never rewritten); the why starts with "Related to #N"; the copy button gives Claude the project, the issue, the paper's abstract and link, and the question "would this help, what would I change, what is the cheapest experiment".
6. **Releases.** For each library in `libraries` (default `STMicroelectronics/STM32CubeF4`, and `FreeRTOS/FreeRTOS-Kernel` mapped to #18), the newest release or version tag is recorded as a baseline the first time, with no card; a later tag makes one card (linked to the mapped issue if it is open) and retires the library's older release card.
7. **Auto-resolve.** A research card whose issue is closed is auto-resolved on the next fast tick (and by `jarvis research`).

`jarvis watch` runs research every `JARVIS_RESEARCH_INTERVAL` hours (default 6), first about 2 minutes after startup (or later, if the last run was recent), under the same refresh lock as the fast ticks, with one log line per run and one notification when it made cards:

```
20:09:23 research: 7 issues (17 skipped), 21 queries, 85 candidates, 21 judged (0 cached), 0 new, 0 resolved (arXiv 10s, embed 9s, judge 107s, total 155s)
```

The window's watch tooltip shows the last and next research run.

Calibration notes (October 2026, this repo's issues): arXiv has little recent work on single-beam / 2D-laser problems, so most candidates are 3D-LiDAR, drone or odometry papers, and embedding similarity alone does not separate them (junk reached 0.60, the one clearly useful older paper scored 0.41). The checklist judge rejects them; with the default 3-year window a run usually makes no paper card. `years: 6` in research.yaml found one solid card (adaptive Kalman noise covariances for lidar pedestrian tracking, for #10).

## Tests

```sh
QT_QPA_PLATFORM=offscreen JARVIS_REDUCE_MOTION=1 uv run pytest -q
```

## Database upgrades and backups

The schema version is SQLite's `user_version`; the steps are `db.MIGRATIONS`. Whatever opens the
database first (`jarvis watch`, the window, the CLI) upgrades it. Before an existing database is
changed, it is copied with SQLite's backup API to `jarvis.db.bak-v<old version>-<UTC time>` next
to it (mode 600) and the copy is checked (integrity check, same rows per table). All steps run in
one transaction, so a failed upgrade leaves the database at its old version. Two processes never
upgrade at once (`jarvis.db.migrate.lock`). A database from a newer Jarvis is refused, not touched.

To put a backup back, stop the service and close the window, then:

```sh
systemctl --user stop jarvis.service
uv run python -c "from jarvis import db; print(db.restore_backup('$HOME/.local/share/jarvis/jarvis.db.bak-v0-…', '$HOME/.local/share/jarvis/jarvis.db'))"
```

Backups are never deleted automatically.

## Phase status

Phases 1 to 7 done (reader, briefing, window, electrical cards, fast loop, research loop, daily use); phase 8 (tuning) is next.

## Daily use (runs at login)

```bash
./contrib/install.sh
```

Installs the `jarvis` and `jarvis-window` commands, a systemd user service that runs
`jarvis watch` from login, and a launcher entry. Put a `GITHUB_TOKEN` in
`~/.config/jarvis/env` (mode 600) if you want authenticated requests. New cards raise a
desktop notification. Logs: `journalctl --user -u jarvis -f`.
