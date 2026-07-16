"""The scraper's single network chokepoint: every request passes a shared rate limiter."""

import random
import threading
import time
from typing import NamedTuple

import requests
from bs4 import BeautifulSoup
from loguru import logger
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException

from linkedin_job_scraper.config import HttpConfig
from linkedin_job_scraper.net.headers import session_headers


class Throttled(RequestException):
    """LinkedIn turned us away for now: 429, 403, or the 999 authwall.

    Named in place of the generic ``HTTPError`` so throttling is greppable in the logs.
    """


class Fetch(NamedTuple):
    """A fetch's outcome: the parsed page, or None with ``gone`` telling a removed resource from a miss."""

    soup: BeautifulSoup | None
    gone: bool = False  # True only on a definitive 404/410: the posting is removed, not a transient failure


# All three are temporary and lift on their own; 999 is the guest authwall, 403 its stand-in under load.
THROTTLE_STATUSES = frozenset({403, 429, 999})
# A removed resource, not a hiccup: no retry, and callers can tell it apart from a failed fetch.
GONE_STATUSES = frozenset({404, 410})


class RateLimiter:
    """Paces requests to ``rate_per_minute`` across all worker threads.

    Each :meth:`acquire` reserves the next slot and blocks until it comes round, so the
    threads share one budget rather than each getting their own.

    The scraper's only throttle: a sleep anywhere else in the request path subtracts from
    this rate without saying so.
    """

    def __init__(self, rate_per_minute: float, jitter: float = 0.4, max_interval: float = 60.0):
        self._mean_interval = 60.0 / rate_per_minute
        self._max_interval = max(max_interval, self._mean_interval)
        self._jitter = jitter
        self._lock = threading.Lock()
        # Allow the very first request to fire immediately.
        self._next_allowed = time.monotonic()

    def _interval(self) -> float:
        """A gap either side of the mean, so the request timing carries no fingerprint."""
        return self._mean_interval * random.uniform(1.0 - self._jitter, 1.0 + self._jitter)

    def acquire(self) -> None:
        """Block until this thread is cleared to make one request."""
        while True:
            with self._lock:
                now = time.monotonic()
                wait = self._next_allowed - now
                if wait <= 0:
                    self._next_allowed = now + self._interval()
                    return
            time.sleep(wait)

    def slow_down(self, factor: float = 2.0) -> None:
        """Permanently stretch the request spacing, up to ``max_interval``.

        Called repeatedly as throttles pile up; the ceiling stops it pacing the run to a crawl.
        """
        with self._lock:
            self._mean_interval = min(self._mean_interval * factor, self._max_interval)
        logger.warning(f"Rate limiter slowing down: mean interval now {self._mean_interval:.1f}s")


class HttpClient:
    """A rate-limited, connection-pooling HTTP client returning parsed soup."""

    def __init__(
        self,
        rate_limiter: RateLimiter,
        *,
        timeout: float,
        retries: int,
        backoff_base: float,
        backoff_max: float,
        backoff_jitter: float,
        retry_after_cap: float,
        pool_size: int,
        slowdown_every: int = 3,
    ):
        self._limiter = rate_limiter
        self._timeout = timeout
        self._retries = retries
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._backoff_jitter = backoff_jitter
        self._retry_after_cap = retry_after_cap
        self._slowdown_every = slowdown_every

        self._throttle_count = 0
        self._count_lock = threading.Lock()

        self._pool_size = pool_size
        self._session = self._new_session()

    def _new_session(self) -> requests.Session:
        """A fresh session: new cookie jar, newly drawn browser profile, its own connection pool."""
        session = requests.Session()
        session.headers.update(session_headers())

        # Pool wide enough that every worker keeps its own kept-alive connection.
        adapter = HTTPAdapter(pool_connections=self._pool_size, pool_maxsize=self._pool_size)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def renew_session(self) -> None:
        """Trade the session for a fresh one; LinkedIn deals each session its serving pipeline.

        Not thread-safe: only call between scraping phases, never while workers share the client.
        """
        self._session.close()
        self._session = self._new_session()

    @classmethod
    def from_config(cls, http: HttpConfig) -> HttpClient:
        """Build the one shared, rate-limited client used for the whole run."""
        return cls(
            RateLimiter(rate_per_minute=http.max_requests_per_minute, jitter=http.rate_jitter),
            timeout=http.timeout,
            retries=http.retries,
            backoff_base=http.backoff_base,
            backoff_max=http.backoff_max,
            backoff_jitter=http.backoff_jitter,
            retry_after_cap=http.retry_after_cap,
            pool_size=max(http.search_workers, http.description_workers),
        )

    def close(self) -> None:
        self._session.close()

    def fetch(self, url: str) -> Fetch:
        """Fetch and soup a page, retrying with backoff. The result's ``soup`` is None when it gave up;
        ``gone`` distinguishes a removed resource (404/410) from a transient miss, for callers that care."""
        for attempt in range(1, self._retries + 1):
            self._limiter.acquire()
            try:
                response = self._session.get(url, timeout=self._timeout)
                if response.status_code in THROTTLE_STATUSES:
                    self._handle_throttle(response)
                    raise Throttled(str(response.status_code))
                if response.status_code in GONE_STATUSES:
                    logger.warning(f"HTTP {response.status_code} (gone), not retrying: {url}")
                    return Fetch(None, gone=True)
                if 400 <= response.status_code < 500:
                    # Every remaining 4xx is a verdict on the request, not a hiccup.
                    logger.warning(f"HTTP {response.status_code}, not retrying: {url}")
                    return Fetch(None)
                response.raise_for_status()
                return Fetch(BeautifulSoup(response.content, "html.parser"))
            except Throttled as e:
                # Backoff already served inside _handle_throttle; go straight to retry.
                logger.debug(f"HTTP {e}: retry {attempt}/{self._retries} — {url}")
            except RequestException as e:
                backoff = self._backoff(attempt)
                status = getattr(e.response, "status_code", None)
                label = f"HTTP {status}" if status else type(e).__name__
                logger.debug(f"{label}: retry {attempt}/{self._retries} in {backoff:.1f}s — {url}")
                if status and 500 <= status < 600:
                    # Persistent guest-endpoint 5xx is usually soft-throttling, so ease off too.
                    self._register_pressure()
                time.sleep(backoff)

        logger.warning(f"Couldn't scrape page after {self._retries} attempts: {url}")
        return Fetch(None)

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff capped at ``backoff_max``, plus anti-fingerprint jitter."""
        base = min(self._backoff_max, self._backoff_base**attempt)
        return base + random.uniform(0, self._backoff_jitter)

    def _handle_throttle(self, response: requests.Response) -> None:
        """Honour Retry-After, then slow the whole run down every ``slowdown_every`` throttles.

        Only a 429 carries Retry-After; a 403 or 999 gets the cap, which is the right guess
        for a wall that names no duration of its own.
        """
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                wait = min(float(retry_after), self._retry_after_cap)
            except ValueError:
                wait = self._retry_after_cap  # HTTP-date form: just wait the cap
        else:
            wait = self._retry_after_cap

        logger.warning(f"HTTP {response.status_code} (Throttled) on {response.url} — backing off {wait:.0f}s")
        self._register_pressure()
        time.sleep(wait)

    def _register_pressure(self) -> None:
        """Count one throttle or server error and widen the spacing every ``slowdown_every``."""
        with self._count_lock:
            self._throttle_count += 1
            trip = self._throttle_count % self._slowdown_every == 0
        if trip:
            self._limiter.slow_down(2.0)
