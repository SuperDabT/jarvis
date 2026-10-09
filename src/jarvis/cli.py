from __future__ import annotations

import argparse
import sys
import time

from jarvis.briefing import build_briefing, render_briefing
from jarvis.config import Config
from jarvis.db import connect
from jarvis.facts import compute_facts
from jarvis.github import GitHubClient
from jarvis.ollama import OllamaClient
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
    args = parser.parse_args(argv)

    cfg = Config.from_env()
    conn = connect(cfg.db_path)
    try:
        if args.cmd == "summary":
            print(render_summary(conn, days=args.days, repo=cfg.repo), end="")
        elif args.cmd == "sync":
            _run_sync(cfg, conn)
        elif args.cmd == "brief":
            _run_brief(cfg, conn, force=args.force, no_model=args.no_model,
                       show_facts=args.show_facts, verbose=args.verbose)
        else:
            _run_sync(cfg, conn, quiet=True)
            _run_brief(cfg, conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
