"""Ken French FF5 daily factors downloader.

Downloads F-F_Research_Data_5_Factors_2x3_daily_CSV.zip from Ken French's
Dartmouth site, extracts and parses the CSV, converts to parquet at
data/source/ken_french/ff5_daily.parquet.

Replaces the stub in sprint1_parallel_ingest.py:ingest_ken_french.
"""

from __future__ import annotations

import io
import logging
import zipfile
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp
import pandas as pd

if TYPE_CHECKING:
    from sprint1_parallel_ingest import CheckpointManager, ShutdownManager

logger = logging.getLogger(__name__)

# Ken French CSV URL (HTTP, not FTP — FTP is deprecated)
KEN_FRENCH_URL = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
    "F-F_Research_Data_5_Factors_2x3_daily_CSV.zip"
)

OUT_PATH = Path("data/source/ken_french/ff5_daily.parquet")
RETRY_MAX = 3
RETRY_BASE_DELAY = 2.0


def _parse_ff5_csv(raw_bytes: bytes) -> pd.DataFrame:
    """Parse Ken French FF5 daily CSV bytes into a DataFrame.

    The CSV format from Ken French's site looks like::

        This file was created by using the 202607 CRSP database.
        ...header text...
        <blank>
        ,Mkt-RF,SMB,HML,RMW,CMA,RF
        19630701,   -0.67,    0.00,   -0.33,   -0.01,    0.16,    0.01
        ...
        <blank>
        Copyright 2026 Eugene F. Fama and Kenneth R. French

    The header row starts with a comma (empty column for the date).
    Daily data rows are ``YYYYMMDD,f1,f2,...``.
    Columns are in percent (e.g. ``1.23`` = 1.23 %); we convert to decimal.
    """
    text = raw_bytes.decode("utf-8", errors="replace")
    lines = text.splitlines()

    # Find the header row: it starts with ',' and contains 'Mkt-RF'
    header_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(",") and "Mkt-RF" in stripped:
            header_idx = i
            break

    if header_idx is None:
        raise ValueError(
            "Cannot locate header row (',Mkt-RF,...') in Ken French CSV"
        )

    # Collect data lines: everything after the header until a blank line
    data_lines = []
    for line in lines[header_idx + 1 :]:
        stripped = line.strip()
        if stripped == "":
            break
        data_lines.append(stripped)

    if not data_lines:
        raise ValueError("No data rows found after header in Ken French CSV")

    # Build a clean CSV: first column is date (YYYYMMDD), rest are factors
    header = lines[header_idx].strip()
    csv_text = header + "\n" + "\n".join(data_lines)

    df = pd.read_csv(
        io.StringIO(csv_text),
        header=0,
        skipinitialspace=True,
    )

    # Normalise column names
    df.columns = [c.strip() for c in df.columns]

    # The first column is unnamed (empty string from leading comma) — treat as Date
    date_col = df.columns[0]
    df = df.rename(columns={date_col: "Date"})

    # Parse YYYYMMDD integer → datetime
    df["Date"] = pd.to_datetime(df["Date"].astype(str).str.strip(), format="%Y%m%d")
    df = df.set_index("Date").sort_index()

    # Ensure all factor columns are present
    expected = ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"]
    for col in expected:
        if col not in df.columns:
            raise ValueError(
                f"Expected column '{col}' not found. Got: {list(df.columns)}"
            )

    # Convert from percent to decimal (divide by 100)
    df[expected] = df[expected].astype(float) / 100.0

    return df[expected]


async def ingest_ken_french_real(
    checkpoint_mgr: CheckpointManager,
    shutdown_mgr: ShutdownManager,
    *,
    rate_limiter=None,
) -> None:
    """Download and parse Ken French FF5 daily factors.

    Parameters
    ----------
    checkpoint_mgr : CheckpointManager
        Sprint-1 checkpoint manager for progress tracking.
    shutdown_mgr : ShutdownManager
        Graceful-shutdown coordinator.
    rate_limiter : RateLimiter, optional
        Per-source rate limiter.  Falls back to ``RATE_LIMITERS["ken_french"]``
        from ``sprint1_parallel_ingest`` if not supplied.
    """
    source = "ken_french"
    state = checkpoint_mgr.get(source)

    # Late import to avoid circular deps — caller passes rate_limiter
    if rate_limiter is None:
        from sprint1_parallel_ingest import RATE_LIMITERS
        rate_limiter = RATE_LIMITERS["ken_french"]

    checkpoint_mgr.mark_started(source, total=1)
    logger.info("Ken French FF5: starting download from %s", KEN_FRENCH_URL)

    last_err: Exception | None = None

    for attempt in range(1, RETRY_MAX + 1):
        if shutdown_mgr.check_shutdown():
            checkpoint_mgr.mark_stopped(source)
            return

        await rate_limiter.acquire()

        try:
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(KEN_FRENCH_URL) as resp:
                    if resp.status != 200:
                        raise IOError(f"HTTP {resp.status} fetching Ken French data")
                    zip_bytes = await resp.read()

            logger.info(
                "Ken French: downloaded %d bytes (attempt %d/%d)",
                len(zip_bytes), attempt, RETRY_MAX,
            )

            # Extract the single CSV from the ZIP
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
                if not csv_names:
                    raise ValueError(
                        f"No .csv in ZIP. Contents: {zf.namelist()}"
                    )
                csv_name = csv_names[0]
                logger.info("Ken French: extracting %s", csv_name)
                csv_bytes = zf.read(csv_name)

            if shutdown_mgr.check_shutdown():
                checkpoint_mgr.mark_stopped(source)
                return

            # Parse
            df = _parse_ff5_csv(csv_bytes)
            logger.info(
                "Ken French: parsed %d rows, %s → %s, columns: %s",
                len(df), df.index.min().date(), df.index.max().date(),
                list(df.columns),
            )

            # Write parquet (overwrite)
            OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(OUT_PATH)

            logger.info("Ken French: wrote %s (%d rows)", OUT_PATH, len(df))

            # Update checkpoint
            checkpoint_mgr.mark_progress(
                source, 1,
                {
                    "url": KEN_FRENCH_URL,
                    "path": str(OUT_PATH),
                    "rows": len(df),
                    "date_range": f"{df.index.min().date()} → {df.index.max().date()}",
                    "downloaded_at": datetime.utcnow().isoformat(),
                },
            )
            checkpoint_mgr.mark_done(source)
            return  # success

        except Exception as exc:
            last_err = exc
            logger.warning(
                "Ken French attempt %d/%d failed: %s",
                attempt, RETRY_MAX, exc,
            )
            if attempt < RETRY_MAX:
                delay = RETRY_BASE_DELAY ** attempt
                await asyncio.sleep(delay)

    # All retries exhausted
    error_msg = f"Failed after {RETRY_MAX} attempts: {last_err}"
    checkpoint_mgr.mark_failed(source, error_msg)
    logger.error("Ken French: %s", error_msg)


# Need asyncio for sleep in retry loop
import asyncio  # noqa: E402
