import time
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger

from linkedin_job_scraper.config import load_config
from linkedin_job_scraper.constants import CONFIG_PATH, DB_PATH, MAX_PAGES, RECHECK_DAYS
from linkedin_job_scraper.filters import relevance_predicate
from linkedin_job_scraper.geo import searched_countries
from linkedin_job_scraper.logger import init_logging
from linkedin_job_scraper.net.http import HttpClient
from linkedin_job_scraper.scrape.scraping import BlockedError, fetch_postings, scrape_jobs
from linkedin_job_scraper.store.db import JobsDb


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

    # Staging is the scrape's write-ahead log, cleared only once its rows reach jobs_raw. Leftover rows
    # mean a prior run crashed mid-promotion; fold them into this run rather than drop them.
    if orphaned := db.staged_count():
        logger.warning(f"Recovering {orphaned:,} staged rows from an interrupted prior run")
    blocked: BlockedError | None = None
    try:
        scrape_jobs(config=config, client=client, stage=db.stage_jobs, max_pages=max_pages)
    except BlockedError as e:
        logger.error(f"Blocked by LinkedIn: {e}. Storing the jobs staged before the block")
        blocked = e

    jobs_deduped, attribution = db.staged_scrape()
    scraped = sum(sum(counter.values()) for counter in attribution.values())
    logger.info(f"Total jobs scraped: {scraped:,}")
    logger.info(f"After removing duplicates: {len(jobs_deduped):,}")

    # Every scraped job is stored, relevant or not, so irrelevant ones are not re-scraped next run.
    new_urls = db.insert_jobs(jobs=jobs_deduped, countries=countries)
    db.reset_staging()  # rows are in jobs_raw now; clear the WAL for the next run

    # Record provenance before judging, since refresh_relevance reads each job's query links.
    run_ts = datetime.now().isoformat(sep=" ", timespec="seconds")
    db.upsert_queries(config.scrape_queries, run_ts=run_ts)
    db.record_attribution(attribution, seen_at=run_ts)

    # A location that named no country (a multi-city urban area) takes the country its queries searched.
    db.fill_missing_country_from_queries()

    # After the insert and attribution, so this run's new rows are judged with their links.
    flipped = db.refresh_relevance(predicate=relevance_predicate(config))
    relevant = db.relevant_among(new_urls)
    logger.info(f"Relevant this run: {relevant:,} of {len(new_urls):,} new")

    if blocked is not None:
        logger.warning("Not fetching postings while blocked; next run picks up the missing ones")
    else:
        # From the DB, not this run's scrape, so refreshed verdicts and strays from past runs count too:
        # jobs still lacking a description, plus open ones old enough to be re-checked for closure.
        cutoff = (datetime.now() - timedelta(days=RECHECK_DAYS)).isoformat(sep=" ", timespec="seconds")
        to_refresh = db.postings_to_refresh(cutoff)
        if to_refresh:
            fetch_postings(to_refresh, client, config.http.description_workers, db.record_postings)
        else:
            logger.info("No stored jobs are due for a refresh")

    # Recount duplicate groups now that the kept set has settled.
    db.refresh_dup_counts()
    db.clear_dead_descriptions()

    db.record_run(
        started_at=started_at,
        finished_at=datetime.now().isoformat(sep=" ", timespec="seconds"),
        status="blocked" if blocked is not None else "completed",
        counts={
            "scraped": scraped,
            "deduped": len(jobs_deduped),
            "relevant": relevant,
            "added": len(new_urls),
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
