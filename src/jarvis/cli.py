from __future__ import annotations

import argparse
import sys
import textwrap
import time

from jarvis import cards as cardlib
from jarvis.briefing import build_briefing, render_briefing
from jarvis.checks import STATUS_ORDER
from jarvis.config import Config
from jarvis.db import connect
from jarvis.electrical import active_counts, carded_findings
from jarvis.facts import compute_facts
from jarvis.github import GitHubClient
from jarvis.lock import RefreshBusy, refresh_lock
from jarvis.ollama import OllamaClient
from jarvis.pipeline import load_checks, refresh, run_checks
from jarvis.summary import render_summary
from jarvis.sync import sync


def _run_sync(cfg: Config, conn, quiet: bool = False) -> dict[str, str]:
    if not cfg.token:
        print("GITHUB_TOKEN not set; continuing unauthenticated", file=sys.stderr)
    with GitHubClient(cfg.repo, cfg.token, conn) as client:
        outcomes = sync(conn, client)
    out = sys.stderr if quiet else sys.stdout
    for name, outcome in outcomes.items():
        print(f"{name}: {outcome}", file=out)
    return outcomes


def _run_brief(cfg: Config, conn, force: bool = False, no_model: bool = False,
               show_facts: bool = False, verbose: bool = False) -> None:
    start = time.monotonic()
    client = None if no_model else OllamaClient(cfg.ollama_url, cfg.model)
    try:
        b = build_briefing(conn, client, force=force)
    finally:
        if client:
            client.close()
    print(render_briefing(b), end="")
    if show_facts:
        print("\nFacts")
        for f in compute_facts(conn):
            mark = "~" if not f.certain else " "
            print(f" {mark} [{f.priority}] {f.id}: {f.text}")
    if verbose:
        err = sys.stderr
        print(f"\n[{'cache hit' if b.from_cache else 'generated'} in "
              f"{time.monotonic() - start:.1f}s, facts {b.facts_hash[:12]}]", file=err)
        if b.fallback_reason:
            print(f"[fallback: {b.fallback_reason}]", file=err)
        for text, why in b.dropped:
            print(f"[dropped: {why}] {text}", file=err)
        if not b.dropped and not b.from_cache:
            print("[no sentences dropped]", file=err)


def _electrical_line(conn) -> str:
    counts = active_counts(conn)
    n = sum(counts.values())
    detail = ", ".join(f"{counts[s]} {s}" for s in STATUS_ORDER if counts.get(s))
    return f"{n} electrical card{'s' if n != 1 else ''} active" + (f" ({detail})" if detail else "")


BUSY = "A refresh is already running in the background (jarvis watch or the window)"


def _show_stored_briefing(conn) -> None:
    """Print the newest stored briefing without syncing or calling the model."""
    from jarvis.briefing import _from_row
    from jarvis.gui.data import latest_briefing_row

    row = latest_briefing_row(conn)
    if row is None:
        print("No briefing stored yet.")
        return
    print(render_briefing(_from_row(row, len(compute_facts(conn)))), end="")
    print("\n" + _electrical_line(conn))


def _run_default(cfg: Config, conn) -> None:
    if not cfg.token:
        print("GITHUB_TOKEN not set; continuing unauthenticated", file=sys.stderr)
    try:
        with refresh_lock(cfg.db_path):
            r = refresh(conn, cfg)
    except RefreshBusy:
        print(f"{BUSY}; showing the stored briefing.", file=sys.stderr)
        _show_stored_briefing(conn)
        return
    for name, outcome in r.sync.items():
        print(f"{name}: {outcome}", file=sys.stderr)
    for note in r.notes:
        print(note, file=sys.stderr)
    print(render_briefing(r.briefing), end="")
    extra = []
    if r.cards_new:
        extra.append(f"{r.cards_new} new")
    if r.cards_resolved:
        extra.append(f"{r.cards_resolved} auto-resolved this run")
    print("\n" + _electrical_line(conn) + (f"; {', '.join(extra)}" if extra else ""))


def _run_checks(cfg: Config, conn, no_model: bool = False) -> int:
    loaded = load_checks(conn, cfg)
    if isinstance(loaded, str):
        print(loaded)
        return 1
    src, _hw, findings = loaded
    client = None if no_model else OllamaClient(cfg.ollama_url, cfg.model)
    try:
        new, resolved, notes = run_checks(conn, cfg, client)
    finally:
        if client:
            client.close()
    print(f"hardware: {src.label}")
    carded = {f.check_id for f in carded_findings(findings)}
    for status in STATUS_ORDER:
        group = [f for f in findings if f.status == status]
        if not group:
            continue
        print(f"\n{status.upper()} ({len(group)})")
        for f in group:
            mark = "*" if f.check_id in carded else " "
            print(f" {mark} {f.check_id}")
            print(f"     {f.title}")
            print(textwrap.fill(f.why, width=88, initial_indent="     ",
                                subsequent_indent="     "))
            nums = ", ".join(f"{k}={_fmt_num(v)}" for k, v in f.numbers.items())
            if nums:
                print(textwrap.fill(nums, width=88, initial_indent="     numbers: ",
                                    subsequent_indent="       "))
    print("\n* = has a card.  " + _electrical_line(conn)
          + f"; {new} new, {resolved} auto-resolved this run")
    for note in notes[1:]:
        print(f"note: {note}", file=sys.stderr)
    return 0


def _fmt_num(v) -> str:
    from jarvis.checks import fmt
    return fmt(v) if isinstance(v, float) else ("null" if v is None else str(v))


def _run_cards(conn, show_all: bool) -> None:
    statuses = ("active", "done", "auto_resolved") if show_all else ("active",)
    rows = [c for st in statuses for c in cardlib.list_cards(conn, st)]
    if not rows:
        print("No cards." if show_all else "No active cards.")
        return
    for c in rows:
        print(f"{c.id:>4}  {c.status:<13} {c.type:<10} {c.title}")


def _run_research(cfg: Config, conn, dry_run: bool, verbose: bool,
                  issue: int | None) -> int:
    from jarvis.research.runner import render_result, run_research
    from jarvis.research.settings import load_settings
    from jarvis.research.sources import ArxivClient

    settings, note = load_settings(cfg.research_path)
    if not cfg.token:
        print("GITHUB_TOKEN not set; release checks run unauthenticated", file=sys.stderr)
    with GitHubClient(cfg.repo, cfg.token, conn) as gh, ArxivClient() as arxiv, \
            OllamaClient(cfg.ollama_url, cfg.model) as ollama:
        res = run_research(conn, settings, repo=cfg.repo, ollama=ollama, arxiv=arxiv, gh=gh,
                           dry_run=dry_run, only_issue=issue, notes=[note] if note else None)
    print(render_result(res, settings, verbose=verbose), end="")
    return 1 if res.errors and not res.cards else 0


def _run_add(cfg: Config, conn, text: str, plain: bool = False,
             interactive: bool | None = None, read=input, client=None) -> int:
    """``jarvis add``: capture free text. 0 saved, 1 not saved (error or no answer),
    3 a question was left unanswered (not a terminal)."""
    from jarvis import capture as cap

    if interactive is None:
        interactive = sys.stdin.isatty()
    if plain:
        res = cap.capture_plain(conn, text)
    else:
        own = client is None
        client = cap.make_client(cfg) if own else client
        try:
            res = cap.capture(conn, text, client)
            while res.status == "clarify":
                c = res.clarification
                print(c.question)
                for n, (label, _) in enumerate(c.options, 1):
                    print(f"  {n}) {label}")
                if not interactive:
                    print("Not saved. Run it again with the date spelled out, or answer in "
                          "a terminal.", file=sys.stderr)
                    return 3
                try:
                    reply = read("> " if not c.options else "number or answer> ").strip()
                except EOFError:
                    reply = ""
                if not reply:
                    print("Not saved.", file=sys.stderr)
                    return 1
                if reply.isdigit() and 1 <= int(reply) <= len(c.options):
                    reply = c.options[int(reply) - 1][1]
                res = cap.capture(conn, text, client, answers=res.answered(reply))
        finally:
            if own:
                client.close()
    if not res.saved:
        print(cap.summary(res), file=sys.stderr)
        if not plain and "model" in (res.error or ""):
            print('Use  jarvis add --plain "..."  to save it as typed.', file=sys.stderr)
        return 1
    print(cap.summary(res))
    if res.item.body:
        print(f"  {res.item.body}")
    for note in res.interpretation.notes:
        print(f"  note: {note}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("sync", help="fetch the latest repo data")
    sp = sub.add_parser("summary", help="print a raw summary from the local database")
    sp.add_argument("--days", type=int, default=7)
    bp = sub.add_parser("brief", help="print the briefing from the local database")
    bp.add_argument("--force", action="store_true", help="regenerate even if cached")
    bp.add_argument("--no-model", action="store_true", help="use the plain template")
    bp.add_argument("--show-facts", action="store_true", help="also list the facts")
    bp.add_argument("--verbose", action="store_true",
                    help="print timing, fallback reason and dropped sentences to stderr")
    cp = sub.add_parser("checks", help="run the electrical checks and update the cards")
    cp.add_argument("--no-model", action="store_true", help="template card text only")
    lp = sub.add_parser("cards", help="list cards")
    lp.add_argument("--all", action="store_true", help="include done and auto-resolved")
    gp = sub.add_parser("card", help="print a card's ready-to-paste Claude prompt")
    gp.add_argument("id", type=int)
    dp = sub.add_parser("done", help="mark a card done (it never comes back)")
    dp.add_argument("id", type=int)
    rp = sub.add_parser("research", help="search arXiv and library releases for open issues")
    rp.add_argument("--dry-run", action="store_true",
                    help="show queries, candidates, verdicts and the cards it would create; "
                         "write nothing")
    rp.add_argument("--verbose", action="store_true",
                    help="list skipped issues, queries, similarities and verdicts")
    rp.add_argument("--issue", type=int, default=None, help="only this issue number")
    adp = sub.add_parser("add", help="capture a task, idea or reminder in plain words")
    adp.add_argument("text", nargs="+", help='e.g. "remind me to buy cat food tomorrow at 5pm"')
    adp.add_argument("--plain", action="store_true",
                     help="save the text as a task as typed, without the model")
    ap = sub.add_parser("ask", help="ask a question about the project (answered from synced data)")
    ap.add_argument("question", nargs="+")
    wp = sub.add_parser("watch", help="keep the database current (the fast loop daemon)")
    wp.add_argument("--interval", type=int, default=None,
                    help="seconds between GitHub checks (default $JARVIS_INTERVAL or 180; "
                         "never below 60 or GitHub's X-Poll-Interval)")
    wp.add_argument("--once", action="store_true", help="run one tick and exit")
    wp.add_argument("--verbose", action="store_true", help="per-tick detail")
    args = parser.parse_args(argv)

    cfg = Config.from_env()
    if args.cmd == "ask":
        from jarvis.chat import _main as ask_main

        return ask_main([" ".join(args.question)])
    if args.cmd == "watch":
        from jarvis.daemon import run_watch

        return run_watch(cfg, interval=args.interval, once=args.once, verbose=args.verbose)
    conn = connect(cfg.db_path)
    try:
        if args.cmd == "summary":
            print(render_summary(conn, days=args.days, repo=cfg.repo), end="")
        elif args.cmd == "add":
            return _run_add(cfg, conn, " ".join(args.text), plain=args.plain)
        elif args.cmd == "research":
            try:
                with refresh_lock(cfg.db_path):
                    return _run_research(cfg, conn, args.dry_run, args.verbose, args.issue)
            except RefreshBusy:
                print(f"{BUSY}; try again in a moment.", file=sys.stderr)
                return 75
        elif args.cmd in ("sync", "brief", "checks"):
            # These write what a refresh writes (and may call the model): one at a time.
            try:
                with refresh_lock(cfg.db_path):
                    if args.cmd == "sync":
                        _run_sync(cfg, conn)
                    elif args.cmd == "brief":
                        _run_brief(cfg, conn, force=args.force, no_model=args.no_model,
                                   show_facts=args.show_facts, verbose=args.verbose)
                    else:
                        return _run_checks(cfg, conn, no_model=args.no_model)
            except RefreshBusy:
                print(f"{BUSY}; try again in a moment.", file=sys.stderr)
                return 75  # EX_TEMPFAIL
        elif args.cmd == "cards":
            _run_cards(conn, args.all)
        elif args.cmd in ("card", "done"):
            card = cardlib.get_card(conn, args.id)
            if card is None:
                print(f"no card {args.id}", file=sys.stderr)
                return 1
            if args.cmd == "done":
                cardlib.mark_done(conn, card.id)
                print(f"card {card.id} done: {card.title}")
            else:
                print(f"# card {card.id} [{card.status}] {card.title}\n# {card.why}\n",
                      file=sys.stderr)
                print(card.claude_prompt, end="")
        else:
            _run_default(cfg, conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
