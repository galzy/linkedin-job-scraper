import pytest
import yaml

from linkedin_scraper.config import ConfigurationError, SearchQuery, load_and_validate_config, load_config


def raw(**query_overrides):
    query = {"keywords": "k", "location": "l"} | query_overrides
    return {"search_queries": [query]}


def write_config(tmp_path, text, encoding="utf-8"):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding=encoding)
    return str(path)


def test_enum_names_map_to_linkedin_query_values():
    q = load_and_validate_config(raw(distance="KM_40", timespan="MONTH")).search_queries[0]
    assert (q.distance, q.timespan) == ("25", "r2592000")


def test_omitted_and_empty_filters_become_empty_strings():
    q = load_and_validate_config(raw()).search_queries[0]
    assert (q.distance, q.timespan) == ("", "")


def test_workplace_type_normalizes_case_insensitively_to_tokens():
    q = load_and_validate_config(raw(workplace_type=["REMOTE", "hybrid"])).search_queries[0]
    assert q.workplace_type == ["remote", "hybrid"]


def test_an_invalid_workplace_type_is_rejected():
    with pytest.raises(ConfigurationError, match="workplace_type"):
        load_and_validate_config(raw(workplace_type=["office"]))


def test_a_validated_config_is_not_itself_valid_config_input():
    """The enum mapping is one-way, so anything writing a config back must start from the raw dict.

    ``distance`` holds ``"25"`` once validated, and ``"25"`` is a LinkedIn query value, not the name
    of a DistanceType member. ``title_include`` is materialised from the keywords the same way.
    """
    config = load_and_validate_config(raw(distance="KM_40"))

    with pytest.raises(ConfigurationError, match="distance"):
        load_and_validate_config(config.model_dump())


def test_keywords_and_location_are_held_raw_and_quoted_only_in_the_url():
    q = load_and_validate_config(raw(keywords="a OR b", location="New York")).search_queries[0]

    assert (q.keywords, q.location) == ("a OR b", "New York")
    assert q.label == "a OR b @ New York"
    assert "keywords=a%20OR%20b" in q.page_url(0)
    assert "location=New%20York" in q.page_url(0)


@pytest.mark.parametrize(
    "overrides",
    [
        {"keywords": "etl"},
        {"location": "Ireland"},
        {"distance": "KM_40"},
        {"harvest_type": "remote"},
        {"timespan": "WEEK"},
    ],
)
def test_query_id_changes_when_any_search_field_changes(overrides):
    base_fields = {"keywords": "python", "location": "Bologna"}
    base = SearchQuery(**base_fields)
    assert SearchQuery(**(base_fields | overrides)).query_id != base.query_id


def test_page_url_carries_the_variants_f_wt_code_and_leaves_a_keyword_slash_unescaped():
    variants = load_and_validate_config(raw(keywords="a/b")).search_queries[0].harvest_variants()
    remote = next(v for v in variants if v.harvest_type == "remote")

    url = remote.page_url(0)
    assert "f_WT=2" in url
    assert "keywords=a/b" in url


def test_page_url_omits_distance_when_any_since_an_empty_distance_500s_the_endpoint():
    any_distance = SearchQuery(keywords="python", location="Bologna", distance="ANY")
    assert "distance=" not in any_distance.page_url(0)

    with_distance = SearchQuery(keywords="python", location="Bologna", distance="KM_40")
    assert "distance=25" in with_distance.page_url(0)

    zero = SearchQuery(keywords="python", location="Bologna", distance="KM_0")
    assert "distance=0" in zero.page_url(0)  # a real 0km filter, not the same as ANY


def test_an_omitted_title_include_becomes_the_keyword_terms_of_every_query():
    config = load_and_validate_config(
        {
            "search_queries": [
                {"keywords": "python OR (developer AND cloud)", "location": "l"},
                {"keywords": "etl OR python", "location": "l"},
            ],
        }
    )
    assert config.title_include == ["python", "developer", "cloud", "etl"]


@pytest.mark.parametrize(
    "keywords, expected",
    [
        (
            '"data engineer" OR etl',
            ["data engineer", "etl"],
        ),
        ("python or Etl and PYTHON", ["python", "etl"]),
        ("c++ OR c# OR .net", ["c++", "c#", ".net"]),
        ("python OR (etl", ["python", "etl"]),
    ],
)
def test_deriving_title_include_survives_exotic_keyword_expressions(keywords, expected):
    assert load_and_validate_config(raw(keywords=keywords)).title_include == expected


@pytest.mark.parametrize(
    "keywords, reason",
    [
        ("python NOT junior", "NOT would flatten an exclusion into a way in"),
        ("", "no terms at all"),
        ("()", "no terms once the parens go"),
        ("AND OR", "operators only"),
    ],
)
def test_a_keywords_expression_that_cannot_derive_title_include_is_rejected(keywords, reason):
    with pytest.raises(ConfigurationError):
        load_and_validate_config(raw(keywords=keywords))

    assert load_and_validate_config(raw(keywords=keywords) | {"title_include": []}).title_include == []


@pytest.mark.parametrize(
    "title_include, reason",
    [([], "an explicit empty list disables the filter"), (["etl"], "an explicit list is used verbatim")],
)
def test_a_title_include_in_the_config_is_never_overwritten(title_include, reason):
    config = load_and_validate_config(raw(keywords="python") | {"title_include": title_include})
    assert config.title_include == title_include


@pytest.mark.parametrize(
    "bad, reason",
    [
        ({"search_queries": []}, "empty search_queries"),
        ({}, "missing search_queries"),
        (raw(distance="KM_99"), "unknown enum name"),
        (raw(timespan="FORTNIGHT"), "unknown enum name"),
    ],
)
def test_invalid_config_raises_configuration_error(bad, reason):
    with pytest.raises(ConfigurationError):
        load_and_validate_config(bad)


def test_load_config_reads_non_ascii_as_utf8(tmp_path):
    path = write_config(tmp_path, yaml.safe_dump(raw(location="München"), allow_unicode=True))

    q = load_config(path).search_queries[0]
    assert q.location == "München"


def test_an_alias_expands_to_the_value_its_anchor_holds(tmp_path):
    path = write_config(
        tmp_path,
        "x-keywords: &kw a OR b\nsearch_queries:\n  - {keywords: *kw, location: l}\n",
    )

    assert load_config(path).search_queries[0].keywords == "a OR b"


def test_phrase_lists_flatten_grouped_alias_lists(tmp_path):
    path = write_config(
        tmp_path,
        "x-title-exclude:\n  roles: &roles [a, b]\n"
        "search_queries:\n  - {keywords: k, location: l}\n"
        "title_exclude:\n  - *roles\n  - c\n",
    )
    assert load_config(path).title_exclude == ["a", "b", "c"]


@pytest.mark.parametrize(
    "make_path, reason",
    [
        (lambda tmp_path: str(tmp_path / "absent.yaml"), "unreadable file (OSError)"),
        (lambda tmp_path: write_config(tmp_path, "{not yaml"), "malformed YAML (YAMLError)"),
        (lambda tmp_path: write_config(tmp_path, "location: M\xfcnchen", "cp1252"), "not UTF-8 (ValueError)"),
        (lambda tmp_path: write_config(tmp_path, ""), "empty file (parses to None)"),
    ],
)
def test_load_config_reports_every_failure_as_configuration_error(tmp_path, make_path, reason):
    with pytest.raises(ConfigurationError):
        load_config(make_path(tmp_path))


def test_a_location_list_fans_into_one_query_per_location_in_order():
    config = load_and_validate_config(raw(keywords="python", location=["Milan", "Italy", "Ireland"]))
    assert [q.location for q in config.search_queries] == ["Milan", "Italy", "Ireland"]
    assert all(q.keywords == "python" for q in config.search_queries)
    assert len({q.query_id for q in config.search_queries}) == 3


def test_a_location_list_matches_the_same_queries_written_out_by_hand():
    listed = load_and_validate_config(raw(keywords="python", location=["Milan", "Italy"]))
    unrolled = load_and_validate_config(
        {"search_queries": [{"keywords": "python", "location": "Milan"}, {"keywords": "python", "location": "Italy"}]}
    )
    assert [q.query_id for q in listed.search_queries] == [q.query_id for q in unrolled.search_queries]


def test_a_string_location_still_yields_a_single_query():
    config = load_and_validate_config(raw(location="Bologna"))
    assert [q.location for q in config.search_queries] == ["Bologna"]


def test_a_location_list_fans_out_across_the_workplace_variants():
    config = load_and_validate_config(raw(location=["Milan", "Italy"]))  # 2 locations x 3 default types
    assert len(config.scrape_queries) == 6


def test_only_empty_location_lists_leave_nothing_to_search():
    with pytest.raises(ConfigurationError, match="at least one query"):
        load_and_validate_config(raw(location=[]))


def test_harvest_variants_default_to_the_three_tagged_types_with_distinct_ids():
    q = load_and_validate_config(raw()).search_queries[0]
    variants = q.harvest_variants()
    assert [v.harvest_type for v in variants] == ["on_site", "remote", "hybrid"]
    assert len({v.query_id for v in variants}) == 3


def test_a_keep_list_narrows_the_harvest_variants():
    q = load_and_validate_config(raw(workplace_type=["remote"])).search_queries[0]
    assert [v.harvest_type for v in q.harvest_variants()] == ["remote"]


def test_untagged_is_rejected_as_a_keep_list_value():
    with pytest.raises(ConfigurationError, match="workplace_type"):
        load_and_validate_config(raw(workplace_type=["untagged"]))


def test_label_tags_a_tagged_variant_but_not_the_catch_all():
    def at(ht):
        return SearchQuery(keywords="python", location="Bologna", harvest_type=ht).label

    assert at("remote") == "python @ Bologna [remote]"
    assert at("untagged") == "python @ Bologna"
