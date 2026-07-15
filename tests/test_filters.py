from collections import Counter

from linkedin_scraper.config import load_and_validate_config
from linkedin_scraper.filters import (
    derive_workplace_types,
    job_query_links,
    relevance_predicate,
    remove_duplicates,
    remove_irrelevant_jobs,
)
from linkedin_scraper.job import Job


def job(title="Engineer", company="ACME", date="2024-01-01", url="https://x/1/"):
    return Job(title=title, company=company, date=date, job_url=url)


def test_remove_duplicates_keeps_first_occurrence_and_order():
    jobs = [job(url="https://x/b/"), job(url="https://x/a/"), job(url="https://x/b/")]
    out = remove_duplicates(jobs)
    assert [j.job_url for j in out] == ["https://x/b/", "https://x/a/"]


def test_remove_duplicates_collapses_one_url_even_when_the_titles_differ():
    """LinkedIn can serve one posting under drifting title text. The URL is the identity the
    jobs table uses, so the in-memory dedupe and the DB agree on what one job is."""
    jobs = [job(title="Engineer"), job(title="Engineer (Remote)")]
    assert [j.title for j in remove_duplicates(jobs)] == ["Engineer"]


def _config(**overrides):
    base = {
        "search_queries": [{"keywords": "k", "location": "l"}],
    }
    return load_and_validate_config(base | overrides)


def test_remove_irrelevant_jobs_applies_include_exclude_and_company_filters():
    jobs = [
        job(title="Python Engineer", company="ACME"),
        job(title="Clinical Engineer", company="ACME"),
        job(title="Python Engineer", company="BadCorp"),
        job(title="Chef", company="ACME"),
    ]
    out = remove_irrelevant_jobs(
        jobs,
        _config(title_include=["engineer"], title_exclude=["clinical"], company_exclude=["badcorp"]),
        links={},  # no query links: nothing is judged on workplace type here
    )
    assert [(j.title, j.company) for j in out] == [("Python Engineer", "ACME")]


def test_an_empty_include_list_keeps_everything_rather_than_nothing():
    """The asymmetry: an empty title_include means "no filter". An empty exclude list would
    drop nothing anyway, so only include needs the guard."""
    jobs = [job(title="Chef"), job(title="Engineer")]

    assert remove_irrelevant_jobs(jobs, _config(title_include=[]), links={}) == jobs
    assert remove_irrelevant_jobs(jobs, _config(title_include=["engineer"]), links={}) == [job(title="Engineer")]


def test_relevance_predicate_is_case_insensitive_both_ways():
    keep = relevance_predicate(_config(title_include=["Engineer"], company_exclude=["BadCorp"]))
    assert keep("python engineer", "acme", "remote", set()) is True
    assert keep("PYTHON ENGINEER", "BADCORP", "remote", set()) is False


def test_derive_workplace_types_reads_the_type_from_the_query_that_found_it():
    attribution = {"qa": Counter({"u1": 1}), "qb": Counter({"u2": 1})}
    assert derive_workplace_types(attribution, {"qa": "remote", "qb": "untagged"}) == {"u1": "remote", "u2": "untagged"}


def test_derive_workplace_types_lets_a_tagged_search_win_over_the_catch_all():
    attribution = {"tagged": Counter({"u1": 1}), "catch_all": Counter({"u1": 3})}
    assert derive_workplace_types(attribution, {"tagged": "on_site", "catch_all": "untagged"}) == {"u1": "on_site"}


def test_derive_workplace_types_breaks_a_two_type_tie_by_precedence():
    # A url tagged both remote and hybrid with equal sightings — remote wins on precedence.
    attribution = {"r": Counter({"u1": 2}), "h": Counter({"u1": 2})}
    assert derive_workplace_types(attribution, {"r": "remote", "h": "hybrid"}) == {"u1": "remote"}


def test_job_query_links_inverts_attribution_to_the_queries_per_job():
    attribution = {"qa": Counter({"u1": 1, "u2": 1}), "qb": Counter({"u1": 3})}
    assert job_query_links(attribution) == {"u1": {"qa", "qb"}, "u2": {"qa"}}


def _query_id(config):
    return config.scrape_queries[0].query_id  # all four variants share the query's keep-list


def _wants(*types):
    return _config(title_include=[], search_queries=[{"keywords": "k", "location": "l", "workplace_type": list(types)}])


def test_a_querys_keeplist_drops_jobs_of_the_types_it_does_not_want():
    config = _wants("remote")
    qid = _query_id(config)
    remote = job().with_workplace_type("remote")
    onsite = job(url="https://x/2/").with_workplace_type("on_site")
    links = {remote.job_url: {qid}, onsite.job_url: {qid}}

    assert remove_irrelevant_jobs([remote, onsite], config, links) == [remote]


def test_an_empty_keeplist_keeps_every_workplace_type():
    config = _config(title_include=[])  # the default query keeps all types
    onsite = job().with_workplace_type("on_site")
    assert remove_irrelevant_jobs([onsite], config, {onsite.job_url: {_query_id(config)}}) == [onsite]


def test_a_job_no_current_query_surfaced_is_not_rejected_on_workplace():
    onsite = job().with_workplace_type("on_site")
    assert remove_irrelevant_jobs([onsite], _wants("remote"), links={}) == [onsite]
