# Jarvis Assistant Build Plan

Oct 8, 2026 · @Tal Elbert

A Linux desktop app that reads your pan-tilt repo on GitHub, uses gemma4:26b through Ollama to write suggestion cards, and gives each card a button that copies a ready-made prompt for Claude.

## At a glance

&#91;embedded content: how the parts fit · 3 inputs, 5 backend parts, the window and Claude\]

The two loops feed the model, the model asks Python for the numbers, and the cards land in a database that the window reads. Claude only sees what you choose to paste.

## What v1 does

v1 watches one project, the pan-tilt tracker, and shows two kinds of suggestion cards: electrical sanity checks and research updates.

**In v1**

- Reads the repo's issues, milestones and commits from GitHub. It does not read the code, to keep the local model fast. The one exception is `hardware.yaml`, fetched through the contents API with its own ETag, so a wiring change in a commit refreshes the checks.
- Electrical cards and research cards, each tied to something real in the repo, such as an open servo issue.
- A copy button on every card that puts a ready-made prompt on the clipboard for Claude.
- A done button, and a saved list so finished or dismissed cards never come back.
- A "not useful" button with an optional reason (vague, wrong, repeat), logged so phase 7 tuning is a query, not memory.
- Runs as a headless backend daemon plus a separate window that only reads, so a web view can be added later.

**Later**

- Parts research, which only matters when a project starts.
- School and other topics, added as new context sources such as Obsidian notes.
- Reading the code itself, to catch things like pin conflicts.
- A web view for the laptop.

## Staying current

Stale suggestions are what would make you stop trusting the app, so freshness is the main design rule. Your desktop has no public address for GitHub webhooks, so the app asks GitHub for changes on a timer. It feels instant with a delay of a few minutes.

| Loop | Runs | Does | Regenerates |
| --- | --- | --- | --- |
| Fast | Every 3 to 5 minutes | Asks GitHub whether issues, milestones, commits or `hardware.yaml` changed (repo events endpoint plus ETags). If not, stops. | Electrical cards |
| Slow | Every few hours | Searches outside sources for new work related to your open issues. | Research cards |

**How a card stays honest**

1. **Auto-resolve.** Electrical cards close themselves when their Python check passes, re-run whenever `hardware.yaml` changes. Research cards close when the issue they point at closes.
2. **One fingerprint table.** Every card ever made, active, done or resolved, keeps its fingerprint. A new card whose fingerprint already exists is never shown again.
   - Electrical: `electrical + source + check_id`, so one issue can carry several distinct concerns.
   - Research: `research + paper_id + issue`, so the same paper never returns, even for another issue.

**Cheap polling.** Rate limits are not a concern (5,000 authenticated requests an hour against roughly 60 used). The repo events endpoint gives one "what changed" call, and conditional requests (saved ETags) make unchanged answers a short 304.

## Cards

A card is one suggestion, tied to one thing in the repo, with a prompt ready to paste into Claude.

| Field | Holds |
| --- | --- |
| type | electrical or research |
| title | One line, for example "Power check: servos and LiDAR share the 5V rail" |
| why | One or two sentences on what in the repo triggered it |
| source | The issue, milestone or commit it points at, as a link |
| claude\_prompt | The full text the copy button puts on the clipboard |
| status | active, done or auto-resolved |
| check\_id / paper\_id | Which Python check, or which arXiv paper, produced the card |
| fingerprint | Type plus source plus check or paper id, used so a dismissed card never returns |
| feedback | Optional "not useful" reason: vague, wrong or repeat |

**Copy flow.** You press copy, switch to Claude, and paste. The prompt already contains the project context, the flagged issue and a clear question, so you never retype the setup. Claude does the deeper review; the local model only finds what is worth reviewing.

## Stack

One language and a small set of parts, so Claude Code can build it in pieces and you can read all of it.

| Part | Choice | Why |
| --- | --- | --- |
| Language | Python 3 | One language for the backend, the AI calls and the window |
| Window | PySide6 (Qt) | Native Linux window, notifications and clipboard, no browser wrapper |
| Local AI | Ollama at localhost:11434 with gemma4:26b | Already installed; pass a JSON schema in Ollama's `format` parameter so cards always parse |
| GitHub | REST API with a read-only fine-grained token | Supports the conditional requests the fast loop relies on |
| Storage | SQLite file in WAL mode | Cards, fingerprints, feedback and saved ETags in one place; WAL lets the daemon write while the window reads |
| Scheduling | Background thread or APScheduler | Runs both loops inside the app |
| Research sources | arXiv API plus release feeds of libraries the project uses | Free and need no keys. Confirm the list once the app runs |
| Electrical math | Plain Python functions | The model picks what to check; Python does the arithmetic |
| Autostart | systemd user service for the headless daemon only | Loops keep running after login; the token comes from an `EnvironmentFile=` with mode 600. The window opens on demand from a keybind |
| Notifications | `notify-send` from the daemon | Shown by mako on Omarchy, no GUI process needed |

The backend (loops, GitHub, Ollama, database) lives in its own package and runs as a headless daemon; the window is a separate process that only reads from the database and writes done and feedback marks. That is what makes a web view for the laptop a small addition later.

## Build order

Each phase ends with something you can run and check, so a broken phase is caught before the next one builds on it.

1. **Project state reader.** Pull issues, milestones and commits from the pan-tilt repo into SQLite and print a plain summary of what is open, what is done and what changed recently. No AI yet.
2. **Electrical cards.** Add `hardware.yaml` to the repo listing each part with its voltage and current limits from the datasheets, and fetch it via the contents API. Python runs every check and produces deterministic findings with template text, no model yet. Then add Gemma only to link each finding to a related issue and polish the wording. Print the cards in the terminal.
3. **The window.** Show the cards, with a copy-prompt button, a done button and a "not useful" button with a reason. Done writes the fingerprint to the database.
4. **Fast loop.** Poll the repo events endpoint and `hardware.yaml` with ETags, re-run checks only when something changed, and auto-resolve electrical cards whose check now passes.
5. **Research loop.** Every few hours, fetch from the research sources, dedupe by arXiv id, have Ollama keep only what relates to open issues, and make research cards. Auto-resolve them when their issue closes.
6. **Daily use.** Headless daemon as a systemd user service, `notify-send` notifications for new cards, and a Hyprland keybind to open the window. Add a tray icon only if your bar shows one.
7. **Tune.** Use it for a week, then fix the cards that annoyed you: repeats, vague titles or wrong math. This phase decides whether you keep using it.

## Risks

| Risk | What could go wrong | Plan |
| --- | --- | --- |
| Model speed | A 26B model that does not fit fully in GPU memory slows down a lot, and the fast loop would lag | Run `ollama ps` in phase 2 to see whether it runs fully on the GPU. Send only issue summaries, never code. If it is slow, use a smaller model for the fast loop |
| Wrong electrical math | Language models make arithmetic mistakes, and a confident wrong number is worse than none | Python does every calculation from the hardware file. The model only chooses which checks apply and writes the card text |
| Stale or repeated cards | One repeated suggestion is enough to make you stop trusting it | One fingerprint table including check or paper ids, auto-resolve when the check passes, and the full card history |
| Database locked | Daemon writes while the window reads | SQLite WAL mode from phase 1 |
| Weak research results | Free sources may return papers that are too general | Start with narrow queries drawn from open issue titles, then adjust the source list after a week of use |
| Hardware file goes out of date | Checks use old values after a part change | It lives in the repo and is fetched with its own ETag; update it in the same commit as the wiring change |

## Starter prompt for Claude Code

Paste this into Claude Code in an empty project folder. It builds phase 1 only, so you can check each step before the next.

```text
I'm building "Jarvis", a Linux-only personal assistant app for my engineering projects. Build it in phases and stop after each phase so I can test it.

What it does:
- Watches one GitHub repo (my STM32 pan-tilt LiDAR tracker: SuperDabT/stm32-lidar-scanner) through the GitHub REST API, using a read-only fine-grained token from the environment variable GITHUB_TOKEN.
- Uses a local Ollama model (gemma4:26b at http://localhost:11434) to turn what it finds into suggestion cards of two types: "electrical" (power and wiring sanity checks) and "research" (new outside work relevant to my open issues).
- Each card has: type, title, why, source (the issue or commit it points at), check_id or paper_id, claude_prompt (text I copy and paste into Claude), status (active, done or auto-resolved), feedback, and a fingerprint (type + source + check_id or paper_id) so a dismissed card never returns.
- Auto-resolve electrical cards when their Python check passes; auto-resolve research cards when their issue closes.

Rules:
- Python 3, SQLite in WAL mode, PySide6 for the window later. The backend is its own package and runs as a headless daemon; the GUI is a separate reader process.
- Use conditional requests (If-None-Match with a saved ETag) when polling GitHub so unchanged repos cost nothing.
- Never let the language model do arithmetic. All electrical calculations are plain Python functions that read from a hardware.yaml file listing each part with its voltage and current limits.
- Do not read the repo's code, only issues, milestones, commits and the single file hardware.yaml via the contents API.

Phase 1 only: fetch issues, milestones, recent commits and hardware.yaml (if present) for the repo into SQLite, then print a plain summary of what is open, what is closed and what changed recently. No AI and no window yet. Include a README with setup steps and tests for the ETag handling.
```

After phase 1 works, ask Claude Code for phase 2 from the build order above.
