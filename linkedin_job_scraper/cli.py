"""The command-line interface: the Typer app and the subcommands behind it."""

import csv
import os
import shutil
import sys
import tomllib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

from linkedin_job_scraper.config import ConfigurationError, load_config
from linkedin_job_scraper.console import live_status
from linkedin_job_scraper.constants import (
    CONFIG_PATH,
    CONFIGS_PATH,
    DB_PATH,
    MAX_PAGES,
    PROJECT_ROOT,
    RECHECK_DAYS,
    REPORTS_PATH,
)
from linkedin_job_scraper.filters import relevance_predicate
from linkedin_job_scraper.fit import DEFAULT_JUDGE_MODEL, FitJudgeError, judge_batches
from linkedin_job_scraper.logger import init_logging
from linkedin_job_scraper.main import main as run_scrape
from linkedin_job_scraper.net.http import HttpClient
from linkedin_job_scraper.scrape.scraping import BlockedError, NoFilteringSessionError, fetch_postings
from linkedin_job_scraper.store.db import FIT_EXPORT_COLUMNS, JobsDb
from linkedin_job_scraper.verdicts import is_firm

SAMPLE_CONFIG = CONFIGS_PATH / "config.sample.yaml"

app = typer.Typer(no_args_is_help=True, help="Scrape LinkedIn jobs, filter them, and store them in a database.")

ConfigArg = Annotated[Path, typer.Argument(help="the config file to use", show_default=False)]
EXPORT_PATH = REPORTS_PATH / "jobs.csv"


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
    with live_status("Fetching postings…"):
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
        f" {counts['missing_descriptions']} missing descriptions, {counts['unjudged']} unjudged"
    )


def _write_csv(dest: Path, columns: list[str], rows) -> None:
    """Write a CSV atomically as UTF-8 with BOM; a failed write never clobbers an existing file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    line_seps = {0x2028: "\n", 0x2029: "\n"}  # fold Unicode LS/PS to \n; CSV readers mis-split on them
    # Write a temp sibling and swap it in, so a failed write never clobbers a good export.
    tmp = dest.parent / (dest.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(columns)
            writer.writerows([c.translate(line_seps) if isinstance(c, str) else c for c in row] for row in rows)
        os.replace(tmp, dest)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def export(path: str | Path = EXPORT_PATH, all_rows: bool = False, descriptions: bool = True) -> None:
    """Write stored jobs to ``path`` as CSV: the kept set by default, every stored row with ``all_rows``."""
    if not _has_db():
        return
    db = JobsDb(path=str(DB_PATH))
    columns, rows = db.export_rows(all_rows, descriptions)
    db.close()
    dest = Path(path)
    try:
        _write_csv(dest, columns, rows)
    except OSError as e:
        print(f"Could not write {dest}: {e}. If it is open in another program, close it and retry.", file=sys.stderr)
        raise SystemExit(1) from e
    print(f"Exported {len(rows):,} jobs to {dest}")


def fit(
    dest: Path | None = None,
    days: int = 5,
    model: str = DEFAULT_JUDGE_MODEL,
    claude: str = "claude",
    export_today: bool = False,
) -> None:
    """Judge the last ``days`` days' unjudged rows through ``claude``, then write each day's CSV of survivors."""
    init_logging()
    if not _has_db():
        return
    rubric_path = CONFIGS_PATH / "fit-criteria.md"
    if not rubric_path.exists():
        logger.error(f"No fit rubric at {rubric_path}; write one before judging")
        raise SystemExit(1)
    if shutil.which(claude) is None:
        logger.error(f"claude CLI not found at {claude!r}")
        raise SystemExit(1)
    rubric = rubric_path.read_text(encoding="utf-8")

    db = JobsDb(path=str(DB_PATH))
    db.create_schema()
    today = datetime.now()
    window = [(today - timedelta(days=back)).strftime("%Y-%m-%d") for back in reversed(range(days))]
    by_day: dict[str, list] = {}
    for row in db.fit_cohort(since=window[0]):
        by_day.setdefault(row.first_seen[:10], []).append(row)
    failed = []
    for day in window:
        if not (rows := by_day.get(day)):
            continue
        logger.info(f"{day}: judging {len(rows)} rows")
        try:
            # Import batch by batch, so a judge dying mid-day keeps the batches it already won.
            for verdicts in judge_batches(rows, rubric, claude=claude, model=model):
                db.import_verdicts(verdicts)
        except FitJudgeError as e:
            failed.append(day)
            logger.error(f"{day}: judging failed — {e}")
    _export_fit_days(db, dest, window, export_today)
    db.close()
    if failed:
        raise SystemExit(1)


def _export_fit_days(db: JobsDb, dest: Path | None, window: list[str], export_today: bool) -> None:
    """Write each day's CSV of clean-or-"?" arrivals, once: a day is skipped while its file exists.

    Today's file waits for ``export_today`` — the nightly passes it, its scrape having just ended
    the day; a daytime run must not close out a day still gathering rows. A day still carrying
    unjudged rows is left for a later run too, so a file never goes out partial.
    """
    if dest is None:
        return
    if not dest.is_dir():
        logger.warning(f"{dest} is unreachable; leaving the fit exports to a later run")
        return
    for day in window:
        if day == window[-1] and not export_today:
            continue
        out = dest / f"new-jobs-{day}.csv"
        if out.exists():
            continue
        if pending := db.unjudged_on(day):
            logger.warning(f"{day}: {pending} rows still unjudged; leaving its export to a later run")
            continue
        survivors = [row for row in db.fit_export_rows(day) if not is_firm(row.fit_verdict)]
        try:
            _write_csv(out, list(FIT_EXPORT_COLUMNS), survivors)
        except OSError as e:
            logger.warning(f"Could not write {out}: {e}")
            continue
        logger.success(f"{day}: exported {len(survivors)} clean-or-? jobs to {out}")


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
    with live_status("Scraping LinkedIn…"):
        run_scrape(config, max_pages=max_pages)


@app.command("init-config", help="write a starter config from the sample to the given path")
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


@app.command(
    "export",
    help="write stored jobs to a CSV file — the kept (relevant, not closed) set by default; overwrites",
)
def _export(
    path: Annotated[Path, typer.Argument(help="where to write the CSV")] = EXPORT_PATH,
    all_rows: Annotated[bool, typer.Option("--all", help="include every stored job, rejected and closed")] = False,
    descriptions: Annotated[bool, typer.Option(help="include the job_description column")] = True,
) -> None:
    export(path, all_rows, descriptions)


@app.command(
    "fit",
    help="judge unjudged jobs against configs/fit-criteria.md through a headless claude call, "
    "then write each day's clean-or-? arrivals to a per-day CSV",
)
def _fit(
    dest: Annotated[
        Path | None, typer.Option(help="folder for the per-day CSVs; judge without exporting when omitted")
    ] = None,
    days: Annotated[int, typer.Option(callback=_positive_days, help="judge and export this many days back")] = 5,
    model: Annotated[str, typer.Option(help="the model that judges")] = DEFAULT_JUDGE_MODEL,
    claude: Annotated[str, typer.Option(help="the claude executable to run")] = "claude",
    export_today: Annotated[
        bool,
        typer.Option(
            "--export-today", help="write today's file too — for the nightly, whose scrape just ended the day"
        ),
    ] = False,
) -> None:
    fit(dest, days, model, claude, export_today)


@app.command("prune", help="permanently delete old irrelevant or closed jobs older than the given number of days")
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
    # 4 no filtering session, 5 anything unforeseen.
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
    except Exception:
        logger.exception("Unexpected error")
        sys.exit(5)
