"""Read the fit verdicts a person wrote about a posting."""

import re

_ENTRY = re.compile(r"^\s*([a-g])(\??)\s*:")  # "c?: UK listing; g: Java" — letter, "?" if suspected, reason


def is_firm(verdict: str | None) -> bool:
    """Whether a verdict states a code outright; a "?" means revisit, and unparsable text no code."""
    return any((entered := _ENTRY.match(entry)) and not entered.group(2) for entry in (verdict or "").split(";"))


def is_wellformed(verdict: str) -> bool:
    """Whether text parses as a verdict: empty, or code-headed entries joined by ";".

    A person may store free text as a note to self; a machine judge may not, so this is the gate
    its output passes before ``import_verdicts`` sees it.
    """
    return verdict == "" or all(re.fullmatch(r"\s*[a-g]\??\s*:\s*\S.*", entry) for entry in verdict.split(";"))
