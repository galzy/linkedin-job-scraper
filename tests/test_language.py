from linkedin_job_scraper.constants import NO_DESCRIPTION
from linkedin_job_scraper.language import description_lang


def test_a_missing_description_is_undetermined():
    assert description_lang(None) is None
    assert description_lang("") is None
    assert description_lang(NO_DESCRIPTION) is None


def test_too_little_text_is_undetermined():
    assert description_lang("Python dev") is None  # under the min-chars floor, a guess would be noise


def test_featureless_text_is_undetermined():
    assert description_lang("!!!!! ///// ..... ----- ##### $$$$$") is None  # langdetect raises; we swallow it


def test_the_language_is_named_by_its_code():
    assert description_lang("We are hiring a backend engineer to build data pipelines in Python.") == "en"
    assert description_lang("Cerchiamo uno sviluppatore backend per le nostre pipeline di dati.") == "it"
