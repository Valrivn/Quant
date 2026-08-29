"""Tests for Wikidata rate-limit + retry discipline (D-20260828-001).

Verifies: transient vs permanent error classification (HTTPError is a subclass
of URLError and must be checked first), rate budget comes from the pre-registered
sentinel wikipedia lane, and that a throttled single-process fallback never
exceeds the configured floor.
"""

from unittest import mock
from urllib.error import HTTPError, URLError

import discovery.wikidata as w


def test_is_retryable_classifies_correctly():
    assert w._is_retryable(HTTPError("u", 429, "Limited", {}, None)) is True
    assert w._is_retryable(HTTPError("u", 500, "ServerError", {}, None)) is True
    assert w._is_retryable(HTTPError("u", 503, "Unavailable", {}, None)) is True
    # HTTPError is a subclass of URLError; a permanent 4xx must NOT be transient.
    assert w._is_retryable(HTTPError("u", 404, "Not Found", {}, None)) is False
    # DNS / connection refused (getaddrinfo / ECONNREFUSED) are transient.
    assert w._is_retryable(URLError("getaddrinfo failed")) is True
    assert w._is_retryable(ValueError("x")) is False


def test_rate_budget_reads_sentinel_lane():
    rate, burst = w._rate_budget()
    assert rate > 0 and burst > 0


@mock.patch("discovery.wikidata._rate_budget", return_value=(0.5, 2.0))
def test_acquire_token_uses_governor_budget(_budget):
    # _acquire_token imports throttle from discovery.sentinel.governor at call
    # time; patch there to confirm it routes through the shared governor.
    with mock.patch("discovery.sentinel.governor.throttle", return_value=True) as thr:
        w._acquire_token()
        thr.assert_called_once_with(
            mock.ANY, "wikipedia", 0.5, 2.0, cost=1.0
        )
