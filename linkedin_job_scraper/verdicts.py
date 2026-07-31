"""Read the fit verdicts a person wrote about a posting."""

import re

_ENTRY = re.compile(r"^\s*([a-g])(\??)\s*:")  # "c?: UK listing; g: Java" — letter, "?" if suspected, reason


def is_firm(verdict: str | None) -> bool:
    """Whether a verdict states a code outright; a "?" means revisit, and unparsable text no code."""
    return any((entered := _ENTRY.match(entry)) and not entered.group(2) for entry in (verdict or "").split(";"))
