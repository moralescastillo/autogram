"""Command-line entry point.

    autogram run [--dry-run]

``--dry-run`` walks the whole pipeline — discovery, validation, scheduling,
staging decisions — and reports what would be published without calling
Instagram or moving anything. Worth using before every real change; the
previous system had it on every stage and it earned its keep.
"""

from __future__ import annotations

import argparse
import logging
import sys

from autogram import __version__
from autogram.config import ConfigError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autogram",
        description="Publish scheduled content to Instagram from your own storage.",
    )
    parser.add_argument("--version", action="version", version=f"autogram {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Do one scheduling pass and publish if due.")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would happen without publishing or moving anything.",
    )
    run.add_argument(
        "--verbose", "-v", action="store_true", help="Log every step."
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    try:
        if args.command == "run":
            from autogram.run import run as do_run

            return do_run(dry_run=args.dry_run)
    except ConfigError as exc:
        # Configuration problems are the user's to fix, so they get a plain
        # message rather than a traceback.
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
