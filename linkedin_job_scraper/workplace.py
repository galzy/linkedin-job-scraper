"""What a posting says about working remotely, when it says anything at all."""

import re

from linkedin_job_scraper.config import WorkplaceType
from linkedin_job_scraper.constants import NO_DESCRIPTION

# Only wording that settles the question on its own; weaker wording leaves the type unread.
_REMOTE = re.compile(
    r"\bfully[- ]remote\b|\b100\s*%\s*remote\b|\bremote[- ]first\b|\bfully distributed\b"
    r"|\bwork from (?:home|anywhere)\b|\bvollst[aä]ndig remote\b|\bkomplett remote\b"
    r"|\bcompletamente da remoto\b|\bfull\s*remote\b|\btelelavoro\b|\bvolledig op afstand\b",
    re.IGNORECASE,
)
# Only phrases that rule remote out; an office named in passing does not.
_ON_SITE = re.compile(
    r"\b(?:fully|100\s*%)\s*on-?site\b|\bon-?site only\b|\bno remote\b|\bnot a remote\b"
    r"|\bremote work is not\b|\bkein remote\b|\bsolo in sede\b",
    re.IGNORECASE,
)
# A title says it plainly or not at all, so the bare word is enough here.
_TITLE_REMOTE = re.compile(r"\bremote\b|\bda remoto\b", re.IGNORECASE)


def infer_workplace_type(title: str | None, description: str | None) -> str:
    """The workplace type the posting states outright, or ``untagged`` when it states none.

    Hybrid is never returned: its wording appears in remote ads too often to read safely.
    Remote is read before on-site, so an ad offering remote work is not turned down by the
    office it also mentions.
    """
    title, description = title or "", description or ""
    if description == NO_DESCRIPTION:
        description = ""
    if _TITLE_REMOTE.search(title) or _REMOTE.search(description):
        return WorkplaceType.REMOTE.value
    if _ON_SITE.search(description):
        return WorkplaceType.ON_SITE.value
    return WorkplaceType.UNTAGGED.value
