import pytest

from linkedin_job_scraper.constants import NO_DESCRIPTION
from linkedin_job_scraper.workplace import infer_workplace_type


@pytest.mark.parametrize(
    "description",
    [
        "This is a fully remote position.",
        "We are 100% remote.",
        "A remote-first company.",
        "You may work from home every day.",
        "Full remote, no office.",
        "Si tratta di un ruolo completamente da remoto.",
        "Die Stelle ist komplett remote.",
    ],
)
def test_an_ad_that_says_it_is_remote_is_read_as_remote(description):
    assert infer_workplace_type("Backend Engineer", description) == "remote"


def test_a_title_saying_remote_settles_it_without_the_description():
    assert infer_workplace_type("Backend Engineer (Remote)", "We build things.") == "remote"


@pytest.mark.parametrize(
    "description",
    ["This is a fully on-site role.", "On-site only, no exceptions.", "No remote work is offered.", "Solo in sede."],
)
def test_an_ad_that_rules_remote_out_is_read_as_on_site(description):
    assert infer_workplace_type("Backend Engineer", description) == "on_site"


@pytest.mark.parametrize(
    "description",
    [
        "We build things. Apply now.",
        "Hybrid working, three days per week in the office.",  # too often written in remote ads to read
        "You will visit the Munich office now and then.",
        "",
        None,
        NO_DESCRIPTION,
    ],
)
def test_an_ad_that_settles_nothing_stays_untagged(description):
    assert infer_workplace_type("Backend Engineer", description) == "untagged"


def test_remote_wins_over_the_office_the_same_ad_mentions():
    """An ad may offer remote work and still name an office; the offer is what decides."""
    ad = "Fully remote, though on-site only applies to our support team."
    assert infer_workplace_type("Engineer", ad) == "remote"
