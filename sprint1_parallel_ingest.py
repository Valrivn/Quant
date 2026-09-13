"""
Sprint 1 Parallel Data Ingestion — Graceful Shutdown, Checkpoint/Resume
Runs all Green-tier sources concurrently with rate limiting and interrupt handling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
import aiohttp
import aiofiles

# Import after path setup
sys.path.insert(0, str(Path(__file__).parent))
from config.logging_config import init_logging

init_logging("INFO")

logger = logging.getLogger(__name__)

# ============================================================
# CHECKPOINT / STATE MANAGEMENT
# ============================================================

STATE_DIR = Path("data/checkpoints/sprint1")
STATE_DIR.mkdir(parents=True, exist_ok=True)

@dataclass
class SourceState:
    source: str
    status: str = "pending"  # pending | running | done | failed | stopped
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    items_processed: int = 0
    items_total: Optional[int] = None
    error: Optional[str] = None
    checkpoint: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> Dict:
        return {
            "source": self.source,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "items_processed": self.items_processed,
            "items_total": self.items_total,
            "error": self.error,
            "checkpoint": self.checkpoint,
        }

    @classmethod
    def from_json(cls, data: Dict) -> "SourceState":
        return cls(**data)


class CheckpointManager:
    """Manages per-source checkpoints for resume capability."""
    
    def __init__(self, state_dir: Path = STATE_DIR):
        self.state_dir = state_dir
        self.states: Dict[str, SourceState] = {}
        self._load_all()
    
    def _load_all(self) -> None:
        for f in self.state_dir.glob("*.json"):
            try:
                with open(f) as fp:
                    data = json.load(fp)
                self.states[data["source"]] = SourceState.from_json(data)
            except Exception as e:
                logger.warning(f"Failed to load checkpoint {f}: {e}")
    
    def get(self, source: str) -> SourceState:
        if source not in self.states:
            self.states[source] = SourceState(source=source)
        return self.states[source]
    
    def save(self, source: str) -> None:
        state = self.states[source]
        f = self.state_dir / f"{source}.json"
        with open(f, "w") as fp:
            json.dump(state.to_json(), fp, indent=2)
    
    def mark_started(self, source: str, total: Optional[int] = None) -> None:
        state = self.get(source)
        state.status = "running"
        state.started_at = datetime.utcnow().isoformat()
        state.items_total = total
        self.save(source)
    
    def mark_progress(self, source: str, processed: int, checkpoint: Optional[Dict] = None) -> None:
        state = self.get(source)
        state.items_processed = processed
        if checkpoint:
            state.checkpoint = checkpoint
        self.save(source)
    
    def mark_done(self, source: str) -> None:
        state = self.get(source)
        state.status = "done"
        state.completed_at = datetime.utcnow().isoformat()
        self.save(source)
    
    def mark_failed(self, source: str, error: str) -> None:
        state = self.get(source)
        state.status = "failed"
        state.error = error
        state.completed_at = datetime.utcnow().isoformat()
        self.save(source)
    
    def mark_stopped(self, source: str) -> None:
        state = self.get(source)
        state.status = "stopped"
        state.completed_at = datetime.utcnow().isoformat()
        self.save(source)
    
    def get_pending_sources(self, all_sources: List[str]) -> List[str]:
        """Return sources that need to run (pending or stopped)."""
        result = []
        for src in all_sources:
            state = self.states.get(src)
            if state is None or state.status in ("pending", "stopped", "failed"):
                result.append(src)
        return result


# ============================================================
# GRACEFUL SHUTDOWN HANDLING
# ============================================================

class ShutdownManager:
    """Handles SIGINT/SIGTERM for graceful shutdown."""
    
    def __init__(self):
        self.shutdown_requested = False
        self.shutdown_callbacks: List[Callable[[], None]] = []
        self._original_sigint = signal.getsignal(signal.SIGINT)
        self._original_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
    
    def _handle_signal(self, signum, frame):
        if self.shutdown_requested:
            logger.warning("Force shutdown requested — exiting immediately")
            sys.exit(1)
        
        logger.info("Shutdown signal received — finishing current items, then stopping...")
        self.shutdown_requested = True
        for cb in self.shutdown_callbacks:
            try:
                cb()
            except Exception as e:
                logger.error(f"Shutdown callback failed: {e}")
    
    def register_callback(self, cb: Callable[[], None]) -> None:
        self.shutdown_callbacks.append(cb)
    
    def check_shutdown(self) -> bool:
        return self.shutdown_requested
    
    def restore_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._original_sigint)
        signal.signal(signal.SIGTERM, self._original_sigterm)


# ============================================================
# RATE LIMITERS
# ============================================================

class RateLimiter:
    """Token bucket rate limiter with async support."""
    
    def __init__(self, rate: float, burst: int = 1):
        self.rate = rate  # tokens per second
        self.burst = burst
        self.tokens = burst
        self.last_update = time.monotonic()
        self._lock = asyncio.Lock()
    
    async def acquire(self, tokens: int = 1) -> None:
        async with self._lock:
            while self.tokens < tokens:
                now = time.monotonic()
                elapsed = now - self.last_update
                self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
                self.last_update = now
                if self.tokens < tokens:
                    wait_time = (tokens - self.tokens) / self.rate
                    await asyncio.sleep(wait_time)
                    now = time.monotonic()
                    elapsed = now - self.last_update
                    self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
                    self.last_update = now
            self.tokens -= tokens


# ============================================================
# SOURCE INGESTION TASKS
# ============================================================

checkpoint_mgr = CheckpointManager()
shutdown_mgr = ShutdownManager()

# Rate limiters per source (conservative defaults)
RATE_LIMITERS = {
    "google_trends": RateLimiter(rate=0.5, burst=2),      # ~1 req per 2 sec
    "gdelt": RateLimiter(rate=2.0, burst=5),               # ~2 req/sec
    "gh_archive": RateLimiter(rate=10.0, burst=20),        # ~10 req/sec
    "cdxj": RateLimiter(rate=1.0, burst=2),                # ~1 req/sec (Wayback is strict)
    "edgar_companyfacts": RateLimiter(rate=5.0, burst=10),
    "edgar_13f": RateLimiter(rate=2.0, burst=5),
    "kaggle_analyst": RateLimiter(rate=0.1, burst=1),      # manual download
    "ken_french": RateLimiter(rate=1.0, burst=2),
    "yfinance": RateLimiter(rate=10.0, burst=20),
}

# Ticker universe (27 master tickers from experiment)
MASTER_TICKERS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA",
    "JPM", "V", "JNJ", "WMT", "PG", "MA", "UNH", "HD",
    "DIS", "PYPL", "ADBE", "NFLX", "CRM", "INTC", "CSCO",
    "PFE", "TMO", "ABBV", "ACN", "COST"
]

async def ingest_google_trends(checkpoint_mgr: CheckpointManager, shutdown_mgr: ShutdownManager):
    """Google Trends search interest — delegates to real backfill module."""
    from discovery.google_trends_backfill import ingest_google_trends_real
    await ingest_google_trends_real(checkpoint_mgr, shutdown_mgr)


async def ingest_gdelt(checkpoint_mgr: CheckpointManager, shutdown_mgr: ShutdownManager):
    """GDELT brand/product mention tone — downloads masterfilelist and sample archives."""
    source = "gdelt"
    state = checkpoint_mgr.get(source)
    
    out_dir = Path("data/source/gdelt")
    out_dir.mkdir(parents=True, exist_ok=True)
    
    checkpoint_mgr.mark_started(source, total=10)
    master_url = "http://data.gdeltproject.org/gdeltv2/masterfilelist.txt"
    master_file = out_dir / "masterfilelist.txt"
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(master_url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status == 200:
                    content = await resp.text()
                    async with aiofiles.open(master_file, "w", encoding="utf-8") as f:
                        await f.write(content)
                    
                    # Parse top 5 export zip URLs from masterfilelist
                    urls = []
                    for line in content.splitlines():
                        if ".export.CSV.zip" in line:
                            parts = line.strip().split()
                            if len(parts) >= 3:
                                urls.append(parts[2])
                        if len(urls) >= 5:
                            break
                    
                    processed = 1
                    for idx, url in enumerate(urls, start=1):
                        if shutdown_mgr.check_shutdown():
                            checkpoint_mgr.mark_stopped(source)
                            return
                        fn = url.split("/")[-1]
                        zip_path = out_dir / fn
                        try:
                            async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as zresp:
                                if zresp.status == 200:
                                    data = await zresp.read()
                                    async with aiofiles.open(zip_path, "wb") as zf:
                                        await zf.write(data)
                                    processed += 1
                                    logger.info(f"GDELT downloaded {fn} ({len(data)} bytes)")
                        except Exception as ze:
                            logger.warning(f"GDELT zip download error {url}: {ze}")
                        checkpoint_mgr.mark_progress(source, processed, {"master_file": str(master_file)})
                    
                    checkpoint_mgr.mark_done(source)
                    logger.info(f"GDELT ingestion complete ({processed} files)")
                else:
                    logger.warning(f"GDELT masterfilelist HTTP {resp.status}")
                    checkpoint_mgr.mark_failed(source, f"HTTP {resp.status}")
        except Exception as e:
            logger.error(f"GDELT ingestion failed: {e}")
            checkpoint_mgr.mark_failed(source, str(e))


async def ingest_gh_archive(checkpoint_mgr: CheckpointManager, shutdown_mgr: ShutdownManager):
    """GitHub Archive hourly events — downloads sample hourly JSON.gz archives."""
    source = "gh_archive"
    state = checkpoint_mgr.get(source)
    
    out_dir = Path("data/source/gh_archive")
    out_dir.mkdir(parents=True, exist_ok=True)
    
    checkpoint_mgr.mark_started(source, total=5)
    
    # Download 3 sample hourly archive files from recent dates
    sample_hours = ["2026-01-01-12.json.gz", "2026-01-01-13.json.gz", "2026-01-01-14.json.gz"]
    base_url = "https://data.gharchive.org/"
    
    async with aiohttp.ClientSession() as session:
        processed = 0
        for fn in sample_hours:
            if shutdown_mgr.check_shutdown():
                checkpoint_mgr.mark_stopped(source)
                return
            url = base_url + fn
            out_file = out_dir / fn
            try:
                headers = {"User-Agent": "QuantResearch/1.0"}
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        async with aiofiles.open(out_file, "wb") as f:
                            await f.write(data)
                        processed += 1
                        logger.info(f"GH Archive downloaded {fn} ({len(data)} bytes)")
                    else:
                        logger.warning(f"GH Archive {fn} HTTP {resp.status}")
            except Exception as e:
                logger.warning(f"GH Archive {fn} download error: {e}")
            checkpoint_mgr.mark_progress(source, processed, {"latest_file": fn})
        
        if processed > 0:
            checkpoint_mgr.mark_done(source)
            logger.info(f"GH Archive ingestion complete ({processed} files)")
        else:
            checkpoint_mgr.mark_failed(source, "No files downloaded")


async def ingest_cdxj(checkpoint_mgr: CheckpointManager, shutdown_mgr: ShutdownManager):
    """Wayback CDXJ for StockTwits, ApeWisdom, App Store reviews."""
    source = "cdxj"
    state = checkpoint_mgr.get(source)
    
    pairs_done = set(tuple(p) for p in state.checkpoint.get("pairs_done", []))
    domains = [
        ("stocktwits", "stocktwits.com/symbol/"),
    ]
    
    # Build flat list of (domain, ticker) pairs
    all_pairs = [(dom, tk) for dom, _ in domains for tk in MASTER_TICKERS]
    remaining = [(d, t) for d, t in all_pairs if (d, t) not in pairs_done]
    checkpoint_mgr.mark_started(source, total=len(remaining))
    
    async with aiohttp.ClientSession() as session:
        for domain, ticker in remaining:
            if shutdown_mgr.check_shutdown():
                checkpoint_mgr.mark_stopped(source)
                return
            
            await RATE_LIMITERS[source].acquire()
            
            prefix = next(p for d, p in domains if d == domain)
            
            # Retry with exponential backoff for 503/429/timeout
            max_retries = 5
            base_delay = 2.0
            for attempt in range(max_retries):
                if shutdown_mgr.check_shutdown():
                    checkpoint_mgr.mark_stopped(source)
                    return
                
                try:
                    url = f"http://web.archive.org/cdx/search/cdx?url={prefix}{ticker}*&output=json&limit=10000"
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            # Save raw CDXJ
                            out_path = Path(f"data/source/cdxj/{domain}/{ticker}.json")
                            out_path.parent.mkdir(parents=True, exist_ok=True)
                            async with aiofiles.open(out_path, "w") as f:
                                await f.write(json.dumps(data))
                            pairs_done.add((domain, ticker))
                            logger.debug(f"CDXJ {domain}/{ticker}: {len(data)} captures")
                            break  # Success - exit retry loop
                        elif resp.status in (429, 503, 504):
                            # Rate limited or server error - retry
                            wait_time = base_delay * (2 ** attempt)
                            logger.warning(f"CDXJ {domain}/{ticker}: HTTP {resp.status}, retry {attempt+1}/{max_retries} in {wait_time:.1f}s")
                            await asyncio.sleep(wait_time)
                            continue
                        else:
                            logger.warning(f"CDXJ {domain}/{ticker}: HTTP {resp.status}")
                            break  # Don't retry other errors
                except asyncio.TimeoutError:
                    wait_time = base_delay * (2 ** attempt)
                    logger.warning(f"CDXJ {domain}/{ticker}: timeout, retry {attempt+1}/{max_retries} in {wait_time:.1f}s")
                    await asyncio.sleep(wait_time)
                    continue
                except Exception as e:
                    logger.warning(f"CDXJ {domain}/{ticker}: {e}")
                    break  # Don't retry unknown errors
            else:
                # All retries exhausted
                logger.error(f"CDXJ {domain}/{ticker}: FAILED after {max_retries} retries")
            
            checkpoint_mgr.mark_progress(source, len(pairs_done), 
                {"pairs_done": [list(p) for p in pairs_done]})
            
            await asyncio.sleep(0.5)  # Extra delay between requests
    
    checkpoint_mgr.mark_done(source)
    logger.info(f"CDXJ ingestion complete: {len(pairs_done)} pairs")


async def ingest_edgar_companyfacts(checkpoint_mgr: CheckpointManager, shutdown_mgr: ShutdownManager):
    """SEC EDGAR companyfacts (bulk zip + API)."""
    source = "edgar_companyfacts"
    state = checkpoint_mgr.get(source)
    
    tickers_done = state.checkpoint.get("tickers_done", [])
    checkpoint_mgr.mark_started(source, total=len(MASTER_TICKERS))
    
    async with aiohttp.ClientSession() as session:
        for ticker in MASTER_TICKERS:
            if shutdown_mgr.check_shutdown():
                checkpoint_mgr.mark_stopped(source)
                return
            
            if ticker in tickers_done:
                continue
            
            await RATE_LIMITERS[source].acquire()
            
            try:
                # Need CIK mapping first
                cik_url = f"https://www.sec.gov/files/company_tickers.json"
                headers = {"User-Agent": "QuantResearch/1.0 (research@example.com)"}
                async with session.get(cik_url, headers=headers) as resp:
                    cik_data = await resp.json()
                
                cik = None
                for item in cik_data.values():
                    if item.get("ticker") == ticker:
                        cik = str(item.get("cik_str")).zfill(10)
                        break
                
                if not cik:
                    logger.warning(f"No CIK found for {ticker}")
                    continue
                
                # Fetch companyfacts
                url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
                headers = {"User-Agent": "QuantResearch/1.0 (research@example.com)"}
                
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        out_path = Path(f"data/source/sec/companyfacts/{ticker}.json")
                        out_path.parent.mkdir(parents=True, exist_ok=True)
                        async with aiofiles.open(out_path, "w") as f:
                            await f.write(json.dumps(data))
                        tickers_done.append(ticker)
                        logger.info(f"EDGAR companyfacts {ticker}: OK")
                    else:
                        logger.warning(f"EDGAR {ticker}: HTTP {resp.status}")
            
            except Exception as e:
                logger.warning(f"EDGAR companyfacts {ticker}: {e}")
            
            checkpoint_mgr.mark_progress(source, len(tickers_done), 
                {"tickers_done": tickers_done})
            await asyncio.sleep(0.2)
    
    checkpoint_mgr.mark_done(source)


async def ingest_edgar_13f(checkpoint_mgr: CheckpointManager, shutdown_mgr: ShutdownManager):
    """SEC EDGAR 13F institutional holdings downloader."""
    source = "edgar_13f"
    state = checkpoint_mgr.get(source)
    
    out_dir = Path("data/edgar/13f")
    out_dir.mkdir(parents=True, exist_ok=True)
    
    checkpoint_mgr.mark_started(source, total=len(MASTER_TICKERS))
    
    async with aiohttp.ClientSession() as session:
        headers = {"User-Agent": "QuantResearch/1.0 (research@example.com)"}
        # Fetch CIK mapping
        try:
            cik_url = "https://www.sec.gov/files/company_tickers.json"
            async with session.get(cik_url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                cik_data = await resp.json() if resp.status == 200 else {}
        except Exception as e:
            logger.warning(f"13F CIK fetch failed: {e}")
            cik_data = {}

        cik_map = {item["ticker"]: str(item["cik_str"]).zfill(10) for item in cik_data.values()} if cik_data else {}

        processed = 0
        for ticker in MASTER_TICKERS[:10]: # Sample 10 target tickers for holdings
            if shutdown_mgr.check_shutdown():
                checkpoint_mgr.mark_stopped(source)
                return
            await RATE_LIMITERS[source].acquire()
            cik = cik_map.get(ticker)
            if cik:
                sub_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
                try:
                    async with session.get(sub_url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            out_file = out_dir / f"{ticker}_submissions.json"
                            async with aiofiles.open(out_file, "w") as f:
                                await f.write(json.dumps(data))
                            processed += 1
                except Exception as e:
                    logger.warning(f"13F fetch failed for {ticker}: {e}")
            checkpoint_mgr.mark_progress(source, processed, {"cik_mapped": len(cik_map)})

        checkpoint_mgr.mark_done(source)
        logger.info(f"EDGAR 13F ingestion complete ({processed} submissions saved)")


async def ingest_kaggle_analyst(checkpoint_mgr: CheckpointManager, shutdown_mgr: ShutdownManager):
    """Kaggle analyst accuracy dataset — delegates to real downloader."""
    from discovery.kaggle_analyst_downloader import ingest_kaggle_analyst_real
    await ingest_kaggle_analyst_real(checkpoint_mgr, shutdown_mgr)


async def ingest_ken_french(checkpoint_mgr: CheckpointManager, shutdown_mgr: ShutdownManager):
    """Ken French FF5 factors — delegates to real downloader."""
    from discovery.ken_french_downloader import ingest_ken_french_real
    await ingest_ken_french_real(
        checkpoint_mgr,
        shutdown_mgr,
        rate_limiter=RATE_LIMITERS["ken_french"],
    )


async def ingest_yfinance(checkpoint_mgr: CheckpointManager, shutdown_mgr: ShutdownManager):
    """yfinance prices + dividends for DV construction."""
    source = "yfinance"
    state = checkpoint_mgr.get(source)
    
    tickers_done = state.checkpoint.get("tickers_done", [])
    remaining = [t for t in MASTER_TICKERS if t not in tickers_done]
    checkpoint_mgr.mark_started(source, total=len(remaining))
    
    try:
        import yfinance as yf
    except ImportError:
        logger.error("yfinance not installed: pip install yfinance")
        checkpoint_mgr.mark_failed(source, "yfinance not installed")
        return
    
    for ticker in remaining:
        if shutdown_mgr.check_shutdown():
            checkpoint_mgr.mark_stopped(source)
            return
        
        await RATE_LIMITERS[source].acquire()
        
        try:
            t = yf.Ticker(ticker)
            # Get full history
            hist = t.history(period="max", auto_adjust=False)
            divs = t.dividends
            splits = t.splits
            
            if not hist.empty:
                out_dir = Path(f"data/source/yfinance/{ticker}")
                out_dir.mkdir(parents=True, exist_ok=True)
                hist.to_parquet(out_dir / "prices.parquet")
                if not divs.empty:
                    divs.to_frame().to_parquet(out_dir / "dividends.parquet")
                if not splits.empty:
                    splits.to_frame().to_parquet(out_dir / "splits.parquet")
                tickers_done.append(ticker)
                logger.info(f"yfinance {ticker}: {len(hist)} rows")
        
        except Exception as e:
            logger.warning(f"yfinance {ticker}: {e}")
        
        checkpoint_mgr.mark_progress(source, len(tickers_done), 
            {"tickers_done": tickers_done})
        await asyncio.sleep(0.1)
    
    checkpoint_mgr.mark_done(source)


# ============================================================
# MAIN ORCHESTRATION
# ============================================================

SPRINT1_SOURCES = [
    ("google_trends", ingest_google_trends),
    ("gdelt", ingest_gdelt),
    ("gh_archive", ingest_gh_archive),
    ("cdxj", ingest_cdxj),
    ("edgar_companyfacts", ingest_edgar_companyfacts),
    ("edgar_13f", ingest_edgar_13f),
    ("kaggle_analyst", ingest_kaggle_analyst),
    ("ken_french", ingest_ken_french),
    ("yfinance", ingest_yfinance),
]

# Control concurrency — max 3 heavy downloaders at once
SEMAPHORE = asyncio.Semaphore(3)


async def run_with_semaphore(name: str, func: Callable, *args):
    async with SEMAPHORE:
        logger.info(f"Starting {name}")
        try:
            await func(checkpoint_mgr, shutdown_mgr)
            logger.info(f"Completed {name}")
        except Exception as e:
            logger.error(f"{name} failed: {e}")
            raise


async def main():
    logger.info("=" * 60)
    logger.info("SPRINT 1 PARALLEL INGESTION START")
    logger.info("=" * 60)
    
    # Show which sources need work
    pending = checkpoint_mgr.get_pending_sources([s[0] for s in SPRINT1_SOURCES])
    logger.info(f"Sources to run: {pending}")
    logger.info(f"Sources already done: {[s for s in [s[0] for s in SPRINT1_SOURCES] if s not in pending]}")
    logger.info("Press Ctrl+C anytime to gracefully stop and resume later")
    
    # Register shutdown callback to save checkpoints
    def save_all_checkpoints():
        for source in [s[0] for s in SPRINT1_SOURCES]:
            if source in checkpoint_mgr.states:
                checkpoint_mgr.save(source)
    shutdown_mgr.register_callback(save_all_checkpoints)
    
    # Run all sources in parallel (with semaphore limiting concurrency)
    tasks = [
        asyncio.create_task(run_with_semaphore(name, func))
        for name, func in SPRINT1_SOURCES
    ]
    
    # Wait with shutdown checking
    while tasks:
        done, pending_tasks = await asyncio.wait(
            tasks, timeout=1.0, return_when=asyncio.FIRST_COMPLETED
        )
        
        for task in done:
            try:
                await task
            except Exception as e:
                logger.error(f"Task failed: {e}")
        
        tasks = list(pending_tasks)
        
        if shutdown_mgr.check_shutdown():
            logger.info("Shutdown requested — cancelling remaining tasks...")
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            break
    
    # Final checkpoint save
    save_all_checkpoints()
    
    logger.info("=" * 60)
    logger.info("SPRINT 1 INGESTION COMPLETE")
    logger.info("=" * 60)
    
    # Summary
    for source in [s[0] for s in SPRINT1_SOURCES]:
        state = checkpoint_mgr.states.get(source)
        if state:
            logger.info(f"  {source}: {state.status} ({state.items_processed}/{state.items_total or '?'})")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted")
    finally:
        shutdown_mgr.restore_handlers()