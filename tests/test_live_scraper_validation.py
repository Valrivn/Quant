"""
test_live_scraper_validation.py
=================================
LIVE integration tests — actually scrape Glassdoor and G2 for:
  NVDA  (NVIDIA)
  AVGO  (Broadcom)
  INTC  (Intel)
  AMD   (Advanced Micro Devices)

Test flow:
  1. Run the real scraper (curl_cffi + proxy rotation + Cloudflare bypass)
  2. Compare fetched CEO-approval % and company rating against sanity bounds
  3. If the scraper returns hardcoded / stale / out-of-bounds values → emit a
     detailed MISMATCH report and attempt automatic self-heal via Antigravity Daemon

Run:  LIVE_SCRAPE=1 pytest tests/test_live_scraper_validation.py -v -s
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# Guard — only run when LIVE_SCRAPE=1 to avoid CI noise
# ---------------------------------------------------------------------------
LIVE = os.environ.get("LIVE_SCRAPE", "0").strip() == "1"
pytestmark = pytest.mark.skipif(not LIVE, reason="Set LIVE_SCRAPE=1 to run live scraper tests")

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE))

from psychological.scrapers.corp_audit import (
    GlassdoorScraper,
    G2EmployerScraper,
    GlassdoorScore,
    G2EmployerScore,
)

# ---------------------------------------------------------------------------
# Company catalogue
# ---------------------------------------------------------------------------
TARGET_COMPANIES = {
    "NVDA": {
        "glassdoor_slug": "NVIDIA",
        "g2_slug": "nvidia",
        "company_name": "NVIDIA",
        "ceo_approval_min": 60,
        "ceo_approval_max": 100,
        "rating_min": 3.0,
        "rating_max": 5.0,
    },
    "AVGO": {
        "glassdoor_slug": "broadcom",
        "g2_slug": "broadcom",
        "company_name": "Broadcom",
        "ceo_approval_min": 40,
        "ceo_approval_max": 100,
        "rating_min": 2.5,
        "rating_max": 5.0,
    },
    "INTC": {
        "glassdoor_slug": "intel",
        "g2_slug": "intel-corporation",
        "company_name": "Intel",
        "ceo_approval_min": 30,
        "ceo_approval_max": 100,
        "rating_min": 2.5,
        "rating_max": 5.0,
    },
    "AMD": {
        "glassdoor_slug": "advanced-micro-devices",
        "g2_slug": "advanced-micro-devices",
        "company_name": "Advanced Micro Devices",
        "ceo_approval_min": 50,
        "ceo_approval_max": 100,
        "rating_min": 3.0,
        "rating_max": 5.0,
    },
}

# Known-bad sentinel values that indicate hardcoding
HARDCODED_SENTINELS = {
    "ceo_approval": [90, 91, 92, 95, 85, 88, 80],
    "raw_score": [4.5, 4.0, 3.5, 4.25, 4.1],
    "overall_rating": [4.5, 4.25, 4.0, 3.5],
}

MISMATCH_LOG = WORKSPACE / "lane_results" / "live_mismatch.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_sentinel(field: str, value: Optional[float]) -> bool:
    if value is None:
        return False
    sentinels = HARDCODED_SENTINELS.get(field, [])
    return any(abs(value - s) < 0.001 for s in sentinels)


def _log_mismatch(ticker: str, field: str, got: Any, bounds: tuple, sentinel: bool = False):
    MISMATCH_LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ticker": ticker,
        "field": field,
        "got": got,
        "bounds": list(bounds),
        "sentinel_detected": sentinel,
    }
    existing = []
    if MISMATCH_LOG.exists():
        try:
            existing = json.loads(MISMATCH_LOG.read_text())
        except Exception:
            existing = []
    existing.append(record)
    MISMATCH_LOG.write_text(json.dumps(existing, indent=2))
    return record


def _trigger_antigravity(ticker: str, reason: str) -> int:
    """Fire Antigravity Daemon to audit for hardcoding and attempt self-heal."""
    daemon = WORKSPACE / "opencode_scripts" / "antigravity_daemon.py"
    if not daemon.exists():
        print(f"  [ANTIGRAVITY] daemon not found — cannot self-heal {ticker}")
        return -1
    venv_py = WORKSPACE / ".venv" / "bin" / "python"
    py_bin = str(venv_py) if venv_py.exists() else sys.executable
    try:
        result = subprocess.run(
            [py_bin, str(daemon), "--ticker", ticker, "--reason", reason, "--auto-fix"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(WORKSPACE),
        )
        print(f"\n[ANTIGRAVITY] ticker={ticker} exit={result.returncode}")
        if result.stdout:
            print(result.stdout[-3000:])
        if result.stderr:
            print(result.stderr[-500:])
        return result.returncode
    except subprocess.TimeoutExpired:
        print(f"  [ANTIGRAVITY] timed out for {ticker}")
        return -2


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="module")
async def glassdoor_scraper():
    """Real GlassdoorScraper with proxy rotation + Cloudflare bypass."""
    config = {
        "glassdoor": {
            "company_slugs": {
                "NVDA": "nvidia",
                "AVGO": "broadcom",
                "INTC": "intel",
                "AMD": "advanced-micro-devices",
            }
        },
        "proxy": {
            "sources": [
                "https://free-proxy-list.net/",
                "https://www.sslproxies.org/",
                "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all",
                "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
            ],
            "min_proxies": 2,
            "refresh_interval": 600,
        },
    }
    scraper = GlassdoorScraper(config_dict=config)
    await scraper.initialize()
    yield scraper
    await scraper.close()


@pytest_asyncio.fixture(scope="module")
async def g2_scraper():
    """Real G2EmployerScraper with proxy rotation + Cloudflare bypass."""
    config = {
        "glassdoor": {
            "company_slugs": {
                "NVDA": "nvidia",
                "AVGO": "broadcom",
                "INTC": "intel-corporation",
                "AMD": "advanced-micro-devices",
            }
        },
        "proxy": {
            "sources": [
                "https://free-proxy-list.net/",
                "https://www.sslproxies.org/",
                "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all",
                "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
            ],
            "min_proxies": 2,
            "refresh_interval": 600,
        },
    }
    scraper = G2EmployerScraper(config_dict=config)
    await scraper.initialize()
    yield scraper
    await scraper.close()


# ---------------------------------------------------------------------------
# Glassdoor tests — CEO approval + company rating
# ---------------------------------------------------------------------------

class TestGlassdoorLive:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ticker", list(TARGET_COMPANIES.keys()))
    async def test_glassdoor_rating_live(self, glassdoor_scraper, ticker):
        meta = TARGET_COMPANIES[ticker]
        print(f"\n[LIVE] Scraping Glassdoor for {ticker} ({meta['company_name']})...")

        result: GlassdoorScore = await glassdoor_scraper.scrape_company(ticker)

        assert isinstance(result, GlassdoorScore), f"Expected GlassdoorScore, got {type(result)}"
        assert result.ticker == ticker

        failures = []
        if result.raw_score is None:
            failures.append(f"raw_score is None — Glassdoor scraper returned no data for {ticker}")
        else:
            if not (meta["rating_min"] <= result.raw_score <= meta["rating_max"]):
                rec = _log_mismatch(ticker, "raw_score", result.raw_score,
                                    (meta["rating_min"], meta["rating_max"]))
                failures.append(
                    f"raw_score {result.raw_score} outside [{meta['rating_min']}, {meta['rating_max']}]"
                )

            if _is_sentinel("raw_score", result.raw_score):
                rec = _log_mismatch(ticker, "raw_score", result.raw_score,
                                    (meta["rating_min"], meta["rating_max"]), sentinel=True)
                failures.append(
                    f"raw_score {result.raw_score} matches a known hardcoded sentinel! "
                    "Scraper may be returning static mock data."
                )

        if failures:
            exit_code = _trigger_antigravity(ticker, "; ".join(failures))
            pytest.fail(
                f"[{ticker}] Glassdoor rating failures (antigravity exit={exit_code}):\n"
                + "\n".join(f"  * {f}" for f in failures)
            )

        print(f"  OK {ticker} GD rating={result.raw_score}/5.0 "
              f"reviews={result.review_count} ceo_approval={result.ceo_approval}%")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ticker", list(TARGET_COMPANIES.keys()))
    async def test_glassdoor_ceo_approval_live(self, glassdoor_scraper, ticker):
        meta = TARGET_COMPANIES[ticker]
        print(f"\n[LIVE] CEO approval check for {ticker}...")

        result: GlassdoorScore = await glassdoor_scraper.scrape_company(ticker)

        if result.ceo_approval is None:
            print(f"  WARN {ticker}: ceo_approval not found in scraped page (may require auth)")
            return

        failures = []
        if not (meta["ceo_approval_min"] <= result.ceo_approval <= meta["ceo_approval_max"]):
            rec = _log_mismatch(ticker, "ceo_approval", result.ceo_approval,
                                (meta["ceo_approval_min"], meta["ceo_approval_max"]))
            failures.append(
                f"ceo_approval {result.ceo_approval}% outside "
                f"[{meta['ceo_approval_min']}, {meta['ceo_approval_max']}]"
            )

        if _is_sentinel("ceo_approval", result.ceo_approval):
            rec = _log_mismatch(ticker, "ceo_approval", result.ceo_approval,
                                (meta["ceo_approval_min"], meta["ceo_approval_max"]),
                                sentinel=True)
            failures.append(
                f"ceo_approval {result.ceo_approval}% matches known hardcoded sentinel!"
            )

        if failures:
            exit_code = _trigger_antigravity(ticker, "; ".join(failures))
            pytest.fail(
                f"[{ticker}] CEO approval failures (antigravity exit={exit_code}):\n"
                + "\n".join(f"  * {f}" for f in failures)
            )

        print(f"  OK {ticker} CEO approval={result.ceo_approval}%")


# ---------------------------------------------------------------------------
# G2 rating tests
# ---------------------------------------------------------------------------

class TestG2Live:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ticker", list(TARGET_COMPANIES.keys()))
    async def test_g2_rating_live(self, g2_scraper, ticker):
        meta = TARGET_COMPANIES[ticker]
        print(f"\n[LIVE] Scraping G2 for {ticker} ({meta['company_name']})...")

        result: G2EmployerScore = await g2_scraper.scrape_company(ticker)

        assert isinstance(result, G2EmployerScore)
        assert result.ticker == ticker

        failures = []
        if result.overall_rating is None:
            failures.append(f"overall_rating is None — G2 scraper returned no data for {ticker}")
        else:
            if not (meta["rating_min"] <= result.overall_rating <= meta["rating_max"]):
                rec = _log_mismatch(ticker, "overall_rating", result.overall_rating,
                                    (meta["rating_min"], meta["rating_max"]))
                failures.append(
                    f"G2 overall_rating {result.overall_rating} outside "
                    f"[{meta['rating_min']}, {meta['rating_max']}]"
                )

            if _is_sentinel("overall_rating", result.overall_rating):
                rec = _log_mismatch(ticker, "overall_rating", result.overall_rating,
                                    (meta["rating_min"], meta["rating_max"]), sentinel=True)
                failures.append(
                    f"G2 overall_rating {result.overall_rating} matches hardcoded sentinel!"
                )

        if failures:
            exit_code = _trigger_antigravity(ticker, "; ".join(failures))
            pytest.fail(
                f"[{ticker}] G2 rating failures (antigravity exit={exit_code}):\n"
                + "\n".join(f"  * {f}" for f in failures)
            )

        print(f"  OK {ticker} G2 rating={result.overall_rating}/5.0 "
              f"reviews={result.review_count} recommend={result.would_recommend_pct}%")


# ---------------------------------------------------------------------------
# Cross-source consistency (Glassdoor vs G2)
# ---------------------------------------------------------------------------

class TestCrossSourceConsistency:
    """
    Glassdoor and G2 ratings for the same company shouldn't diverge by more
    than 1.5 points. Large divergence => one source may be returning stale data.
    """

    MAX_DIVERGENCE = 1.5

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ticker", list(TARGET_COMPANIES.keys()))
    async def test_cross_source_divergence(self, glassdoor_scraper, g2_scraper, ticker):
        print(f"\n[LIVE] Cross-source divergence check for {ticker}...")

        gd: GlassdoorScore = await glassdoor_scraper.scrape_company(ticker)
        g2: G2EmployerScore = await g2_scraper.scrape_company(ticker)

        if gd.raw_score is None or g2.overall_rating is None:
            print(f"  WARN {ticker}: one or both sources returned None — skipping divergence")
            return

        divergence = abs(gd.raw_score - g2.overall_rating)
        print(f"  {ticker}: GD={gd.raw_score:.2f}  G2={g2.overall_rating:.2f}  delta={divergence:.2f}")

        if divergence > self.MAX_DIVERGENCE:
            rec = _log_mismatch(ticker, "cross_source_divergence", divergence, (0, self.MAX_DIVERGENCE))
            exit_code = _trigger_antigravity(
                ticker,
                f"Cross-source divergence {divergence:.2f} > {self.MAX_DIVERGENCE}: "
                f"GD={gd.raw_score} G2={g2.overall_rating}"
            )
            pytest.fail(
                f"[{ticker}] GD={gd.raw_score} vs G2={g2.overall_rating} diverge by {divergence:.2f} "
                f"(max={self.MAX_DIVERGENCE}) — antigravity exit={exit_code}"
            )

        print(f"  OK {ticker} cross-source divergence {divergence:.2f} within tolerance")


# ---------------------------------------------------------------------------
# Hardcoding detection — static AST/regex scan of corp_audit.py
# ---------------------------------------------------------------------------

class TestAntigravityHardcodingGuard:
    """
    Antigravity Daemon's static analysis gate:
    corp_audit.py must NOT contain hardcoded numeric literals in return paths.
    """

    SCRAPER_FILE = WORKSPACE / "psychological" / "scrapers" / "corp_audit.py"

    FORBIDDEN_PATTERNS = [
        (r'ceo_approval\s*=\s*\d{2,3}(?!\s*[\*\+\-/])', "Hardcoded ceo_approval literal"),
        (r'raw_score\s*=\s*\d+\.\d+(?!\s*[\*\+\-/])', "Hardcoded raw_score literal"),
        (r'overall_rating\s*=\s*\d+\.\d+(?!\s*[\*\+\-/])', "Hardcoded overall_rating literal"),
        (r'recommend_to_friend\s*=\s*\d{2,3}(?!\s*[\*\+\-/])', "Hardcoded recommend literal"),
    ]

    REQUIRED_PATTERNS = [
        (r"BeautifulSoup", "HTML parser (BeautifulSoup)"),
        (r"re\.search", "regex parser (re.search)"),
        (r"_parse_score|_parse_rating", "parse helper functions"),
        (r"curl_cffi|AsyncSession", "curl_cffi session"),
        (r"proxy", "proxy rotation"),
    ]

    def test_no_hardcoded_scraper_values(self):
        src = self.SCRAPER_FILE.read_text()
        violations = []

        for pattern, label in self.FORBIDDEN_PATTERNS:
            for lineno, line in enumerate(src.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#") or "mock" in stripped.lower() or "test" in stripped.lower():
                    continue
                if re.search(pattern, line, re.IGNORECASE):
                    violations.append(f"  Line {lineno}: [{label}] -> {stripped[:80]}")

        if violations:
            exit_code = _trigger_antigravity(
                "ALL",
                f"Static scan: {len(violations)} hardcoded value violations in corp_audit.py"
            )
            pytest.fail(
                f"Hardcoded values detected in corp_audit.py "
                f"(antigravity daemon exit={exit_code}):\n"
                + "\n".join(violations)
            )

    def test_scraper_uses_dynamic_parsing(self):
        src = self.SCRAPER_FILE.read_text()
        missing = []
        for pat, label in self.REQUIRED_PATTERNS:
            if not re.search(pat, src):
                missing.append(label)

        if missing:
            pytest.fail(
                f"corp_audit.py is missing critical dynamic-parsing constructs:\n"
                + "\n".join(f"  * {m}" for m in missing)
                + "\nThis suggests the scraping logic was replaced with stubs."
            )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    os.environ["LIVE_SCRAPE"] = "1"
    pytest.main([__file__, "-v", "-s", "--tb=short"])
