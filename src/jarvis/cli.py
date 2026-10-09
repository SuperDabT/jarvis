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
    wp = sub.add_parser("watch", help="keep the database current (the fast loop daemon)")
    wp.add_argument("--interval", type=int, default=None,
                    help="seconds between GitHub checks (default $JARVIS_INTERVAL or 180; "
                         "never below 60 or GitHub's X-Poll-Interval)")
    wp.add_argument("--once", action="store_true", help="run one tick and exit")
    wp.add_argument("--verbose", action="store_true", help="per-tick detail")
    args = parser.parse_args(argv)

    cfg = Config.from_env()
    if args.cmd == "watch":
        from jarvis.daemon import run_watch

        return run_watch(cfg, interval=args.interval, once=args.once, verbose=args.verbose)
    conn = connect(cfg.db_path)
    try:
        if args.cmd == "summary":
            print(render_summary(conn, days=args.days, repo=cfg.repo), end="")
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
