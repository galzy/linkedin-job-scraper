"""Decide which pages to fetch. Networking lives in ``http.py``, parsing in ``parsing.py``."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from enum import StrEnum
from typing import NamedTuple

from loguru import logger

from linkedin_job_scraper.config import Config, SearchQuery, WorkplaceType
from linkedin_job_scraper.constants import MAX_PAGES, SESSION_DRAWS
from linkedin_job_scraper.job import Job
from linkedin_job_scraper.net.http import HttpClient
from linkedin_job_scraper.scrape.parsing import (
    count_cards,
    has_job_cards,
    parse_job_description,
    parse_job_open,
    parse_page_jobs,
)


class BlockedError(RuntimeError):
    """LinkedIn stopped serving results mid-run; the jobs gathered before it are already staged."""


class NoFilteringSessionError(RuntimeError):
    """Every session draw landed the non-filtering pipeline; scraping would fake every workplace label."""


class QueryOutcome(StrEnum):
    """Why a query stopped paging."""

    EXHAUSTED = "exhausted"  # an empty page: no more results for this query
    CEILING = "ceiling"  # max_pages reached with results still to come
    FAILED = "failed"  # a fetch gave up after all its retries


class QueryResult(NamedTuple):
    jobs: list[Job]
    outcome: QueryOutcome


def scrape_query(query: SearchQuery, client: HttpClient, tag: str, max_pages: int = MAX_PAGES) -> QueryResult:
    """Page through one query's results, stopping as soon as LinkedIn runs out.

    An empty page only counts once two consecutive refetches confirm it: the endpoint serves flaky empty 200s.
    ``tag`` names the query in the log; scrape_jobs prints the query it stands for once, up front.
    The outcome travels back with the jobs so the caller can tell a block (FAILED) from a clean end.
    """
    jobs: list[Job] = []
    for page in range(max_pages):
        for _ in range(3):
            soup = client.fetch(query.page_url(page)).soup
            if soup is None:
                logger.warning(f"[{tag}] page {page + 1} failed; stopping early with {len(jobs):,} jobs")
                return QueryResult(jobs, QueryOutcome.FAILED)
            if has_job_cards(soup):
                break
        else:
            logger.info(f"[{tag}] exhausted after {page} pages, {len(jobs):,} jobs")
            return QueryResult(jobs, QueryOutcome.EXHAUSTED)
        page_jobs = parse_page_jobs(soup)
        logger.debug(f"[{tag}] scraped page {page + 1}: parsed {len(page_jobs)} of {count_cards(soup)} cards")
        jobs.extend(page_jobs)

    logger.info(f"[{tag}] hit the {max_pages}-page ceiling with {len(jobs):,} jobs")
    return QueryResult(jobs, QueryOutcome.CEILING)


def scrape_jobs(
    config: Config, client: HttpClient, stage: Callable[[list[Job], str], None], max_pages: int = MAX_PAGES
) -> None:
    """Run every query once, staging each query's cards as it finishes.

    Queries run in parallel; the shared RateLimiter, not the worker count, is what holds the
    request rate down. ``stage`` persists a query's cards the moment it completes, so a crash
    mid-run keeps what the finished queries scraped.

    Raises BlockedError two ways: a query that gives up mid-page (FAILED), or an empty first
    page that :func:`_channel_open` confirms is a block rather than a genuinely dry query.
    """
    rate = config.http.max_requests_per_minute
    paging = (
        "paging until each query runs dry"
        if max_pages == MAX_PAGES
        else f"at most {max_pages} page{'s' if max_pages > 1 else ''} per query"
    )
    queries = config.scrape_queries  # each search fanned out per keep-list workplace type
    logger.info(f"Scraping {len(queries)} queries at {rate:.0f} req/min, {paging}")
    tags = [f"q{n}" for n in range(1, len(queries) + 1)]
    _log_roster(tags, queries)

    gave_up: list[str] = []
    empty: list[str] = []
    with ThreadPoolExecutor(max_workers=config.http.search_workers) as executor:
        results = executor.map(lambda t, q: scrape_query(q, client, t, max_pages), tags, queries)
        for tag, query, result in zip(tags, queries, results, strict=True):
            stage(result.jobs, query.query_id)  # persist this query before the next one runs
            if result.outcome is QueryOutcome.FAILED:
                gave_up.append(tag)
            elif result.outcome is QueryOutcome.EXHAUSTED and not result.jobs:
                empty.append(tag)

    if gave_up:
        raise BlockedError(f"{', '.join(gave_up)} gave up mid-query")
    # A healthy canary means the empty pages are real; only a blocked one ends the run.
    if empty and not _channel_open(config, client):
        raise BlockedError(f"{', '.join(empty)} came back empty and the canary is blocked")

    logger.info("Finished scraping jobs")


def _log_roster(tags: list[str], queries: list[SearchQuery]) -> None:
    """Log the roster, one line per query."""
    for tag, query in zip(tags, queries, strict=True):
        logger.info(f"[{tag}] {query.label}")


def acquire_filtering_session(config: Config, client: HttpClient, max_draws: int = SESSION_DRAWS) -> bool:
    """Renew the client's session until LinkedIn deals one that honors the workplace filter.

    Each guest session is dealt one of two pipelines for its whole life: one applies f_WT,
    the other ignores it and serves every variant the identical unfiltered list. Two requests
    per draw. False means no draw landed the filtering pipeline; the caller should abort the
    run, since workplace labels from a non-filtering session would be fiction.
    """
    for draw in range(1, max_draws + 1):
        if draw > 1:
            client.renew_session()
        verdict = _session_filters(config.search_queries[0], client)
        if verdict:
            logger.info(f"Session draw {draw}/{max_draws}: filtering pipeline, keeping it")
            return True
        state = "non-filtering pipeline" if verdict is False else "probe unanswerable"
        logger.info(f"Session draw {draw}/{max_draws}: {state}")
    return False


def _session_filters(query: SearchQuery, client: HttpClient) -> bool | None:
    """Whether this session's pipeline honors f_WT, judged at ``query``'s first page.

    The non-filtering pipeline serves the remote variant and the catch-all the identical page;
    the filtering one gives them nearly disjoint pages — nothing in between has been observed.
    Remote is the probe because its honest first page least resembles the unfiltered one.
    None (unanswerable) when either page fails or has no cards, e.g. mid-block or a dry query.
    """
    variants = (WorkplaceType.REMOTE, WorkplaceType.UNTAGGED)
    pages = [client.fetch(query.model_copy(update={"harvest_type": t.value}).page_url(0)).soup for t in variants]
    if any(page is None or not has_job_cards(page) for page in pages):
        return None
    remote_ids, untagged_ids = ([job.job_url for job in parse_page_jobs(page)] for page in pages)
    return remote_ids != untagged_ids


def _channel_open(config: Config, client: HttpClient) -> bool:
    """Whether LinkedIn is still serving results, judged by one deliberately broad query.

    The canary's keywords and location cannot honestly be empty, so its first page settles it:
    cards mean the channel is open and the callers' empty pages are real; empty means blocked.
    """
    canary = config.search_queries[0].broadened()
    soup = client.fetch(canary.page_url(0)).soup
    is_open = soup is not None and has_job_cards(soup)
    verdict = "results, channel open" if is_open else "empty, blocked"
    logger.info(f"Canary [{canary.label}]: {verdict}")
    return is_open


def fetch_posting(job: Job, client: HttpClient) -> Job:
    """A copy of ``job`` carrying its description and open-status from one fetch of the posting page.

    A removed posting (the fetch is ``gone``) is recorded closed. Any other failed fetch leaves both
    None, marking the row for a retry next run; the not-found description placeholder would look like
    an answer.
    """
    result = client.fetch(job.description_url)
    if result.gone:
        logger.debug(f"Posting gone, marking closed: {job.title} @ {job.company}")
        return job.with_posting(description=None, is_open=False)
    if result.soup is None:
        return job.with_posting(description=None, is_open=None)
    logger.debug(f"Scraped posting: {job.title} @ {job.company}")
    return job.with_posting(parse_job_description(result.soup), parse_job_open(result.soup))


def fetch_postings(jobs: list[Job], client: HttpClient, workers: int, store: Callable[[list[Job]], dict]) -> dict:
    """Fetch the postings in parallel, storing each one's description and open-status; returns the store's counts."""
    logger.info(f"Fetching {len(jobs):,} postings with {workers} workers")
    with ThreadPoolExecutor(max_workers=workers) as executor:
        fetched = list(executor.map(lambda job: fetch_posting(job, client), jobs))

    failed = sum(job.job_description is None and job.is_open is None for job in fetched)
    if failed:
        # A transient miss leaves both NULL and is retried next run; a gone posting is closed, not failed.
        logger.warning(f"{failed:,} postings failed; will retry next run")
    return store(fetched)
