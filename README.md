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

## Tests

```sh
uv run pytest -q
```

## Phase status

Phase 1 done; next is phase 2 (electrical cards).
