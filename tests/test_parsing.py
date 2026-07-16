from pathlib import Path

from bs4 import BeautifulSoup
from loguru import logger

from linkedin_job_scraper.constants import NO_DESCRIPTION, PAGE_SIZE
from linkedin_job_scraper.scrape.parsing import count_cards, has_job_cards, parse_job_description, parse_page_jobs

RESULTS_PAGE = Path(__file__).parent / "fixtures" / "results_page.html"

CARD = """
<div data-entity-urn="urn:li:jobPosting:4242">
  <div class="base-search-card__info">
    <h3>  Python Engineer </h3>
    <a class="hidden-nested-link">ACME\nCorp</a>
    <span class="job-search-card__location">Bologna</span>
    <time class="job-search-card__listdate" datetime="2024-05-01"></time>
  </div>
</div>
"""

# no <h3>, no data-entity-urn on the parent, no <time>
MALFORMED_CARD = '<div><div class="base-search-card__info"><span>nothing useful</span></div></div>'

# a title, but no posting id: nothing to identify it by and no page to fetch
CARD_WITHOUT_POSTING_ID = '<div><div class="base-search-card__info"><h3>Python Engineer</h3></div></div>'


def soup(html):
    return BeautifulSoup(html, "html.parser")


def test_parses_a_well_formed_card():
    (job,) = parse_page_jobs(soup(CARD))
    assert job.title == "Python Engineer"
    assert job.company == "ACME Corp"
    assert job.location == "Bologna"
    assert job.date == "2024-05-01"
    assert job.job_url == "https://www.linkedin.com/jobs/view/4242/"
    assert job.job_description is None  # NULL until fetched


def test_malformed_card_is_skipped_not_fatal():
    assert parse_page_jobs(soup(MALFORMED_CARD + CARD)) != []
    assert parse_page_jobs(soup(MALFORMED_CARD)) == []


def test_a_card_without_a_title_is_skipped_with_a_warning():
    """A titleless card is dropped, but noisily, so a silent parser skip can't hide behind a short page."""
    messages: list[str] = []
    handler = logger.add(messages.append, level="WARNING")
    try:
        assert parse_page_jobs(soup(MALFORMED_CARD)) == []
    finally:
        logger.remove(handler)
    assert any("no title" in m for m in messages)


def test_count_cards_counts_the_li_slots_of_a_real_page():
    """Each served card is one <li>; a real page has PAGE_SIZE of them."""
    assert count_cards(soup(RESULTS_PAGE.read_text(encoding="utf-8"))) == PAGE_SIZE


def test_a_card_without_a_posting_id_is_skipped_not_fatal():
    """job_url is the job's identity and the only way to reach its description, so a card
    lacking one is dropped rather than stored as a row nothing can ever fill in."""
    assert parse_page_jobs(soup(CARD_WITHOUT_POSTING_ID)) == []
    assert parse_page_jobs(soup(CARD_WITHOUT_POSTING_ID + CARD)) != []


def test_parse_job_description_returns_the_placeholder_when_the_page_carries_none():
    """A page that loaded with no description is an answer, so it is stored and not retried."""
    assert parse_job_description(soup("<html><body>nothing here</body></html>")) == NO_DESCRIPTION


def test_has_job_cards_separates_an_exhausted_query_from_an_unparseable_page():
    """The end of the results is an empty body. A page of only malformed cards still has results,
    so it must not read as exhaustion even though it parses to zero jobs."""
    assert has_job_cards(soup(CARD)) is True
    assert has_job_cards(soup(MALFORMED_CARD)) is True
    assert has_job_cards(soup("<html><head></head><body></body></html>")) is False


def test_parse_job_description_extracts_text():
    html = '<div class="description__text description__text--rich"><p>Build things.</p>Show less</div>'
    assert parse_job_description(soup(html)) == "Build things."
