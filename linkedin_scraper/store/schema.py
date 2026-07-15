"""The SQLite tables as declarative models. Operations over them live in ``db.py``."""

from sqlalchemy import Computed
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from linkedin_scraper.constants import (
    TABLE_JOB_QUERIES,
    TABLE_JOBS_RAW,
    TABLE_QUERIES,
    TABLE_RUN_QUERIES,
    TABLE_RUNS,
    TABLE_SCRAPE_STAGING,
)


class SqlBase(DeclarativeBase):
    pass


class JobRow(SqlBase):
    """One job as stored, keyed on its posting URL.

    Its columns are ``Job``'s scraped fields, the ``country`` that ``_row`` derives from ``location``,
    and the DB's own ``first_seen``/``last_seen``/``runs_seen``/``is_relevant``.
    """

    __tablename__ = TABLE_JOBS_RAW

    # Mapped[str] is what emits the NOT NULL, and here that is load-bearing: SQLite lets a NULL
    # sit in a PRIMARY KEY, and two of them would be distinct.
    job_url: Mapped[str] = mapped_column(primary_key=True)
    title: Mapped[str]
    company: Mapped[str]
    date: Mapped[str]
    location: Mapped[str] = mapped_column(default="")
    country: Mapped[str | None] = mapped_column(default=None)
    job_description: Mapped[str | None] = mapped_column(default=None)
    is_english: Mapped[bool | None] = mapped_column(default=None)  # NULL until a fetched description is judged
    workplace_type: Mapped[str] = mapped_column(default="untagged")
    first_seen: Mapped[str]
    last_seen: Mapped[str]
    runs_seen: Mapped[int] = mapped_column(default=1)
    is_relevant: Mapped[bool | None] = mapped_column(default=None)  # NULL until refresh_relevance first judges it
    is_open: Mapped[bool | None] = mapped_column(default=None)  # NULL until first verified against the posting page
    last_verified: Mapped[str | None] = mapped_column(default=None)  # when is_open was last checked
    # A posting's identity for spotting duplicates: its title and company, normalized.
    dup_group: Mapped[str] = mapped_column(Computed("lower(trim(title)) || ' @ ' || lower(trim(company))"))
    dup_count: Mapped[int | None] = mapped_column(default=None)  # other kept rows sharing dup_group; NULL until counted


class QueryRow(SqlBase):
    """One distinct search query, keyed by ``SearchQuery.query_id``."""

    __tablename__ = TABLE_QUERIES

    query_id: Mapped[str] = mapped_column(primary_key=True)
    keywords: Mapped[str]
    location: Mapped[str]
    distance: Mapped[str]
    harvest_type: Mapped[str]
    timespan: Mapped[str]
    first_used: Mapped[str]
    last_used: Mapped[str]


class JobQueryRow(SqlBase):
    """Which query found which job, keyed on the pair. A job can appear under several queries."""

    __tablename__ = TABLE_JOB_QUERIES

    job_url: Mapped[str] = mapped_column(primary_key=True)
    query_id: Mapped[str] = mapped_column(primary_key=True)
    first_seen: Mapped[str]
    last_seen: Mapped[str]
    times_seen: Mapped[int] = mapped_column(default=1)  # cards from this query across runs


class RunRow(SqlBase):
    """One scrape run: when it ran, how it ended, its counts, and the config it used."""

    __tablename__ = TABLE_RUNS

    run_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    started_at: Mapped[str]
    finished_at: Mapped[str]
    status: Mapped[str]  # "completed" or "blocked"
    scraped: Mapped[int]
    deduped: Mapped[int]
    relevant: Mapped[int]
    added: Mapped[int]
    flipped: Mapped[int]
    config_yaml: Mapped[str]  # the raw config file, verbatim: reproduces the run (model_dump would not)


class RunQueryRow(SqlBase):
    """Which queries a run used, keyed on the pair. Links a run to the queries table."""

    __tablename__ = TABLE_RUN_QUERIES

    run_id: Mapped[int] = mapped_column(primary_key=True)
    query_id: Mapped[str] = mapped_column(primary_key=True)


class StagingRow(SqlBase):
    """One posting a query scraped this run, staged before the end-of-run pipeline consumes it.

    Wiped at the start of every run; a crash mid-scrape leaves the completed queries' rows behind.
    """

    __tablename__ = TABLE_SCRAPE_STAGING

    job_url: Mapped[str] = mapped_column(primary_key=True)
    query_id: Mapped[str] = mapped_column(primary_key=True)
    title: Mapped[str]
    company: Mapped[str]
    date: Mapped[str]
    location: Mapped[str] = mapped_column(default="")
    times_seen: Mapped[int] = mapped_column(default=1)  # cards for this posting under this query
    seen_at: Mapped[str]
