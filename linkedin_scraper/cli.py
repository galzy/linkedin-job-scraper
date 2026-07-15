"""The command-line interface: argument parsing and the subcommands behind it."""

import argparse
import shutil
import sys
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

from linkedin_scraper.config import ConfigurationError, load_config
from linkedin_scraper.constants import CONFIG_PATH, CONFIGS_PATH, DB_PATH, MAX_PAGES, PROJECT_ROOT, RECHECK_DAYS
from linkedin_scraper.filters import relevance_predicate
from linkedin_scraper.logger import init_logging
from linkedin_scraper.main import main as run_scrape
from linkedin_scraper.net.http import HttpClient
from linkedin_scraper.scrape.scraping import BlockedError, NoFilteringSessionError, fetch_postings
from linkedin_scraper.store.db import JobsDb

SAMPLE_CONFIG = CONFIGS_PATH / "config.sample.yaml"


def _version() -> str:
    """The project version, read from pyproject.toml — its single source of truth."""
    data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def _has_db() -> bool:
    """Whether a jobs database exists; warns and returns False when nothing has been scraped yet."""
    if DB_PATH.exists():
        return True
    logger.warning("No jobs stored yet — run scrape first")
    return False


def init_config(path: str | Path) -> None:
    """Copy the sample config to ``path``, refusing to overwrite an existing file."""
    dest = Path(path)
    if dest.exists():
        print(f"{dest} already exists; refusing to overwrite", file=sys.stderr)
        raise SystemExit(1)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SAMPLE_CONFIG, dest)
    print(f"wrote {dest} from {SAMPLE_CONFIG.name}; edit it, then scrape")


def recompute(config_file: str | Path) -> None:
    """Re-derive a config's verdicts over every stored job — relevance, then duplicate counts — no scraping."""
    init_logging()
    config = load_config(config_file)
    if not _has_db():
        return
    db = JobsDb(path=str(DB_PATH))
    db.create_schema()
    db.refresh_relevance(predicate=relevance_predicate(config))
    db.refresh_dup_counts()  # relevance moved the kept set, so the per-group counts must follow
    db.close()


def refresh(config_file: str | Path, recheck_days: int = RECHECK_DAYS) -> None:
    """Fetch descriptions and re-check open-status for stored relevant jobs that are due."""
    init_logging()
    config = load_config(config_file)
    if not _has_db():
        return
    db = JobsDb(path=str(DB_PATH))
    db.create_schema()
    cutoff = (datetime.now() - timedelta(days=recheck_days)).isoformat(sep=" ", timespec="seconds")
    jobs = db.postings_to_refresh(cutoff)
    if not jobs:
        logger.info("No stored jobs are due for a refresh")
        db.close()
        return

    client = HttpClient.from_config(config.http)
    counts = fetch_postings(jobs, client, config.http.description_workers, db.record_postings)
    client.close()
    db.close()
    logger.success(
        f"Filled {counts['described']} descriptions, verified {counts['checked']} postings "
        f"({counts['closed']} now closed)"
    )


def status() -> None:
    """Print the last run and the stored-job totals."""
    if not _has_db():
        return
    db = JobsDb(path=str(DB_PATH))
    run = db.last_run()
    counts = db.totals()
    db.close()
    if run is None:
        print("No runs recorded yet")
    else:
        print(
            f"Last run: {run.started_at} -> {run.finished_at} ({run.status}):"
            f" scraped {run.scraped}, deduped {run.deduped}, relevant {run.relevant},"
            f" added {run.added}, flipped {run.flipped}"
        )
    print(
        f"Stored: {counts['stored']} jobs, {counts['relevant']} relevant,"
        f" {counts['missing_descriptions']} missing descriptions"
    )


def page_count(value: str) -> int:
    """An argparse type: a page cap inside the range LinkedIn serves."""
    pages = int(value)
    if not 1 <= pages <= MAX_PAGES:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_PAGES}")
    return pages


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="linkedin_scraper")
    parser.add_argument("--version", action="version", version=_version())
    sub = parser.add_subparsers(dest="command", required=True)

    scrape = sub.add_parser("scrape", help="scrape jobs into the database")
    scrape.add_argument("config", nargs="?", default=CONFIG_PATH, help="a config to scrape with")
    scrape.add_argument(
        "--max-pages",
        type=page_count,
        default=MAX_PAGES,
        help=f"stop each query after this many pages (default: {MAX_PAGES}, i.e. page to exhaustion)",
    )

    init = sub.add_parser("init-config", help="write a starter config from the sample")
    init.add_argument("path", help="where to write the new config")

    recompute_cmd = sub.add_parser(
        "recompute",
        help="re-derive stored verdicts (relevance and duplicate counts) from a config, no scraping",
    )
    recompute_cmd.add_argument("config", nargs="?", default=CONFIG_PATH, help="the config whose filters to apply")

    refresh_cmd = sub.add_parser("refresh", help="fetch missing descriptions and re-check open-status for stored jobs")
    refresh_cmd.add_argument("config", nargs="?", default=CONFIG_PATH, help="the config providing HTTP settings")
    refresh_cmd.add_argument(
        "--recheck-days",
        type=int,
        default=RECHECK_DAYS,
        help=f"re-check a posting's open-status once it is older than this many days (default: {RECHECK_DAYS})",
    )

    sub.add_parser("status", help="show the last run and stored-job totals")

    return parser


def main() -> None:
    """Parse the command line and run the chosen subcommand."""
    args = build_parser().parse_args()
    try:
        if args.command == "scrape":
            run_scrape(args.config, max_pages=args.max_pages)
        elif args.command == "init-config":
            init_config(args.path)
        elif args.command == "recompute":
            recompute(args.config)
        elif args.command == "refresh":
            refresh(args.config, args.recheck_days)
        elif args.command == "status":
            status()
    # A cron job has nothing but the exit status to go on: 1 config/DB error, 2 usage, 3 blocked,
    # 4 no filtering session.
    except BlockedError as e:
        logger.error(e)
        sys.exit(3)
    except NoFilteringSessionError as e:
        logger.error(e)
        sys.exit(4)
    except (ConfigurationError, SQLAlchemyError) as e:
        logger.error(e)
        sys.exit(1)
