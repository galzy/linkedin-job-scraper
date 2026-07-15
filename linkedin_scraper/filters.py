"""Job transforms: derive workplace types and the config's relevance predicate."""

from collections import Counter, defaultdict
from collections.abc import Callable

from loguru import logger

from linkedin_scraper.config import Config, WorkplaceType

_TAGGED = {WorkplaceType.ON_SITE.value, WorkplaceType.REMOTE.value, WorkplaceType.HYBRID.value}


def derive_workplace_types(attribution: dict[str, Counter[str]], query_types: dict[str, str]) -> dict[str, str]:
    """Each job's workplace type: the tagged search that surfaced it most, by sighting count.

    ``query_types`` maps a query id to the ``harvest_type`` its search targeted. Every scraped query
    is tagged, so a non-tagged one is an anomaly — warned about and skipped, leaving its jobs for the
    caller to default to untagged.
    """
    tagged: dict[str, Counter[str]] = defaultdict(Counter)
    for query_id, counter in attribution.items():
        workplace = query_types.get(query_id)
        if workplace not in _TAGGED:
            logger.warning(f"Attribution query {query_id} is {workplace!r}, not a tagged workplace search; skipping")
            continue
        for job_url, count in counter.items():
            tagged[job_url][workplace] += count
    return {job_url: max(hits, key=hits.get) for job_url, hits in tagged.items()}


def relevance_predicate(config: Config) -> Callable[[str, str, str, set[str]], bool]:
    """Whether a job survives the config's filters, given the queries that surfaced it.

    Title and company are global; workplace type is per query — a job is kept if any query that
    found it wants that type. The one place the filter is written down: ``refresh_relevance``
    applies it and caches the verdict in the ``is_relevant`` column.
    """
    title_include = [phrase.lower() for phrase in config.title_include]
    title_exclude = [phrase.lower() for phrase in config.title_exclude]
    company_exclude = {name.lower() for name in config.company_exclude}  # whole-name match, not substring
    keep_lists = {variant.query_id: variant.workplace_type for variant in config.scrape_queries}

    def keep(title: str, company: str, workplace_type: str, query_ids: set[str]) -> bool:
        title, company = " ".join(title.lower().split()), company.lower()
        # An empty include list means "no filter", not "match nothing". The exclude checks need
        # no such guard: neither an empty list nor an empty set excludes anything.
        if title_include and not any(phrase in title for phrase in title_include):
            return False
        if any(phrase in title for phrase in title_exclude):
            return False
        if company in company_exclude:
            return False
        # An empty keep-list keeps every type; a job no current query surfaced isn't judged on type.
        surfaced = [keep_lists[q] for q in query_ids if q in keep_lists]
        return any(not wanted or workplace_type in wanted for wanted in surfaced) if surfaced else True

    return keep
