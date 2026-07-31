from collections import Counter, defaultdict
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from pydantic import ValidationError

from linkedin_job_scraper.config import Config, SearchQuery
from linkedin_job_scraper.constants import MAX_PAGES, NO_DESCRIPTION, PAGE_SIZE
from linkedin_job_scraper.job import Job
from linkedin_job_scraper.net.http import Fetch
from linkedin_job_scraper.scrape.parsing import parse_page_jobs
from linkedin_job_scraper.scrape.scraping import (
    BlockedError,
    QueryOutcome,
    acquire_filtering_session,
    fetch_posting,
    scrape_jobs,
    scrape_query,
)

# One real results page, recorded from the live endpoint.
RESULTS_PAGE = Path(__file__).parent / "fixtures" / "results_page.html"

CARD_PAGE = """
<li>
<div data-entity-urn="urn:li:jobPosting:4242">
  <div class="base-search-card__info">
    <h3>Python Engineer</h3>
    <a class="hidden-nested-link">ACME</a>
  </div>
</div>
</li>
"""

# What LinkedIn serves once a query has no more results.
EMPTY_PAGE = "<html><head></head><body></body></html>"

# A real card container holding nothing parseable: results exist, this page just yields no jobs.
MALFORMED_PAGE = '<li><div><div class="base-search-card__info"><span>nothing useful</span></div></div></li>'


GONE = object()  # a scripted page entry standing for a removed posting (404/410)


class StubClient:
    """Serves a scripted page per request. `None` stands for a failed fetch, `GONE` for a removed one."""

    def __init__(self, pages: list):
        self._pages = pages
        self.urls: list[str] = []

    def fetch(self, url: str) -> Fetch:
        page = self._pages[len(self.urls)]
        self.urls.append(url)
        if page is GONE:
            return Fetch(None, gone=True)
        return Fetch(None if page is None else BeautifulSoup(page, "html.parser"))


def query() -> SearchQuery:
    return SearchQuery(keywords="k", location="l")


def offsets(client: StubClient) -> list[int]:
    return [int(url.split("start=")[1]) for url in client.urls]


def a_job(**overrides):
    return Job(title="Engineer", company="ACME", date="2024-01-01", job_url="https://x/1/", **overrides)


def test_paging_stops_once_two_refetches_confirm_the_empty_page():
    client = StubClient([CARD_PAGE, CARD_PAGE, EMPTY_PAGE, EMPTY_PAGE, EMPTY_PAGE, CARD_PAGE])
    result = scrape_query(query(), client, "q1")

    assert len(result.jobs) == 2
    assert result.outcome is QueryOutcome.EXHAUSTED
    # The empty page is refetched twice at the same offset; the page after is never requested.
    assert offsets(client) == [0, PAGE_SIZE, 2 * PAGE_SIZE, 2 * PAGE_SIZE, 2 * PAGE_SIZE]


def test_a_flaky_empty_page_does_not_end_the_query():
    """The endpoint serves the same URL empty one minute and full the next; one empty proves nothing."""
    client = StubClient([CARD_PAGE, EMPTY_PAGE, CARD_PAGE, EMPTY_PAGE, EMPTY_PAGE, EMPTY_PAGE])
    result = scrape_query(query(), client, "q1")

    assert len(result.jobs) == 2
    assert result.outcome is QueryOutcome.EXHAUSTED
    assert offsets(client) == [0, PAGE_SIZE, PAGE_SIZE, 2 * PAGE_SIZE, 2 * PAGE_SIZE, 2 * PAGE_SIZE]


def test_cards_on_the_second_refetch_keep_the_query_alive():
    """Two consecutive empties no longer end a query: a third try can still bring the page back."""
    client = StubClient([CARD_PAGE, EMPTY_PAGE, EMPTY_PAGE, CARD_PAGE, EMPTY_PAGE, EMPTY_PAGE, EMPTY_PAGE])
    result = scrape_query(query(), client, "q1")

    assert len(result.jobs) == 2
    assert result.outcome is QueryOutcome.EXHAUSTED
    assert offsets(client) == [0, PAGE_SIZE, PAGE_SIZE, PAGE_SIZE, 2 * PAGE_SIZE, 2 * PAGE_SIZE, 2 * PAGE_SIZE]


def test_a_failed_fetch_stops_the_query_and_is_not_read_as_exhaustion():
    """A None soup means the fetch failed, not that results ran out. We keep the jobs we
    have and stop, rather than paging on into whatever is rejecting us. The outcome says
    which of the two happened, so the run can end on a block instead of reporting success."""
    client = StubClient([CARD_PAGE, None, CARD_PAGE])
    result = scrape_query(query(), client, "q1")

    assert len(result.jobs) == 1
    assert result.outcome is QueryOutcome.FAILED
    assert offsets(client) == [0, PAGE_SIZE]


def test_a_page_of_only_malformed_cards_does_not_stop_paging():
    """Zero parsed jobs is not zero results — only an absent card container ends the query."""
    client = StubClient([MALFORMED_PAGE, CARD_PAGE, EMPTY_PAGE, EMPTY_PAGE, EMPTY_PAGE])
    result = scrape_query(query(), client, "q1")

    assert len(result.jobs) == 1
    assert offsets(client) == [0, PAGE_SIZE, 2 * PAGE_SIZE, 2 * PAGE_SIZE, 2 * PAGE_SIZE]


def test_paging_never_runs_past_the_page_ceiling():
    """LinkedIn 400s on start >= 1000, so an endless run of full pages must still stop at 990."""
    client = StubClient([CARD_PAGE] * (MAX_PAGES + 5))
    result = scrape_query(query(), client, "q1")

    assert len(result.jobs) == MAX_PAGES
    assert result.outcome is QueryOutcome.CEILING
    assert max(offsets(client)) == 990


def test_max_pages_stops_the_query_before_it_runs_dry():
    """The --max-pages flag caps a query that still has results to give."""
    client = StubClient([CARD_PAGE] * 5)
    result = scrape_query(query(), client, "q1", max_pages=2)

    assert len(result.jobs) == 2
    assert result.outcome is QueryOutcome.CEILING
    assert offsets(client) == [0, PAGE_SIZE]


def test_page_size_matches_what_linkedin_actually_serves():
    """A recorded page pins PAGE_SIZE: guess too high and paging skips the jobs in between."""
    soup = BeautifulSoup(RESULTS_PAGE.read_text(encoding="utf-8"), "html.parser")

    assert len(parse_page_jobs(soup)) == PAGE_SIZE


# --- scrape_jobs: telling a block from an exhausted query ---------------------


class WallClient:
    """Serves ``good`` pages of cards, then empty pages forever: LinkedIn's soft block."""

    def __init__(self, good: int):
        self._good = good
        self.calls = 0

    def fetch(self, url: str) -> Fetch:
        self.calls += 1
        return Fetch(BeautifulSoup(CARD_PAGE if self.calls <= self._good else EMPTY_PAGE, "html.parser"))


class ByKeywordClient:
    """Serves one page of cards to queries whose keywords say so, empty pages to the rest.

    A ``walled`` keyword gets a failed fetch instead, standing for a URL that exhausted its retries.
    """

    def __init__(self, walled: str = ""):
        self._walled = walled

    def fetch(self, url: str) -> Fetch:
        if self._walled and f"keywords={self._walled}&" in url:
            return Fetch(None)
        has_cards = "keywords=yes&" in url and url.endswith("start=0")
        return Fetch(BeautifulSoup(CARD_PAGE if has_cards else EMPTY_PAGE, "html.parser"))


def config(queries: list[SearchQuery], search_workers: int = 3) -> Config:
    return Config(search_queries=queries, http={"search_workers": search_workers})


# Each user query fans out into one variant per keep-list type — all three when unset — and a "yes"
# keyword surfaces the one job under every variant, so a single "yes" query yields VARIANTS jobs.
VARIANTS = len(query().harvest_variants())


class StagingSink:
    """Collects what scrape_jobs stages per query, standing in for db.stage_jobs."""

    def __init__(self):
        self.jobs: list[Job] = []
        self.attribution: dict[str, Counter[str]] = defaultdict(Counter)
        self.query_types: dict[str, str] = {}

    def __call__(self, jobs: list[Job], query_id: str, harvest_type: str) -> None:
        self.jobs.extend(jobs)
        self.attribution[query_id].update(job.job_url for job in jobs)
        self.query_types[query_id] = harvest_type


def test_an_empty_run_is_a_block_not_a_finish():
    """Blocked, LinkedIn serves empty 200s that parse exactly like an exhausted query. A run
    of nothing but them is the tell — without it the run stores nothing and reports success."""
    client = WallClient(good=0)

    with pytest.raises(BlockedError):
        scrape_jobs(config([query()]), client, StagingSink())


def test_a_block_mid_run_keeps_the_jobs_staged_before_it():
    client = WallClient(good=1)  # one page of cards, then the wall goes up
    sink = StagingSink()

    with pytest.raises(BlockedError):
        scrape_jobs(config([query()]), client, sink)

    assert len(sink.jobs) == 1


def test_one_barren_query_does_not_read_as_a_block():
    """A location may genuinely have no matches; only a run with nothing at all is a block."""
    queries = [SearchQuery(keywords="yes", location="l"), SearchQuery(keywords="no", location="l")]
    sink = StagingSink()

    scrape_jobs(config(queries), ByKeywordClient(), sink)

    assert len(sink.jobs) == VARIANTS  # "yes" surfaces its job under each of its variants


def test_one_query_giving_up_ends_the_run_even_though_the_run_has_jobs():
    """The run is not empty, so only the query's outcome says we were walled. Reading it as a
    finished query would exit 0 on a run that collected a fraction of its jobs."""
    queries = [SearchQuery(keywords="yes", location="l"), SearchQuery(keywords="walled", location="l")]
    sink = StagingSink()

    with pytest.raises(BlockedError, match="gave up mid-query"):
        scrape_jobs(config(queries), ByKeywordClient(walled="walled"), sink)

    assert len(sink.jobs) == VARIANTS  # the run still staged what "yes" found before it ended


def test_a_mid_run_block_is_caught_even_though_the_run_has_jobs():
    """The gap the canary closes: q1 gets a job, then the wall goes up and q2 comes back empty.
    The run is not empty and nothing failed outright, so only the canary — itself now walled —
    reveals that q2's empty page was a block, not a dry query."""
    queries = [SearchQuery(keywords="a", location="l"), SearchQuery(keywords="b", location="l")]
    client = WallClient(good=1)  # q1 page 0 only; everything after, q2 and the canary, is walled

    with pytest.raises(BlockedError, match="canary is blocked"):
        scrape_jobs(config(queries, search_workers=1), client, StagingSink())  # 1 worker so the calls stay ordered


def test_scrape_jobs_attributes_each_job_to_the_query_that_found_it():
    q = SearchQuery(keywords="yes", location="l")
    sink = StagingSink()

    scrape_jobs(config([q]), ByKeywordClient(), sink)

    # The search fans out, so the job is attributed to each variant that surfaced it, once apiece.
    assert set(sink.attribution) == {v.query_id for v in q.harvest_variants()}
    assert all(sum(counter.values()) == 1 for counter in sink.attribution.values())
    # Each variant stages the workplace type it searched, so recovery can label its jobs.
    assert sink.query_types == {v.query_id: v.harvest_type for v in q.harvest_variants()}


def test_a_block_stages_the_attribution_gathered_before_it():
    queries = [SearchQuery(keywords="yes", location="l"), SearchQuery(keywords="walled", location="l")]
    sink = StagingSink()

    with pytest.raises(BlockedError):
        scrape_jobs(config(queries), ByKeywordClient(walled="walled"), sink)

    variant_ids = {v.query_id for v in queries[0].harvest_variants()}
    assert all(sum(sink.attribution[qid].values()) == 1 for qid in variant_ids)


# --- acquire_filtering_session ------------------------------------------------

# A second, distinct card, so a filtering session's remote and catch-all pages differ.
OTHER_CARD_PAGE = """
<li>
<div data-entity-urn="urn:li:jobPosting:9999">
  <div class="base-search-card__info">
    <h3>Data Engineer</h3>
    <a class="hidden-nested-link">Initech</a>
  </div>
</div>
</li>
"""


class SessionClient:
    """One scripted pipeline state per session; renew_session moves to the next.

    "A" serves every variant the identical page, "B" gives remote its own, "empty" and
    "fail" stand for a probe page without cards and a fetch that gave up, "dry-remote"
    for an empty remote page alongside a populated unfiltered one.
    """

    def __init__(self, states: list[str]):
        self._states = states
        self._current = 0
        self.renewals = 0

    def renew_session(self) -> None:
        self.renewals += 1
        self._current = min(self._current + 1, len(self._states) - 1)

    def fetch(self, url: str) -> Fetch:
        state = self._states[self._current]
        if state == "fail":
            return Fetch(None)
        if state == "empty":
            return Fetch(BeautifulSoup(EMPTY_PAGE, "html.parser"))
        if state == "dry-remote" and "f_WT=2" in url:
            return Fetch(BeautifulSoup(EMPTY_PAGE, "html.parser"))
        page = OTHER_CARD_PAGE if state == "B" and "f_WT=2" in url else CARD_PAGE
        return Fetch(BeautifulSoup(page, "html.parser"))


class FlakyRemoteClient:
    """A filtering session whose remote page comes back empty once, then serves its cards."""

    def __init__(self):
        self.remote_fetches = 0
        self.renewals = 0

    def renew_session(self) -> None:
        self.renewals += 1

    def fetch(self, url: str) -> Fetch:
        if "f_WT=2" not in url:
            return Fetch(BeautifulSoup(CARD_PAGE, "html.parser"))
        self.remote_fetches += 1
        page = EMPTY_PAGE if self.remote_fetches == 1 else OTHER_CARD_PAGE
        return Fetch(BeautifulSoup(page, "html.parser"))


def test_a_filtering_session_is_kept_without_a_renewal():
    client = SessionClient(["B"])

    assert acquire_filtering_session(config([query()]), client)
    assert client.renewals == 0


def test_non_filtering_sessions_are_renewed_until_one_filters():
    """Identical remote and catch-all pages mean the session ignores f_WT; redraw."""
    client = SessionClient(["A", "A", "B"])

    assert acquire_filtering_session(config([query()]), client)
    assert client.renewals == 2


def test_the_draws_run_out_when_every_session_ignores_the_filter():
    client = SessionClient(["A"])

    assert not acquire_filtering_session(config([query()]), client, max_draws=3)
    assert client.renewals == 2  # renewed between draws, not after the last


def test_an_inconclusive_probe_costs_a_draw_and_moves_on():
    """A failed or cardless probe page proves nothing about the pipeline; redraw."""
    for state in ("empty", "fail"):
        client = SessionClient([state, "B"])

        assert acquire_filtering_session(config([query()]), client)
        assert client.renewals == 1


def test_an_empty_remote_page_under_a_populated_one_is_a_filtering_session():
    """Only a session applying f_WT can serve nothing remote while the unfiltered page has cards."""
    client = SessionClient(["dry-remote"])

    assert acquire_filtering_session(config([query()]), client)
    assert client.renewals == 0


def test_a_flaky_empty_remote_page_is_refetched_before_it_counts():
    """One empty remote page settles nothing; the refetch finds the cards and the comparison runs."""
    client = FlakyRemoteClient()

    assert acquire_filtering_session(config([query()]), client)
    assert client.remote_fetches == 2


# --- fetch_posting -----------------------------------------------------------

DESCRIPTION_PAGE = '<div class="description__text description__text--rich"><p>Build things.</p></div>'
CLOSED_PAGE = (
    '<figure class="closed-job closed-job__flavor topcard__flavor-row">'
    '<figcaption class="closed-job__flavor--closed">No longer accepting applications</figcaption></figure>'
)


def test_fetch_posting_attaches_the_text_and_marks_it_open():
    client = StubClient([DESCRIPTION_PAGE])
    job = fetch_posting(a_job(), client)
    assert job.job_description == "Build things."
    assert job.is_open is True


def test_fetch_posting_marks_a_closed_posting_not_open():
    client = StubClient([CLOSED_PAGE])
    job = fetch_posting(a_job(), client)
    assert job.is_open is False


def test_fetch_posting_marks_a_gone_posting_closed():
    """A removed posting (404/410) is gone for good, so record it closed rather than leave it unchecked."""
    client = StubClient([GONE])
    job = fetch_posting(a_job(), client)
    assert job.is_open is False
    assert job.job_description is None  # nothing to store, so an existing description survives


def test_a_failed_fetch_leaves_description_and_open_none_rather_than_a_placeholder():
    """None is what marks the row for a retry next run. Writing the not-found placeholder here
    would look like a real answer and the description would never be fetched again."""
    client = StubClient([None])
    job = fetch_posting(a_job(), client)
    assert job.job_description is None
    assert job.is_open is None


def test_fetch_posting_does_not_mutate_the_job_it_was_given():
    """main() has already stored these jobs by the time it calls this. Job is frozen, so the
    old mutation would now raise rather than silently write through."""
    client = StubClient([DESCRIPTION_PAGE])
    original = a_job()

    described = fetch_posting(original, client)

    assert original.job_description is None
    assert described.job_description == "Build things."

    with pytest.raises(ValidationError):
        original.job_description = "written through"


def test_a_page_without_a_description_stores_the_placeholder_not_none():
    """The page loaded and genuinely has no description — a real answer, so don't retry it."""
    client = StubClient(["<html><body>no description here</body></html>"])
    job = fetch_posting(a_job(), client)
    assert job.job_description == NO_DESCRIPTION
