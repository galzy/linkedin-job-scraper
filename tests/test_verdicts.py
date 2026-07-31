import pytest

from linkedin_job_scraper.verdicts import is_firm


@pytest.mark.parametrize("verdict", [None, "", "   "])
def test_nothing_written_is_not_a_rejection(verdict):
    assert is_firm(verdict) is False


def test_a_code_stated_outright_is_firm():
    assert is_firm("d: AI training-data authoring, not product engineering")


def test_a_suspected_code_alone_is_not_firm():
    """A "?" means revisit, so a row carrying only those is still an open question."""
    assert not is_firm("c?: UK listing, GBP band; d?: recruiter intermediary")


def test_one_stated_code_among_suspicions_is_firm():
    assert is_firm("a?: salary withheld pending a call; g: Node.js/TypeScript backend")


def test_a_reason_naming_a_letter_and_colon_is_not_read_as_a_code():
    """Only what heads an entry is a code; prose inside a reason must not turn a "?" into a rejection."""
    assert not is_firm("c?: remote within Germany, note: e: not stated")


def test_free_text_carrying_no_code_is_not_a_rejection():
    assert not is_firm("looks interesting, ask about the salary")
