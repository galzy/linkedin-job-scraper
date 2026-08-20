"""Judge unjudged rows against the fit rubric through headless claude calls."""

import json
import re
import shutil
import subprocess
from collections.abc import Iterator, Sequence

from loguru import logger

from linkedin_job_scraper.language import readable_words
from linkedin_job_scraper.verdicts import PASS, is_wellformed

DEFAULT_JUDGE_MODEL = "sonnet"
BATCH_SIZE = 12  # ads per claude call, the size the rubric's pipeline was tuned on
_DESCRIPTION_CAP = 3500
_HITS_PER_KIND = 4
_TIMEOUT = 600  # seconds per batch; a judge slower than this has hung

_MIN_WORDS = 25  # under this an ad is a skeleton of bullets, which the judge waves through half the time
_CLEAN_ALARM = 0.03  # no day the judge read properly has cleared 1.2%; the day it stopped reading hit 4.1%
_ALARM_FLOOR = 50  # under this a day is too small for a share to mean anything

# Pointers that speed the judge's reading; the prompt tells it they decide nothing. "Go" keeps its
# case, or every "go to" would hit.
_HIT_PATTERNS = {
    "salary": re.compile(
        r"€|£|\bCHF\b|\bEUR\b|\bGBP\b|\bUSD\b|\bRAL\b|\bretribu|\bsalar|\bstipendio\b|\bpensum\b"
        r"|\bcompensation\b|\bal mese\b|\bday rate\b|\b\d{2,3}[.,]?\d{0,3}\s*k\b",
        re.IGNORECASE,
    ),
    "language": re.compile(
        r"\bfluent|\bnative\b|\bmadrelingua\b|\bmuttersprach|\b[BC][12]\b|\bGerman\b|\bFrench\b"
        r"|\bDutch\b|\bDeutsch|\bfran[cç]ais|\bnederlands\b|\blanguage skills\b",
        re.IGNORECASE,
    ),
    "residency": re.compile(
        r"\bbased in\b|\blocated in\b|\bcitizen|\bclearance\b|\bsponsor|\bon-?site\b|\boffice\b"
        r"|\bhybrid\b|\bdays? (?:per|a) week\b|\brelocat|\bwork permit\b|\bvisa\b|\bsede\b",
        re.IGNORECASE,
    ),
    "frontend": re.compile(
        r"\bReact\b|\bAngular\b|\bVue\b|\bHTML\b|\bCSS\b|\bfront-?end\b|\bfull-?stack\b", re.IGNORECASE
    ),
    "stack": re.compile(
        r"(?-i:\bGo(?:lang)?\b)|\bRust\b|\bJava\b|\bC#|\bC\+\+|\.NET\b|\bTypeScript\b|\bJavaScript\b|\bNode\b"
        r"|\bDevOps\b|\bSRE\b|\bKubernetes\b|\bTerraform\b|\bdata warehouse\b|\bDWH\b|\bPower ?BI\b"
        r"|\bmachine learning\b|\bSAP\b|\bSalesforce\b|\bembedded\b|\bmobile\b|\bAndroid\b|\biOS\b|\bsecurity\b",
        re.IGNORECASE,
    ),
    "customer": re.compile(
        r"\bclients?\b|\bclienti\b|\bcliente\b|\bcustomers?\b|\bpre-?sales\b|\baccount management\b"
        r"|\bcustomer[- ]facing\b|\bworkshops?\b|\bconsultan|\bconsulenz",
        re.IGNORECASE,
    ),
}

_PROMPT = """\
Judge the LinkedIn job ads below against this fit rubric. Apply its Conditions, the Italy section,
and the error asymmetry it states: prefer a "?" code over a firm one when unsure, and write nothing
you cannot point to in the ad. Its cohort and pipeline sections describe other tooling; ignore them.

<rubric>
{rubric}
</rubric>

The "hits" lines under each ad are regex-extracted pointers to speed reading; they decide nothing.

{ads}

Reply with only a JSON object, no code fences or commentary, mapping every jid to its verdict:
{{"4261234567": "b: fluent German required; g?: Databricks stack", "4267654321": "{clean}", ...}}.
A verdict is "{clean}" when no condition applies, or "letter: reason" / "letter?: reason" entries joined
by "; ", letters in alphabetical order, each reason a short lowercase noun phrase naming the ad's
evidence. Never reply with an empty string. Every jid must appear: {jids}.
"""


class FitJudgeError(Exception):
    """A batch that produced no valid verdicts, even on retry."""


def looks_lenient(clean: int, judged: int) -> bool:
    """Whether a day cleared too many ads to trust — the shape of a judge that stopped reading them."""
    return judged >= _ALARM_FLOOR and clean / judged > _CLEAN_ALARM


def _settled(row) -> str | None:
    """The verdict a rule already decides, or None when the ad is for the judge to read.

    Language is not among them: ``description_lang_include`` settles that a layer earlier, in the
    relevance predicate, where a config edit and a recompute can take it back.
    """
    if readable_words(row.job_description) < _MIN_WORDS:
        return "d?: no readable description"
    return None


def _hits(description: str) -> list[str]:
    """One "kind hits: …" line per matching pattern, each hit the trimmed line around a match."""
    lines = []
    for kind, pattern in _HIT_PATTERNS.items():
        hits: list[str] = []
        for match in pattern.finditer(description):
            start = description.rfind("\n", 0, match.start()) + 1
            end = description.find("\n", match.end())
            end = end if end != -1 else len(description)
            if end - start > 160:  # single-block text: a window around the match instead of its line
                start, end = max(start, match.start() - 60), min(end, match.end() + 100)
            hit = description[start:end].strip()
            if hit.lower() not in (seen.lower() for seen in hits):
                hits.append(hit)
            if len(hits) == _HITS_PER_KIND:
                break
        if hits:
            lines.append(f"{kind} hits: " + " | ".join(hits))
    return lines


def _ad(row) -> str:
    """One ad as markdown: header fields, hit pointers, and the capped description."""
    jid = row.job_url.rstrip("/").rsplit("/", 1)[-1]
    header = (
        f"### jid {jid}\n"
        f"{row.title} — {row.company} — {row.location or 'location unstated'} (country: {row.country or '?'})\n"
        f"workplace: {row.workplace_type} | posted: {row.date or '?'} | lang: {row.description_lang or '?'}"
        f" | stated_locations: {row.stated_locations or '-'} | eligibility: {row.work_eligibility or '-'}"
        f" | dup_count: {row.dup_count if row.dup_count is not None else '?'}"
    )
    description = row.job_description
    capped = description[:_DESCRIPTION_CAP]
    return "\n".join([header, *_hits(description), f"description (first {_DESCRIPTION_CAP} chars):", capped])


def _ask(prompt: str, claude: str, model: str) -> str:
    """One headless claude call, returning the reply text; FitJudgeError on a nonzero exit."""
    exe = shutil.which(claude) or claude
    result = subprocess.run(
        [exe, "-p", "--model", model],
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_TIMEOUT,
    )
    if result.returncode != 0:
        raise FitJudgeError(f"claude exited {result.returncode}: {(result.stderr or result.stdout).strip()[:300]}")
    return result.stdout


def _parse(reply: str, expected: set[str]) -> dict[str, str]:
    """The verdicts in a reply, keyed by jid; FitJudgeError when any jid is missing or malformed."""
    start, end = reply.find("{"), reply.rfind("}")
    if start == -1 or end <= start:
        raise FitJudgeError(f"no JSON object in the reply: {reply.strip()[:200]!r}")
    try:
        data = json.loads(reply[start : end + 1])
    except json.JSONDecodeError as e:
        raise FitJudgeError(f"unparsable JSON: {e}") from e
    if missing := expected - data.keys():
        raise FitJudgeError(f"verdicts missing for {sorted(missing)}")
    bad = {jid: data[jid] for jid in expected if not (isinstance(data[jid], str) and is_wellformed(data[jid].strip()))}
    if bad:
        raise FitJudgeError(f"malformed verdicts: {bad}")
    return {jid: data[jid].strip() for jid in expected}


def judge_batches(
    rows: Sequence, rubric: str, claude: str, model: str = DEFAULT_JUDGE_MODEL
) -> Iterator[dict[str, str]]:
    """Judge the rows in batches, yielding verdicts by job_url per batch so each lands as it is won.

    Rows a rule settles skip the judge and come out first. A batch is retried once; failing again
    raises FitJudgeError, keeping what earlier batches yielded.
    """
    ruled, unread = {}, []
    for row in rows:
        if verdict := _settled(row):
            ruled[row.job_url] = verdict
        else:
            unread.append(row)
    if ruled:
        logger.info(f"{len(ruled)} rows settled by rule; {len(unread)} going to the judge")
        yield ruled
    batches = [unread[i : i + BATCH_SIZE] for i in range(0, len(unread), BATCH_SIZE)]
    for number, batch in enumerate(batches, 1):
        by_jid = {row.job_url.rstrip("/").rsplit("/", 1)[-1]: row.job_url for row in batch}
        prompt = _PROMPT.format(
            rubric=rubric, ads="\n\n".join(_ad(row) for row in batch), jids=", ".join(by_jid), clean=PASS
        )
        logger.info(f"Judging batch {number}/{len(batches)} ({len(batch)} ads)")
        for attempt in (1, 2):
            try:
                verdicts = _parse(_ask(prompt, claude, model), set(by_jid))
                break
            except (FitJudgeError, subprocess.SubprocessError, OSError) as e:
                logger.warning(f"Batch {number}, attempt {attempt}: {e}")
                if attempt == 2:
                    raise FitJudgeError(f"batch {number} failed twice; its rows stay unjudged") from e
        yield {by_jid[jid]: verdict for jid, verdict in verdicts.items()}
