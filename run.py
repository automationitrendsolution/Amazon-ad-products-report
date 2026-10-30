#!/usr/bin/env python3
"""Products report command-line entry point.

Opens Amazon Ads Campaign Manager -> Products (Advertised products), selects
the Today or Yesterday date range, shows 200 results per page, exports every
page, and replaces that date in powerbidb.Products.

    python run.py yesterday             # cron: daily at 14:00 IST
    python run.py today                 # the US day in progress
    python run.py yesterday --dry-run   # full workflow, no MySQL writes
    python run.py login                 # one time: sign in + OTP, saves the session

Exit codes: 0 success, 2 config, 3 authentication (needs a person),
4 navigation, 5 page setup, 6 download, 7 CSV, 8 values, 9 database,
11 locked, 14 date mismatch, 15 incomplete export.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app import __version__
from app.config import PERIODS, load_settings
from app.exceptions import AdFlowError, ConfigurationError
from app.logging_config import configure_logging, get_logger
from app.main import ProductsReportPipeline, RunMode, RunOptions, handle_failure


_EXIT_CODES = """exit codes:
  0 success, 2 config, 3 authentication (needs a person), 4 navigation,
  5 page setup (required filter missing), 6 download, 7 CSV, 8 values,
  9 database, 11 locked, 14 date mismatch, 15 incomplete export
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="products-report",
        description=(
            "Export the Amazon Ads Products page (Today or Yesterday, every page) "
            "and load it into powerbidb.Products."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_EXIT_CODES,
    )
    parser.add_argument("command", choices=[*PERIODS, "login"],
                        help="today / yesterday: export that date and load it; "
                             "login: one-time Amazon sign-in (OTP typed here), saves the session.")
    parser.add_argument("--dry-run", action="store_true", help="Do everything except write to MySQL.")
    parser.add_argument("--env-file", type=Path, metavar="PATH", help="Alternative .env file.")
    parser.add_argument("--version", action="version", version=f"products-report {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.env_file)
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return ConfigurationError.exit_code

    settings.ensure_directories()
    configure_logging(settings.log_dir, level=settings.log_level, retention_days=settings.log_retention_days)
    logger = get_logger("cli")

    if args.command == "login":
        options = RunOptions(mode=RunMode.LOGIN)
    else:
        options = RunOptions(mode=RunMode.LOAD_PRODUCTS, period=args.command, dry_run=args.dry_run)
    pipeline = ProductsReportPipeline(settings, options)
    try:
        pipeline.run()
    except AdFlowError as exc:
        handle_failure(pipeline, exc)
        return exc.exit_code
    except KeyboardInterrupt:
        logger.warning("Interrupted")
        return 130
    except Exception:  # already logged with traceback
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
