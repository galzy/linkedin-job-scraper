from collections import Counter, defaultdict
from collections.abc import Callable, Collection
from datetime import datetime

from loguru import logger
from sqlalchemy import Row, bindparam, case, create_engine, delete, func, select, text, update
from sqlalchemy.dialects.sqlite import insert

from linkedin_scraper.config import SearchQuery
from linkedin_scraper.constants import (
    TABLE_JOB_QUERIES,
    TABLE_JOBS_RAW,
    TABLE_RUNS,
    VIEW_JOBS_FILTERED,
)
from linkedin_scraper.geo import country_of
from linkedin_scraper.job import Job
from linkedin_scraper.store.schema import (
    JobQueryRow,
    JobRow,
    QueryRow,
    RunQueryRow,
    RunRow,
    SqlBase,
    StagingRow,
)


def _row(job: Job, seen_at: str, countries: frozenset[str]) -> dict:
    """The scraped job as a row: its fields, the country ``location`` names, and the DB's own stamps."""
    stamps = {"first_seen": seen_at, "last_seen": seen_at, "runs_seen": 1}
    return job.model_dump() | {"country": country_of(job.location, countries)} | stamps


# A re-scraped job updates these and nothing else: first_seen stays first, and job_description
# survives rather than being refetched next run.
_insert = insert(JobRow)
_UPSERT = _insert.on_conflict_do_update(
    index_elements=[JobRow.job_url],
    set_={
        "runs_seen": JobRow.runs_seen + 1,
        "last_seen": _insert.excluded.last_seen,
        # Refresh the type, but never downgrade a known one to untagged when this run missed it.
        "workplace_type": case(
            (_insert.excluded.workplace_type == "untagged", JobRow.workplace_type),
            else_=_insert.excluded.workplace_type,
        ),
    },
)

# A query seen again keeps its first_used and just advances last_used.
_query_insert = insert(QueryRow)
_QUERY_UPSERT = _query_insert.on_conflict_do_update(
    index_elements=[QueryRow.query_id],
    set_={"last_used": _query_insert.excluded.last_used},
)

# An attribution seen again accumulates its sightings and advances last_seen.
_job_query_insert = insert(JobQueryRow)
_JOB_QUERY_UPSERT = _job_query_insert.on_conflict_do_update(
    index_elements=[JobQueryRow.job_url, JobQueryRow.query_id],
    set_={
        "times_seen": JobQueryRow.times_seen + _job_query_insert.excluded.times_seen,
        "last_seen": _job_query_insert.excluded.last_seen,
    },
)

# A posting re-staged under the same query accumulates its card count.
_staging_insert = insert(StagingRow)
_STAGING_UPSERT = _staging_insert.on_conflict_do_update(
    index_elements=[StagingRow.job_url, StagingRow.query_id],
    set_={"times_seen": StagingRow.times_seen + _staging_insert.excluded.times_seen},
)


class JobsDb:
    """The jobs database: the jobs themselves plus the queries, attribution, and runs behind them."""

    def __init__(self, path: str) -> None:
        self.engine = create_engine(f"sqlite:///{path}")

    def create_schema(self) -> None:
        """Create the tables and the filtered view. Idempotent."""
        SqlBase.metadata.create_all(self.engine)

        # SQLite freezes a view's column list at creation, so recreate it from the model's own.
        columns = ", ".join(JobRow.__table__.columns.keys())
        create_view = (
            f"CREATE VIEW {VIEW_JOBS_FILTERED} AS SELECT {columns} FROM {TABLE_JOBS_RAW} WHERE is_relevant = 1"
        )
        with self.engine.begin() as conn:
            conn.execute(text(f"DROP VIEW IF EXISTS {VIEW_JOBS_FILTERED}"))
            conn.execute(text(create_view))

    def relevant_jobs_without_description(self) -> list[Job]:
        """Stored relevant jobs whose description is still NULL, as Jobs ready to fetch."""
        cols = (JobRow.title, JobRow.company, JobRow.date, JobRow.job_url, JobRow.location, JobRow.workplace_type)
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(*cols).where(JobRow.is_relevant.is_(True), JobRow.job_description.is_(None))
            ).all()
        return [Job(**row._asdict()) for row in rows]

    def last_run(self) -> Row | None:
        """The newest runs row — timestamps, status, and counts — or None before any run."""
        with self.engine.connect() as conn:
            return conn.execute(select(RunRow).order_by(RunRow.run_id.desc()).limit(1)).first()

    def totals(self) -> dict[str, int]:
        """Counts over jobs_raw: every stored row, the relevant ones, and relevant rows lacking a description."""
        relevant = JobRow.is_relevant.is_(True)
        with self.engine.connect() as conn:
            stored, kept, lacking = conn.execute(
                select(
                    func.count(),
                    func.count().filter(relevant),
                    func.count().filter(relevant, JobRow.job_description.is_(None)),
                ).select_from(JobRow)
            ).one()
        return {"stored": stored, "relevant": kept, "missing_descriptions": lacking}

    def relevant_among(self, job_urls: Collection[str]) -> int:
        """How many of the given URLs are currently marked relevant."""
        if not job_urls:
            return 0
        with self.engine.connect() as conn:
            relevant = {u for (u,) in conn.execute(select(JobRow.job_url).where(JobRow.is_relevant.is_(True)))}
        return len(relevant & set(job_urls))

    def reset_staging(self) -> None:
        """Clear the staging table so a new run starts from an empty scrape."""
        with self.engine.begin() as conn:
            conn.execute(delete(StagingRow))

    def stage_jobs(self, jobs: list[Job], query_id: str) -> None:
        """Persist one query's scraped cards, collapsing a posting's per-page repeats into times_seen."""
        if not jobs:
            return
        seen_at = datetime.now().isoformat(sep=" ", timespec="seconds")
        counts = Counter(job.job_url for job in jobs)
        cards: dict[str, Job] = {}
        for job in jobs:
            cards.setdefault(job.job_url, job)  # first card wins, as the in-memory dedupe did
        staged = [
            {
                "job_url": url,
                "query_id": query_id,
                "title": card.title,
                "company": card.company,
                "date": card.date,
                "location": card.location,
                "times_seen": counts[url],
                "seen_at": seen_at,
            }
            for url, card in cards.items()
        ]
        with self.engine.begin() as conn:
            conn.execute(_STAGING_UPSERT, staged)

    def staged_scrape(self) -> tuple[list[Job], dict[str, Counter[str]]]:
        """This run's staged cards as (postings deduped by url, per-query attribution)."""
        attribution: dict[str, Counter[str]] = defaultdict(Counter)
        jobs: dict[str, Job] = {}
        with self.engine.connect() as conn:
            for row in conn.execute(select(StagingRow)):
                attribution[row.query_id][row.job_url] = row.times_seen
                if row.job_url not in jobs:
                    jobs[row.job_url] = Job(
                        title=row.title,
                        company=row.company,
                        date=row.date,
                        job_url=row.job_url,
                        location=row.location,
                    )
        return list(jobs.values()), attribution

    def insert_jobs(self, jobs: list[Job], countries: frozenset[str] = frozenset()) -> int:
        """Store the jobs, returning how many were new; ``countries`` scopes each location's metro label."""
        if not jobs:
            return 0

        seen_at = datetime.now().isoformat(sep=" ", timespec="seconds")
        rows = [_row(job, seen_at, countries) for job in jobs]

        with self.engine.begin() as conn:
            # An upserted row is updated, not skipped, so rowcount counts it too.
            before = conn.scalar(select(func.count()).select_from(JobRow))
            conn.execute(_UPSERT, rows)
            added = conn.scalar(select(func.count()).select_from(JobRow)) - before

        logger.info(f"Added {added:,} of {len(jobs):,} scraped jobs to {TABLE_JOBS_RAW}")
        return added

    def refresh_relevance(self, predicate: Callable[[str, str, str, set[str]], bool]) -> int:
        """Re-decide is_relevant for every stored row, returning how many settled verdicts reversed.

        Every row, because the verdict follows config, not the job. A first judgment (NULL -> verdict)
        is written but not counted as a flip. The predicate needs the queries that found each job, so
        record_attribution must run before this.
        """
        with self.engine.begin() as conn:
            links: dict[str, set[str]] = defaultdict(set)
            for job_url, query_id in conn.execute(select(JobQueryRow.job_url, JobQueryRow.query_id)):
                links[job_url].add(query_id)
            stored = conn.execute(
                select(JobRow.job_url, JobRow.title, JobRow.company, JobRow.workplace_type, JobRow.is_relevant)
            ).all()
            # Update every row whose verdict differs. A NULL prior always differs, but that first
            # judgment is a decision, not a reversal, so it is written without counting as flipped.
            updates: list[dict] = []
            to_relevant = to_irrelevant = 0
            for row in stored:
                verdict = predicate(row.title, row.company, row.workplace_type, links.get(row.job_url, set()))
                if verdict == row.is_relevant:
                    continue
                updates.append({"url": row.job_url, "verdict": verdict})
                if row.is_relevant is not None:  # a reversal, not a first judgment
                    if verdict:
                        to_relevant += 1
                    else:
                        to_irrelevant += 1
            if updates:
                conn.execute(
                    update(JobRow).where(JobRow.job_url == bindparam("url")).values(is_relevant=bindparam("verdict")),
                    updates,
                )

        flipped = to_relevant + to_irrelevant
        logger.info(
            f"Relevance refresh: {flipped:,} verdicts flipped "
            f"({to_relevant:,} now relevant, {to_irrelevant:,} now irrelevant) in {TABLE_JOBS_RAW}"
        )
        return flipped

    def fill_missing_country_from_queries(self) -> int:
        """Set ``country`` on rows still NULL to the single country their queries name; returns how many.

        A multi-city urban area LinkedIn serves without a resolvable city ("Cologne Bonn Region")
        names no country in its location, but the search that found it does. When every query behind
        such a row names one country, that is the job's; rows whose queries disagree, or name none,
        stay NULL.
        """
        with self.engine.begin() as conn:
            by_query = {qid: country_of(loc) for qid, loc in conn.execute(select(QueryRow.query_id, QueryRow.location))}
            by_job: dict[str, set[str]] = defaultdict(set)
            for job_url, query_id in conn.execute(select(JobQueryRow.job_url, JobQueryRow.query_id)):
                if (country := by_query.get(query_id)) is not None:
                    by_job[job_url].add(country)
            updates = [
                {"url": job_url, "country": next(iter(named))}
                for (job_url,) in conn.execute(select(JobRow.job_url).where(JobRow.country.is_(None)))
                if len(named := by_job.get(job_url, set())) == 1
            ]
            if updates:
                conn.execute(
                    update(JobRow).where(JobRow.job_url == bindparam("url")).values(country=bindparam("country")),
                    updates,
                )

        return len(updates)

    def upsert_queries(self, queries: list[SearchQuery], run_ts: str) -> None:
        """Record every query the run used, keeping each one's first_used and advancing last_used."""
        # Dedup by id: two identical queries in one config would hit the same row twice in one upsert.
        rows = {
            q.query_id: {
                "query_id": q.query_id,
                "keywords": q.keywords,
                "location": q.location,
                "distance": q.distance,
                "harvest_type": q.harvest_type,
                "timespan": q.timespan,
                "label": q.label,
                "first_used": run_ts,
                "last_used": run_ts,
            }
            for q in queries
        }
        if not rows:
            return
        with self.engine.begin() as conn:
            conn.execute(_QUERY_UPSERT, list(rows.values()))

    def record_attribution(self, attribution: dict[str, Counter[str]], seen_at: str) -> None:
        """Link jobs to the queries that found them, accumulating sightings on a re-scrape."""
        rows = [
            {"job_url": job_url, "query_id": query_id, "first_seen": seen_at, "last_seen": seen_at, "times_seen": count}
            for query_id, counter in attribution.items()
            for job_url, count in counter.items()
        ]
        if not rows:
            return
        with self.engine.begin() as conn:
            conn.execute(_JOB_QUERY_UPSERT, rows)
        logger.debug(f"Recorded {len(rows):,} job-query attributions in {TABLE_JOB_QUERIES}")

    def record_run(
        self,
        *,
        started_at: str,
        finished_at: str,
        status: str,
        counts: dict[str, int],
        config_yaml: str,
        query_ids: list[str],
    ) -> int:
        """Store one run's timestamps, counts, config, and the queries it used, returning its run_id."""
        with self.engine.begin() as conn:
            result = conn.execute(
                insert(RunRow).values(
                    started_at=started_at,
                    finished_at=finished_at,
                    status=status,
                    scraped=counts["scraped"],
                    deduped=counts["deduped"],
                    relevant=counts["relevant"],
                    added=counts["added"],
                    flipped=counts["flipped"],
                    config_yaml=config_yaml,
                )
            )
            run_id = result.inserted_primary_key[0]
            links = [{"run_id": run_id, "query_id": query_id} for query_id in dict.fromkeys(query_ids)]
            if links:
                conn.execute(insert(RunQueryRow), links)
        logger.info(f"Recorded run {run_id} ({status}) in {TABLE_RUNS}")
        return run_id

    def update_descriptions(self, jobs: list[Job]) -> int:
        """Fill job_description on rows already stored, matched on job_url."""
        if not jobs:
            return 0

        with self.engine.begin() as conn:
            result = conn.execute(
                update(JobRow)
                .where(JobRow.job_url == bindparam("url"))
                .values(job_description=bindparam("description")),
                [{"url": job.key, "description": job.job_description} for job in jobs],
            )
            updated = result.rowcount

        logger.info(f"Filled descriptions on {updated:,} rows in {TABLE_JOBS_RAW}")
        return updated

    def close(self) -> None:
        self.engine.dispose()
