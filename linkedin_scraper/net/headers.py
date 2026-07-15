"""The browser fingerprint the scraper wears: request headers and the UA profiles behind them."""

import random

from linkedin_scraper.constants import SEARCH_REFERER


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


# One profile per run, chosen at construction, so each entry earns its place by being
# internally consistent rather than by padding the list. Chrome 150 is stable as of
# 2026-07; refresh when stale, since an obsolete UA fingerprints better than a common one.
BROWSER_PROFILES = [
    _chrome(150, "Windows NT 10.0; Win64; x64", "Windows"),
    _chrome(150, "Macintosh; Intel Mac OS X 10_15_7", "macOS"),
    _chrome(150, "X11; Linux x86_64", "Linux"),
    _chrome(149, "Windows NT 10.0; Win64; x64", "Windows"),
    {
        # Safari implements no client hints, and froze its OS token at 10_15_7.
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/26.0 Safari/605.1.15"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    },
]

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
