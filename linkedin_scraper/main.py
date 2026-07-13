import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from loguru import logger

from linkedin_scraper.config import WorkplaceType, load_config
from linkedin_scraper.constants import CONFIG_PATH, DB_PATH, MAX_PAGES
from linkedin_scraper.filters import (
    derive_workplace_types,
    relevance_predicate,
    remove_duplicates,
)
from linkedin_scraper.geo import searched_countries
from linkedin_scraper.job import Job
from linkedin_scraper.logger import init_logging
from linkedin_scraper.net.http import HttpClient
from linkedin_scraper.scrape.scraping import (
    BlockedError,
    NoFilteringSessionError,
    acquire_filtering_session,
    fetch_description,
    scrape_jobs,
)
from linkedin_scraper.store.db import JobsDb


def format_duration(seconds: float) -> str:
    """Render a run time as a compact human string, e.g. "7h 27m 25s"."""
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{seconds:.2f}s"


def describe_jobs(jobs: list[Job], client: HttpClient, workers: int, db: JobsDb) -> int:
    """Fetch the jobs' descriptions in parallel and store the ones that arrived, returning how many."""
    logger.info(f"Scraping {len(jobs):,} job descriptions with {workers} workers")
    with ThreadPoolExecutor(max_workers=workers) as executor:
        described = list(executor.map(lambda job: fetch_description(job, client), jobs))

    fetched = [job for job in described if job.job_description is not None]
    if len(fetched) < len(described):
        # Left NULL on purpose, so the next run picks them up again.
        logger.warning(f"{len(described) - len(fetched):,} descriptions failed; will retry next run")
    db.update_descriptions(jobs=fetched)
    return len(fetched)


def main(config_file: str | Path = CONFIG_PATH, max_pages: int = MAX_PAGES) -> None:
    """Scrape, dedupe, and store the jobs, judge relevance, then describe the ones still lacking a description."""
    init_logging()
    logger.info(f"Starting scrape with {config_file}")
    start_time = time.perf_counter()
    started_at = datetime.now().isoformat(sep=" ", timespec="seconds")
    config = load_config(config_file)
    config_yaml = Path(config_file).read_text(encoding="utf-8")  # the exact file this run validated
    countries = searched_countries(q.location for q in config.search_queries)  # scopes metro-label resolution
    db = JobsDb(path=str(DB_PATH))
    db.create_schema()
    client = HttpClient.from_config(config.http)

    # Ensure we have a filtering session before scraping, so we don't waste time on a run that will be ignored.
    if not acquire_filtering_session(config, client):
        client.close()
        db.close()
        raise NoFilteringSessionError("Every session draw ignored the workplace filter; nothing scraped")

    # A block still leaves the jobs of the queries that ran; store them, skip descriptions, re-raise.
    blocked: BlockedError | None = None
    try:
        jobs_raw, attribution = scrape_jobs(config=config, client=client, max_pages=max_pages)
    except BlockedError as e:
        logger.error(f"Blocked by LinkedIn: {e}. Storing the {len(e.jobs):,} jobs scraped so far")
        blocked, jobs_raw, attribution = e, e.jobs, e.attribution

    logger.info(f"Total jobs scraped: {len(jobs_raw):,}")
    jobs_deduped = remove_duplicates(values=jobs_raw)
    logger.info(f"Total jobs after removing duplicates: {len(jobs_deduped):,}")

    # Label each job by the tagged query that found it, so the workplace filter can judge it.
    query_types = {q.query_id: q.harvest_type for q in config.scrape_queries}
    types = derive_workplace_types(attribution, query_types)
    jobs_deduped = [
        job.with_workplace_type(types.get(job.job_url, WorkplaceType.UNTAGGED.value)) for job in jobs_deduped
    ]

    # Every scraped job is stored, relevant or not, so irrelevant ones are not re-scraped next run.
    added = db.insert_jobs(jobs=jobs_deduped, countries=countries)

    # Record provenance before judging, since refresh_relevance reads each job's query links.
    run_ts = datetime.now().isoformat(sep=" ", timespec="seconds")
    db.upsert_queries(config.scrape_queries, run_ts=run_ts)
    db.record_attribution(attribution, seen_at=run_ts)

    # A location that named no country (a multi-city urban area) takes the country its queries searched.
    db.fill_missing_country_from_queries()

    # After the insert and attribution, so this run's new rows are judged with their links.
    flipped = db.refresh_relevance(predicate=relevance_predicate(config))
    relevant = db.relevant_among({job.job_url for job in jobs_deduped})
    logger.info(f"Relevant: {relevant:,} of {len(jobs_deduped):,} jobs")

    if blocked is not None:
        logger.warning("Not describing while blocked; next run picks up the missing descriptions")
    else:
        # From the DB, not this run's scrape, so refreshed verdicts and strays from past runs count too.
        to_describe = db.relevant_jobs_without_description()
        if to_describe:
            describe_jobs(to_describe, client, config.http.description_workers, db)
        else:
            logger.info("Every relevant job already has a description")

    db.record_run(
        started_at=started_at,
        finished_at=datetime.now().isoformat(sep=" ", timespec="seconds"),
        status="blocked" if blocked is not None else "completed",
        counts={
            "scraped": len(jobs_raw),
            "deduped": len(jobs_deduped),
            "relevant": relevant,
            "added": added,
            "flipped": flipped,
        },
        config_yaml=config_yaml,
        query_ids=[q.query_id for q in config.scrape_queries],
    )

    client.close()
    db.close()
    if blocked is not None:
        raise blocked
    end_time = time.perf_counter()
    logger.success(f"Scraping finished in {format_duration(end_time - start_time)}")
