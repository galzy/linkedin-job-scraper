"""Read the country a job's free-text location names."""

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


def searched_countries(locations: Iterable[str]) -> frozenset[str]:
    """The countries the ``locations`` name — the scope a metro label may resolve within.

    Fed a run's search locations, it bounds metro resolution so a namesake abroad (Geneva, Illinois)
    can't outweigh the city the label means. Locations naming no country ("Bologna") contribute none.
    """
    return frozenset(c for loc in locations if (c := country_of(loc)))
