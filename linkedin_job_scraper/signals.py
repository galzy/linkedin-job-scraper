"""Where a posting says you must be, and what it requires you to already hold."""

import re
from functools import cache
from importlib import resources

import yaml

from linkedin_job_scraper.constants import NO_DESCRIPTION
from linkedin_job_scraper.geo import city_country, country_vocabulary

WIDE = "EU"  # stands in for any clause open to a whole region rather than named countries

_APOSTROPHES = "'" + chr(0x2019)  # straight and curly; ads use both, often in the same line

# The article between a phrase and the place it introduces, so the phrases need not carry one.
_ARTICLES = r"(?:the|der|die|das|dem|den|il|la|lo|i|gli|le|de|het|een)"

_SPEC = yaml.safe_load((resources.files("linkedin_job_scraper") / "location_phrases.yaml").read_text(encoding="utf-8"))

#: The languages whose phrases have been checked against real ads and are asserted by the tests.
#: Anything outside this set is read with the other languages' phrases alone, which finds only what
#: an ad happens to write in one of them.
COVERED_LANGUAGES = frozenset(code for code, entry in _SPEC["languages"].items() if entry["covered"])

_WIDE_WORDS = "|".join(_SPEC["regions"])
_INFORMAL = {name.lower(): country for name, country in _SPEC["informal"].items()}


def _anchor(phrase: str) -> str:
    """One phrase from the file as a regex reaching up to the place it introduces.

    A ``*`` opens a gap of up to three words, and a colon may follow any phrase, whether or not it
    was written with one. What trails every phrase is the filler a place hides behind: a possessive
    ("based in our Dublin office"), an article, and a city standing in front of its country. The city
    step is reluctant, or "based in Italy, Germany" would skip past Italy to read Germany alone.
    """
    parts = [r"(?:\s+\S+){0,3}?" if word == "*" else re.escape(word) for word in phrase.rstrip(":").split()]
    body = parts[0]
    for part in parts[1:]:
        body += part if part.startswith(r"(?:\s+") else rf"\s+{part}"
    return rf"\b{body}(?:\s*:\s*|\s+)(?:(?:one\s+of\s+)?our\s+)?(?:{_ARTICLES}\s+)?(?:\w+,\s+)??"


_ANCHORS = tuple(_anchor(phrase) for entry in _SPEC["languages"].values() for phrase in entry["phrases"] or ())

# Pay-transparency boilerplate ("For roles based in X, the typical salary…") names a country without
# scoping the role, and a vendor's or the staff's whereabouts is not the candidate's. A relative
# clause is always about someone else: an ad addressing you says "you must be based in", never
# "who is based in". "For candidates" is deliberately absent — it heads a real scope as often as a
# pay band ("an opportunity for candidates based in the UK"), which _PAY_FOLLOWS tells apart.
_NOT_THE_CANDIDATE = re.compile(
    r"(?:for\s+(?:roles|positions|jobs)|if\s+you\s+are)\b[^.]{0,40}$"
    r"|\b(?:customers?|clients?|colleagues?|teams?|users?|headquarters|servers?|provider|"
    r"vendors?|partners?|stakeholders?)\b[^.]{0,40}$"
    # An office only speaks for itself right against the clause: "our offices are located in Germany"
    # is the company's, while "two days in the office and is based in Munich" is still the role's.
    r"|\boffices?\s*(?:is|are)?\s*$"
    # The copula is spelled out because the phrases stop at "based", so it is what sits in between.
    r"|\bwho\s+(?:is|are|will\s+be)?\s*$"
    # A firm's own address, which sits right against the clause: "we're based in Paris", "a PropTech
    # company based in Munich". Only right against it, or "we are looking for people based in the UK"
    # would go with it.
    rf"|\b(?:we[{_APOSTROPHES}]re|we\s+are|company|start-?up|firm|agency|consultancy|business|brand)\s*,?\s*$"
    r"|(?:Inc\.|LLC|GmbH|Software,)\s*,?\s*[^.]{0,30}$",
    re.IGNORECASE,
)

# What follows tells a pay band from an address: "located in Poland the salary range is 30,400 PLN"
# scopes nothing, where "based in Sweden looking for your next role" scopes the role.
_PAY_FOLLOWS = re.compile(r"^[^.]{0,50}\b(?:salary|salaries|compensation|pay|range|gross|annually)\b", re.IGNORECASE)


@cache
def _scopes(language: str | None) -> tuple[list[re.Pattern], dict[str, str]]:
    """The anchored patterns to search ``language`` with, and the names they may capture.

    Longest name first, so "United Kingdom" wins the alternation before "United" can. Anchoring is
    what makes a list this size safe: a country only counts where the sentence puts a location, so
    the short ambiguous names (Chad, Georgia, Jordan) can't fire on ordinary prose.
    """
    vocabulary = country_vocabulary(language) | _INFORMAL
    names = "|".join(re.escape(name) for name in sorted(vocabulary, key=len, reverse=True))
    # A region reaches the alternation past a partitive ("UK and parts of Europe"), which a country
    # may not: "Germany and hiring in France" names no second place you could work. Only partitives,
    # or "living in Ireland and have either EU citizenship" would read as open to the whole EU.
    region = rf"(?:(?:parts?|rest|all|any|the|much)\s+(?:of\s+)?){{0,2}}(?:{_WIDE_WORDS})"
    chain = rf"(?:\s*(?:,|/|\bor\b|\band\b)\s*(?:the\s+)?(?:{names}|{region}))*"
    patterns = [re.compile(rf"{anchor}(({names}|{_WIDE_WORDS}){chain})", re.IGNORECASE) for anchor in _ANCHORS]
    return patterns, vocabulary


# The same anchors reading a proper noun instead of a known country, so "based in our London office"
# can be resolved through the city. Capitalisation is what marks the noun, so only the anchor folds case.
_PLACE = rf"[A-Z][\w{_APOSTROPHES}\-]+(?:[ -][A-Z][\w{_APOSTROPHES}\-]+){{0,2}}"
_CITY_PATTERNS = tuple(
    re.compile(rf"(?i:{anchor})({_PLACE}(?:\s*(?:,|/|\bor\b|\band\b)\s*{_PLACE})*)") for anchor in _ANCHORS
)


def _about_someone_else(description: str, match: re.Match) -> bool:
    """Whether the clause belongs to a pay band or to somebody who is not the candidate."""
    return bool(
        _NOT_THE_CANDIDATE.search(description[max(0, match.start() - 70) : match.start()])
        or _PAY_FOLLOWS.match(description[match.end() : match.end() + 60])
    )


def stated_locations(description: str | None, language: str | None = None) -> str | None:
    """Where the ad says you must be: comma-separated countries, ``EU`` for a region-wide clause.

    None when it says nothing. A clause naming both ("UK or Europe") keeps both, since the wider
    half is what decides whether somewhere unnamed still qualifies. ``language`` is the description's
    own, which adds that language's country names to the ones looked for. A clause naming only a city
    counts through the country that city sits in.
    """
    if not description or description == NO_DESCRIPTION:
        return None
    patterns, vocabulary = _scopes(language)
    found: set[str] = set()
    for pattern in patterns:
        for match in pattern.finditer(description):
            if _about_someone_else(description, match):
                continue
            for token in re.split(r"\s*(?:,|/|\bor\b|\band\b)\s*", match.group(1)):
                token = token.strip().lower()
                # Searched, not matched whole: the filler the chain allowed rides along on the token.
                if re.search(rf"\b(?:{_WIDE_WORDS})\b", token, re.IGNORECASE):
                    found.add(WIDE)
                elif name := vocabulary.get(token) or vocabulary.get(token.removeprefix("the ")):
                    found.add(name)
    for pattern in _CITY_PATTERNS:
        for match in pattern.finditer(description):
            if _about_someone_else(description, match):
                continue
            for token in re.split(r"\s*(?:,|/|\bor\b|\band\b)\s*", match.group(1)):
                if country := city_country(token):
                    found.add(country)
    return ", ".join(sorted(found)) or None


# Bars that name no country at all: refusing a visa or demanding a national clearance restricts the
# role to wherever it already sits, so the posting's own country is what they scope it to.
_SPONSOR = re.compile(r"\bsponsor(?:ship|ing|s|ed)?\b", re.IGNORECASE)
_DENIED_BEFORE = re.compile(
    r"\b(?:un(?:able|willing)|not|no|cannot|can'?t|do(?:es)?n'?t|never|without|nor|lack\w*)\b[^.]{0,45}$",
    re.IGNORECASE,
)
_DENIED_AFTER = re.compile(r"^[^.]{0,30}\b(?:is\s+)?(?:not\b|un(?:available|able)\b)", re.IGNORECASE)

# A denial has to sit beside work-permit language, or "we cannot name our project sponsors" qualifies.
_WORK_PERMIT = re.compile(
    r"\b(?:visas?|work\s+permits?|immigration|right\s+to\s+work|work\s+authoris\w*|work\s+authoriz\w*)\b",
    re.IGNORECASE,
)

_CLEARANCE = re.compile(
    r"\b(?:security|SC|DV|BPSS|NPPV|CTC|NATO|government|police|baseline)[\s-]{0,2}"
    r"(?:clearance|cleared|vetting)\b"
    r"|\bclearance\s*(?::|level\b|is\s+required\b|required\b)",  # "Clearance: must be eligible for…"
    re.IGNORECASE,
)
# A clearance offered as a nicety bars nobody: "willingness to get security cleared is a plus".
_CLEARANCE_WAIVED = re.compile(
    r"^[^.]{0,25}\b(?:is\s+|are\s+|would\s+be\s+)?"
    r"(?:not\s+(?:required|necessary|needed)|an?\s+(?:plus|bonus|advantage)|desirable|advantageous|"
    r"beneficial|nice\s+to\s+have|preferred\s+but\s+not)",
    re.IGNORECASE,
)


def work_eligibility(description: str | None) -> str | None:
    """The bars an ad sets on who may take it: ``clearance``, ``no sponsorship``, or both.

    Read in English only, the language such boilerplate almost always arrives in. None when the ad
    sets neither, which is the ordinary case.
    """
    if not description or description == NO_DESCRIPTION:
        return None
    found: set[str] = set()
    for match in _CLEARANCE.finditer(description):
        if not _CLEARANCE_WAIVED.match(description[match.end() : match.end() + 40]):
            found.add("clearance")
    for match in _SPONSOR.finditer(description):
        before = description[max(0, match.start() - 60) : match.start()]
        after = description[match.end() : match.end() + 40]
        denied = _DENIED_BEFORE.search(before) or _DENIED_AFTER.match(after)
        # "Sponsorship" on its own is already the immigration sense; a bare "sponsor" needs the context.
        if denied and (_WORK_PERMIT.search(before + after) or match.group().lower() == "sponsorship"):
            found.add("no sponsorship")
    return ", ".join(sorted(found)) or None
