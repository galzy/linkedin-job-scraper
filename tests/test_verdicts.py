import pytest

from linkedin_job_scraper.verdicts import PASS, is_firm, is_wellformed


@pytest.mark.parametrize("verdict", [None, "", "   "])
def test_nothing_written_is_not_a_rejection(verdict):
    assert is_firm(verdict) is False


def test_a_clean_verdict_is_not_a_rejection():
    assert is_firm(PASS) is False


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


@pytest.mark.parametrize(
    "verdict", [PASS, "a: below the floor", "b?: german-language ad; g: java backend", "c?: UK-anchored, GBP band"]
)
def test_a_verdict_the_grammar_allows_is_wellformed(verdict):
    assert is_wellformed(verdict)


@pytest.mark.parametrize(
    "verdict", ["z: unknown code", "b", "b:", "b: x;", "", "  ", "looks interesting, ask about salary"]
)
def test_text_outside_the_grammar_is_not_wellformed(verdict):
    """Free text is fine from a person's hand but not from the judge, whose output this gates.

    Empty text among them: a judge that loses track returns it for every ad, so it must not pass.
    """
    assert not is_wellformed(verdict)
