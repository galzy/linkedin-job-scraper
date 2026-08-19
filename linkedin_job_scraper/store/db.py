"""The jobs database: the jobs themselves plus the queries, attribution, and runs behind them."""

import os
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable, Collection
from datetime import datetime

from loguru import logger
from sqlalchemy import Engine, Row, and_, create_engine, delete, event, func, insert, or_, select, text, update

from linkedin_job_scraper.config import SearchQuery
from linkedin_job_scraper.constants import (
    TABLE_JOB_QUERIES,
    TABLE_JOBS_RAW,
    TABLE_RUNS,
    VIEW_JOBS_FILTERED,
)
from linkedin_job_scraper.geo import country_of
from linkedin_job_scraper.job import Job
from linkedin_job_scraper.language import description_lang
from linkedin_job_scraper.signals import stated_locations, work_eligibility
from linkedin_job_scraper.store.schema import (
    JobQueryRow,
    JobRow,
    QueryRow,
    RunQueryRow,
    RunRow,
    SqlBase,
    StagingRow,
)
from linkedin_job_scraper.store.statements import (
    _JOB_FETCH_COLS,
    _JOB_QUERY_UPSERT,
    _JOB_UPSERT,
    _QUERY_UPSERT,
    _STAGING_UPSERT,
    _row,
    _update_jobs_by_url,
)
from linkedin_job_scraper.verdicts import is_firm

# What the per-day fit export carries, in its column order.
FIT_EXPORT_COLUMNS = (
    "job_url",
    "title",
    "company",
    "location",
    "country",
    "job_description",
    "description_lang",
    "stated_locations",
    "work_eligibility",
    "workplace_type",
    "fit_verdict",
    "dup_group",
    "dup_count",
)


def _apply_pragmas(dbapi_connection: sqlite3.Connection, _record) -> None:
    """WAL plus a busy timeout so a read command and a live scrape's writes wait each other out."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode = WAL")
    cursor.execute("PRAGMA busy_timeout = 5000")
    cursor.execute("PRAGMA synchronous = NORMAL")
    cursor.close()


class JobsDb:
    """The jobs database: the jobs themselves plus the queries, attribution, and runs behind them."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._quarantine_if_corrupt()  # heal the file before opening it
        self.engine = self._build_engine()

    def _build_engine(self) -> Engine:
        engine = create_engine(f"sqlite:///{self.path}")
        event.listen(engine, "connect", _apply_pragmas)
        return engine

    def _quarantine_if_corrupt(self) -> None:
        """Move a corrupt DB file aside so an empty one takes its place; scraped data rebuilds next run.

        Only a malformed image is discarded; a locked or unreadable file is left to surface normally,
        since the fix there is to wait out the lock or correct the path, not to lose data.
        """
        if not os.path.isfile(self.path):  # :memory: or a not-yet-created file — nothing to check
            return
        try:
            connection = sqlite3.connect(self.path)
            try:
                # quick_check scans every page: ~50ms at 20MB, growing with the file
                healthy = connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            finally:
                connection.close()
        except sqlite3.OperationalError:
            return  # locked or unreadable, not corrupt
        except sqlite3.DatabaseError:
            healthy = False  # malformed image
        if healthy:
            return
        quarantined = f"{self.path}.corrupt-{datetime.now():%Y%m%d-%H%M%S}"
        os.replace(self.path, quarantined)
        for sidecar in (f"{self.path}-wal", f"{self.path}-shm"):
            if os.path.exists(sidecar):
                os.remove(sidecar)
        logger.error(f"Database was corrupt; moved it to {quarantined} and rebuilt from scratch")

    def create_schema(self) -> None:
        """Create the tables and the filtered view. Idempotent."""
        SqlBase.metadata.create_all(self.engine)

        # SQLite freezes a view's column list at creation, so recreate it from the model's own.
        # is_open IS NOT 0 keeps the still-open and the not-yet-checked (NULL), dropping only confirmed-closed.
        columns = ", ".join(JobRow.__table__.columns.keys())
        create_view = (
            f"CREATE VIEW {VIEW_JOBS_FILTERED} AS SELECT {columns} FROM {TABLE_JOBS_RAW} "
            "WHERE is_relevant = 1 AND is_open IS NOT 0 "
            "ORDER BY date DESC, company, title"
        )
        with self.engine.begin() as conn:
            conn.execute(text(f"DROP VIEW IF EXISTS {VIEW_JOBS_FILTERED}"))
            conn.execute(text(create_view))

    # --- reads ---------------------------------------------------------------

    def relevant_jobs_without_description(self) -> list[Job]:
        """Stored relevant jobs whose description is still NULL, as Jobs ready to fetch."""
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(*_JOB_FETCH_COLS).where(JobRow.is_relevant.is_(True), JobRow.job_description.is_(None))
            ).all()
        return [Job(**row._asdict()) for row in rows]

    def last_run(self) -> Row | None:
        """The newest runs row — timestamps, status, and counts — or None before any run."""
        with self.engine.connect() as conn:
            return conn.execute(select(RunRow).order_by(RunRow.run_id.desc()).limit(1)).first()

    @staticmethod
    def _unjudged():
        """Rows still awaiting a fit verdict: relevant, not confirmed-closed, and carrying none yet."""
        return and_(JobRow.is_relevant.is_(True), JobRow.is_open.isnot(False), JobRow.fit_verdict.is_(None))

    def totals(self) -> dict[str, int]:
        """Counts over jobs_raw: every stored row, the relevant ones, those lacking a description, the unjudged."""
        relevant = JobRow.is_relevant.is_(True)
        with self.engine.connect() as conn:
            stored, kept, lacking, unjudged = conn.execute(
                select(
                    func.count(),
                    func.count().filter(relevant),
                    func.count().filter(relevant, JobRow.job_description.is_(None)),
                    func.count().filter(self._unjudged()),
                ).select_from(JobRow)
            ).one()
        return {"stored": stored, "relevant": kept, "missing_descriptions": lacking, "unjudged": unjudged}

    def relevant_among(self, job_urls: Collection[str]) -> int:
        """How many of the given URLs are currently marked relevant."""
        if not job_urls:
            return 0
        with self.engine.connect() as conn:
            relevant = {u for (u,) in conn.execute(select(JobRow.job_url).where(JobRow.is_relevant.is_(True)))}
        return len(relevant & set(job_urls))

    def staged_count(self) -> int:
        """How many rows sit in staging — nonzero only after a prior run crashed mid-promotion."""
        with self.engine.connect() as conn:
            return conn.scalar(select(func.count()).select_from(StagingRow))

    def staged_scrape(self) -> tuple[list[Job], dict[str, Counter[str]], dict[str, str]]:
        """The staged cards as (postings deduped by url, per-query attribution, per-query harvest type)."""
        attribution: dict[str, Counter[str]] = defaultdict(Counter)
        query_types: dict[str, str] = {}
        jobs: dict[str, Job] = {}
        with self.engine.connect() as conn:
            for row in conn.execute(select(StagingRow)):
                attribution[row.query_id][row.job_url] = row.times_seen
                query_types[row.query_id] = row.harvest_type
                if row.job_url not in jobs:
                    jobs[row.job_url] = Job(
                        title=row.title,
                        company=row.company,
                        date=row.date,
                        job_url=row.job_url,
                        location=row.location,
                    )
        return list(jobs.values()), attribution, query_types

    def export_rows(self, all_rows: bool = False, descriptions: bool = True) -> tuple[list[str], list[Row]]:
        """The jobs_raw columns and rows to export: the kept set by default, every row with ``all_rows``."""
        cols = [c for c in JobRow.__table__.columns if descriptions or c.key != "job_description"]
        stmt = select(*cols)
        if not all_rows:
            stmt = stmt.where(JobRow.is_relevant.is_(True), JobRow.is_open.isnot(False))
        with self.engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [c.key for c in cols], rows

    def postings_to_refresh(self, stale_before: str) -> list[Job]:
        """Relevant, not-closed jobs to (re)fetch: those still missing data, plus ones due to be re-checked.

        A missing description or a never-determined open-status (is_open NULL) is fetched on sight,
        regardless of age. A job is otherwise due when last verified before ``stale_before`` — or never:
        what a re-check discovers is whatever changed since the last look, so that is the clock, not the
        posting date. A confirmed-closed posting is left be, even one still missing a description: a
        removed listing keeps 404ing, so re-fetching is futile. So is one already turned down: whether
        it is still open stopped mattering when it was judged.
        """
        due = or_(JobRow.last_verified.is_(None), JobRow.last_verified < stale_before)
        with self.engine.connect() as conn:
            judged = conn.execute(select(JobRow.job_url, JobRow.fit_verdict).where(JobRow.fit_verdict.isnot(None)))
            turned_down = {url for url, verdict in judged if is_firm(verdict)}
            rows = conn.execute(
                select(*_JOB_FETCH_COLS).where(
                    JobRow.is_relevant.is_(True),
                    JobRow.is_open.isnot(False),  # never re-fetch a confirmed-closed posting; NULL and open qualify
                    or_(JobRow.job_description.is_(None), JobRow.is_open.is_(None), due),
                )
            ).all()
        return [Job(**row._asdict()) for row in rows if row.job_url not in turned_down]

    def fit_cohort(self, since: str | None = None) -> list[Row]:
        """The rows awaiting a fit verdict, newest first — what a judging pass reads, in full.

        Whole rows rather than Jobs: judging weighs the signals the scrape derived (``description_lang``,
        ``stated_locations``, ``work_eligibility``, ``dup_count``) alongside the ad's own text. ``since``
        drops rows first seen before it, so one night's arrivals can be judged on their own without
        rebuilding the backlog sitting behind them.
        """
        stmt = select(JobRow).where(self._unjudged())
        if since:
            stmt = stmt.where(JobRow.first_seen >= since)
        with self.engine.connect() as conn:
            return conn.execute(stmt.order_by(JobRow.date.desc(), JobRow.company, JobRow.title)).all()

    def unjudged_on(self, day: str) -> int:
        """How many rows first seen on ``day`` (YYYY-MM-DD) still await a fit verdict."""
        with self.engine.connect() as conn:
            return conn.scalar(
                select(func.count()).select_from(JobRow).where(self._unjudged(), JobRow.first_seen.like(f"{day}%"))
            )

    def fit_export_rows(self, day: str) -> list[Row]:
        """The kept, judged rows first seen on ``day``, carrying ``FIT_EXPORT_COLUMNS``, newest first."""
        cols = [JobRow.__table__.c[name] for name in FIT_EXPORT_COLUMNS]
        with self.engine.connect() as conn:
            return conn.execute(
                select(*cols)
                .where(
                    JobRow.is_relevant.is_(True),
                    JobRow.is_open.isnot(False),
                    JobRow.fit_verdict.isnot(None),
                    JobRow.first_seen.like(f"{day}%"),
                )
                .order_by(JobRow.date.desc(), JobRow.company, JobRow.title)
            ).all()

    # --- writes --------------------------------------------------------------

    def reset_staging(self) -> None:
        """Clear the staging table once its rows have reached jobs_raw."""
        with self.engine.begin() as conn:
            conn.execute(delete(StagingRow))

    @staticmethod
    def _prunable(cutoff: str):
        """Rows to prune: irrelevant or confirmed-closed, and older than ``cutoff``.

        Age is the posting date, falling back to first_seen when the card carried none.
        Not-yet-judged rows (is_relevant/is_open NULL) never match.
        """
        age = func.coalesce(func.nullif(JobRow.date, ""), JobRow.first_seen)
        return and_(age < cutoff, or_(JobRow.is_relevant.is_(False), JobRow.is_open.is_(False)))

    def count_prunable(self, cutoff: str) -> int:
        """How many stored jobs prune_old would delete at ``cutoff`` — for the pre-delete preview."""
        with self.engine.connect() as conn:
            return conn.scalar(select(func.count()).select_from(JobRow).where(self._prunable(cutoff)))

    def prune_old(self, cutoff: str) -> int:
        """Delete irrelevant/closed jobs older than ``cutoff`` and their attributions; returns rows removed."""
        stale = self._prunable(cutoff)
        with self.engine.begin() as conn:
            conn.execute(delete(JobQueryRow).where(JobQueryRow.job_url.in_(select(JobRow.job_url).where(stale))))
            deleted = conn.execute(delete(JobRow).where(stale)).rowcount
        logger.debug(f"Pruned {deleted:,} irrelevant or closed jobs older than {cutoff}")
        return deleted

    def stage_jobs(self, jobs: list[Job], query_id: str, harvest_type: str) -> None:
        """Persist one query's scraped cards, collapsing a posting's per-page repeats into times_seen."""
        if not jobs:
            return
        seen_at = datetime.now().isoformat(sep=" ", timespec="seconds")
        counts: Counter[str] = Counter()
        cards: dict[str, Job] = {}
        for job in jobs:
            cards.setdefault(job.job_url, job)  # first card wins, as the in-memory dedupe did
            counts[job.job_url] += 1
        staged = [
            {
                "job_url": url,
                "query_id": query_id,
                "harvest_type": harvest_type,
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

    def insert_jobs(self, jobs: list[Job], countries: frozenset[str] = frozenset()) -> set[str]:
        """Store the jobs, returning the URLs that were new; ``countries`` scopes each location's metro label."""
        if not jobs:
            return set()

        seen_at = datetime.now().isoformat(sep=" ", timespec="seconds")
        rows = [_row(job, seen_at, countries) for job in jobs]

        with self.engine.begin() as conn:
            # An upsert updates a matched row rather than skipping it, so identify new rows by URL, not rowcount.
            existing = {u for (u,) in conn.execute(select(JobRow.job_url))}
            conn.execute(_JOB_UPSERT, rows)
        new = {job.job_url for job in jobs} - existing

        logger.info(f"New jobs added to {TABLE_JOBS_RAW}: {len(new):,}")
        return new

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
                updates.append({"url": row.job_url, "is_relevant": verdict})
                if row.is_relevant is not None:  # a reversal, not a first judgment
                    if verdict:
                        to_relevant += 1
                    else:
                        to_irrelevant += 1
            if updates:
                _update_jobs_by_url(conn, ["is_relevant"], updates)

        flipped = to_relevant + to_irrelevant
        message = f"Re-judged all stored jobs against current config: {flipped:,} verdicts flipped"
        if flipped:
            message += f" ({to_relevant:,} now relevant, {to_irrelevant:,} now irrelevant)"
        logger.info(message)
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
                _update_jobs_by_url(conn, ["country"], updates)

        return len(updates)

    def import_verdicts(self, verdicts: dict[str, str]) -> tuple[int, int]:
        """Store fit verdicts by job_url; returns rows given one and reposts that inherited one.

        A stated code turns down the job, so it carries to the rest of its dup_group; a "?" reads
        this posting's wording and stays put. A repost only fills where nothing was written.
        """
        if not verdicts:
            return 0, 0
        with self.engine.begin() as conn:
            updates = [{"url": url, "fit_verdict": verdict} for url, verdict in verdicts.items()]
            given = _update_jobs_by_url(conn, ["fit_verdict"], updates)
            named = select(JobRow.job_url, JobRow.dup_group).where(JobRow.job_url.in_(verdicts))
            groups = dict(conn.execute(named).all())
            inherited = sum(
                conn.execute(
                    update(JobRow)
                    .where(JobRow.dup_group == groups[url], JobRow.fit_verdict.is_(None))
                    .values(fit_verdict=verdict)
                ).rowcount
                for url, verdict in verdicts.items()
                if url in groups and is_firm(verdict)
            )
        logger.info(f"Imported {given:,} fit verdicts; {inherited:,} reposts inherited one")
        return given, inherited

    def refresh_dup_counts(self) -> int:
        """Recount how many other kept rows share each ``dup_group``, writing dup_count on every row.

        Kept means relevant and not closed, so a kept row's dup_count is how many other kept rows are the same
        posting under a different URL — 0 when it stands alone. Only changed rows are written; returns how many.
        Run after relevance and open-status settle.
        """
        with self.engine.begin() as conn:
            sizes = dict(
                conn.execute(
                    select(JobRow.dup_group, func.count())
                    .where(JobRow.is_relevant.is_(True), JobRow.is_open.isnot(False))
                    .group_by(JobRow.dup_group)
                ).all()
            )
            others = {group: count - 1 for group, count in sizes.items()}  # the group minus the row itself
            updates = [
                {"url": url, "dup_count": others.get(group, 0)}
                for url, group, current in conn.execute(select(JobRow.job_url, JobRow.dup_group, JobRow.dup_count))
                if others.get(group, 0) != current
            ]
            if updates:
                _update_jobs_by_url(conn, ["dup_count"], updates)
        logger.debug(f"Recomputed dup_count on {len(updates):,} rows")
        return len(updates)

    def clear_dead_descriptions(self) -> int:
        """Drop job_description on rows outside jobs_filtered — their text is never shown; returns how many.

        Non-kept means irrelevant or confirmed-closed. A cleared irrelevant row re-fetches its
        description if it later flips relevant; a closed one does not (its posting 404s), and isn't
        shown regardless. The signals read off it at fetch time are left intact. Run after the kept set settles.
        """
        with self.engine.begin() as conn:
            result = conn.execute(
                update(JobRow)
                .where(
                    JobRow.job_description.isnot(None),
                    or_(JobRow.is_relevant.isnot(True), JobRow.is_open.is_(False)),
                )
                .values(job_description=None)
            )
        logger.debug(f"Cleared descriptions from {result.rowcount:,} non-kept rows")
        return result.rowcount

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

    def record_postings(self, jobs: list[Job]) -> dict[str, int]:
        """Store fetched descriptions and open-status on rows already stored, matched on job_url.

        A job carries either, both, or neither: only the fields that arrived are written, so a failed
        fetch (both None) touches nothing. A written description is read for its language and for any
        location it scopes the role to, in the same update. Returns how many descriptions and
        open-status were written.
        """
        verified_at = datetime.now().isoformat(sep=" ", timespec="seconds")
        described = [
            {
                "url": job.key,
                "job_description": job.job_description,
                "description_lang": language,
                # The language it reads as narrows which country names are worth looking for.
                "stated_locations": stated_locations(job.job_description, language),
                "work_eligibility": work_eligibility(job.job_description),
            }
            for job in jobs
            if job.job_description is not None
            for language in [description_lang(job.job_description)]
        ]
        verified = [
            {"url": job.key, "is_open": job.is_open, "last_verified": verified_at}
            for job in jobs
            if job.is_open is not None
        ]
        with self.engine.begin() as conn:
            columns = ["job_description", "description_lang", "stated_locations", "work_eligibility"]
            filled = _update_jobs_by_url(conn, columns, described) if described else 0
            if verified:
                _update_jobs_by_url(conn, ["is_open", "last_verified"], verified)
        closed = sum(job.is_open is False for job in jobs)
        return {"described": filled, "checked": len(verified), "closed": closed}

    def close(self) -> None:
        self.engine.dispose()
