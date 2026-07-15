from linkedin_scraper.constants import NO_DESCRIPTION
from linkedin_scraper.language import is_english


def test_a_missing_description_is_undetermined():
    assert is_english(None) is None
    assert is_english("") is None
    assert is_english(NO_DESCRIPTION) is None


def test_too_little_text_is_undetermined():
    assert is_english("Python dev") is None  # under the min-chars floor, a guess would be noise


def test_featureless_text_is_undetermined():
    assert is_english("!!!!! ///// ..... ----- ##### $$$$$") is None  # langdetect raises; we swallow it
