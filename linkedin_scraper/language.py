"""Flag whether a job description reads as English."""

from langdetect import DetectorFactory, detect
from langdetect.lang_detect_exception import LangDetectException

from linkedin_scraper.constants import NO_DESCRIPTION

DetectorFactory.seed = 0  # pin langdetect's RNG so a description always classifies the same way

_MIN_CHARS = 20  # below this the guess is noise


def is_english(description: str | None) -> bool | None:
    """Whether the description reads as English, or None when there's too little text to judge."""
    if not description or description == NO_DESCRIPTION or len(description.strip()) < _MIN_CHARS:
        return None
    try:
        return detect(description) == "en"
    except LangDetectException:
        return None
