"""Name the language a job description is written in."""

from langdetect import DetectorFactory, detect
from langdetect.lang_detect_exception import LangDetectException

from linkedin_job_scraper.constants import NO_DESCRIPTION

DetectorFactory.seed = 0  # pin langdetect's RNG so a description always classifies the same way

_MIN_CHARS = 20  # below this the guess is noise


def description_lang(description: str | None) -> str | None:
    """The ISO 639-1 code the description reads as, or None when there's too little text to judge."""
    if not description or description == NO_DESCRIPTION or len(description.strip()) < _MIN_CHARS:
        return None
    try:
        return detect(description)
    except LangDetectException:
        return None
