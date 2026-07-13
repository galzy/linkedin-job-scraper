"""Pure job transforms: dedupe, derive workplace types, and the config's relevance predicate."""

from collections import Counter, defaultdict
from collections.abc import Callable

from linkedin_scraper.config import Config, WorkplaceType
from linkedin_scraper.job import Job

_TAGGED = {WorkplaceType.ON_SITE.value, WorkplaceType.REMOTE.value, WorkplaceType.HYBRID.value}
# Tie-break when noise shows a url under two tagged searches at once.
_PRECEDENCE = {WorkplaceType.REMOTE.value: 0, WorkplaceType.HYBRID.value: 1, WorkplaceType.ON_SITE.value: 2}


def remove_duplicates(values: list[Job]) -> list[Job]:
    """Keep the first job seen for each posting URL, preserving order.

    The URL carries LinkedIn's own posting id, which is what tells two openings apart:
    one company can list several distinct jobs under one title.
    """
    seen = set()
    unique = []
    for job in values:
        if job.job_url not in seen:
            seen.add(job.job_url)
            unique.append(job)
    return unique


def derive_workplace_types(attribution: dict[str, Counter[str]], query_types: dict[str, str]) -> dict[str, str]:
    """Each job's workplace type, read from which tagged search surfaced it.

    A job seen only under the catch-all search is untagged; any tagged search is authoritative over it.
    ``query_types`` maps a query id to the ``harvest_type`` token its search targeted.
    """
    tagged: dict[str, Counter[str]] = defaultdict(Counter)
    seen: set[str] = set()
    for query_id, counter in attribution.items():
        workplace = query_types.get(query_id, WorkplaceType.UNTAGGED.value)
        for job_url, count in counter.items():
            seen.add(job_url)
            if workplace in _TAGGED:
                tagged[job_url][workplace] += count

    result: dict[str, str] = {}
    for job_url in seen:
        hits = tagged.get(job_url)
        if hits:
            result[job_url] = max(hits, key=lambda wt: (hits[wt], -_PRECEDENCE[wt]))
        else:
            result[job_url] = WorkplaceType.UNTAGGED.value
    return result


def relevance_predicate(config: Config) -> Callable[[str, str, str, set[str]], bool]:
    """Whether a job survives the config's filters, given the queries that surfaced it.

    Title and company are global; workplace type is per query — a job is kept if any query that
    found it wants that type. The one place the filter is written down: ``refresh_relevance``
    applies it and caches the verdict in the ``is_relevant`` column.
    """
    title_include = [phrase.lower() for phrase in config.title_include]
    title_exclude = [phrase.lower() for phrase in config.title_exclude]
    company_exclude = [phrase.lower() for phrase in config.company_exclude]
    keep_lists = {variant.query_id: variant.workplace_type for variant in config.scrape_queries}

    def keep(title: str, company: str, workplace_type: str, query_ids: set[str]) -> bool:
        title, company = title.lower(), company.lower()
        # An empty include list means "no filter", not "match nothing". The exclude lists need
        # no such guard: any() over an empty list already excludes nothing.
        if title_include and not any(phrase in title for phrase in title_include):
            return False
        if any(phrase in title for phrase in title_exclude):
            return False
        if any(phrase in company for phrase in company_exclude):
            return False
        # An empty keep-list keeps every type; a job no current query surfaced isn't judged on type.
        surfaced = [keep_lists[q] for q in query_ids if q in keep_lists]
        return any(not wanted or workplace_type in wanted for wanted in surfaced) if surfaced else True

    return keep
