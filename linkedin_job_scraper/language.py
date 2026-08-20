"""Name the language a job description is written in."""

import re

from langdetect import DetectorFactory, detect
from langdetect.lang_detect_exception import LangDetectException

from linkedin_job_scraper.constants import NO_DESCRIPTION

DetectorFactory.seed = 0  # pin langdetect's RNG so a description always classifies the same way

_MIN_WORDS = 20  # below this the guess is noise, and a skeleton of bullets still classifies confidently
_WORD = re.compile(r"[^\W\d_]{2,}")


def readable_words(text: str | None) -> int:
    """How many words of prose the text holds — what an ad scraped down to bullets has almost none of."""
    return len(_WORD.findall(text or ""))


def description_lang(description: str | None) -> str | None:
    """The ISO 639-1 code the description reads as, or None when there's too little text to judge."""
    if not description or description == NO_DESCRIPTION or readable_words(description) < _MIN_WORDS:
        return None
    try:
        return detect(description)
    except LangDetectException:
        return None
