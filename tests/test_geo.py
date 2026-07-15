import pytest

from linkedin_scraper.geo import country_of, searched_countries

# Metro labels resolve within these countries, as main scopes to the config's search locations.
SCOPE = searched_countries(["Italy", "Switzerland", "Ireland", "France"])


@pytest.mark.parametrize(
    "location, country",
    [
        ("Bologna, Emilia-Romagna, Italy", "Italy"),
        ("Dublin, County Dublin, Ireland", "Ireland"),
        ("London, England, United Kingdom", "United Kingdom"),
        ("Ireland", "Ireland"),
        ("San Francisco, CA", "United States"),  # a US state code, not Canada's alpha-2
        ("Milano, Lombardia, Italia", None),  # Italian labels don't resolve — we request English
        ("Moscow, Russia", "Russian Federation"),  # aliases cover names off the ISO spelling
        ("Istanbul, Turkey", "Türkiye"),
        ("London, UK", "United Kingdom"),
        ("Edinburgh, Scotland", "United Kingdom"),  # UK constituent, standalone segment
        ("Wales", "United Kingdom"),
        ("Amsterdam, Holland", "Netherlands"),
        ("Pristina, Kosovo", "Kosovo"),  # absent from ISO 3166 entirely
        ("Tuscany, Italy", "Italy"),  # the rightmost segment that resolves wins, whatever precedes it
        ("Remote", None),
        ("Bologna", None),  # a bare city names no country
        ("", None),
    ],
)
def test_country_of_reads_the_rightmost_country_segment(location, country):
    assert country_of(location) == country


@pytest.mark.parametrize(
    "location, country",
    [
        ("Greater Bologna Metropolitan Area", "Italy"),  # the city sheds its affixes
        ("Zürich Metropolitan Area", "Switzerland"),
        ("Greater Paris Metropolitan Region", "France"),
        ("Geneva Metropolitan Area", "Switzerland"),  # not Geneva, Illinois: scope excludes namesakes
        ("Waterford Metropolitan Area", "Ireland"),  # the more populous Waterford, CT is out of scope
        ("Greater Munich Metropolitan Area", None),  # a city outside the scoped countries stays NULL
        ("San Francisco Bay Area", None),  # "San Francisco Bay" is no city GeoNames knows
        ("Bologna", None),  # a bare city is no metro label, even in scope
    ],
)
def test_country_of_resolves_a_metro_label_within_scope(location, country):
    assert country_of(location, SCOPE) == country


def test_country_of_leaves_a_metro_label_unresolved_without_a_scope():
    assert country_of("Greater Bologna Metropolitan Area") is None


def test_searched_countries_reads_only_the_locations_that_name_a_country():
    """City and region locations name no country, so only country locations widen the scope."""
    assert searched_countries(["Bologna", "Emilia-Romagna", "Germany", "Ireland"]) == frozenset({"Germany", "Ireland"})
