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

## Tests

```sh
uv run pytest -q
```

## Phase status

Phases 1 to 5 done (reader, briefing, window, electrical cards, fast loop); next is phase 6 (research loop).
