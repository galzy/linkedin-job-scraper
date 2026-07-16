"""Request headers and the browser profiles behind them."""

import random
from datetime import date

from loguru import logger

from linkedin_job_scraper.constants import SEARCH_REFERER


def _chrome(major: int, ua_platform: str, ch_platform: str) -> dict[str, str]:
    """Headers for one desktop Chrome build, client hints included.

    Chrome freezes everything past the major version and sends the real detail in
    ``Sec-CH-UA-*`` instead, unprompted. A Chrome UA without those is a mismatch.
    """
    return {
        "User-Agent": f"Mozilla/5.0 ({ua_platform}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36",
        # "Not;A=Brand" is GREASE: a fake brand, so servers can't hardcode the list.
        "Sec-CH-UA": f'"Not;A=Brand";v="8", "Chromium";v="{major}", "Google Chrome";v="{major}"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": f'"{ch_platform}"',
        "Accept-Language": "en-US,en;q=0.9",
    }


# Chrome 150 / Safari 26 were stable at the anchor; the majors are projected forward from
# there so an idle checkout doesn't send a years-old UA.
_ANCHOR = date(2026, 7, 1)
_ANCHOR_CHROME = 150
_DAYS_PER_MAJOR = 32  # Chrome ships a stable major ~monthly; bias to lag, never lead
_ANCHOR_SAFARI = 26  # Safari bumps ~yearly; step by whole years past the anchor
_STALE_AFTER_DAYS = 550  # ~18 months: warn to re-baseline before the projection drifts


def current_chrome_major(today: date | None = None) -> int:
    """Chrome's likely-current stable major, projected from the anchor's ~monthly cadence."""
    elapsed = ((today or date.today()) - _ANCHOR).days
    return _ANCHOR_CHROME + max(0, elapsed) // _DAYS_PER_MAJOR


def _build_profiles(chrome: int, safari: int) -> list[dict[str, str]]:
    """One profile per run, drawn at construction: three latest, one a version back, plus Safari.

    Each entry is internally consistent (UA matched to its client hints) rather than padding.
    """
    return [
        _chrome(chrome, "Windows NT 10.0; Win64; x64", "Windows"),
        _chrome(chrome, "Macintosh; Intel Mac OS X 10_15_7", "macOS"),
        _chrome(chrome, "X11; Linux x86_64", "Linux"),
        _chrome(chrome - 1, "Windows NT 10.0; Win64; x64", "Windows"),
        {
            # Safari implements no client hints, and froze its OS token at 10_15_7.
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                f"(KHTML, like Gecko) Version/{safari}.0 Safari/605.1.15"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    ]


_today = date.today()
if (_today - _ANCHOR).days > _STALE_AFTER_DAYS:
    logger.warning("Browser-profile anchor in net/headers.py is over 18 months old; re-baseline it")
BROWSER_PROFILES = _build_profiles(
    current_chrome_major(_today), _ANCHOR_SAFARI + max(0, _today.year - _ANCHOR.year)
)

# No Accept-Encoding: requests derives it from the decoders we actually have, and
# claiming `br` without one would soup compressed bytes into an empty page.
# SEARCH_REFERER is the page a human would be on before the guest API fires its XHRs.
_COMMON_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": SEARCH_REFERER,
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


def session_headers() -> dict[str, str]:
    """The common headers plus one randomly chosen browser profile, for a run's whole session."""
    return _COMMON_HEADERS | random.choice(BROWSER_PROFILES)
