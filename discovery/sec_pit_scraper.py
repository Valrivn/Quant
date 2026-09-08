"""SEC EDGAR PIT Fundamentals Scraper — comprehensive async collection.

Fetches 10-K/10-Q filing history and companyfacts XBRL data for a roster of
tickers, back to 1999.  Builds point-in-time fundamental tables where every
value traces to a specific SEC filing (accession number + filing date).

Rate limit: 10 req/s (SEC policy).  Retry-After respected.
Checkpointing: per-ticker JSON files allow resume after interruption.

Usage:
    python -m discovery.sec_pit_scraper [--tickers AAPL,MSFT] [--test-aapl]
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

import aiohttp

from discovery.xbrl_parser import (
    FilingRecord,
    extract_all_filings_from_companyfacts,
    coverage_summary,
)

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
USER_AGENT = "QuantResearch/1.0 (research@example.com)"
RATE_LIMIT_RPS = 9  # Stay below 10/sec SEC limit
MIN_DELAY = 1.0 / RATE_LIMIT_RPS

DATA_DIR = Path("data/pit_fundamentals")
CHECKPOINT_DIR = Path("data/checkpoints/pit_fundamentals")

# Filing types we collect
TARGET_FORMS: Set[str] = {
    "10-K", "10-K/A", "10-Q", "10-Q/A",
    "10-K405", "10-KT", "10-KT/A",
}

# Date range
DATE_MIN = "1999-01-01"
DATE_MAX = "2026-09-06"


@dataclass
class TickerState:
    """Mutable checkpoint state for one ticker."""
    ticker: str
    cik: str
    submissions_fetched: bool = False
    companyfacts_fetched: bool = False
    filings_indexed: int = 0
    filings_parsed: int = 0
    filings_total: int = 0
    errors: List[str] = field(default_factory=list)
    first_filing_date: Optional[str] = None
    last_filing_date: Optional[str] = None
    coverage: Optional[dict] = None


class SECRateLimiter:
    """Async rate limiter for SEC EDGAR (10 req/s max)."""
    
    def __init__(self, max_rps: float = RATE_LIMIT_RPS):
        self._min_interval = 1.0 / max_rps
        self._last_request = 0.0
        self._lock = asyncio.Lock()
    
    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            wait = self._min_interval - (now - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()


class SECEdgarScraper:
    """Async SEC EDGAR scraper with checkpointing."""

    def __init__(
        self,
        tickers_ciks: Dict[str, str],
        rate_limit: float = RATE_LIMIT_RPS,
    ):
        """
        Args:
            tickers_ciks: {ticker: CIK} mapping (will be zero-padded to 10 digits)
            rate_limit: max requests per second
        """
        # Zero-pad CIKs to 10 digits for SEC API
        self.tickers_ciks = {t: cik.zfill(10) for t, cik in tickers_ciks.items()}
        self.limiter = SECRateLimiter(rate_limit)
        self.states: Dict[str, TickerState] = {
            t: TickerState(ticker=t, cik=cik.zfill(10))
            for t, cik in tickers_ciks.items()
        }
        # Ensure directories exist
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)

    def _checkpoint_path(self, ticker: str) -> Path:
        return CHECKPOINT_DIR / f"{ticker}_checkpoint.json"
    
    def _ticker_dir(self, ticker: str) -> Path:
        d = DATA_DIR / ticker
        d.mkdir(parents=True, exist_ok=True)
        return d
    
    def _save_checkpoint(self, ticker: str):
        state = self.states[ticker]
        cp = {
            "ticker": state.ticker,
            "cik": state.cik,
            "submissions_fetched": state.submissions_fetched,
            "companyfacts_fetched": state.companyfacts_fetched,
            "filings_indexed": state.filings_indexed,
            "filings_parsed": state.filings_parsed,
            "filings_total": state.filings_total,
            "errors": state.errors[-20:],  # keep last 20
            "first_filing_date": state.first_filing_date,
            "last_filing_date": state.last_filing_date,
        }
        self._checkpoint_path(ticker).write_text(
            json.dumps(cp, indent=2), encoding="utf-8")
    
    def _load_checkpoint(self, ticker: str) -> Optional[dict]:
        p = self._checkpoint_path(ticker)
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return None
        return None
    
    async def _fetch_json(
        self, session: aiohttp.ClientSession, url: str, label: str
    ) -> Optional[dict]:
        """Fetch JSON from SEC with rate limiting, retries, and 429 handling."""
        for attempt in range(5):
            await self.limiter.acquire()
            try:
                async with session.get(
                    url,
                    headers={"User-Agent": USER_AGENT},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    elif resp.status == 429:
                        retry_after = resp.headers.get("Retry-After", "5")
                        wait = min(float(retry_after), 30)
                        logger.warning("429 rate limit on %s, waiting %.1fs", label, wait)
                        await asyncio.sleep(wait)
                    elif resp.status == 404:
                        logger.debug("404 for %s", label)
                        return None
                    else:
                        text = await resp.text()
                        logger.warning("HTTP %d on %s: %s", resp.status, label, text[:200])
                        await asyncio.sleep(2 ** attempt)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning("Network error on %s (attempt %d): %s",
                               label, attempt + 1, exc)
                await asyncio.sleep(2 ** attempt)
        return None
    
    async def fetch_submissions(
        self, session: aiohttp.ClientSession, ticker: str
    ) -> List[dict]:
        """Fetch and parse the submissions index for a ticker.
        
        Includes older archive files (CIK{cik}-submissions-NNN.json) to get
        full filing history back to the 1990s.
        
        Returns list of filing dicts: {accession, form, filed, fiscal_end}
        """
        state = self.states[ticker]
        cik = state.cik
        tdir = self._ticker_dir(ticker)
        filings_file = tdir / "filings_index.json"
        
        # Use cache if already fetched
        if state.submissions_fetched and filings_file.exists():
            try:
                return json.loads(filings_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        
        # Step 1: Fetch main submissions index
        url = SUBMISSIONS_URL.format(cik=cik)
        data = await self._fetch_json(session, url, f"{ticker}/submissions")
        if data is None:
            state.errors.append(f"submissions fetch failed for {ticker}")
            self._save_checkpoint(ticker)
            return []
        
        filings = self._parse_recent_submissions(data)
        
        # Step 2: Fetch older archive files for full history
        older_groups = data.get("filings", {}).get("files", [])
        for group in older_groups:
            name = group.get("name", "")
            if not name:
                continue
            older_url = f"https://data.sec.gov/submissions/{name}"
            older_data = await self._fetch_json(
                session, older_url, f"{ticker}/submissions/{name}")
            if older_data is not None:
                older_filings = self._parse_recent_submissions(older_data)
                filings.extend(older_filings)
                logger.info("  Fetched older archive %s: %d filings",
                           name, len(older_filings))
        
        # Deduplicate by accession (same filing can appear in recent + archive)
        seen = set()
        unique = []
        for f in filings:
            if f["accession"] not in seen:
                seen.add(f["accession"])
                unique.append(f)
        filings = unique
        
        # Sort by filed date (oldest first)
        filings.sort(key=lambda f: f["filed"])
        
        # Cache
        filings_file.write_text(json.dumps(filings, indent=2), encoding="utf-8")
        state.submissions_fetched = True
        state.filings_total = len(filings)
        if filings:
            state.first_filing_date = filings[0]["filed"]
            state.last_filing_date = filings[-1]["filed"]
        self._save_checkpoint(ticker)
        return filings
    
    def _parse_recent_submissions(self, data: dict) -> List[dict]:
        """Parse a submissions JSON (recent or older archive) into filing records.
        
        Both the main submissions index and the older archive files share the
        same flat structure: arrays under data['filings']['recent'] or directly
        under the top-level keys for older archives.
        """
        # Main submissions index stores recent under filings.recent
        # Older archives store them directly at top level
        recent = data.get("filings", {}).get("recent", {})
        if not recent:
            # Older archive format: flat keys at top level
            if "form" in data:
                recent = data
            else:
                return []
        
        forms = recent.get("form", [])
        accessions = recent.get("accessionNumber", [])
        filed_dates = recent.get("filingDate", [])
        primary_docs = recent.get("primaryDocument", [])
        
        filings = []
        for i, form in enumerate(forms):
            if form not in TARGET_FORMS:
                continue
            if i >= len(accessions) or i >= len(filed_dates):
                continue
            
            filed = filed_dates[i]
            if filed < DATE_MIN or filed > DATE_MAX:
                continue
            
            accession = accessions[i]
            accession_clean = accession.replace("-", "")
            
            filings.append({
                "accession": accession,
                "accession_clean": accession_clean,
                "form": form,
                "filed": filed,
                "primary_doc": primary_docs[i] if i < len(primary_docs) else "",
            })
        
        return filings
    
    async def fetch_companyfacts(
        self, session: aiohttp.ClientSession, ticker: str
    ) -> Optional[dict]:
        """Fetch companyfacts XBRL data (contains ALL historical facts)."""
        state = self.states[ticker]
        cik = state.cik
        tdir = self._ticker_dir(ticker)
        cache_file = tdir / "companyfacts.json"
        
        # Use cache if already fetched
        if state.companyfacts_fetched and cache_file.exists():
            try:
                return json.loads(cache_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        
        url = COMPANYFACTS_URL.format(cik=cik)
        data = await self._fetch_json(session, url, f"{ticker}/companyfacts")
        if data is None:
            state.errors.append(f"companyfacts fetch failed for {ticker}")
            self._save_checkpoint(ticker)
            return None
        
        # Cache raw companyfacts
        cache_file.write_text(json.dumps(data), encoding="utf-8")
        state.companyfacts_fetched = True
        self._save_checkpoint(ticker)
        return data
    
    def _enrich_filings_with_fiscal_ends(
        self, filings: List[dict], companyfacts: dict
    ) -> List[dict]:
        """Add fiscal_end dates to filings from companyfacts metadata.
        
        SEC submissions don't always include period-of-report dates.
        We extract them from companyfacts entries grouped by accession.
        """
        # Build accession -> fiscal_end from companyfacts
        accn_to_ends: Dict[str, Set[str]] = {}
        for ns_name in ("us-gaap", "ifrs-full"):
            ns = companyfacts.get("facts", {}).get(ns_name, {})
            for concept, cdata in ns.items():
                units = cdata.get("units", {})
                for entries in units.values():
                    if not isinstance(entries, list):
                        continue
                    for entry in entries:
                        accn = entry.get("accn", "")
                        end = entry.get("end", "")
                        if accn and end:
                            accn_to_ends.setdefault(accn, set()).add(end)
        
        enriched = []
        for filing in filings:
            accn = filing["accession"]
            ends = accn_to_ends.get(accn, set())
            if ends:
                # Pick the latest end date (most logical for multi-period entries)
                filing["fiscal_end"] = max(ends)
            else:
                # Fallback: use filed date as fiscal_end
                filing["fiscal_end"] = filing["filed"]
            enriched.append(filing)
        
        return enriched
    
    async def process_ticker(
        self, session: aiohttp.ClientSession, ticker: str
    ) -> List[FilingRecord]:
        """Full pipeline for one ticker: submissions → companyfacts → parse."""
        state = self.states[ticker]
        tdir = self._ticker_dir(ticker)
        parsed_file = tdir / "parsed_filings.json"
        
        # If already fully parsed, load from cache
        if state.filings_parsed > 0 and parsed_file.exists():
            try:
                cached = json.loads(parsed_file.read_text(encoding="utf-8"))
                logger.info("Loaded %d cached parsed filings for %s",
                           len(cached), ticker)
                # Reconstruct FilingRecord objects
                records = []
                for d in cached:
                    rec = FilingRecord(
                        ticker=d["ticker"], accession=d["accession"],
                        form=d["form"], filed=d["filed"],
                        fiscal_end=d["fiscal_end"],
                        facts={k: v for k, v in d.get("facts", {}).items()
                               if v is not None},
                        concepts_used=d.get("concepts_used", {}),
                        missing=d.get("missing", []),
                    )
                    records.append(rec)
                return records
            except Exception:
                pass
        
        logger.info("Processing %s (CIK=%s)", ticker, state.cik)
        
        # Step 1: Fetch submissions
        filings = await self.fetch_submissions(session, ticker)
        if not filings:
            logger.warning("No filings found for %s", ticker)
            return []
        logger.info("  Found %d filings for %s", len(filings), ticker)
        
        # Step 2: Fetch companyfacts
        companyfacts = await self.fetch_companyfacts(session, ticker)
        if companyfacts is None:
            logger.warning("No companyfacts for %s", ticker)
            return []
        
        # Step 3: Enrich with fiscal_end dates
        filings = self._enrich_filings_with_fiscal_ends(filings, companyfacts)
        state.filings_indexed = len(filings)
        self._save_checkpoint(ticker)
        
        # Step 4: Extract XBRL facts for each filing
        records = extract_all_filings_from_companyfacts(
            companyfacts, ticker, filings)
        state.filings_parsed = len(records)
        
        # Step 5: Save parsed results
        parsed_data = []
        for rec in records:
            parsed_data.append({
                "ticker": rec.ticker,
                "accession": rec.accession,
                "form": rec.form,
                "filed": rec.filed,
                "fiscal_end": rec.fiscal_end,
                "facts": rec.facts,
                "concepts_used": rec.concepts_used,
                "missing": rec.missing,
            })
        parsed_file.write_text(
            json.dumps(parsed_data, indent=2), encoding="utf-8")
        
        # Step 6: Coverage summary
        cov = coverage_summary(records)
        state.coverage = cov
        self._save_checkpoint(ticker)
        
        logger.info("  Parsed %d filings for %s — coverage: %s",
                    len(records), ticker,
                    {k: v["pct"] for k, v in cov.get("coverage", {}).items()})
        
        return records
    
    async def run_all(
        self, tickers: Optional[List[str]] = None
    ) -> Dict[str, List[FilingRecord]]:
        """Run the full scrape for all tickers."""
        targets = tickers or list(self.tickers_ciks.keys())
        results: Dict[str, List[FilingRecord]] = {}
        
        connector = aiohttp.TCPConnector(limit=RATE_LIMIT_RPS, force_close=True)
        async with aiohttp.ClientSession(connector=connector) as session:
            for ticker in targets:
                if ticker not in self.tickers_ciks:
                    logger.warning("Ticker %s not in CIK map, skipping", ticker)
                    continue
                try:
                    records = await self.process_ticker(session, ticker)
                    results[ticker] = records
                except Exception as exc:
                    logger.error("Failed to process %s: %s", ticker, exc)
                    self.states[ticker].errors.append(str(exc))
                    self._save_checkpoint(ticker)
                    results[ticker] = []
        
        return results
    
    def build_pit_table(
        self, results: Dict[str, List[FilingRecord]]
    ) -> List[dict]:
        """Consolidate all ticker results into a flat PIT table.
        
        Each row: as_of (= filing date), ticker, field, value.
        This is the format needed for backtesting: given a decision date,
        filter rows where as_of <= decision_date.
        """
        rows = []
        for ticker, records in results.items():
            for rec in records:
                for field_name in ("revenue", "ebit", "tax_rate", "capex",
                                   "depreciation", "change_nwc", "rd_expense",
                                   "total_debt", "cash", "shares_outstanding"):
                    val = rec.facts.get(field_name)
                    if val is not None:
                        rows.append({
                            "as_of": rec.filed,
                            "ticker": ticker,
                            "accession": rec.accession,
                            "form": rec.form,
                            "fiscal_end": rec.fiscal_end,
                            "field": field_name,
                            "value": val,
                            "concept": rec.concepts_used.get(field_name, ""),
                        })
        return rows
    
    def save_pit_parquet(
        self, results: Dict[str, List[FilingRecord]]
    ) -> Path:
        """Save the consolidated PIT table as Parquet."""
        import pandas as pd
        
        rows = self.build_pit_table(results)
        if not rows:
            logger.warning("No data to save as Parquet")
            return DATA_DIR / "pit_fundamentals.parquet"
        
        df = pd.DataFrame(rows)
        df["as_of"] = pd.to_datetime(df["as_of"])
        df["fiscal_end"] = pd.to_datetime(df["fiscal_end"])
        df = df.sort_values(["ticker", "as_of", "field"]).reset_index(drop=True)
        
        out_path = DATA_DIR / "pit_fundamentals.parquet"
        df.to_parquet(out_path, index=False)
        logger.info("Saved PIT table: %d rows to %s", len(df), out_path)
        return out_path
    
    def print_summary(self, results: Dict[str, List[FilingRecord]]):
        """Print collection summary."""
        print("\n" + "=" * 72)
        print("SEC EDGAR PIT Fundamentals Collection Summary")
        print("=" * 72)
        total_filings = 0
        total_rows = 0
        for ticker, records in sorted(results.items()):
            state = self.states[ticker]
            n = len(records)
            total_filings += n
            cov = state.coverage or {}
            avg_pct = 0
            if cov.get("coverage"):
                avg_pct = sum(v["pct"] for v in cov["coverage"].values()) / len(cov["coverage"])
            earliest = state.first_filing_date or "?"
            latest = state.last_filing_date or "?"
            err_count = len(state.errors)
            print(f"  {ticker:6s}: {n:4d} filings | "
                  f"{earliest} -> {latest} | "
                  f"avg coverage {avg_pct:.0f}% | "
                  f"errors {err_count}")
            total_rows += sum(
                len(rec.facts) for rec in records)
        
        print("-" * 72)
        print(f"  TOTAL: {total_filings} filings, {total_rows} fact values")
        print("=" * 72)


# ── Default 27-ticker roster ────────────────────────────────────────────────
# Major S&P 500 names with long SEC filing histories (back to 1999).
# GOOGL IPO'd in 2004; most others have 25+ years of 10-K filings.
DEFAULT_ROSTER = [
    "AAPL", "MSFT", "AMZN", "GOOGL", "META", "NVDA",
    "JPM", "JNJ", "WMT", "PG", "V", "MA",
    "UNH", "HD", "BAC", "XOM", "PFE", "CSCO",
    "ORCL", "INTC", "AMD", "T", "KO", "PEP",
    "MRK", "ABT", "CVX",
]


