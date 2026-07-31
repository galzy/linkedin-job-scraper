"""Read the country a job's free-text location names."""

import gettext
import re
from collections.abc import Iterable
from functools import cache, lru_cache

import geonamescache
import pycountry

# Two-letter US state and territory codes, so "San Francisco, CA" reads as United States rather than
# tripping pycountry's alpha-2 lookup, under which "CA" is Canada.
_US_STATE_CODES = {s.code.removeprefix("US-") for s in pycountry.subdivisions if s.code.startswith("US-")}

# Colloquial names and UK constituents pycountry can't resolve, mapped to ISO names; Kosovo is absent from ISO 3166.
_COUNTRY_ALIASES = {
    "russia": "Russian Federation",
    "turkey": "Türkiye",
    "uk": "United Kingdom",
    "england": "United Kingdom",
    "scotland": "United Kingdom",
    "wales": "United Kingdom",
    "northern ireland": "United Kingdom",
    "kosovo": "Kosovo",
    "palestine": "Palestine, State of",
    "ivory coast": "Côte d'Ivoire",
    "cape verde": "Cabo Verde",
    "holland": "Netherlands",
}

# Affixes of metro-area labels ("Greater Milan Metropolitan Area"), which name a city but no country.
_METRO_AFFIXES = re.compile(r"^Greater\s+|\s+(?:Metropolitan\s+)?(?:Area|Region)$", re.IGNORECASE)


def _country_name(segment: str) -> str | None:
    """The English country ``segment`` names, or None; US state codes and colloquial aliases included."""
    if segment.upper() in _US_STATE_CODES:
        return "United States"
    if alias := _COUNTRY_ALIASES.get(segment.lower()):
        return alias
    try:
        return pycountry.countries.lookup(segment).name
    except LookupError:
        return None


@cache
def _metro_cities(countries: frozenset[str]) -> dict[str, str]:
    """Every city name and alternate of ``countries``, lowercased to its country; most populous wins."""
    iso = {c.alpha_2: c.name for c in pycountry.countries} | {"XK": "Kosovo"}  # ISO 3166 lacks XK
    cities = sorted(geonamescache.GeonamesCache().get_cities().values(), key=lambda c: c["population"])
    return {
        name.lower(): country
        for city in cities
        if (country := iso.get(city["countrycode"])) in countries
        for name in (city["name"], *city["alternatenames"])
    }


def _metro_country(segment: str, countries: frozenset[str]) -> str | None:
    """The country a metro-area label names, when its affixes wrap a city of a scoped country."""
    core = _METRO_AFFIXES.sub("", segment)
    return _metro_cities(countries).get(core.lower()) if core != segment else None


@lru_cache
def country_of(location: str, countries: frozenset[str] = frozenset()) -> str | None:
    """The country a free-text location names, or None.

    The rightmost comma segment that resolves to a country wins; failing that, a lone segment is
    tried as a metro-area label, resolved through the city its affixes wrap within ``countries``.
    """
    segments = [s.strip() for s in location.split(",") if s.strip()]
    country = next((c for s in reversed(segments) if (c := _country_name(s))), None)
    if country is None and len(segments) == 1:
        country = _metro_country(segments[0], countries)
    return country


@cache
def country_vocabulary(language: str | None = None) -> dict[str, str]:
    """Every country name to look for in text, lowercased, mapped to its English name.

    English names and the colloquial aliases always; a description's own language on top, since an
    ad writes "Deutschland" where a card would say "Germany". ISO 3166 ships translated in 163
    languages, so ``language`` only has to name one — anything without a catalog just adds nothing.
    """
    vocabulary = {alias: name for alias, name in _COUNTRY_ALIASES.items()}
    for country in pycountry.countries:
        for attribute in ("name", "common_name", "official_name"):
            if value := getattr(country, attribute, None):
                vocabulary[value.lower()] = country.name
    if language:
        try:
            catalog = gettext.translation("iso3166-1", pycountry.LOCALES_DIR, languages=[language])
        except FileNotFoundError:
            return vocabulary
        for country in pycountry.countries:
            vocabulary[catalog.gettext(country.name).lower()] = country.name
    return vocabulary


@cache
def _major_cities(minimum: int) -> dict[str, str]:
    """Every city of at least ``minimum`` people, lowercased to its country; most populous wins."""
    iso = {c.alpha_2: c.name for c in pycountry.countries} | {"XK": "Kosovo"}
    cities = sorted(geonamescache.GeonamesCache().get_cities().values(), key=lambda c: c["population"])
    return {
        city["name"].lower(): country
        for city in cities
        if city["population"] >= minimum and (country := iso.get(city["countrycode"]))
    }


def city_country(name: str, minimum: int = 100_000) -> str | None:
    """The country a well-known city sits in, or None.

    Only cities of ``minimum`` people, and only their own names rather than the alternates, since an
    ad naming somewhere smaller is likelier a typo or a false friend than a place worth resolving.
    """
    return _major_cities(minimum).get(name.strip().lower())


def searched_countries(locations: Iterable[str]) -> frozenset[str]:
    """The countries the ``locations`` name — the scope a metro label may resolve within.

    Fed a run's search locations, it bounds metro resolution so a namesake abroad (Geneva, Illinois)
    can't outweigh the city the label means. Locations naming no country ("Bologna") contribute none.
    """
    return frozenset(c for loc in locations if (c := country_of(loc)))
