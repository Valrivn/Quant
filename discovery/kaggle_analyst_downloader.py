"""
Kaggle S&P 500 Analyst Rating & Price Target Accuracy — dataset downloader.

Dataset: uradkr/s-and-p-500-analyst-rating-and-price-target-accuracy
  164,231 analyst rating actions, 2011-2026, matched to forward returns.

Drop-in replacement for the stub ``ingest_kaggle_analyst`` in
``sprint1_parallel_ingest.py``.  Call ``ingest_kaggle_analyst_real()`` from
``SPRINT1_SOURCES`` instead.

Design
------
1. Verify ``kaggle`` CLI is installed **and** authenticated.
2. If not authenticated → write a README with setup instructions, mark
   checkpoint ``needs_auth``, return.
3. If authenticated → download + unzip via ``kaggle datasets download …``.
4. Parse every CSV in the archive, convert to Parquet partitioned by year
   (``data/source/kaggle/analyst_accuracy/year=YYYY/``).
5. Record download timestamp + per-file sizes in the checkpoint.
6. Honours the shared ``shutdown_mgr`` (graceful Ctrl-C).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ── constants ────────────────────────────────────────────────────
DATASET_SLUG = "uradkr/s-and-p-500-analyst-rating-and-price-target-accuracy"
DOWNLOAD_DIR = Path("data/source/kaggle/analyst_accuracy")
README_PATH = DOWNLOAD_DIR / "README_SETUP.md"

# Candidate CSV names the dataset is known to contain.  We try every
# ``*.csv`` we find, but this list lets us log expected filenames.
EXPECTED_CSVS = [
    "analyst_ratings.csv",
    "price_targets.csv",
    "accuracy_metrics.csv",
]


# ── helpers ──────────────────────────────────────────────────────

async def _run_cmd(*args: str, timeout: float = 120) -> tuple[int, str, str]:
    """Run a subprocess and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await asyncio.wait_for(
        proc.communicate(), timeout=timeout
    )
    return proc.returncode, stdout_bytes.decode(errors="replace"), stderr_bytes.decode(errors="replace")


async def _kaggle_installed() -> bool:
    """Return True if the ``kaggle`` CLI is on PATH."""
    try:
        rc, _, _ = await _run_cmd(sys.executable, "-m", "kaggle", "--version", timeout=15)
        if rc == 0:
            return True
    except (FileNotFoundError, asyncio.TimeoutError):
        pass

    # Fallback: try bare ``kaggle`` on PATH (works when installed globally).
    try:
        rc, _, _ = await _run_cmd("kaggle", "--version", timeout=15)
        return rc == 0
    except (FileNotFoundError, asyncio.TimeoutError):
        return False


async def _kaggle_authenticated() -> bool:
    """Return True if ``kaggle config view`` succeeds (credentials present)."""
    try:
        rc, _, _ = await _run_cmd(
            sys.executable, "-m", "kaggle", "config", "view", timeout=15,
        )
        if rc == 0:
            return True
    except (FileNotFoundError, asyncio.TimeoutError):
        pass

    try:
        rc, _, _ = await _run_cmd("kaggle", "config", "view", timeout=15)
        return rc == 0
    except (FileNotFoundError, asyncio.TimeoutError):
        return False


def _write_auth_readme() -> None:
    """Write a clear setup guide so a human can unblock the pipeline."""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    README_PATH.write_text(
        f"""# Kaggle Authentication Required
Dataset: `{DATASET_SLUG}`

The automated download could not proceed because the Kaggle CLI is not
authenticated.  Follow these steps:

## 1. Get API credentials
- Go to <https://www.kaggle.com/settings> (Account → API → Create New Token).
- Download `kaggle.json` and place it at:
  - **Linux / macOS:** `~/.kaggle/kaggle.json`
  - **Windows:** `C:\\Users\\<YOU>\\.kaggle\\kaggle.json`

## 2. Install the CLI (if not already)
```bash
pip install kaggle
```

## 3. Verify
```bash
kaggle config view
kaggle datasets list -s "{DATASET_SLUG}"
```

## 4. Re-run the ingest pipeline
```bash
python sprint1_parallel_ingest.py
```

---
Generated: {datetime.utcnow().isoformat()}Z
""",
        encoding="utf-8",
    )
    logger.info("Wrote auth instructions → %s", README_PATH)


# ── parquet conversion ──────────────────────────────────────────

def _convert_csv_to_parquet_by_year(csv_path: Path) -> Dict[str, int]:
    """Read *csv_path*, partition into ``year=YYYY/`` Parquet files.

    Returns a dict ``{year_str: row_count}`` for the checkpoint.
    """
    import pandas as pd

    df = pd.read_csv(csv_path, low_memory=False)

    # Try to detect the date column heuristically.
    date_col: Optional[str] = None
    for candidate in ("date", "Date", "datePublished", "pubDate", "publishedDate", "reportDate"):
        if candidate in df.columns:
            date_col = candidate
            break

    year_counts: Dict[str, int] = {}
    if date_col is not None:
        try:
            df["_parsed_date"] = pd.to_datetime(df[date_col], errors="coerce", utc=True)
            df["_year"] = df["_parsed_date"].dt.year
        except Exception:
            df["_year"] = 0
            date_col = None  # fall through to single partition
    else:
        df["_year"] = 0  # single partition fallback

    out_root = DOWNLOAD_DIR
    for year_val, group in df.groupby("_year"):
        if year_val == 0 or int(year_val) != year_val:
            # Fallback: dump everything into a single partition
            year_dir = out_root / "_unpartitioned"
        else:
            year_str = str(int(year_val))
            year_dir = out_root / f"year={year_str}"
        year_dir.mkdir(parents=True, exist_ok=True)

        out_file = year_dir / csv_path.name.replace(".csv", ".parquet")
        cols_to_drop = ["_parsed_date", "_year"]
        group.drop(columns=[c for c in cols_to_drop if c in group.columns]).to_parquet(
            out_file, index=False
        )
        year_counts[str(int(year_val) if year_val != 0 else "_unpartitioned")] = len(group)
        logger.info("  wrote %s  (%d rows)", out_file, len(group))

    return year_counts


# ── main ingest function ────────────────────────────────────────

async def ingest_kaggle_analyst_real(
    checkpoint_mgr: Any,
    shutdown_mgr: Any,
    rate_limiter: Any = None,
) -> None:
    """Download the Kaggle analyst-accuracy dataset and convert to Parquet.

    Parameters
    ----------
    checkpoint_mgr
        ``CheckpointManager`` instance from ``sprint1_parallel_ingest``.
    shutdown_mgr
        ``ShutdownManager`` instance.
    rate_limiter
        ``RateLimiter`` (optional override; default ``0.1 req/s``).  If
        ``None`` a local limiter is created.
    """
    # Lazy import so module can be imported at top-level without pandas.
    try:
        import pandas as pd  # noqa: F401
    except ImportError:
        logger.error("pandas not installed — pip install pandas pyarrow")
        checkpoint_mgr.mark_failed("kaggle_analyst", "pandas/pyarrow not installed")
        return

    source = "kaggle_analyst"
    state = checkpoint_mgr.get(source)

    # ── resume guard: already done ────────────────────────────────
    if state.status == "done" and state.checkpoint.get("downloaded"):
        logger.info("Kaggle analyst dataset already downloaded and converted — skipping.")
        return

    # ── rate limiter ──────────────────────────────────────────────
    if rate_limiter is None:
        from sprint1_parallel_ingest import RATE_LIMITERS
        rate_limiter = RATE_LIMITERS.get(source)
    if rate_limiter is None:
        # 0.1 req/s default — manual-ish cadence
        from sprint1_parallel_ingest import RateLimiter
        rate_limiter = RateLimiter(rate=0.1, burst=1)

    checkpoint_mgr.mark_started(source)
    await rate_limiter.acquire()

    # ── step 1: kaggle CLI installed? ─────────────────────────────
    if shutdown_mgr.check_shutdown():
        checkpoint_mgr.mark_stopped(source)
        return

    if not await _kaggle_installed():
        msg = "kaggle CLI not found — install with: pip install kaggle"
        logger.error(msg)
        _write_auth_readme()
        checkpoint_mgr.mark_progress(source, 0, {
            "status": "needs_cli",
            "readme": str(README_PATH),
        })
        # Mark as failed so the pipeline can be re-run after install.
        checkpoint_mgr.mark_failed(source, msg)
        return

    # ── step 2: authenticated? ────────────────────────────────────
    if shutdown_mgr.check_shutdown():
        checkpoint_mgr.mark_stopped(source)
        return

    if not await _kaggle_authenticated():
        logger.warning("Kaggle CLI not authenticated — writing setup README.")
        _write_auth_readme()
        checkpoint_mgr.mark_progress(source, 0, {
            "status": "needs_auth",
            "readme": str(README_PATH),
        })
        # Mark failed so the pipeline retries after auth is configured.
        checkpoint_mgr.mark_failed(source, "kaggle not authenticated — see README_SETUP.md")
        return

    # ── step 3: download + unzip ──────────────────────────────────
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    dl_args = [
        sys.executable, "-m", "kaggle", "datasets", "download",
        DATASET_SLUG,
        "-p", str(DOWNLOAD_DIR),
        "--unzip",
    ]
    logger.info("Running: %s", " ".join(dl_args))

    if shutdown_mgr.check_shutdown():
        checkpoint_mgr.mark_stopped(source)
        return

    await rate_limiter.acquire()

    try:
        rc, stdout, stderr = await _run_cmd(*dl_args, timeout=600)
    except asyncio.TimeoutError:
        logger.error("kaggle download timed out (600s)")
        checkpoint_mgr.mark_failed(source, "kaggle download timed out")
        return
    except Exception as exc:
        logger.error("kaggle download failed: %s", exc)
        checkpoint_mgr.mark_failed(source, str(exc))
        return

    if rc != 0:
        combined = (stdout + "\n" + stderr).strip()
        logger.error("kaggle download returned %d:\n%s", rc, combined)
        checkpoint_mgr.mark_failed(source, combined[:500])
        return

    if shutdown_mgr.check_shutdown():
        checkpoint_mgr.mark_stopped(source)
        return

    logger.info("Kaggle download complete → %s", DOWNLOAD_DIR)

    # ── step 4: convert CSVs → parquet by year ────────────────────
    csv_files = sorted(DOWNLOAD_DIR.glob("*.csv"))
    if not csv_files:
        # Dataset may only contain a single CSV or nested structure.
        # Search one level deeper.
        csv_files = sorted(DOWNLOAD_DIR.rglob("*.csv"))

    if not csv_files:
        msg = f"No CSV files found after download in {DOWNLOAD_DIR}"
        logger.error(msg)
        checkpoint_mgr.mark_failed(source, msg)
        return

    logger.info("Found %d CSV file(s) to convert", len(csv_files))

    file_sizes: Dict[str, int] = {}
    all_year_counts: Dict[str, Any] = {}

    for csv_path in csv_files:
        if shutdown_mgr.check_shutdown():
            checkpoint_mgr.mark_stopped(source)
            return

        logger.info("Converting %s …", csv_path.name)
        try:
            year_counts = _convert_csv_to_parquet_by_year(csv_path)
        except Exception as exc:
            logger.warning("Failed to convert %s: %s", csv_path.name, exc)
            continue

        all_year_counts[csv_path.name] = year_counts
        file_sizes[csv_path.name] = csv_path.stat().st_size

    # ── step 5: update checkpoint ─────────────────────────────────
    total_rows = sum(
        sum(year_counts.values()) if isinstance(year_counts, dict) else 0
        for year_counts in all_year_counts.values()
    )

    checkpoint_mgr.mark_progress(source, 1, {
        "downloaded": True,
        "downloaded_at": datetime.utcnow().isoformat(),
        "dataset": DATASET_SLUG,
        "download_dir": str(DOWNLOAD_DIR),
        "csv_files": [p.name for p in csv_files],
        "file_sizes": file_sizes,
        "parquet_partitioning": all_year_counts,
        "total_rows": total_rows,
    })

    if shutdown_mgr.check_shutdown():
        checkpoint_mgr.mark_stopped(source)
        return

    checkpoint_mgr.mark_done(source)
    logger.info(
        "Kaggle analyst ingest complete: %d CSVs → %d total rows (year-partitioned)",
        len(csv_files),
        total_rows,
    )
