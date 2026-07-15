"""The command-line interface: the Typer app and the subcommands behind it."""

import shutil
import sys
import tomllib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer
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

app = typer.Typer(no_args_is_help=True, help="Scrape LinkedIn jobs, filter them, and store them in a database.")

ConfigArg = Annotated[Path, typer.Argument(help="the config file to use", show_default=False)]


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


def _page_count(value: int) -> int:
    """Validate a page cap against the range LinkedIn serves."""
    if not 1 <= value <= MAX_PAGES:
        raise typer.BadParameter(f"must be between 1 and {MAX_PAGES}")
    return value


def _positive_days(value: int) -> int:
    """Validate a strictly positive number of days."""
    if value < 1:
        raise typer.BadParameter("must be a positive number of days")
    return value


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
    db.clear_dead_descriptions()  # jobs the edit made irrelevant no longer show their text
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
    db.clear_dead_descriptions()  # a posting this run found closed no longer shows its text
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


def _confirm_prune(count: int, days: int) -> bool:
    """Two distinct terminal confirmations before a destructive prune; False aborts on any miss."""
    if not sys.stdin.isatty():
        print("prune needs an interactive terminal to confirm; aborting", file=sys.stderr)
        return False
    print(
        f"WARNING: permanently delete {count} irrelevant/closed jobs older than {days} days "
        f"from {DB_PATH.name}? This cannot be undone."
    )
    if input("Type 'yes' to continue: ").strip() != "yes":
        return False
    return input(f"Final confirmation — re-type the number {count} to delete: ").strip() == str(count)


def prune(days: int) -> None:
    """Permanently delete stored jobs that are irrelevant or closed and older than ``days`` days."""
    init_logging()
    if not _has_db():
        return
    db = JobsDb(path=str(DB_PATH))
    db.create_schema()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(sep=" ", timespec="seconds")
    count = db.count_prunable(cutoff)
    if count == 0:
        logger.info(f"No irrelevant or closed jobs older than {days} days to prune")
    elif _confirm_prune(count, days):
        logger.success(f"Pruned {db.prune_old(cutoff)} irrelevant or closed jobs older than {days} days")
    else:
        print("Aborted; nothing was deleted")
    db.close()


def _version_callback(value: bool) -> None:
    if value:
        print(_version())
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="show the version and exit"),
    ] = False,
) -> None:
    pass


@app.command(help="scrape jobs into the database; clears descriptions off the ones filtered out or closed")
def scrape(
    config: ConfigArg = CONFIG_PATH,
    max_pages: Annotated[
        int,
        typer.Option(
            callback=_page_count,
            help=f"stop each query after this many pages (default: {MAX_PAGES}, i.e. page to exhaustion)",
        ),
    ] = MAX_PAGES,
) -> None:
    run_scrape(config, max_pages=max_pages)


@app.command("init-config", help="write a starter config from the sample")
def _init_config(
    path: Annotated[Path, typer.Argument(help="where to write the new config", show_default=False)],
) -> None:
    init_config(path)


@app.command(
    "recompute",
    help="re-derive stored verdicts (relevance and duplicate counts) from a config, no scraping; "
    "clears descriptions off jobs the edit made irrelevant",
)
def _recompute(config: ConfigArg = CONFIG_PATH) -> None:
    recompute(config)


@app.command(
    "refresh",
    help="fetch missing descriptions and re-check open-status for stored jobs; "
    "clears descriptions off any the check found closed",
)
def _refresh(
    config: ConfigArg = CONFIG_PATH,
    recheck_days: Annotated[
        int, typer.Option(help="re-check a posting's open-status once it is older than this many days")
    ] = RECHECK_DAYS,
) -> None:
    refresh(config, recheck_days)


@app.command("status", help="show the last run and stored-job totals")
def _status() -> None:
    status()


@app.command("prune", help="permanently delete old irrelevant or closed jobs from the database")
def _prune(
    days: Annotated[
        int,
        typer.Argument(callback=_positive_days, metavar="days", help="delete matching jobs older than this many days"),
    ],
) -> None:
    prune(days)


def main() -> None:
    """Run the Typer app, mapping run-time failures to exit codes a cron job can read."""
    # A cron job has nothing but the exit status to go on: 1 config/DB error, 2 usage, 3 blocked,
    # 4 no filtering session.
    try:
        app()
    except BlockedError as e:
        logger.error(e)
        sys.exit(3)
    except NoFilteringSessionError as e:
        logger.error(e)
        sys.exit(4)
    except (ConfigurationError, SQLAlchemyError) as e:
        logger.error(e)
        sys.exit(1)
