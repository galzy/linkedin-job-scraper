import time
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger

from linkedin_scraper.config import WorkplaceType, load_config
from linkedin_scraper.constants import CONFIG_PATH, DB_PATH, MAX_PAGES, RECHECK_DAYS
from linkedin_scraper.filters import derive_workplace_types, relevance_predicate
from linkedin_scraper.geo import searched_countries
from linkedin_scraper.logger import init_logging
from linkedin_scraper.net.http import HttpClient
from linkedin_scraper.scrape.scraping import (
    BlockedError,
    NoFilteringSessionError,
    acquire_filtering_session,
    refresh_postings,
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

    # Each query stages as it finishes, so a block still leaves the ones that ran on disk to read back.
    db.reset_staging()
    blocked: BlockedError | None = None
    try:
        scrape_jobs(config=config, client=client, stage=db.stage_jobs, max_pages=max_pages)
    except BlockedError as e:
        logger.error(f"Blocked by LinkedIn: {e}. Storing the jobs staged before the block")
        blocked = e

    jobs_deduped, attribution = db.staged_scrape()
    scraped = sum(sum(counter.values()) for counter in attribution.values())
    logger.info(f"Total jobs scraped: {scraped:,}")
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
        logger.warning("Not refreshing postings while blocked; next run picks up the missing ones")
    else:
        # From the DB, not this run's scrape, so refreshed verdicts and strays from past runs count too:
        # jobs still lacking a description, plus open ones old enough to be re-checked for closure.
        cutoff = (datetime.now() - timedelta(days=RECHECK_DAYS)).isoformat(sep=" ", timespec="seconds")
        to_refresh = db.postings_to_refresh(cutoff)
        if to_refresh:
            refresh_postings(to_refresh, client, config.http.description_workers, db.record_postings)
        else:
            logger.info("No stored jobs are due for a refresh")

    db.record_run(
        started_at=started_at,
        finished_at=datetime.now().isoformat(sep=" ", timespec="seconds"),
        status="blocked" if blocked is not None else "completed",
        counts={
            "scraped": scraped,
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
