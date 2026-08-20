from linkedin_job_scraper.constants import NO_DESCRIPTION
from linkedin_job_scraper.language import description_lang, readable_words


def test_a_missing_description_is_undetermined():
    assert description_lang(None) is None
    assert description_lang("") is None
    assert description_lang(NO_DESCRIPTION) is None


def test_too_little_text_is_undetermined():
    assert description_lang("Python dev") is None  # under the word floor, a guess would be noise


def test_an_ad_scraped_down_to_bullets_is_undetermined():
    """It would otherwise classify confidently off a handful of words, and the keep-list would act on that."""
    assert description_lang("Was Dich erwartet - - - - - - - - Was Du mitbringst - - - - -") is None


def test_featureless_text_is_undetermined():
    assert description_lang("!!!!! ///// ..... ----- ##### $$$$$") is None  # langdetect raises; we swallow it


def test_the_language_is_named_by_its_code():
    assert (
        description_lang(
            "We are hiring a backend engineer to build and run our data pipelines in Python, working "
            "with the team on the API, the database and the weekly release."
        )
        == "en"
    )
    assert (
        description_lang(
            "Cerchiamo uno sviluppatore backend per le nostre pipeline di dati, che lavori con il team "
            "sulle API, sul database e sul rilascio settimanale del software."
        )
        == "it"
    )


def test_readable_words_counts_prose_not_punctuation():
    assert readable_words("- - - - -") == 0
    assert readable_words("Working Model - - - Who You Are") == 5
    assert readable_words(None) == 0
