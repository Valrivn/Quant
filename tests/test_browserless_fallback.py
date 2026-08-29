"""Browserless free-local-fallback tests (D-20260818-001 follow-up).

Verifies that when the paid Browserless endpoint is unreachable, the client
falls back to attaching to the local debuggable browser (127.0.0.1:9222) at
zero cost, and does NOT use the fallback when the endpoint is healthy.
"""

import asyncio

import aiohttp
import pytest

from psychological.scrapers.browserless_client import BrowserlessClient


class _ConnRefused:
    """aiohttp.post() raising a connect error (endpoint down)."""

    class _CM:
        async def __aenter__(self):
            raise ConnectionRefusedError("refused")

        async def __aexit__(self, *args):
            return False

    def post(self, *args, **kwargs):
        return self._CM()


class _HealthyResp:
    status = 200

    async def text(self):
        return "<html>HEALTHY</html>"


class _HealthySession:
    class _CM:
        async def __aenter__(self):
            return _HealthyResp()

        async def __aexit__(self, *args):
            return False

    def post(self, *args, **kwargs):
        return self._CM()


def _offline_client() -> BrowserlessClient:
    client = BrowserlessClient({})
    client.cache_enabled = False
    client.cb_enabled = False
    client.metrics_enabled = False
    client._cache = None
    client._metrics = None
    client._circuit_breaker = None
    client._session = _ConnRefused()
    client.max_retries = 1
    return client


class TestLocalFallback:
    def test_endpoint_down_uses_local_nodriver_fallback(self, monkeypatch):
        client = _offline_client()

        async def fake_local(url, timeout=None):
            assert url == "https://www.glassdoor.com/x"
            return "<html>NVDA</html>"

        monkeypatch.setattr(client, "_local_fetch_html", fake_local)
        result = asyncio.run(client.scrape("https://www.glassdoor.com/x"))
        assert result.success is True
        assert result.html == "<html>NVDA</html>"
        assert result.status_code == 200

    def test_fallback_none_returns_failure(self, monkeypatch):
        client = _offline_client()

        async def _none(url, timeout=None):
            return None

        monkeypatch.setattr(client, "_local_fetch_html", _none)
        result = asyncio.run(client.scrape("https://example.com"))
        assert result.success is False
        assert "refused" in result.error

    def test_healthy_endpoint_never_uses_fallback(self, monkeypatch):
        client = BrowserlessClient({})
        client.cache_enabled = False
        client.cb_enabled = False
        client.metrics_enabled = False
        client._cache = None
        client._metrics = None
        client._circuit_breaker = None
        client._session = _HealthySession()

        used = {"local": False}

        async def fake_local(url, timeout=None):
            used["local"] = True
            return "<html>LOCAL</html>"

        monkeypatch.setattr(client, "_local_fetch_html", fake_local)
        result = asyncio.run(client.scrape("https://example.com"))
        assert result.success is True
        assert result.html == "<html>HEALTHY</html>"
        assert used["local"] is False