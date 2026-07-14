from collections import Counter
from contextlib import contextmanager
from unittest.mock import patch

import pytest
import yaml
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from linkedin_scraper.config import SearchQuery, load_and_validate_config
from linkedin_scraper.constants import NO_DESCRIPTION
from linkedin_scraper.geo import searched_countries
from linkedin_scraper.job import Job
from linkedin_scraper.store.db import JobsDb


@pytest.fixture
def db():
    database = JobsDb(":memory:")
    database.create_schema()
    yield database
    database.close()


def job(
    title="Engineer",
    company="ACME",
    date="2024-01-01",
    job_url="https://x/1/",
    job_description=None,
    location="Bologna",
    is_open=None,
):
    return Job(
        title=title,
        company=company,
        location=location,
        date=date,
        job_url=job_url,
        job_description=job_description,
        is_open=is_open,
    )


def a_query(keywords="python", location="Bologna", **overrides):
    return SearchQuery(keywords=keywords, location=location, **overrides)


def rows(db, *columns, table="jobs_raw"):
    with db.engine.connect() as conn:
        return conn.execute(text(f"SELECT {', '.join(columns)} FROM {table}")).fetchall()


def pragma(db, statement):
    with db.engine.connect() as conn:
        return conn.execute(text(statement)).fetchall()


@contextmanager
def clock(now):
    """Pin the insert timestamp. Real runs stamp to the second, so two inserts can share one."""
    with patch("linkedin_scraper.store.db.datetime") as dt:
        dt.now.return_value.isoformat.return_value = now
        yield


def test_create_schema_is_idempotent(db):
    db.create_schema()  # every run calls it
    db.insert_jobs([job()])
    assert len(rows(db, "title")) == 1


def test_insert_jobs_stamps_one_sighting_time_across_the_batch(db):
    db.insert_jobs([job(job_url="https://x/1/"), job(job_url="https://x/2/")])
    stamped = set(rows(db, "first_seen", "last_seen"))

    assert len(stamped) == 1
    first_seen, last_seen = next(iter(stamped))
    assert first_seen == last_seen


def test_reinserting_an_existing_row_adds_no_row(db):
    """The upsert updates the stored row rather than skipping it, so the count of new rows
    cannot come from rowcount — which would report the touched row as added."""
    db.insert_jobs([job()])
    assert db.insert_jobs([job()]) == 0
    assert len(rows(db, "title")) == 1


def test_insert_jobs_appends_only_the_new_rows(db):
    db.insert_jobs([job()])
    assert db.insert_jobs([job(), job(job_url="https://x/2/")]) == 1
    assert sorted(row[0] for row in rows(db, "job_url")) == ["https://x/1/", "https://x/2/"]


def test_insert_derives_the_country_from_location(db):
    db.insert_jobs([job(location="Milan, Lombardy, Italy")])
    assert rows(db, "country") == [("Italy",)]


def test_a_scope_resolves_a_metro_labels_country_the_default_leaves_it_null(db):
    metro = "Greater Milan Metropolitan Area"
    db.insert_jobs([job(job_url="https://x/1/", location=metro)], countries=searched_countries(["Italy"]))
    db.insert_jobs([job(job_url="https://x/2/", location=metro)])  # no scope: metro label stays unresolved
    assert set(rows(db, "country")) == {("Italy",), (None,)}


def test_runs_seen_counts_runs_one_per_insert(db):
    """Each run advances it by one, whatever the card count that run collapsed into the row."""
    db.insert_jobs([job()])
    db.insert_jobs([job()])
    assert rows(db, "runs_seen") == [(2,)]


def test_a_resighting_moves_last_seen_and_leaves_first_seen_alone(db):
    with clock("2024-01-01 09:00:00"):
        db.insert_jobs([job()])
    with clock("2024-03-02 18:00:00"):
        db.insert_jobs([job()])

    assert rows(db, "first_seen", "last_seen") == [("2024-01-01 09:00:00", "2024-03-02 18:00:00")]


def test_a_resighting_does_not_wipe_a_stored_description(db):
    """The upsert names the columns it touches; job_description is not one, or every re-scrape
    would throw away the description and refetch it next run."""
    db.insert_jobs([job()])
    db.record_postings([job(job_description="Build things.")])
    db.insert_jobs([job()])

    assert rows(db, "job_description") == [("Build things.",)]


def test_one_posting_under_two_titles_is_stored_once(db):
    """The URL alone is the identity, so drifting title text cannot split a job into two rows."""
    db.insert_jobs([job(title="Engineer"), job(title="Engineer (Remote)")])
    assert rows(db, "title") == [("Engineer",)]


def test_a_new_row_is_unjudged_until_refresh(db):
    """Insert leaves is_relevant NULL; refresh_relevance is the one thing that ever decides it."""
    db.insert_jobs([job()])
    assert rows(db, "is_relevant") == [(None,)]


def test_a_new_row_is_presumed_open_but_not_yet_verified(db):
    """It just surfaced in search, so is_open starts True; last_verified stays NULL until a page fetch."""
    db.insert_jobs([job()])
    assert rows(db, "is_open", "last_verified") == [(1, None)]


def test_a_resighting_does_not_reopen_a_closed_job(db):
    """The upsert never touches is_open, so a verified verdict outlives a later search sighting."""
    db.insert_jobs([job()])
    db.record_postings([job(is_open=False)])
    db.insert_jobs([job()])  # seen again in search

    assert rows(db, "is_open") == [(0,)]


def test_refresh_relevance_flags_the_rows_the_filters_reject(db):
    db.insert_jobs([job(title="Engineer"), job(title="Chef", job_url="https://x/2/")])
    db.refresh_relevance(lambda title, company, workplace, qids: title == "Engineer")

    assert dict(rows(db, "title", "is_relevant")) == {"Engineer": 1, "Chef": 0}


def test_refresh_relevance_counts_a_reversal_not_a_first_judgment(db):
    db.insert_jobs([job(title="Engineer")])
    assert db.refresh_relevance(lambda title, company, workplace, qids: True) == 0  # first verdict, not a flip
    assert db.refresh_relevance(lambda title, company, workplace, qids: False) == 1  # relevant -> irrelevant
    assert db.refresh_relevance(lambda title, company, workplace, qids: False) == 0  # unchanged


def test_refresh_relevance_rejudges_rows_this_run_never_scraped(db):
    """The verdict follows config.yaml, not the job, so editing the config stales every row."""
    db.insert_jobs([job(title="Chef")])
    db.refresh_relevance(lambda title, company, workplace, qids: False)

    assert db.refresh_relevance(lambda title, company, workplace, qids: True) == 1
    assert rows(db, "is_relevant") == [(1,)]


def test_the_filtered_view_hides_the_rows_the_filters_reject(db):
    db.insert_jobs([job(title="Engineer"), job(title="Chef", job_url="https://x/2/")])
    db.refresh_relevance(lambda title, company, workplace, qids: title == "Engineer")

    assert rows(db, "title", table="jobs_filtered") == [("Engineer",)]
    assert len(rows(db, "title")) == 2  # the raw row is still there to audit


def test_record_postings_fills_the_matching_row(db):
    db.insert_jobs([job(), job(job_url="https://x/2/")])
    assert db.record_postings([job(job_description="Build things.")]) == {"described": 1, "checked": 0, "closed": 0}

    assert dict(rows(db, "job_url", "job_description")) == {
        "https://x/1/": "Build things.",
        "https://x/2/": None,
    }


def test_record_postings_stamps_open_status_and_verification_time(db):
    db.insert_jobs([job(), job(job_url="https://x/2/")])
    counts = db.record_postings([job(is_open=True), job(job_url="https://x/2/", is_open=False)])

    assert counts == {"described": 0, "checked": 2, "closed": 1}
    assert dict(rows(db, "job_url", "is_open")) == {"https://x/1/": 1, "https://x/2/": 0}
    assert all(verified is not None for (verified,) in rows(db, "last_verified"))


def test_record_postings_leaves_untouched_the_fields_a_job_did_not_carry(db):
    """A failed fetch carries neither description nor open-status; it must overwrite neither."""
    db.insert_jobs([job()])
    db.record_postings([job(job_description="Build things.", is_open=True)])
    db.record_postings([job()])  # both None

    assert rows(db, "job_description", "is_open") == [("Build things.", 1)]


def test_the_filtered_view_hides_closed_jobs_but_keeps_open_and_unchecked_ones(db):
    db.insert_jobs([job(), job(job_url="https://x/2/"), job(job_url="https://x/3/")])
    db.refresh_relevance(lambda title, company, workplace, qids: True)
    db.record_postings([job(is_open=False), job(job_url="https://x/2/", is_open=True)])
    with db.engine.begin() as conn:  # x/3 as a legacy row never checked
        conn.execute(text("UPDATE jobs_raw SET is_open = NULL WHERE job_url = 'https://x/3/'"))

    # x/1 closed -> hidden; x/2 open and x/3 unchecked (NULL) -> kept.
    assert {u for (u,) in rows(db, "job_url", table="jobs_filtered")} == {"https://x/2/", "https://x/3/"}


def test_postings_to_refresh_includes_relevant_rows_missing_a_description_at_any_age(db):
    db.insert_jobs([job(date="2024-01-01"), job(title="Chef", job_url="https://x/2/")])
    db.refresh_relevance(lambda title, company, workplace, qids: title == "Engineer")

    # The cutoff is well before the posting date, so only the missing-description branch can match.
    assert [j.key for j in db.postings_to_refresh("2020-01-01 00:00:00")] == ["https://x/1/"]


def test_postings_to_refresh_rechecks_an_aged_open_job_but_not_one_verified_recently(db):
    db.insert_jobs([job(date="2024-01-01"), job(date="2024-01-01", job_url="https://x/2/")])
    db.refresh_relevance(lambda title, company, workplace, qids: True)
    with clock("2024-01-02 00:00:00"):  # x/1 last verified before the cutoff
        db.record_postings([job(job_description="d", is_open=True)])
    with clock("2024-01-10 00:00:00"):  # x/2 last verified after it
        db.record_postings([job(job_url="https://x/2/", job_description="d", is_open=True)])

    assert [j.key for j in db.postings_to_refresh("2024-01-08 00:00:00")] == ["https://x/1/"]


def test_postings_to_refresh_leaves_a_still_fresh_posting_alone(db):
    db.insert_jobs([job(date="2024-02-01")])  # posted after the cutoff
    db.refresh_relevance(lambda title, company, workplace, qids: True)
    with clock("2024-02-02 00:00:00"):
        db.record_postings([job(job_description="d", is_open=True)])

    assert db.postings_to_refresh("2024-01-08 00:00:00") == []


def test_postings_to_refresh_skips_a_job_already_closed(db):
    db.insert_jobs([job(date="2024-01-01")])
    db.refresh_relevance(lambda title, company, workplace, qids: True)
    with clock("2024-01-02 00:00:00"):
        db.record_postings([job(job_description="d", is_open=False)])

    assert db.postings_to_refresh("2024-01-08 00:00:00") == []


def test_postings_to_refresh_skips_a_closed_job_still_missing_a_description(db):
    """A gone posting (404) is closed with its description never fetched; re-fetching only 404s again."""
    db.insert_jobs([job(date="2024-01-01")])
    db.refresh_relevance(lambda title, company, workplace, qids: True)
    db.record_postings([job(is_open=False)])  # closed, job_description still NULL

    assert db.postings_to_refresh("2024-01-08 00:00:00") == []


def test_postings_to_refresh_ages_a_dateless_card_by_first_seen(db):
    with clock("2024-01-01 00:00:00"):
        db.insert_jobs([job(date="")])  # no posting date, so first_seen stands in for it
    db.refresh_relevance(lambda title, company, workplace, qids: True)
    with clock("2024-01-02 00:00:00"):
        db.record_postings([job(date="", job_description="d", is_open=True)])

    assert [j.key for j in db.postings_to_refresh("2024-01-08 00:00:00")] == ["https://x/1/"]


def test_relevant_jobs_without_description_keeps_only_relevant_rows_still_lacking_one(db):
    """NULL covers both a job never described and a fetch that failed; both stay on the worklist."""
    db.insert_jobs([job(), job(job_url="https://x/2/"), job(title="Chef", job_url="https://x/3/")])
    db.refresh_relevance(lambda title, company, workplace, qids: title == "Engineer")
    db.record_postings([job(job_url="https://x/2/", job_description="Build things.")])

    assert [j.key for j in db.relevant_jobs_without_description()] == ["https://x/1/"]


def test_relevant_jobs_without_description_counts_the_not_found_placeholder_as_described(db):
    """A page that loaded but carried no description is a real answer, not a failure — storing
    the placeholder is what stops it being refetched on every future run."""
    db.insert_jobs([job()])
    db.refresh_relevance(lambda title, company, workplace, qids: True)
    db.record_postings([job(job_description=NO_DESCRIPTION)])

    assert db.relevant_jobs_without_description() == []


def test_a_database_that_cannot_be_opened_raises_rather_than_running_on(tmp_path):
    """create_engine is lazy, so the failure surfaces on first use. __main__ turns it into a
    non-zero exit for cron."""
    database = JobsDb(str(tmp_path))  # a directory, not a file
    with pytest.raises(SQLAlchemyError):
        database.create_schema()


# --- queries, attribution, and runs ------------------------------------------


def test_upsert_queries_stores_one_row_per_distinct_query(db):
    db.upsert_queries([a_query(keywords="python"), a_query(keywords="etl")], run_ts="2024-01-01 09:00:00")
    assert len(rows(db, "query_id", table="queries")) == 2


def test_upsert_queries_folds_identical_queries_into_one_row(db):
    """Two identical queries in one config share an id; upserting both must not touch it twice."""
    db.upsert_queries([a_query(), a_query()], run_ts="2024-01-01 09:00:00")
    assert len(rows(db, "query_id", table="queries")) == 1


def test_upsert_queries_keeps_first_used_and_advances_last_used(db):
    db.upsert_queries([a_query()], run_ts="2024-01-01 09:00:00")
    db.upsert_queries([a_query()], run_ts="2024-03-02 18:00:00")

    assert rows(db, "first_used", "last_used", table="queries") == [("2024-01-01 09:00:00", "2024-03-02 18:00:00")]


def test_record_attribution_links_a_job_to_the_query_that_found_it(db):
    q = a_query()
    db.insert_jobs([job()])
    db.record_attribution({q.query_id: Counter({"https://x/1/": 1})}, seen_at="2024-01-01 09:00:00")

    assert rows(db, "job_url", "query_id", table="job_queries") == [("https://x/1/", q.query_id)]


def test_record_attribution_accumulates_sightings_and_moves_last_seen(db):
    q = a_query()
    db.insert_jobs([job()])
    db.record_attribution({q.query_id: Counter({"https://x/1/": 3})}, seen_at="2024-01-01 09:00:00")
    db.record_attribution({q.query_id: Counter({"https://x/1/": 2})}, seen_at="2024-03-02 18:00:00")

    assert rows(db, "times_seen", "first_seen", "last_seen", table="job_queries") == [
        (5, "2024-01-01 09:00:00", "2024-03-02 18:00:00")
    ]


def test_a_job_found_by_two_queries_gets_a_row_for_each(db):
    q1, q2 = a_query(keywords="python"), a_query(keywords="etl")
    db.insert_jobs([job()])
    db.record_attribution(
        {q1.query_id: Counter({"https://x/1/": 1}), q2.query_id: Counter({"https://x/1/": 1})},
        seen_at="2024-01-01 09:00:00",
    )

    assert sorted(row[0] for row in rows(db, "query_id", table="job_queries")) == sorted([q1.query_id, q2.query_id])


def test_fill_country_takes_the_single_country_a_null_rows_queries_name(db):
    q = a_query(location="Germany")  # a multi-city area resolves to no country, but its search names one
    db.insert_jobs([job(location="Cologne Bonn Region")])
    db.upsert_queries([q], run_ts="2024-01-01 09:00:00")
    db.record_attribution({q.query_id: Counter({"https://x/1/": 1})}, seen_at="2024-01-01 09:00:00")

    assert db.fill_missing_country_from_queries() == 1
    assert rows(db, "country") == [("Germany",)]


def test_fill_country_leaves_a_row_null_when_its_queries_disagree(db):
    de, nl = a_query(location="Germany"), a_query(location="Netherlands")
    db.insert_jobs([job(location="Cologne Bonn Region")])
    db.upsert_queries([de, nl], run_ts="2024-01-01 09:00:00")
    db.record_attribution(
        {de.query_id: Counter({"https://x/1/": 1}), nl.query_id: Counter({"https://x/1/": 1})},
        seen_at="2024-01-01 09:00:00",
    )

    assert db.fill_missing_country_from_queries() == 0
    assert rows(db, "country") == [(None,)]


def test_fill_country_leaves_an_already_resolved_row_untouched(db):
    q = a_query(location="Germany")  # the location resolves on its own; the query must not override it
    db.insert_jobs([job(location="Milan, Italy")])
    db.upsert_queries([q], run_ts="2024-01-01 09:00:00")
    db.record_attribution({q.query_id: Counter({"https://x/1/": 1})}, seen_at="2024-01-01 09:00:00")

    assert db.fill_missing_country_from_queries() == 0
    assert rows(db, "country") == [("Italy",)]


def test_record_run_persists_the_counts_and_returns_the_run_id(db):
    run_id = db.record_run(
        started_at="2024-01-01 09:00:00",
        finished_at="2024-01-01 09:05:00",
        status="completed",
        counts={"scraped": 10, "deduped": 8, "relevant": 5, "added": 5, "flipped": 1},
        config_yaml="search_queries:\n  - {keywords: python, location: l}\n",
        query_ids=[a_query().query_id],
    )

    assert rows(db, "run_id", "status", "scraped", "deduped", "relevant", "added", "flipped", table="runs") == [
        (run_id, "completed", 10, 8, 5, 5, 1)
    ]


def test_record_run_links_the_run_to_every_query_it_used(db):
    q1, q2 = a_query(keywords="python"), a_query(keywords="etl")
    run_id = db.record_run(
        started_at="2024-01-01 09:00:00",
        finished_at="2024-01-01 09:05:00",
        status="completed",
        counts={"scraped": 0, "deduped": 0, "relevant": 0, "added": 0, "flipped": 0},
        config_yaml="",
        query_ids=[q1.query_id, q2.query_id, q1.query_id],  # a repeat folds to one link
    )

    assert sorted(rows(db, "query_id", table="run_queries")) == sorted([(q1.query_id,), (q2.query_id,)])
    assert {row[0] for row in rows(db, "run_id", table="run_queries")} == {run_id}


def test_record_run_stores_a_config_that_reloads(db):
    """The snapshot is the raw file, so it validates again — model_dump()'s one-way enums would not."""
    config_yaml = "search_queries:\n  - keywords: python\n    location: l\n    distance: KM_40\n"
    db.record_run(
        started_at="2024-01-01 09:00:00",
        finished_at="2024-01-01 09:05:00",
        status="completed",
        counts={"scraped": 0, "deduped": 0, "relevant": 0, "added": 0, "flipped": 0},
        config_yaml=config_yaml,
        query_ids=[a_query().query_id],
    )

    stored = rows(db, "config_yaml", table="runs")[0][0]
    reloaded = load_and_validate_config(yaml.safe_load(stored))
    assert reloaded.search_queries[0].distance == "25"  # KM_40 mapped, so it really re-validated


def test_last_run_returns_the_newest_run(db):
    counts = {"scraped": 0, "deduped": 0, "relevant": 0, "added": 0, "flipped": 0}
    db.record_run(
        started_at="2024-01-01 09:00:00",
        finished_at="2024-01-01 09:05:00",
        status="completed",
        counts=counts,
        config_yaml="",
        query_ids=[],
    )
    db.record_run(
        started_at="2024-03-02 18:00:00",
        finished_at="2024-03-02 18:04:00",
        status="blocked",
        counts=counts,
        config_yaml="",
        query_ids=[],
    )

    run = db.last_run()
    assert (run.started_at, run.status) == ("2024-03-02 18:00:00", "blocked")


def test_last_run_before_any_run_is_none(db):
    assert db.last_run() is None


def test_totals_counts_stored_relevant_and_still_undescribed(db):
    db.insert_jobs([job(), job(job_url="https://x/2/"), job(title="Chef", job_url="https://x/3/")])
    db.refresh_relevance(lambda title, company, workplace, qids: title == "Engineer")
    db.record_postings([job(job_description="Build things.")])

    assert db.totals() == {"stored": 3, "relevant": 2, "missing_descriptions": 1}


def test_totals_on_an_empty_database_are_zero(db):
    assert db.totals() == {"stored": 0, "relevant": 0, "missing_descriptions": 0}


def test_relevant_among_counts_only_relevant_rows_within_the_given_urls(db):
    db.insert_jobs([job(), job(job_url="https://x/2/"), job(title="Chef", job_url="https://x/3/")])
    db.refresh_relevance(lambda title, company, workplace, qids: title == "Engineer")
    # x/1 and x/2 are relevant Engineers; x/3 (Chef) is rejected. The count is scoped to the urls
    # passed, so a relevant row outside the set (x/2) does not count, unlike the cumulative totals().
    assert db.relevant_among({"https://x/1/", "https://x/3/"}) == 1
    assert db.relevant_among({"https://x/1/", "https://x/2/", "https://x/3/"}) == 2
    assert db.relevant_among({"https://x/unknown/"}) == 0
    assert db.relevant_among(set()) == 0


def test_refresh_relevance_judges_on_the_stored_workplace_type(db):
    db.insert_jobs([job().with_workplace_type("remote"), job(job_url="https://x/2/").with_workplace_type("on_site")])

    db.refresh_relevance(lambda title, company, workplace, qids: workplace != "on_site")
    assert dict(rows(db, "workplace_type", "is_relevant")) == {"remote": 1, "on_site": 0}


def test_a_rescrape_does_not_downgrade_a_known_type_to_untagged(db):
    db.insert_jobs([job().with_workplace_type("remote")])
    db.insert_jobs([job().with_workplace_type("untagged")])  # a later run that only caught it via none
    assert rows(db, "workplace_type") == [("remote",)]


def test_a_rescrape_upgrades_untagged_to_a_known_type(db):
    db.insert_jobs([job().with_workplace_type("untagged")])
    db.insert_jobs([job().with_workplace_type("remote")])
    assert rows(db, "workplace_type") == [("remote",)]


def test_staged_scrape_dedupes_by_url_and_attributes_per_query(db):
    db.stage_jobs([job(job_url="https://x/1/"), job(job_url="https://x/1/"), job(job_url="https://x/2/")], "qA")
    db.stage_jobs([job(job_url="https://x/1/")], "qB")

    jobs, attribution = db.staged_scrape()

    assert sorted(j.job_url for j in jobs) == ["https://x/1/", "https://x/2/"]  # one row per posting
    assert attribution["qA"] == Counter({"https://x/1/": 2, "https://x/2/": 1})  # per-page cards counted
    assert attribution["qB"] == Counter({"https://x/1/": 1})
    assert sum(sum(c.values()) for c in attribution.values()) == 4  # the pre-dedup scraped total


def test_staged_scrape_keeps_one_row_when_a_postings_title_drifts(db):
    """LinkedIn serves one posting under drifting title text; the url is its identity, so it stays one row."""
    db.stage_jobs([job(title="Engineer"), job(title="Engineer (Remote)")], "qA")  # same url

    jobs, _ = db.staged_scrape()

    assert [j.title for j in jobs] == ["Engineer"]  # first card wins


def test_reset_staging_empties_the_table(db):
    db.stage_jobs([job()], "qA")
    db.reset_staging()

    jobs, attribution = db.staged_scrape()

    assert jobs == []
    assert not attribution
