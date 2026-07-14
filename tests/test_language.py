from linkedin_scraper.constants import NO_DESCRIPTION
from linkedin_scraper.language import is_english


def test_flags_english_prose():
    assert is_english("We are looking for a backend engineer with strong Python and cloud experience.") is True


def test_flags_non_english_prose():
    assert is_english("Cerchiamo uno sviluppatore backend con esperienza in Python e cloud.") is False
    assert is_english("Wir suchen einen Backend-Entwickler mit Erfahrung in Python und Cloud.") is False


def test_a_missing_description_is_undetermined():
    assert is_english(None) is None
    assert is_english("") is None
    assert is_english(NO_DESCRIPTION) is None


def test_too_little_text_is_undetermined():
    assert is_english("Python dev") is None  # under the min-chars floor, a guess would be noise


def test_featureless_text_is_undetermined():
    assert is_english("!!!!! ///// ..... ----- ##### $$$$$") is None  # langdetect raises; we swallow it
