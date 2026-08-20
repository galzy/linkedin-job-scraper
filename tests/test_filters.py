from linkedin_job_scraper.config import load_and_validate_config
from linkedin_job_scraper.filters import relevance_predicate


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
    assert keep("Python Engineer", "ACME", "remote", None, set()) is True
    assert keep("Clinical Engineer", "ACME", "remote", None, set()) is False
    assert keep("Python Engineer", "BadCorp", "remote", None, set()) is False
    assert keep("Chef", "ACME", "remote", None, set()) is False


def test_an_empty_include_list_keeps_everything_rather_than_nothing():
    """The asymmetry: an empty title_include means "no filter". An empty exclude list would
    drop nothing anyway, so only include needs the guard."""
    keeps_all = relevance_predicate(_config(title_include=[]))
    assert keeps_all("Chef", "ACME", "remote", None, set()) is True
    assert keeps_all("Engineer", "ACME", "remote", None, set()) is True

    keep = relevance_predicate(_config(title_include=["engineer"]))
    assert keep("Chef", "ACME", "remote", None, set()) is False
    assert keep("Engineer", "ACME", "remote", None, set()) is True


def test_relevance_predicate_is_case_insensitive_both_ways():
    keep = relevance_predicate(_config(title_include=["Engineer"], company_exclude=["BadCorp"]))
    assert keep("python engineer", "acme", "remote", None, set()) is True
    assert keep("PYTHON ENGINEER", "BADCORP", "remote", None, set()) is False


def test_company_exclude_matches_the_whole_name_not_a_substring():
    # "Turing" must not knock out "...Manufacturing"; company_exclude is whole-name, unlike title_exclude.
    keep = relevance_predicate(_config(title_include=[], company_exclude=["Turing"]))
    assert keep("Engineer", "Turing", "remote", None, set()) is False
    assert keep("Engineer", "Hexagon Manufacturing Intelligence", "remote", None, set()) is True


def _query_id(config):
    return config.scrape_queries[0].query_id


def _wants(*types):
    return _config(title_include=[], search_queries=[{"keywords": "k", "location": "l", "workplace_type": list(types)}])


def test_a_querys_keeplist_drops_jobs_of_the_types_it_does_not_want():
    config = _wants("remote")
    qid = _query_id(config)
    keep = relevance_predicate(config)
    assert keep("Engineer", "ACME", "remote", None, {qid}) is True
    assert keep("Engineer", "ACME", "on_site", None, {qid}) is False


def test_a_type_the_ad_never_stated_is_not_judged_against_a_keep_list():
    # An unstated type must not be read as the wrong one, or the keep-list drops what it cannot see.
    config = _wants("remote")
    keep = relevance_predicate(config)
    assert keep("Engineer", "ACME", "untagged", None, {_query_id(config)}) is True


def test_an_empty_keeplist_keeps_every_workplace_type():
    config = _config(title_include=[])  # the default query keeps all types
    keep = relevance_predicate(config)
    assert keep("Engineer", "ACME", "on_site", None, {_query_id(config)}) is True


def test_a_job_no_current_query_surfaced_is_not_rejected_on_workplace():
    keep = relevance_predicate(_wants("remote"))
    assert keep("Engineer", "ACME", "on_site", None, set()) is True


def test_a_language_keeplist_drops_the_codes_it_does_not_want():
    keep = relevance_predicate(_config(title_include=[], description_lang_include=["en", "it"]))
    assert keep("Engineer", "ACME", "remote", "en", set()) is True
    assert keep("Engineer", "ACME", "remote", "it", set()) is True
    assert keep("Engineer", "ACME", "remote", "de", set()) is False


def test_a_language_no_description_named_is_not_judged_against_the_keeplist():
    """NULL passes, so the first fetch still happens and the next refresh decides on the text it got."""
    keep = relevance_predicate(_config(title_include=[], description_lang_include=["en"]))
    assert keep("Engineer", "ACME", "remote", None, set()) is True


def test_an_empty_language_keeplist_keeps_every_language():
    keep = relevance_predicate(_config(title_include=[]))
    assert keep("Engineer", "ACME", "remote", "de", set()) is True
