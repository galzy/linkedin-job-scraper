from unittest.mock import Mock, patch

import pytest
from bs4 import BeautifulSoup
from requests.exceptions import RequestException

from linkedin_scraper.net.http import HttpClient, RateLimiter


def make_client(**overrides):
    """An HttpClient with a fast, no-real-delay config, ready to have its session mocked."""
    limiter = overrides.pop("limiter", None) or RateLimiter(rate_per_minute=6000)  # 0.01s spacing
    defaults = dict(
        timeout=1.0,
        retries=3,
        backoff_base=2.0,
        backoff_max=60.0,
        backoff_jitter=2.0,
        retry_after_cap=120.0,
        pool_size=2,
        slowdown_every=3,
    )
    defaults.update(overrides)
    return HttpClient(limiter, **defaults)


def fake_response(status_code=200, content=b"<html></html>", headers=None):
    resp = Mock()
    resp.status_code = status_code
    resp.content = content
    resp.headers = headers or {}
    resp.raise_for_status = Mock()
    return resp


# --- RateLimiter -------------------------------------------------------------


def test_rate_limiter_slow_down_widens_the_mean_interval():
    limiter = RateLimiter(rate_per_minute=60)  # 1.0s mean
    before = limiter._mean_interval
    limiter.slow_down(2.0)
    assert limiter._mean_interval == pytest.approx(before * 2.0)


def test_rate_limiter_slow_down_stops_at_the_ceiling():
    """Repeated halvings must not pace the run down to a request an hour."""
    limiter = RateLimiter(rate_per_minute=60, max_interval=4.0)  # 1.0s mean
    for _ in range(10):
        limiter.slow_down(2.0)
    assert limiter._mean_interval == pytest.approx(4.0)


# --- Backoff -----------------------------------------------------------------


def test_backoff_is_monotonic_jittered_and_capped():
    client = make_client(backoff_base=2.0, backoff_max=10.0, backoff_jitter=0.0)
    # base ** attempt: 2, 4, 8, 10(capped), 10(capped)
    values = [client._backoff(a) for a in range(1, 6)]
    assert values == [2.0, 4.0, 8.0, 10.0, 10.0]


# --- get(): success path & session reuse -------------------------------------


def test_get_returns_soup_and_reuses_one_session():
    client = make_client()
    session = client._session
    session.get = Mock(return_value=fake_response(content=b"<div>hi</div>"))

    first = client.get("https://example.com/a")
    second = client.get("https://example.com/b")

    assert isinstance(first, BeautifulSoup) and isinstance(second, BeautifulSoup)
    assert client._session is session  # no new session per request
    assert session.get.call_count == 2  # both calls went through the same session


def test_get_gives_up_and_returns_none_after_retries():
    from requests.exceptions import ConnectionError

    client = make_client(retries=3)
    client._session.get = Mock(side_effect=ConnectionError("boom"))
    with patch("linkedin_scraper.net.http.time.sleep"):  # don't actually sleep through backoff
        result = client.get("https://example.com")
    assert result is None
    assert client._session.get.call_count == 3


def test_get_does_not_retry_a_4xx():
    """A 400 never succeeds on retry; spending attempts plus backoff on one only burns
    requests against the rate limit. LinkedIn 400s on start >= 1000."""
    client = make_client(retries=3)
    client._session.get = Mock(return_value=fake_response(400))
    with patch("linkedin_scraper.net.http.time.sleep"):
        result = client.get("https://example.com")
    assert result is None
    assert client._session.get.call_count == 1


@pytest.mark.parametrize("status", [403, 429, 999])
def test_a_throttle_is_retried_despite_sitting_outside_the_retryable_range(status):
    """429 and 403 sit inside the 4xx range and 999 past 599, but none is a verdict — each
    must reach the throttle handler and be retried, not fall into the no-retry path."""
    client = make_client(retries=3, retry_after_cap=0.0)
    client._session.get = Mock(return_value=fake_response(status, headers={}))
    with patch("linkedin_scraper.net.http.time.sleep"):
        assert client.get("https://example.com") is None
    assert client._session.get.call_count == 3
    assert client._throttle_count == 3


def test_a_5xx_is_still_retried_and_logs_its_status():
    client = make_client(retries=3)
    resp = fake_response(503)
    resp.raise_for_status = Mock(side_effect=RequestException("boom", response=resp))
    client._session.get = Mock(return_value=resp)
    with patch("linkedin_scraper.net.http.time.sleep"), patch("linkedin_scraper.net.http.logger.debug") as debug:
        assert client.get("https://example.com") is None
    assert client._session.get.call_count == 3
    assert any("HTTP 503" in call.args[0] for call in debug.call_args_list)


def test_persistent_5xx_slows_the_run_down_like_a_throttle():
    """A 5xx that keeps coming is usually soft-throttling, so it must feed the slow-down."""
    limiter = RateLimiter(rate_per_minute=60)  # 1.0s interval
    client = make_client(limiter=limiter, retries=3, slowdown_every=3)
    resp = fake_response(503)
    resp.raise_for_status = Mock(side_effect=RequestException("boom", response=resp))
    client._session.get = Mock(return_value=resp)
    with patch("linkedin_scraper.net.http.time.sleep"):
        client.get("https://example.com")  # 3 attempts -> trips once
    assert client._throttle_count == 3
    assert limiter._mean_interval == pytest.approx(2.0)


# --- get(): throttle handling ------------------------------------------------


def test_429_honours_retry_after_and_counts():
    client = make_client(retries=1, retry_after_cap=120.0)
    client._session.get = Mock(return_value=fake_response(429, headers={"Retry-After": "7"}))
    with patch("linkedin_scraper.net.http.time.sleep") as slept:
        result = client.get("https://example.com")
    assert result is None
    assert client._throttle_count == 1
    # the honoured Retry-After (7s) was one of the sleeps requested
    assert any(call.args and call.args[0] == 7.0 for call in slept.call_args_list)


def test_a_999_authwall_waits_the_cap_since_it_names_no_duration():
    client = make_client(retries=1, retry_after_cap=30.0)
    client._session.get = Mock(return_value=fake_response(999, headers={}))
    with patch("linkedin_scraper.net.http.time.sleep") as slept:
        assert client.get("https://example.com") is None
    assert any(call.args and call.args[0] == 30.0 for call in slept.call_args_list)


def test_429_retry_after_is_capped():
    client = make_client(retries=1, retry_after_cap=5.0)
    client._session.get = Mock(return_value=fake_response(429, headers={"Retry-After": "999"}))
    with patch("linkedin_scraper.net.http.time.sleep") as slept:
        client.get("https://example.com")
    assert any(call.args and call.args[0] == 5.0 for call in slept.call_args_list)


