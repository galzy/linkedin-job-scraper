"""Parse LinkedIn guest job pages into Jobs. Networking lives in ``http.py``."""

from bs4 import BeautifulSoup
from loguru import logger

from linkedin_scraper.constants import JOB_VIEW_URL, NO_DESCRIPTION
from linkedin_scraper.job import Job


def _card_text(tag) -> str:
    """A card field's stripped text, or "" when the card left the field out."""
    return tag.get_text(strip=True) if tag is not None else ""


def has_job_cards(soup: BeautifulSoup) -> bool:
    """Whether this results page carries any job cards at all.

    Past the last page LinkedIn serves an empty 200, so this — and not a parse that happens
    to yield zero jobs — is what tells the pager to stop.
    """
    return bool(soup.find("div", class_="base-search-card__info"))


def count_cards(soup: BeautifulSoup) -> int:
    """How many card slots a results page serves: the guest endpoint wraps each in one <li>."""
    return len(soup.find_all("li"))


def parse_page_jobs(soup: BeautifulSoup) -> list[Job]:
    """Pull one Job out of every well-formed card on a search results page.

    The caller checks :func:`has_job_cards` first, so a failed fetch or a page past the
    end of the results never reaches here. A page of only malformed cards yields nothing.
    """
    joblist: list[Job] = []
    for item in soup.find_all("div", class_="base-search-card__info"):
        title_tag = item.find("h3")
        if title_tag is None:
            logger.warning(f"Skipping card with no title: {item.parent.get('data-entity-urn', '') or 'no urn'}")
            continue
        title = title_tag.get_text(strip=True)

        entity_urn = item.parent.get("data-entity-urn", "")
        job_posting_id = entity_urn.split(":")[-1] if entity_urn else ""
        if not job_posting_id:
            # The posting id is the job's identity and the only way to reach its description.
            # No card is known to arrive without one; hear about it if that ever changes.
            logger.warning(f"Skipping card with no posting id: {title}")
            continue
        job_url = f"{JOB_VIEW_URL}/{job_posting_id}/"

        company = _card_text(item.find("a", class_="hidden-nested-link")).replace("\n", " ")
        location = _card_text(item.find("span", class_="job-search-card__location"))

        date_tag = item.find("time", class_="job-search-card__listdate") or item.find(
            "time", class_="job-search-card__listdate--new"
        )
        date = date_tag["datetime"] if date_tag and date_tag.has_attr("datetime") else ""

        joblist.append(Job(title=title, company=company, location=location, date=date, job_url=job_url))
    return joblist


# Applied in order to the flattened text: drop blank pairs, turn the browser's bullet marker into a
# dash, pull that dash onto its item's line, and strip the expander button labels.
_DESCRIPTION_CLEANUPS = (("\n\n", ""), ("::marker", "-"), ("-\n", "- "), ("Show less", ""), ("Show more", ""))


def parse_job_description(soup: BeautifulSoup) -> str:
    """Extract the description text, or NO_DESCRIPTION if the page carries none.

    Never called with a failed fetch: the caller keeps those None, so "we never got an answer"
    and "the page carries none" stay distinguishable.
    """
    body = soup.find("div", class_="description__text description__text--rich")
    if body is None:
        return NO_DESCRIPTION

    for chrome in body.find_all(["a", "span"]):  # inline links and styling, not content
        chrome.decompose()
    for group in body.find_all("ul"):
        for bullet in group.find_all("li"):
            bullet.insert(0, "-")

    text = body.get_text(separator="\n").strip()
    for old, new in _DESCRIPTION_CLEANUPS:
        text = text.replace(old, new)
    return text.strip()  # the button-label removals can leave trailing whitespace
