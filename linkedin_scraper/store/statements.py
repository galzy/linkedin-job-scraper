"""Shared SQL building blocks: the row builder, the by-URL update, and the upsert statements."""

from sqlalchemy import Connection, bindparam, case, update
from sqlalchemy.dialects.sqlite import Insert, insert

from linkedin_scraper.geo import country_of
from linkedin_scraper.job import Job
from linkedin_scraper.store.schema import JobQueryRow, JobRow, QueryRow, StagingRow


def _row(job: Job, seen_at: str, countries: frozenset[str]) -> dict:
    """The scraped job as a row: its fields, the country ``location`` names, and the DB's own stamps.

    is_open starts True — the job just surfaced in search; a later posting-page fetch settles it, and
    last_verified stays NULL until then, so the row is still due for a real check.
    """
    stamps = {"first_seen": seen_at, "last_seen": seen_at, "runs_seen": 1}
    return job.model_dump() | {"country": country_of(job.location, countries), "is_open": True} | stamps


def _update_jobs_by_url(conn: Connection, column: str, updates: list[dict]) -> int:
    """Apply many ``{"url", "value"}`` updates to one jobs_raw column, matched by job_url; returns rows changed."""
    result = conn.execute(
        update(JobRow).where(JobRow.job_url == bindparam("url")).values(**{column: bindparam("value")}),
        updates,
    )
    return result.rowcount


def _upsert(row, on, assign) -> Insert:
    """INSERT for ``row``; on a key conflict over ``on``, update the existing row with ``assign(excluded)``."""
    stmt = insert(row)
    return stmt.on_conflict_do_update(index_elements=on, set_=assign(stmt.excluded))


# A re-scraped job updates these and nothing else: first_seen stays first, and job_description
# survives rather than being refetched next run.
_JOB_UPSERT = _upsert(
    JobRow,
    on=[JobRow.job_url],
    assign=lambda excluded: {
        "runs_seen": JobRow.runs_seen + 1,
        "last_seen": excluded.last_seen,
        # Refresh the type, but never downgrade a known one to untagged when this run missed it.
        "workplace_type": case(
            (excluded.workplace_type == "untagged", JobRow.workplace_type),
            else_=excluded.workplace_type,
        ),
    },
)

# A query seen again keeps its first_used and just advances last_used.
_QUERY_UPSERT = _upsert(QueryRow, on=[QueryRow.query_id], assign=lambda excluded: {"last_used": excluded.last_used})

# An attribution seen again accumulates its sightings and advances last_seen.
_JOB_QUERY_UPSERT = _upsert(
    JobQueryRow,
    on=[JobQueryRow.job_url, JobQueryRow.query_id],
    assign=lambda excluded: {
        "times_seen": JobQueryRow.times_seen + excluded.times_seen,
        "last_seen": excluded.last_seen,
    },
)

# A posting re-staged under the same query accumulates its card count.
_STAGING_UPSERT = _upsert(
    StagingRow,
    on=[StagingRow.job_url, StagingRow.query_id],
    assign=lambda excluded: {"times_seen": StagingRow.times_seen + excluded.times_seen},
)

# The columns that rebuild a Job to (re)fetch: its identity and card labels, not the job_description
# or is_open that a fetch is about to replace.
_JOB_FETCH_COLS = (JobRow.title, JobRow.company, JobRow.date, JobRow.job_url, JobRow.location, JobRow.workplace_type)
