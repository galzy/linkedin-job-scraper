from collections import Counter

from linkedin_scraper.config import load_and_validate_config
from linkedin_scraper.filters import derive_workplace_types, relevance_predicate


def _config(**overrides):
    base = {
        "search_queries": [{"keywords": "k", "location": "l"}],
    }
    return load_and_validate_config(base | overrides)


def test_relevance_predicate_applies_include_exclude_and_company_filters():
    keep = relevance_predicate(
        _config(title_include=["engineer"], title_exclude=["clinical"], company_exclude=["badcorp"])
    )
    # No query links, so nothing is judged on workplace type here.
    assert keep("Python Engineer", "ACME", "remote", set()) is True
    assert keep("Clinical Engineer", "ACME", "remote", set()) is False
    assert keep("Python Engineer", "BadCorp", "remote", set()) is False
    assert keep("Chef", "ACME", "remote", set()) is False


def test_an_empty_include_list_keeps_everything_rather_than_nothing():
    """The asymmetry: an empty title_include means "no filter". An empty exclude list would
    drop nothing anyway, so only include needs the guard."""
    keeps_all = relevance_predicate(_config(title_include=[]))
    assert keeps_all("Chef", "ACME", "remote", set()) is True
    assert keeps_all("Engineer", "ACME", "remote", set()) is True

    keep = relevance_predicate(_config(title_include=["engineer"]))
    assert keep("Chef", "ACME", "remote", set()) is False
    assert keep("Engineer", "ACME", "remote", set()) is True


def test_relevance_predicate_is_case_insensitive_both_ways():
    keep = relevance_predicate(_config(title_include=["Engineer"], company_exclude=["BadCorp"]))
    assert keep("python engineer", "acme", "remote", set()) is True
    assert keep("PYTHON ENGINEER", "BADCORP", "remote", set()) is False


def test_company_exclude_matches_the_whole_name_not_a_substring():
    # "Turing" must not knock out "...Manufacturing"; company_exclude is whole-name, unlike title_exclude.
    keep = relevance_predicate(_config(title_include=[], company_exclude=["Turing"]))
    assert keep("Engineer", "Turing", "remote", set()) is False
    assert keep("Engineer", "Hexagon Manufacturing Intelligence", "remote", set()) is True


def test_derive_workplace_types_reads_the_type_from_the_query_that_found_it():
    attribution = {"qa": Counter({"u1": 1}), "qb": Counter({"u2": 1})}
    assert derive_workplace_types(attribution, {"qa": "remote", "qb": "hybrid"}) == {"u1": "remote", "u2": "hybrid"}


def test_derive_workplace_types_resolves_a_cross_search_job_by_sightings():
    # A url found under two tagged searches takes the type that saw it more often.
    attribution = {"r": Counter({"u1": 1}), "h": Counter({"u1": 3})}
    assert derive_workplace_types(attribution, {"r": "remote", "h": "hybrid"}) == {"u1": "hybrid"}


def test_derive_workplace_types_skips_a_non_tagged_query():
    # No scraped query is untagged; if one ever is, its jobs get no type here and the caller defaults them.
    attribution = {"probe": Counter({"u1": 1})}
    assert derive_workplace_types(attribution, {"probe": "untagged"}) == {}


def _query_id(config):
    return config.scrape_queries[0].query_id  # all four variants share the query's keep-list


def _wants(*types):
    return _config(title_include=[], search_queries=[{"keywords": "k", "location": "l", "workplace_type": list(types)}])


def test_a_querys_keeplist_drops_jobs_of_the_types_it_does_not_want():
    config = _wants("remote")
    qid = _query_id(config)
    keep = relevance_predicate(config)
    assert keep("Engineer", "ACME", "remote", {qid}) is True
    assert keep("Engineer", "ACME", "on_site", {qid}) is False


def test_an_empty_keeplist_keeps_every_workplace_type():
    config = _config(title_include=[])  # the default query keeps all types
    keep = relevance_predicate(config)
    assert keep("Engineer", "ACME", "on_site", {_query_id(config)}) is True


def test_a_job_no_current_query_surfaced_is_not_rejected_on_workplace():
    keep = relevance_predicate(_wants("remote"))
    assert keep("Engineer", "ACME", "on_site", set()) is True
