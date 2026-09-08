"""Automated FRED/ALFRED Data Refresh Scheduler and Pipeline Orchestrator.

Provides automated scheduling, incremental updates, vintage refreshes, monthly revalidation,
structured JSON logging, health check reporting, and Task Scheduler/cron setup.

Requirements:
1. Daily Incremental Update (6 AM ET):
   - Fetch latest observations for all 69 daily/monthly series
   - Upsert into data lake (append new dates only)
   - Validate no gaps > 5 business days for daily series
   - Alert on missing data via log

2. Weekly ALFRED Vintage Refresh (Sunday 8 AM ET):
   - Re-fetch vintage dates for macro series
   - Pull new vintage observations (batched x10)
   - Update data lake with point-in-time revisions
   - Log revision magnitude (mean absolute revision)

3. Monthly Full Revalidation (1st of month):
   - Full history integrity check for all series
   - Compare row counts vs FRED API metadata
   - Rebuild any corrupted Parquet files
   - Generate data quality report

4. Infrastructure & Health Check:
   - Configurable via config/fred_schedule.json
   - Health check endpoint / method returning status, last successful run, next scheduled
   - Windows Task Scheduler / cron task generator

5. Monitoring:
   - Structured JSON log to logs/fred_pipeline.log
   - Metrics: rows added, vintages updated, errors, duration
   - Alert thresholds: >10% missing daily obs, revision > 2 std
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

# Add project root to sys.path
project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from ingestion.data_lake import DataLakeStore
from ingestion.fred_alfred import (
    EXPANDED_SERIES_CATALOG,
    KEY_ECONOMIC_SERIES,
    FredAlfredClient,
    FredIngestionPipeline,
    resolve_series_id,
)

# Constants
DEFAULT_CONFIG_PATH = project_root / "config" / "fred_schedule.json"
DEFAULT_LOG_PATH = project_root / "logs" / "fred_pipeline.log"
DEFAULT_HEALTH_PATH = project_root / "logs" / "fred_health_check.json"


class StructuredJsonFormatter(logging.Formatter):
    """Custom logging formatter emitting single-line JSON records."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if hasattr(record, "task"):
            log_entry["task"] = getattr(record, "task")
        if hasattr(record, "series_id"):
            log_entry["series_id"] = getattr(record, "series_id")
        if hasattr(record, "metrics"):
            log_entry["metrics"] = getattr(record, "metrics")
        if hasattr(record, "alert"):
            log_entry["alert"] = getattr(record, "alert")
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry)


def setup_structured_logging(log_file_path: Union[str, Path] = DEFAULT_LOG_PATH) -> logging.Logger:
    """Configure structured JSON file logging and standard console logging."""
    log_path = Path(log_file_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("fred_scheduler")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    # File Handler (Structured JSON)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(StructuredJsonFormatter())
    logger.addHandler(file_handler)

    # Console Handler (Human Readable)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    return logger


logger = setup_structured_logging()


def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize DataFrame so 'date' is explicitly a column with pandas datetime type."""
    if df is None or df.empty:
        return pd.DataFrame()

    res = df.copy()
    if "date" not in res.columns:
        if res.index.name == "date" or isinstance(res.index, pd.DatetimeIndex):
            res = res.reset_index()
        elif "index" in res.columns:
            res.rename(columns={"index": "date"}, inplace=True)
        elif "DATE" in res.columns:
            res.rename(columns={"DATE": "date"}, inplace=True)
        else:
            res = res.reset_index()
            if "index" in res.columns:
                res.rename(columns={"index": "date"}, inplace=True)

    if "date" in res.columns:
        res["date"] = pd.to_datetime(res["date"])

    return res


def load_schedule_config(config_path: Union[str, Path] = DEFAULT_CONFIG_PATH) -> Dict[str, Any]:
    """Load configuration from config/fred_schedule.json with sensible fallbacks."""
    c_path = Path(config_path)
    if c_path.exists():
        try:
            with open(c_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            logger.error(f"Failed to read schedule config from {c_path}: {exc}")

    return {
        "schedule": {
            "daily_update": {
                "schedule_cron": "0 6 * * *",
                "time_et": "06:00",
                "max_business_day_gap": 5,
                "alert_missing_threshold": 0.10,
            },
            "weekly_refresh": {
                "schedule_cron": "0 8 * * 0",
                "day_of_week": "Sunday",
                "time_et": "08:00",
                "batch_size": 10,
                "std_dev_revision_threshold": 2.0,
            },
            "monthly_revalidation": {
                "schedule_cron": "0 0 1 * *",
                "day_of_month": 1,
                "time_et": "00:00",
            },
        },
        "paths": {
            "log_file": "logs/fred_pipeline.log",
            "health_check_file": "logs/fred_health_check.json",
            "data_lake_dir": "data/data_lake",
        },
        "series_config": {"total_series_count": 69},
    }


def compute_next_scheduled_times() -> Dict[str, str]:
    """Calculate next scheduled run timestamps in UTC (based on ET schedule rules)."""
    now = datetime.now(timezone.utc)
    
    # 6 AM ET daily (~10:00 UTC or 11:00 UTC depending on DST; using 10:00 UTC base)
    next_daily = (now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    if now.hour < 10:
        next_daily = now.replace(hour=10, minute=0, second=0, microsecond=0)

    # Sunday 8 AM ET (~12:00 UTC)
    days_until_sunday = (6 - now.weekday()) % 7
    if days_until_sunday == 0 and now.hour >= 12:
        days_until_sunday = 7
    next_weekly = (now + timedelta(days=days_until_sunday)).replace(hour=12, minute=0, second=0, microsecond=0)

    # 1st of month 00:00 ET (~04:00 UTC)
    if now.day == 1 and now.hour < 4:
        next_monthly = now.replace(hour=4, minute=0, second=0, microsecond=0)
    else:
        if now.month == 12:
            next_monthly = datetime(now.year + 1, 1, 1, 4, 0, 0, tzinfo=timezone.utc)
        else:
            next_monthly = datetime(now.year, now.month + 1, 1, 4, 0, 0, tzinfo=timezone.utc)

    return {
        "daily_update": next_daily.isoformat(),
        "weekly_refresh": next_weekly.isoformat(),
        "monthly_revalidation": next_monthly.isoformat(),
    }


class FredScheduler:
    """Automated Scheduler and Pipeline Manager for FRED & ALFRED ingestion."""

    def __init__(
        self,
        config_path: Union[str, Path] = DEFAULT_CONFIG_PATH,
        data_lake: Optional[DataLakeStore] = None,
        client: Optional[FredAlfredClient] = None,
    ):
        self.config_path = Path(config_path)
        self.config = load_schedule_config(self.config_path)
        self.data_lake = data_lake or DataLakeStore()
        self.client = client or FredAlfredClient()
        self.pipeline = FredIngestionPipeline(data_lake=self.data_lake, client=self.client)

        paths = self.config.get("paths", {})
        self.log_file = project_root / paths.get("log_file", "logs/fred_pipeline.log")
        self.health_file = project_root / paths.get("health_check_file", "logs/fred_health_check.json")
        self.health_file.parent.mkdir(parents=True, exist_ok=True)

    def get_all_catalog_series(self) -> List[Dict[str, Any]]:
        """Extract all 69 series defined across categories in EXPANDED_SERIES_CATALOG."""
        series_list: List[Dict[str, Any]] = []
        for cat_name, cat_series in EXPANDED_SERIES_CATALOG.items():
            for s in cat_series:
                item = dict(s)
                item["category"] = cat_name
                item["domain"] = "alfred" if s.get("type") == "macro" else "fred"
                series_list.append(item)
        return series_list

    def get_daily_series(self) -> List[Dict[str, Any]]:
        """Filter daily financial series from catalog."""
        return [s for s in self.get_all_catalog_series() if s.get("frequency") == "daily"]

    def get_macro_series(self) -> List[Dict[str, Any]]:
        """Filter macro series (with ALFRED vintages) from catalog."""
        return [s for s in self.get_all_catalog_series() if s.get("type") == "macro"]

    def check_daily_series_gaps(
        self,
        df: pd.DataFrame,
        max_allowed_gap_bd: int = 5,
        missing_threshold: float = 0.10,
    ) -> Tuple[int, float, List[str]]:
        """Validate business day gaps and missing observation percentages for daily series.

        Returns:
            (max_gap_business_days, missing_fraction, list_of_alert_messages)
        """
        if df.empty or "date" not in df.columns:
            return 0, 0.0, ["Empty dataset or missing 'date' column"]

        valid_df = df.dropna(subset=["value"]).copy()
        if valid_df.empty:
            return 0, 1.0, ["All observations are null"]

        valid_df["date"] = pd.to_datetime(valid_df["date"])
        valid_df.sort_values("date", inplace=True)
        dates = valid_df["date"].drop_duplicates().reset_index(drop=True)

        if len(dates) < 2:
            return 0, 0.0, []

        # Calculate business day gaps between consecutive observations
        max_gap = 0
        for i in range(1, len(dates)):
            d1 = dates.iloc[i - 1]
            d2 = dates.iloc[i]
            # bdate_range count minus 1 gives business days between d1 and d2
            b_days = max(0, len(pd.bdate_range(start=d1, end=d2)) - 1)
            if b_days > max_gap:
                max_gap = b_days

        # Check gap between latest date and today (if today is past latest date)
        today = pd.Timestamp.now().normalize()
        latest_date = dates.iloc[-1].normalize()
        if latest_date < today:
            current_gap = max(0, len(pd.bdate_range(start=latest_date, end=today)) - 1)
            max_gap = max(max_gap, current_gap)

        # Missing fraction over expected business days range
        full_bdate_range = pd.bdate_range(start=dates.iloc[0], end=dates.iloc[-1])
        expected_bd_count = len(full_bdate_range)
        actual_bd_count = len(set(dates.dt.normalize()).intersection(set(full_bdate_range)))
        
        missing_fraction = 0.0
        if expected_bd_count > 0:
            missing_fraction = max(0.0, (expected_bd_count - actual_bd_count) / float(expected_bd_count))

        alerts = []
        if max_gap > max_allowed_gap_bd:
            alerts.append(
                f"Business day gap of {max_gap} days exceeds max limit of {max_allowed_gap_bd} business days"
            )
        if missing_fraction > missing_threshold:
            alerts.append(
                f"Missing observation percentage {missing_fraction:.1%} exceeds threshold of {missing_threshold:.1%}"
            )

        return max_gap, missing_fraction, alerts

    def run_daily_update(self, series_subset: Optional[List[str]] = None) -> Dict[str, Any]:
        """Requirement 1: Daily Incremental Update (run at 6 AM ET).

        - Fetch latest observations for all 69 daily/monthly series (or subset)
        - Upsert into data lake (append new dates only)
        - Validate no gaps > 5 business days for daily series
        - Alert on missing data via structured JSON log
        """
        task_start = time.time()
        logger.info("=== Starting Task 1: Daily Incremental Update (6 AM ET) ===")

        all_series = self.get_all_catalog_series()
        if series_subset:
            all_series = [s for s in all_series if s["series_id"] in series_subset]

        daily_cfg = self.config.get("schedule", {}).get("daily_update", {})
        max_gap_limit = daily_cfg.get("max_business_day_gap", 5)
        missing_threshold = daily_cfg.get("alert_missing_threshold", 0.10)

        total_series_count = len(all_series)
        rows_added_total = 0
        total_rows_stored = 0
        errors: List[Dict[str, Any]] = []
        alerts_raised: List[Dict[str, Any]] = []
        series_summaries: List[Dict[str, Any]] = []

        for s_info in all_series:
            s_id = s_info["series_id"]
            cat = s_info.get("category", "Uncategorized")
            s_type = s_info.get("type", "standard")
            freq = s_info.get("frequency", "monthly")
            domain = s_info.get("domain", "fred")

            try:
                # 1. Load existing data lake observations if present
                existing_df = pd.DataFrame()
                try:
                    existing_df = _normalize_df(self.data_lake.load_dataframe(domain=domain, series_id=s_id))
                except Exception:
                    existing_df = pd.DataFrame()

                # Determine start date for incremental fetch
                observation_start = None
                if not existing_df.empty and "date" in existing_df.columns:
                    max_date = existing_df["date"].max()
                    # Fetch with a 30-day lookback buffer to catch any recent revisions
                    start_buffer = max_date - pd.Timedelta(days=30)
                    observation_start = start_buffer.strftime("%Y-%m-%d")

                # 2. Fetch latest observations
                meta = self.client.get_series_metadata(s_id)
                meta["category"] = cat
                meta["series_type"] = s_type

                fetched_df, fetch_meta = self.client.get_series_observations(
                    series_id=s_id,
                    observation_start=observation_start,
                )
                fetched_df = _normalize_df(fetched_df)

                # 3. Incremental Merge & Upsert (append new dates only)
                if not existing_df.empty and not fetched_df.empty:
                    dedup_cols = [c for c in ["date", "realtime_start", "realtime_end"] if c in existing_df.columns and c in fetched_df.columns]
                    if not dedup_cols:
                        dedup_cols = ["date"]

                    existing_keys = set(tuple(x) for x in existing_df[dedup_cols].values)
                    fetched_keys = set(tuple(x) for x in fetched_df[dedup_cols].values)
                    new_keys = fetched_keys - existing_keys
                    new_rows_count = len(new_keys)

                    combined_df = pd.concat([existing_df, fetched_df], ignore_index=True)
                    combined_df.drop_duplicates(subset=dedup_cols, keep="last", inplace=True)
                    combined_df.sort_values(by=dedup_cols, inplace=True)
                    combined_df.reset_index(drop=True, inplace=True)
                    final_df = combined_df
                elif not fetched_df.empty:
                    final_df = fetched_df
                    new_rows_count = len(fetched_df)
                else:
                    final_df = existing_df
                    new_rows_count = 0

                rows_added_total += new_rows_count
                total_rows_stored += len(final_df)

                meta.update(fetch_meta)
                meta["last_daily_update"] = datetime.now(timezone.utc).isoformat()
                meta["row_count"] = len(final_df)

                # Save updated series to Data Lake
                if not final_df.empty:
                    self.data_lake.save_dataframe(domain=domain, series_id=s_id, df=final_df, metadata=meta)
                    self.data_lake.save_json(domain=domain, series_id=s_id, data=meta, filename=f"{s_id}_meta.json")

                # 4. Gap & Missing Data Validation for daily series
                max_gap = 0
                missing_pct = 0.0
                gap_alerts: List[str] = []

                if freq == "daily" or s_type == "financial_daily":
                    max_gap, missing_pct, gap_alerts = self.check_daily_series_gaps(
                        final_df,
                        max_allowed_gap_bd=max_gap_limit,
                        missing_threshold=missing_threshold,
                    )

                    if gap_alerts:
                        for alt_msg in gap_alerts:
                            alert_record = {
                                "series_id": s_id,
                                "category": cat,
                                "message": alt_msg,
                                "max_gap_bd": max_gap,
                                "missing_pct": missing_pct,
                            }
                            alerts_raised.append(alert_record)
                            logger.warning(
                                f"DAILY DATA QUALITY ALERT [{s_id}]: {alt_msg}",
                                extra={
                                    "task": "daily_update",
                                    "series_id": s_id,
                                    "metrics": {"max_gap_bd": max_gap, "missing_pct": round(missing_pct, 4)},
                                    "alert": "MISSING_OR_GAP_ALERT",
                                },
                            )

                series_summary = {
                    "series_id": s_id,
                    "category": cat,
                    "rows_added": new_rows_count,
                    "total_rows": len(final_df),
                    "max_business_gap_days": max_gap,
                    "missing_pct": round(missing_pct, 4),
                    "status": "SUCCESS",
                }
                series_summaries.append(series_summary)

                logger.info(
                    f"Daily update complete for {s_id}: +{new_rows_count} rows (total: {len(final_df)})",
                    extra={
                        "task": "daily_update",
                        "series_id": s_id,
                        "metrics": {"rows_added": new_rows_count, "total_rows": len(final_df)},
                    },
                )

            except Exception as exc:
                err_msg = f"Failed daily update for series {s_id}: {exc}"
                logger.error(
                    err_msg,
                    extra={"task": "daily_update", "series_id": s_id, "alert": "INGESTION_ERROR"},
                    exc_info=True,
                )
                errors.append({"series_id": s_id, "error": str(exc)})

        duration_sec = round(time.time() - task_start, 3)

        result_summary = {
            "task": "daily_update",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_series_processed": total_series_count,
            "rows_added": rows_added_total,
            "total_rows_stored": total_rows_stored,
            "alerts_count": len(alerts_raised),
            "alerts": alerts_raised,
            "error_count": len(errors),
            "errors": errors,
            "duration_sec": duration_sec,
            "status": "WARNING" if (alerts_raised or errors) else "HEALTHY",
        }

        self._update_health_status("daily_update", result_summary)
        logger.info(
            f"=== Daily Incremental Update Completed in {duration_sec}s: +{rows_added_total} rows, "
            f"{len(alerts_raised)} alerts, {len(errors)} errors ===",
            extra={"task": "daily_update", "metrics": result_summary},
        )
        return result_summary

    def run_weekly_refresh(self, series_subset: Optional[List[str]] = None) -> Dict[str, Any]:
        """Requirement 2: Weekly ALFRED Vintage Refresh (run Sunday 8 AM ET).

        - Re-fetch vintage dates for macro series
        - Pull new vintage observations (batched x10)
        - Update data lake with point-in-time revisions
        - Log revision magnitude (mean absolute revision) & alert if > 2 std
        """
        task_start = time.time()
        logger.info("=== Starting Task 2: Weekly ALFRED Vintage Refresh (Sunday 8 AM ET) ===")

        macro_series = self.get_macro_series()
        if series_subset:
            macro_series = [s for s in macro_series if s["series_id"] in series_subset]

        weekly_cfg = self.config.get("schedule", {}).get("weekly_refresh", {})
        std_threshold = weekly_cfg.get("std_dev_revision_threshold", 2.0)

        total_vintages_fetched = 0
        total_rows_updated = 0
        all_revisions: List[float] = []
        errors: List[Dict[str, Any]] = []
        alerts_raised: List[Dict[str, Any]] = []
        series_summaries: List[Dict[str, Any]] = []

        for s_info in macro_series:
            s_id = s_info["series_id"]
            cat = s_info.get("category", "Uncategorized")
            domain = "alfred"

            try:
                # 1. Load existing data lake dataset before refresh
                old_df = pd.DataFrame()
                try:
                    old_df = _normalize_df(self.data_lake.load_dataframe(domain=domain, series_id=s_id))
                except Exception:
                    old_df = pd.DataFrame()

                # 2. Re-fetch vintage dates for macro series
                vintages = self.client.get_vintage_dates(s_id)
                vintage_count = len(vintages)
                total_vintages_fetched += vintage_count

                # 3. Pull new vintage observations (batched x10 internally by client)
                new_df, fetch_meta = self.client.get_series_observations(
                    series_id=s_id,
                    vintage_dates=vintages if vintages else None,
                )
                new_df = _normalize_df(new_df)

                # 4. Save point-in-time revisions to data lake
                meta = self.client.get_series_metadata(s_id)
                meta.update(fetch_meta)
                meta["category"] = cat
                meta["vintage_dates_count"] = vintage_count
                meta["last_weekly_refresh"] = datetime.now(timezone.utc).isoformat()

                if not new_df.empty:
                    self.data_lake.save_dataframe(domain=domain, series_id=s_id, df=new_df, metadata=meta)
                    self.data_lake.save_json(domain=domain, series_id=s_id, data=meta, filename=f"{s_id}_meta.json")

                total_rows_updated += len(new_df)

                # 5. Compute Revision Magnitude (Mean Absolute Revision)
                mean_abs_revision = 0.0
                std_revision = 0.0
                series_revisions: List[float] = []

                if not old_df.empty and not new_df.empty and "date" in old_df.columns and "date" in new_df.columns:
                    merged = pd.merge(old_df, new_df, on=["date"], suffixes=("_old", "_new"))
                    if "value_old" in merged.columns and "value_new" in merged.columns:
                        valid_pairs = merged.dropna(subset=["value_old", "value_new"])
                        rev_deltas = (valid_pairs["value_new"] - valid_pairs["value_old"]).abs()
                        nonzero_revs = rev_deltas[rev_deltas > 1e-9]

                        if not nonzero_revs.empty:
                            series_revisions = nonzero_revs.tolist()
                            all_revisions.extend(series_revisions)
                            mean_abs_revision = float(nonzero_revs.mean())
                            std_revision = float(nonzero_revs.std()) if len(nonzero_revs) > 1 else 0.0

                            # Alert check: revision magnitude > std_threshold * std_dev
                            max_rev = float(nonzero_revs.max())
                            if std_revision > 0 and max_rev > (std_threshold * std_revision):
                                alt_msg = (
                                    f"Revision peak of {max_rev:.4f} for {s_id} exceeds {std_threshold:.1f} std "
                                    f"dev threshold ({std_threshold * std_revision:.4f})"
                                )
                                alerts_raised.append({"series_id": s_id, "category": cat, "message": alt_msg})
                                logger.warning(
                                    f"WEEKLY VINTAGE REVISION ALERT [{s_id}]: {alt_msg}",
                                    extra={
                                        "task": "weekly_refresh",
                                        "series_id": s_id,
                                        "metrics": {
                                            "mean_abs_revision": round(mean_abs_revision, 4),
                                            "max_revision": round(max_rev, 4),
                                            "std_dev": round(std_revision, 4),
                                        },
                                        "alert": "REVISION_EXCEEDED_STD_DEV",
                                    },
                                )

                series_summary = {
                    "series_id": s_id,
                    "category": cat,
                    "vintage_count": vintage_count,
                    "rows_updated": len(new_df),
                    "mean_abs_revision": round(mean_abs_revision, 4),
                    "std_revision": round(std_revision, 4),
                    "status": "SUCCESS",
                }
                series_summaries.append(series_summary)

                logger.info(
                    f"Weekly vintage refresh complete for {s_id}: {vintage_count} vintages, "
                    f"MAR={mean_abs_revision:.4f}",
                    extra={
                        "task": "weekly_refresh",
                        "series_id": s_id,
                        "metrics": {
                            "vintages": vintage_count,
                            "rows": len(new_df),
                            "mean_abs_revision": round(mean_abs_revision, 4),
                        },
                    },
                )

            except Exception as exc:
                err_msg = f"Failed weekly vintage refresh for series {s_id}: {exc}"
                logger.error(
                    err_msg,
                    extra={"task": "weekly_refresh", "series_id": s_id, "alert": "VINTAGE_REFRESH_ERROR"},
                    exc_info=True,
                )
                errors.append({"series_id": s_id, "error": str(exc)})

        overall_mar = float(np.mean(all_revisions)) if all_revisions else 0.0
        overall_std = float(np.std(all_revisions)) if len(all_revisions) > 1 else 0.0
        duration_sec = round(time.time() - task_start, 3)

        result_summary = {
            "task": "weekly_refresh",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "macro_series_count": len(macro_series),
            "vintages_fetched": total_vintages_fetched,
            "rows_updated": total_rows_updated,
            "overall_mean_abs_revision": round(overall_mar, 4),
            "overall_revision_std": round(overall_std, 4),
            "alerts_count": len(alerts_raised),
            "alerts": alerts_raised,
            "error_count": len(errors),
            "errors": errors,
            "duration_sec": duration_sec,
            "status": "WARNING" if (alerts_raised or errors) else "HEALTHY",
        }

        self._update_health_status("weekly_refresh", result_summary)
        logger.info(
            f"=== Weekly ALFRED Vintage Refresh Completed in {duration_sec}s: "
            f"{total_vintages_fetched} vintages, overall MAR={overall_mar:.4f} ===",
            extra={"task": "weekly_refresh", "metrics": result_summary},
        )
        return result_summary

    def run_monthly_revalidation(self, series_subset: Optional[List[str]] = None) -> Dict[str, Any]:
        """Requirement 3: Monthly Full Revalidation (1st of month).

        - Full history integrity check for all 69 series
        - Compare row counts vs FRED API metadata
        - Rebuild any corrupted Parquet files
        - Generate data quality report
        """
        task_start = time.time()
        logger.info("=== Starting Task 3: Monthly Full Revalidation (1st of Month) ===")

        all_series = self.get_all_catalog_series()
        if series_subset:
            all_series = [s for s in all_series if s["series_id"] in series_subset]

        total_checked = len(all_series)
        rebuilt_count = 0
        healthy_count = 0
        revalidation_results: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []

        for s_info in all_series:
            s_id = s_info["series_id"]
            cat = s_info.get("category", "Uncategorized")
            s_type = s_info.get("type", "standard")
            domain = s_info.get("domain", "fred")

            is_corrupted = False
            rebuild_needed = False
            rebuild_reason = None
            local_row_count = 0
            api_row_count = 0

            try:
                # 1. File Integrity Check
                try:
                    local_df = _normalize_df(self.data_lake.load_dataframe(domain=domain, series_id=s_id))
                    if local_df.empty or "date" not in local_df.columns:
                        is_corrupted = True
                        rebuild_reason = "Parquet file empty or missing required 'date' column"
                    else:
                        local_row_count = len(local_df)
                except Exception as read_exc:
                    is_corrupted = True
                    rebuild_reason = f"Parquet read exception: {read_exc}"

                # 2. Compare row count vs FRED API metadata
                meta = self.client.get_series_metadata(s_id)
                fetched_df, fetch_meta = self.client.get_series_observations(s_id)
                fetched_df = _normalize_df(fetched_df)
                api_row_count = len(fetched_df)

                if not is_corrupted and local_row_count > 0:
                    row_diff_pct = abs(local_row_count - api_row_count) / float(max(api_row_count, 1))
                    if row_diff_pct > 0.15:
                        rebuild_needed = True
                        rebuild_reason = (
                            f"Row count mismatch > 15%: local={local_row_count}, API={api_row_count}"
                        )

                # 3. Rebuild Corrupted or Mismatched Parquet Files
                if is_corrupted or rebuild_needed:
                    logger.warning(
                        f"Rebuilding Parquet file for series {s_id} (Reason: {rebuild_reason})",
                        extra={
                            "task": "monthly_revalidation",
                            "series_id": s_id,
                            "alert": "REBUILDING_PARQUET",
                        },
                    )

                    # Re-ingest full history
                    meta.update(fetch_meta)
                    meta["category"] = cat
                    meta["series_type"] = s_type
                    meta["rebuilt_at"] = datetime.now(timezone.utc).isoformat()

                    self.data_lake.save_dataframe(domain=domain, series_id=s_id, df=fetched_df, metadata=meta)
                    self.data_lake.save_json(domain=domain, series_id=s_id, data=meta, filename=f"{s_id}_meta.json")

                    rebuilt_count += 1
                    status = "REBUILT"
                else:
                    healthy_count += 1
                    status = "HEALTHY"

                res_record = {
                    "series_id": s_id,
                    "category": cat,
                    "domain": domain,
                    "local_rows": local_row_count if not is_corrupted else 0,
                    "api_rows": api_row_count,
                    "status": status,
                    "rebuild_reason": rebuild_reason,
                }
                revalidation_results.append(res_record)

            except Exception as exc:
                err_msg = f"Failed monthly revalidation for series {s_id}: {exc}"
                logger.error(
                    err_msg,
                    extra={"task": "monthly_revalidation", "series_id": s_id, "alert": "REVALIDATION_ERROR"},
                    exc_info=True,
                )
                errors.append({"series_id": s_id, "error": str(exc)})

        duration_sec = round(time.time() - task_start, 3)

        # 4. Data Quality Report Generation
        data_quality_report = {
            "task": "monthly_revalidation",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_series_checked": total_checked,
            "healthy_count": healthy_count,
            "rebuilt_count": rebuilt_count,
            "error_count": len(errors),
            "execution_time_sec": duration_sec,
            "series_details": revalidation_results,
            "errors": errors,
        }

        # Save quality report to Data Lake
        self.data_lake.save_json(
            domain="reports",
            series_id="fred_data_quality",
            data=data_quality_report,
            filename=f"fred_data_quality_{datetime.now(timezone.utc).strftime('%Y%m%d')}.json",
        )

        result_summary = {
            "task": "monthly_revalidation",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_series_checked": total_checked,
            "healthy_count": healthy_count,
            "rebuilt_count": rebuilt_count,
            "error_count": len(errors),
            "errors": errors,
            "duration_sec": duration_sec,
            "status": "WARNING" if errors else "HEALTHY",
        }

        self._update_health_status("monthly_revalidation", result_summary)
        logger.info(
            f"=== Monthly Full Revalidation Completed in {duration_sec}s: "
            f"{healthy_count} healthy, {rebuilt_count} rebuilt, {len(errors)} errors ===",
            extra={"task": "monthly_revalidation", "metrics": result_summary},
        )
        return result_summary

    def run_all_tasks(self, series_subset: Optional[List[str]] = None) -> Dict[str, Any]:
        """Execute daily, weekly, and monthly refresh tasks sequentially."""
        logger.info("Executing full pipeline refresh (Daily + Weekly + Monthly)...")
        r_daily = self.run_daily_update(series_subset=series_subset)
        r_weekly = self.run_weekly_refresh(series_subset=series_subset)
        r_monthly = self.run_monthly_revalidation(series_subset=series_subset)

        return {
            "daily_update": r_daily,
            "weekly_refresh": r_weekly,
            "monthly_revalidation": r_monthly,
            "health_status": self.get_health_status(),
        }

    def _update_health_status(self, task_name: str, task_result: Dict[str, Any]) -> None:
        """Internal helper to update logs/fred_health_check.json."""
        current_health = self._read_health_file()
        tasks = current_health.get("tasks", {})

        scheduled = compute_next_scheduled_times()

        task_entry = {
            "last_successful_run": task_result.get("timestamp"),
            "next_scheduled": scheduled.get(task_name),
            "status": task_result.get("status", "HEALTHY"),
            "duration_sec": task_result.get("duration_sec", 0.0),
            "metrics": task_result,
        }
        tasks[task_name] = task_entry

        # Determine overall system health
        overall_status = "HEALTHY"
        for t_data in tasks.values():
            st = t_data.get("status")
            if st == "UNHEALTHY":
                overall_status = "UNHEALTHY"
                break
            elif st == "WARNING" and overall_status != "UNHEALTHY":
                overall_status = "WARNING"

        payload = {
            "status": overall_status,
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "next_scheduled": scheduled,
            "tasks": tasks,
        }

        try:
            with open(self.health_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as exc:
            logger.error(f"Failed writing health check file to {self.health_file}: {exc}")

    def _read_health_file(self) -> Dict[str, Any]:
        """Read health status file if it exists."""
        if self.health_file.exists():
            try:
                with open(self.health_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"status": "UNKNOWN", "tasks": {}}

    def get_health_status(self) -> Dict[str, Any]:
        """Requirement 4: Health Check Endpoint / Method.

        Returns status, last successful run, next scheduled run for each task.
        """
        health_data = self._read_health_file()
        if not health_data.get("tasks"):
            health_data = {
                "status": "UNKNOWN",
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "next_scheduled": compute_next_scheduled_times(),
                "tasks": {
                    "daily_update": {"status": "NEVER_RUN", "next_scheduled": compute_next_scheduled_times()["daily_update"]},
                    "weekly_refresh": {"status": "NEVER_RUN", "next_scheduled": compute_next_scheduled_times()["weekly_refresh"]},
                    "monthly_revalidation": {"status": "NEVER_RUN", "next_scheduled": compute_next_scheduled_times()["monthly_revalidation"]},
                },
            }
        return health_data

    def generate_task_scheduler_script(self) -> str:
        """Generate Windows Task Scheduler (schtasks) and Crontab commands for automated setup."""
        py_exe = sys.executable
        script_path = (project_root / "scripts" / "fred_scheduler.py").resolve()

        cmd_daily = f'schtasks /Create /TN "Quant_FRED_Daily_Update" /TR "{py_exe} {script_path} --daily" /SC DAILY /ST 06:00 /F'
        cmd_weekly = f'schtasks /Create /TN "Quant_FRED_Weekly_Refresh" /TR "{py_exe} {script_path} --weekly" /SC WEEKLY /D SUN /ST 08:00 /F'
        cmd_monthly = f'schtasks /Create /TN "Quant_FRED_Monthly_Revalidation" /TR "{py_exe} {script_path} --monthly" /SC MONTHLY /D 1 /ST 00:00 /F'

        cron_daily = f"0 6 * * * {py_exe} {script_path} --daily >> {self.log_file} 2>&1"
        cron_weekly = f"0 8 * * 0 {py_exe} {script_path} --weekly >> {self.log_file} 2>&1"
        cron_monthly = f"0 0 1 * * {py_exe} {script_path} --monthly >> {self.log_file} 2>&1"

        output = [
            "=" * 80,
            "FRED / ALFRED AUTOMATED PIPELINE SCHEDULER SETUP COMMANDS",
            "=" * 80,
            "\n--- WINDOWS TASK SCHEDULER COMMANDS (Run in Administrator PowerShell/CMD) ---",
            cmd_daily,
            cmd_weekly,
            cmd_monthly,
            "\n--- CRONTAB FORMAT (Linux / macOS) ---",
            cron_daily,
            cron_weekly,
            cron_monthly,
            "=" * 80,
        ]
        return "\n".join(output)


def main():
    parser = argparse.ArgumentParser(description="FRED / ALFRED Automated Refresher and Scheduler CLI")
    parser.add_argument("--daily", action="store_true", help="Run Daily Incremental Update (6 AM ET)")
    parser.add_argument("--weekly", action="store_true", help="Run Weekly ALFRED Vintage Refresh (Sunday 8 AM ET)")
    parser.add_argument("--monthly", action="store_true", help="Run Monthly Full Revalidation (1st of Month)")
    parser.add_argument("--all", action="store_true", help="Run Daily + Weekly + Monthly tasks")
    parser.add_argument("--health", action="store_true", help="Display system health check status")
    parser.add_argument("--install-tasks", action="store_true", help="Print Task Scheduler / crontab setup commands")
    parser.add_argument("--test-daily", action="store_true", help="Test run daily update on a sample subset of series")
    args = parser.parse_args()

    scheduler = FredScheduler()

    if args.health:
        health = scheduler.get_health_status()
        print(json.dumps(health, indent=2))
        return

    if args.install_tasks:
        print(scheduler.generate_task_scheduler_script())
        return

    if args.test_daily:
        print("Running test daily update on representative sample series (DGS10, UNRATE, GDP)...")
        res = scheduler.run_daily_update(series_subset=["DGS10", "UNRATE", "GDP"])
        print("\nTest Daily Update Summary:")
        print(json.dumps(res, indent=2))
        return

    if args.daily:
        res = scheduler.run_daily_update()
        print(json.dumps(res, indent=2))
    elif args.weekly:
        res = scheduler.run_weekly_refresh()
        print(json.dumps(res, indent=2))
    elif args.monthly:
        res = scheduler.run_monthly_revalidation()
        print(json.dumps(res, indent=2))
    elif args.all:
        res = scheduler.run_all_tasks()
        print(json.dumps(res, indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
