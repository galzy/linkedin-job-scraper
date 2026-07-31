import pytest

from linkedin_job_scraper.constants import NO_DESCRIPTION
from linkedin_job_scraper.signals import COVERED_LANGUAGES, stated_locations, work_eligibility

# One real clause per language location_phrases.yaml claims to cover. A language may only be marked
# covered once it appears here: three Italian phrases once read zero of 139 Italian ads unnoticed.
COVERAGE = {
    "en": ("This is a fully remote role within the UK.", "United Kingdom"),
    "de": ("Flexibles Arbeiten im Homeoffice innerhalb Deutschlands.", "Germany"),
    "it": ("Sede di lavoro: Bologna, con due giorni di smart working.", "Italy"),
    "nl": ("Voor deze functie ben je woonachtig in Nederland.", "Netherlands"),
}


def test_every_covered_language_is_asserted_here():
    """The claim the file makes is only worth what this test checks."""
    assert set(COVERAGE) == COVERED_LANGUAGES


@pytest.mark.parametrize(("language", "case"), COVERAGE.items())
def test_a_covered_language_reads_its_own_phrasing(language, case):
    description, expected = case
    assert stated_locations(description, language) == expected


@pytest.mark.parametrize("description", [None, "", NO_DESCRIPTION, "A backend role building APIs."])
def test_an_ad_that_scopes_nowhere_states_no_location(description):
    assert stated_locations(description, "en") is None


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("This is a fully remote role within the UK.", "United Kingdom"),
        ("The role is remote, but candidates must be based in the Netherlands.", "Netherlands"),
        ("Applicants must have the right to work in Ireland.", "Ireland"),
    ],
)
def test_a_single_named_country_is_read_from_the_clause(description, expected):
    assert stated_locations(description, "en") == expected


@pytest.mark.parametrize(
    ("description", "language", "expected"),
    [
        ("Flexibles Arbeiten im Homeoffice innerhalb Deutschlands.", "de", "Germany"),
        ("Der Kandidat muss seinen Wohnsitz in Deutschland haben.", "de", "Germany"),
        ("Il lavoro è da remoto in Italia.", "it", "Italy"),
        ("Cerchiamo un candidato residente in Italia.", "it", "Italy"),
    ],
)
def test_an_ad_names_its_country_in_its_own_language(description, language, expected):
    """ISO 3166 ships translated, so the description's own language is all we need to name."""
    assert stated_locations(description, language) == expected


def test_a_name_only_another_language_uses_is_not_looked_for_without_it():
    """The vocabulary is what the language opens; English alone has never heard of "Deutschlands"."""
    german = "Flexibles Arbeiten im Homeoffice innerhalb Deutschlands."
    assert stated_locations(german, "en") is None
    assert stated_locations(german, "de") == "Germany"


def test_every_country_of_an_alternation_is_kept():
    text = "We welcome applications from candidates based in Germany or Italy."
    assert stated_locations(text) == "Germany, Italy"


def test_a_region_wide_clause_keeps_both_the_country_and_the_region():
    """'UK or Europe' still admits somewhere unnamed, so the wider half has to survive."""
    assert stated_locations("This is a remote role based in UK or Europe.") == "EU, United Kingdom"


def test_a_region_still_counts_through_the_words_leading_up_to_it():
    """ "UK and parts of Europe" is open to Europe, so the role is not the UK's alone."""
    text = "Fully remote role in UK and parts of Europe within UTC -1 to UTC +3."
    assert stated_locations(text, "en") == "EU, United Kingdom"


def test_only_a_region_is_reached_across_filler_words():
    """A country behind filler names no second place to work, so it is left out."""
    text = "Candidates must be based in Germany, and we are also hiring in France."
    assert stated_locations(text, "en") == "Germany"


def test_a_relative_clause_describes_someone_else_not_you():
    """An ad addressing you says "you must be based in", never "who is based in"."""
    text = "The team is led by a dedicated Engineering Manager who is based in Northern Ireland."
    assert stated_locations(text, "en") is None


@pytest.mark.parametrize(
    "description",
    [
        "For roles based in the United States, the typical starting salary range is listed above.",
        "Only 6% of our customers are located in the Netherlands.",
        "You will collaborate with your colleagues based in India.",
        "Greenhouse Software, Inc., a cloud services provider located in the United States.",
    ],
)
def test_a_country_named_about_someone_else_does_not_scope_the_role(description):
    assert stated_locations(description, "en") is None


def test_candidates_heading_a_clause_still_scopes_the_role():
    """ "For candidates" reads a pay band or a real scope; only what follows tells them apart."""
    scope = "This is a permanent opportunity for candidates based in the UK."
    band = "For candidates based in Poland, the salary range is 30,400 PLN gross monthly."
    assert stated_locations(scope, "en") == "United Kingdom"
    assert stated_locations(band, "en") is None


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("You can work from anywhere in the US or Canada.", "Canada, United States"),
        ("Remote-first setup, anywhere in the UK.", "United Kingdom"),
        ("This role will be based in our Dublin, Ireland office.", "Ireland"),
        ("You will be based in Berlin, Germany.", "Germany"),
    ],
)
def test_a_country_is_reached_past_a_city_or_an_open_invitation(description, expected):
    assert stated_locations(description, "en") == expected


def test_a_leading_country_is_not_skipped_for_a_later_one():
    """The city step is reluctant, or "based in Italy, Germany" would read as Germany alone."""
    assert stated_locations("Open to people based in Italy, Germany.", "en") == "Germany, Italy"


@pytest.mark.parametrize(
    ("description", "language", "expected"),
    [
        ("Voor deze functie ben je woonachtig in Nederland.", "nl", "Netherlands"),
        ("Beachte, dass eine Arbeitserlaubnis für Deutschland erforderlich ist.", "de", "Germany"),
    ],
)
def test_a_country_is_read_from_dutch_and_german_phrasing(description, language, expected):
    assert stated_locations(description, language) == expected


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Applicants must hold active UK SC Security Clearance.", "clearance"),
        ("You will need to undergo BPSS vetting before starting.", "clearance"),
        ("We are unable to provide visa sponsorship at this time.", "no sponsorship"),
        ("This opportunity doesn't provide sponsorship.", "no sponsorship"),
        ("Candidates should possess work authorisation that does not require sponsorship.", "no sponsorship"),
        ("You must have the right to work here without requiring visa sponsorship.", "no sponsorship"),
    ],
)
def test_an_eligibility_bar_is_read_from_the_ad(description, expected):
    assert work_eligibility(description) == expected


@pytest.mark.parametrize(
    "description",
    [
        "A backend role building APIs.",
        "We offer visa sponsorship and relocation support.",
        "Visa sponsorship is available for the right candidate.",
        "Security clearance is not required for this position.",
        "You will present progress to the project sponsors each quarter.",
    ],
)
def test_an_ad_that_bars_nobody_reports_no_eligibility(description):
    assert work_eligibility(description) is None


def test_both_bars_are_kept_when_an_ad_sets_each():
    text = "Requires SC security clearance. We cannot offer visa sponsorship."
    assert work_eligibility(text) == "clearance, no sponsorship"


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("This role is based in London and the team is international.", "United Kingdom"),
        ("Only candidates currently located in Munich will be considered.", "Germany"),
        ("Hybrid work based in Modena, Milan, or Bergamo.", "Italy"),
    ],
)
def test_a_city_scopes_the_role_through_the_country_it_sits_in(description, expected):
    assert stated_locations(description, "en") == expected


@pytest.mark.parametrize(
    "description",
    [
        "We're based in Paris but open to remote work!",
        "casavi is a growing PropTech company based in Munich, Germany.",
        "A global financial services company based in Wilmington, Delaware, USA.",
    ],
)
def test_a_firms_own_address_does_not_scope_the_role(description):
    """ "We're based in Paris but open to remote work" scopes nothing; it is the company's address."""
    assert stated_locations(description, "en") is None


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Ideal candidate based in: Switzerland, willing to travel.", "Switzerland"),
        ("You will primarily work from home within UK.", "United Kingdom"),
        ("Luogo di lavoro: Verona.", "Italy"),
    ],
)
def test_a_place_is_read_past_a_colon_or_a_field_label(description, expected):
    assert stated_locations(description, "en" if "lavoro" not in description else "it") == expected


def test_a_city_resolves_only_under_the_name_english_gives_it():
    """Cities are read from their English names alone, so "Milano" is a known blind spot.

    It costs nothing today — an Italian ad naming an Italian city should not be flagged either way —
    but it is what a distance-from-home reading would have to close first.
    """
    assert stated_locations("Sede di lavoro: Milano.", "it") is None
    assert stated_locations("Sede di lavoro: Milan.", "it") == "Italy"


def test_a_relative_clause_is_caught_with_its_copula_spelled_out():
    """The phrases stop at "based", so "who is" has to be absorbed by the guard, not the anchor."""
    text = "The team is led by an Engineering Manager who is based in Northern Ireland."
    assert stated_locations(text, "en") is None


def test_a_data_protection_clause_is_not_a_job_scope():
    """ "If you are resident in the UK" here introduces a GDPR notice, not where you must live."""
    text = "If you are resident in the UK, EEA or Switzerland, we will process any access request."
    assert stated_locations(text, "en") is None


def test_a_region_is_not_reached_across_a_verb():
    """ "EU" here qualifies the citizenship, not the place, so the role stays Ireland's alone."""
    text = "Applicants must be living in Ireland and have either EU citizenship or a Stamp 4 visa."
    assert stated_locations(text, "en") == "Ireland"
