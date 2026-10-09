from __future__ import annotations

import argparse
import sys

from jarvis.config import Config
from jarvis.db import connect
from jarvis.github import GitHubClient
from jarvis.summary import render_summary
from jarvis.sync import sync


def _run_sync(cfg: Config, conn) -> dict[str, str]:
    if not cfg.token:
        print("GITHUB_TOKEN not set; continuing unauthenticated", file=sys.stderr)
    with GitHubClient(cfg.repo, cfg.token, conn) as client:
        outcomes = sync(conn, client)
    for name, outcome in outcomes.items():
        print(f"{name}: {outcome}")
    return outcomes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("sync", help="fetch the latest repo data")
    sp = sub.add_parser("summary", help="print a summary from the local database")
    sp.add_argument("--days", type=int, default=7)
    args = parser.parse_args(argv)

    cfg = Config.from_env()
    conn = connect(cfg.db_path)
    try:
        if args.cmd == "summary":
            print(render_summary(conn, days=args.days, repo=cfg.repo), end="")
        else:
            _run_sync(cfg, conn)
            if args.cmd is None:
                print()
                print(render_summary(conn, repo=cfg.repo), end="")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
