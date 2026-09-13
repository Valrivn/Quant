"""FRED and ALFRED Ingestion Pipeline.

Provides an API client and ingestion pipeline for Federal Reserve Economic Data (FRED)
and ALFRED (ArchivaL Federal Reserve Economic Data) point-in-time vintage data.

Features:
- Full FRED REST API client support with API key or public CSV fallback
- ALFRED real-time vintage data (realtime_start, realtime_end, vintage_dates)
- Automatic rate limiting (120 req/min compliance)
- Pagination handling (limit/offset)
- Retries with exponential backoff and jitter
- Data Lake storage for series metadata and observations
- High-value quantitative finance macro series across 7 core categories
- Automatic batching (x10) for ALFRED vintage dates
- Comprehensive cataloging and execution reporting
"""

from __future__ import annotations

import logging
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import pandas as pd
import requests

from ingestion.data_lake import DataLakeStore

logger = logging.getLogger(__name__)

# Series Aliases (Mapping user/FRED-MD symbols to official FRED API IDs)
SERIES_ALIASES: Dict[str, str] = {
    "CLAIMSx": "ICSA",
    "CONTINUE": "CCSA",
    "JTSQUIT": "JTSQUL",
    "LNS14000000": "UNEMPLOY",
    "CPIMFSSL": "CPIUFDSL",
    "MOVE": "VXOCLS",
    "T10YIFR": "T5YIFR",
}

def resolve_series_id(series_id: str) -> str:
    """Resolve symbol alias to official FRED series ID."""
    return SERIES_ALIASES.get(series_id, series_id)


# Key series mandated by baseline requirements
KEY_ECONOMIC_SERIES = {
    "GDP": "Gross Domestic Product",
    "CPIAUCSL": "Consumer Price Index for All Urban Consumers: All Items",
    "UNRATE": "Unemployment Rate",
    "FEDFUNDS": "Federal Funds Effective Rate",
    "DGS10": "Market Yield on U.S. Treasury Securities at 10-Year Constant Maturity",
    "DGS2": "Market Yield on U.S. Treasury Securities at 2-Year Constant Maturity",
    "INDPRO": "Industrial Production Index",
    "RSAFS": "Advance Retail Sales: Retail and Food Services",
    "RSXFS": "Advance Retail Sales: Retail Trade Excluding Food Services",
    "HOUST": "Housing Starts: Total New Privately Owned Housing Units Started",
}


# High-Value Quantitative Finance Macro & Financial Series Catalog (7 Categories)
EXPANDED_SERIES_CATALOG: Dict[str, List[Dict[str, Any]]] = {
    "Yield Curve & Rates": [
        {"series_id": "DGS1MO", "title": "1-Month Treasury Constant Maturity Rate", "frequency": "daily", "type": "financial_daily"},
        {"series_id": "DGS3MO", "title": "3-Month Treasury Constant Maturity Rate", "frequency": "daily", "type": "financial_daily"},
        {"series_id": "DGS6MO", "title": "6-Month Treasury Constant Maturity Rate", "frequency": "daily", "type": "financial_daily"},
        {"series_id": "DGS1", "title": "1-Year Treasury Constant Maturity Rate", "frequency": "daily", "type": "financial_daily"},
        {"series_id": "DGS2", "title": "2-Year Treasury Constant Maturity Rate", "frequency": "daily", "type": "financial_daily"},
        {"series_id": "DGS3", "title": "3-Year Treasury Constant Maturity Rate", "frequency": "daily", "type": "financial_daily"},
        {"series_id": "DGS5", "title": "5-Year Treasury Constant Maturity Rate", "frequency": "daily", "type": "financial_daily"},
        {"series_id": "DGS7", "title": "7-Year Treasury Constant Maturity Rate", "frequency": "daily", "type": "financial_daily"},
        {"series_id": "DGS10", "title": "10-Year Treasury Constant Maturity Rate", "frequency": "daily", "type": "financial_daily"},
        {"series_id": "DGS20", "title": "20-Year Treasury Constant Maturity Rate", "frequency": "daily", "type": "financial_daily"},
        {"series_id": "DGS30", "title": "30-Year Treasury Constant Maturity Rate", "frequency": "daily", "type": "financial_daily"},
        {"series_id": "T10Y2Y", "title": "10-Year Treasury Constant Maturity Minus 2-Year Treasury Constant Maturity", "frequency": "daily", "type": "financial_daily"},
        {"series_id": "T10Y3M", "title": "10-Year Treasury Constant Maturity Minus 3-Month Treasury Constant Maturity", "frequency": "daily", "type": "financial_daily"},
        {"series_id": "T5YIE", "title": "5-Year Breakeven Inflation Rate", "frequency": "daily", "type": "financial_daily"},
        {"series_id": "T10YIE", "title": "10-Year Breakeven Inflation Rate", "frequency": "daily", "type": "financial_daily"},
    ],
    "Fed & Money": [
        {"series_id": "WALCL", "title": "Assets: Total Assets: Total Assets (Less Eliminations from Consolidation)", "frequency": "weekly", "type": "macro"},
        {"series_id": "RRPONTSYD", "title": "Overnight Reverse Repurchase Agreements: Total", "frequency": "daily", "type": "financial_daily"},
        {"series_id": "WRESBAL", "title": "Reserve Balances with Federal Reserve Banks", "frequency": "weekly", "type": "macro"},
        {"series_id": "DFEDTARU", "title": "Federal Funds Target Range - Upper Limit", "frequency": "daily", "type": "financial_daily"},
        {"series_id": "DFEDTARL", "title": "Federal Funds Target Range - Lower Limit", "frequency": "daily", "type": "financial_daily"},
        {"series_id": "EFFR", "title": "Effective Federal Funds Rate", "frequency": "daily", "type": "financial_daily"},
        {"series_id": "FEDFUNDS", "title": "Federal Funds Effective Rate", "frequency": "monthly", "type": "macro"},
        {"series_id": "M1SL", "title": "M1 Money Stock", "frequency": "monthly", "type": "macro"},
        {"series_id": "M2SL", "title": "M2 Money Stock", "frequency": "monthly", "type": "macro"},
        {"series_id": "BOGMBASE", "title": "Monetary Base; Total", "frequency": "monthly", "type": "macro"},
    ],
    "Labor Market": [
        {"series_id": "UNRATE", "title": "Unemployment Rate", "frequency": "monthly", "type": "macro"},
        {"series_id": "U6RATE", "title": "Total Unemployed, Plus All Marginally Attached Workers + Part-Time for Economic Reasons", "frequency": "monthly", "type": "macro"},
        {"series_id": "PAYEMS", "title": "All Employees, Total Nonfarm", "frequency": "monthly", "type": "macro"},
        {"series_id": "CIVPART", "title": "Labor Force Participation Rate", "frequency": "monthly", "type": "macro"},
        {"series_id": "EMRATIO", "title": "Employment-Population Ratio", "frequency": "monthly", "type": "macro"},
        {"series_id": "CLAIMSx", "title": "Initial Claims", "frequency": "weekly", "type": "macro"},
        {"series_id": "CONTINUE", "title": "Continued Claims (Insured Unemployment)", "frequency": "weekly", "type": "macro"},
        {"series_id": "JTSJOL", "title": "Job Openings: Total Nonfarm", "frequency": "monthly", "type": "macro"},
        {"series_id": "JTSHIR", "title": "Hires: Total Nonfarm", "frequency": "monthly", "type": "macro"},
        {"series_id": "JTSQUIT", "title": "Quits: Total Nonfarm", "frequency": "monthly", "type": "macro"},
        {"series_id": "LNS14000000", "title": "Unemployment Level", "frequency": "monthly", "type": "macro"},
    ],
    "Inflation": [
        {"series_id": "CPIAUCSL", "title": "Consumer Price Index for All Urban Consumers: All Items", "frequency": "monthly", "type": "macro"},
        {"series_id": "CPILFESL", "title": "Consumer Price Index for All Urban Consumers: All Items Less Food and Energy", "frequency": "monthly", "type": "macro"},
        {"series_id": "PCEPI", "title": "Personal Consumption Expenditures: Chain-type Price Index", "frequency": "monthly", "type": "macro"},
        {"series_id": "PCEPILFE", "title": "Personal Consumption Expenditures Excluding Food and Energy", "frequency": "monthly", "type": "macro"},
        {"series_id": "CPIMEDSL", "title": "Median Consumer Price Index", "frequency": "monthly", "type": "macro"},
        {"series_id": "CPIMFSSL", "title": "Consumer Price Index for All Urban Consumers: Food", "frequency": "monthly", "type": "macro"},
        {"series_id": "T5YIFR", "title": "5-Year, 5-Year Forward Inflation Expectation Rate", "frequency": "daily", "type": "financial_daily"},
        {"series_id": "T10YIFR", "title": "10-Year Inflation Expectation Rate", "frequency": "daily", "type": "financial_daily"},
    ],
    "Real Economy": [
        {"series_id": "GDP", "title": "Gross Domestic Product", "frequency": "quarterly", "type": "macro"},
        {"series_id": "GDPC1", "title": "Real Gross Domestic Product", "frequency": "quarterly", "type": "macro"},
        {"series_id": "GPDI", "title": "Gross Private Domestic Investment", "frequency": "quarterly", "type": "macro"},
        {"series_id": "PCEC", "title": "Personal Consumption Expenditures", "frequency": "monthly", "type": "macro"},
        {"series_id": "INDPRO", "title": "Industrial Production Index", "frequency": "monthly", "type": "macro"},
        {"series_id": "CUMFNS", "title": "Capacity Utilization: Manufacturing", "frequency": "monthly", "type": "macro"},
        {"series_id": "RSAFS", "title": "Advance Retail Sales: Retail and Food Services", "frequency": "monthly", "type": "macro"},
        {"series_id": "RSXFS", "title": "Advance Retail Sales: Retail Trade Excluding Food Services", "frequency": "monthly", "type": "macro"},
        {"series_id": "HOUST", "title": "Housing Starts: Total New Privately Owned Housing Units Started", "frequency": "monthly", "type": "macro"},
        {"series_id": "PERMIT", "title": "New Private Housing Units Authorized by Building Permits", "frequency": "monthly", "type": "macro"},
        {"series_id": "HSN1F", "title": "New Single-Family Houses Sold", "frequency": "monthly", "type": "macro"},
        {"series_id": "TOTBKCR", "title": "Bank Credit, All Commercial Banks", "frequency": "weekly", "type": "macro"},
    ],
    "Financial Conditions": [
        {"series_id": "NFCI", "title": "Chicago Fed National Financial Conditions Index", "frequency": "weekly", "type": "macro"},
        {"series_id": "ANFCI", "title": "Adjusted Chicago Fed National Financial Conditions Index", "frequency": "weekly", "type": "macro"},
        {"series_id": "BAA10Y", "title": "Moody's Seasoned Baa Corporate Bond Yield Relative to 10-Year Treasury Constant Maturity", "frequency": "daily", "type": "financial_daily"},
        {"series_id": "AAA10Y", "title": "Moody's Seasoned Aaa Corporate Bond Yield Relative to 10-Year Treasury Constant Maturity", "frequency": "daily", "type": "financial_daily"},
        {"series_id": "DBAA", "title": "Moody's Seasoned Baa Corporate Bond Yield", "frequency": "daily", "type": "financial_daily"},
        {"series_id": "VIXCLS", "title": "CBOE Volatility Index: VIX", "frequency": "daily", "type": "financial_daily"},
        {"series_id": "MOVE", "title": "CBOE Volatility / MOVE Index", "frequency": "daily", "type": "financial_daily"},
    ],
    "International": [
        {"series_id": "DTWEXBGS", "title": "Nominal Broad U.S. Dollar Index", "frequency": "daily", "type": "financial_daily"},
        {"series_id": "DTWEXM", "title": "Nominal Major Currencies U.S. Dollar Index", "frequency": "daily", "type": "financial_daily"},
        {"series_id": "EXCHUS", "title": "China / U.S. Foreign Exchange Rate", "frequency": "monthly", "type": "financial_daily"},
        {"series_id": "EXJPUS", "title": "Japan / U.S. Foreign Exchange Rate", "frequency": "monthly", "type": "financial_daily"},
        {"series_id": "EXUSEU", "title": "U.S. / Euro Foreign Exchange Rate", "frequency": "monthly", "type": "financial_daily"},
        {"series_id": "INTDSRUSM193N", "title": "Interest Rates, Discount Rate for United States", "frequency": "monthly", "type": "financial_daily"},
    ],
}


def _lookup_catalog_info(series_id: str) -> Optional[Dict[str, Any]]:
    """Search catalog info for series in expanded catalog."""
    for cat, series_list in EXPANDED_SERIES_CATALOG.items():
        for s in series_list:
            if s["series_id"] == series_id:
                return dict(s)
    return None


def _lookup_catalog_title(series_id: str) -> Optional[str]:
    """Search title for series in expanded catalog or key series map."""
    if series_id in KEY_ECONOMIC_SERIES:
        return KEY_ECONOMIC_SERIES[series_id]
    for cat, series_list in EXPANDED_SERIES_CATALOG.items():
        for s in series_list:
            if s["series_id"] == series_id:
                return s["title"]
    return None


class FredAlfredClient:
    """Client for FRED / ALFRED REST APIs with fallback and rate limiting."""

    BASE_URL = "https://api.stlouisfed.org/fred"

    def __init__(
        self,
        api_key: Optional[str] = None,
        rate_limit_per_min: int = 120,
        max_retries: int = 5,
        backoff_factor: float = 1.0,
    ):
        self.api_key = api_key or os.environ.get("FRED_API_KEY", "")
        self.min_interval = 60.0 / max(rate_limit_per_min, 1)
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self._last_request_time = 0.0
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "HouseOfQuant/1.0 Ingestion Engine",
            "Accept": "application/json",
        })

    def _throttle(self) -> None:
        """Enforce rate limits between HTTP requests."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.min_interval:
            sleep_sec = self.min_interval - elapsed
            time.sleep(sleep_sec)
        self._last_request_time = time.time()

    def _request(self, endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Make an authenticated GET request with rate limiting and exponential retries."""
        url = f"{self.BASE_URL}/{endpoint}"
        query_params = dict(params)
        query_params["file_type"] = "json"
        if self.api_key:
            query_params["api_key"] = self.api_key

        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                response = self.session.get(url, params=query_params, timeout=30)
                if response.status_code == 200:
                    return response.json()
                elif response.status_code in (429, 500, 502, 503, 504):
                    backoff = (self.backoff_factor * (2 ** (attempt - 1))) + random.uniform(0, 0.5)
                    logger.warning(
                        f"FRED API HTTP {response.status_code} for {endpoint} "
                        f"(attempt {attempt}/{self.max_retries}), retrying in {backoff:.2f}s..."
                    )
                    time.sleep(backoff)
                else:
                    response.raise_for_status()
            except (requests.RequestException, ValueError) as exc:
                if attempt == self.max_retries:
                    logger.error(f"FRED API request failed for {endpoint} after {self.max_retries} attempts: {exc}")
                    raise
                backoff = (self.backoff_factor * (2 ** (attempt - 1))) + random.uniform(0, 0.5)
                time.sleep(backoff)

        raise RuntimeError(f"Failed to execute FRED request: {endpoint}")

    def get_series_metadata(self, series_id: str) -> Dict[str, Any]:
        """Fetch metadata for a series."""
        fred_id = resolve_series_id(series_id)
        cat_info = _lookup_catalog_info(series_id)
        now_iso = datetime.now(timezone.utc).isoformat()

        if not self.api_key:
            title = (cat_info.get("title") if cat_info else None) or _lookup_catalog_title(series_id) or series_id
            meta = {
                "id": series_id,
                "fred_id": fred_id,
                "title": title,
                "source": "FRED_PUBLIC_CSV_FALLBACK",
                "last_updated": now_iso,
            }
            if cat_info and "frequency" in cat_info:
                meta["frequency"] = cat_info["frequency"]
            if cat_info and "type" in cat_info:
                meta["series_type"] = cat_info["type"]
            return meta

        data = self._request("series", {"series_id": fred_id})
        series_list = data.get("seriess", [])
        if series_list:
            meta = dict(series_list[0])
            meta["requested_id"] = series_id
            meta["fred_id"] = fred_id
            meta["source"] = "FRED_API"
            if "last_updated" not in meta or not meta["last_updated"]:
                meta["last_updated"] = now_iso
            if cat_info and "frequency" in cat_info:
                meta["frequency"] = cat_info["frequency"]
            if cat_info and "type" in cat_info:
                meta["series_type"] = cat_info["type"]
            return meta

        meta = {
            "id": series_id,
            "fred_id": fred_id,
            "title": _lookup_catalog_title(series_id) or series_id,
            "source": "FRED_API",
            "last_updated": now_iso,
        }
        if cat_info and "frequency" in cat_info:
            meta["frequency"] = cat_info["frequency"]
        if cat_info and "type" in cat_info:
            meta["series_type"] = cat_info["type"]
        return meta

    def get_vintage_dates(
        self,
        series_id: str,
        realtime_start: Optional[str] = None,
        realtime_end: Optional[str] = None,
    ) -> List[str]:
        """Fetch ALFRED vintage dates for a series."""
        fred_id = resolve_series_id(series_id)
        if not self.api_key:
            return []

        params: Dict[str, Any] = {"series_id": fred_id}
        if realtime_start:
            params["realtime_start"] = realtime_start
        if realtime_end:
            params["realtime_end"] = realtime_end

        data = self._request("series/vintagedates", params)
        return data.get("vintage_dates", [])

    def get_series_observations(
        self,
        series_id: str,
        realtime_start: Optional[str] = None,
        realtime_end: Optional[str] = None,
        vintage_dates: Optional[List[str]] = None,
        observation_start: Optional[str] = None,
        observation_end: Optional[str] = None,
        units: str = "lin",
        frequency: Optional[str] = None,
        limit: int = 100000,
    ) -> Tuple[pd.DataFrame, Dict[str, Any]]:
        """Fetch series observations from FRED or ALFRED (with vintage support).

        Handles pagination automatically if observation count exceeds limit.

        Returns:
            Tuple of (DataFrame with columns [date, value, realtime_start, realtime_end], pagination_meta)
        """
        fred_id = resolve_series_id(series_id)
        if not self.api_key:
            # Fallback to public CSV download if API key is not present
            return self._fetch_public_csv_fallback(series_id, observation_start, observation_end)

        records: List[Dict[str, Any]] = []
        res: Dict[str, Any] = {}
        page_size = min(limit, 100000)

        params: Dict[str, Any] = {
            "series_id": fred_id,
            "units": units,
            "limit": page_size,
        }
        if realtime_start:
            params["realtime_start"] = realtime_start
        if realtime_end:
            params["realtime_end"] = realtime_end
        if observation_start:
            params["observation_start"] = observation_start
        if observation_end:
            params["observation_end"] = observation_end
        if frequency:
            params["frequency"] = frequency

        if vintage_dates:
            # Batch vintage dates in chunks of up to 10 (FRED API requirement)
            chunks = [vintage_dates[i:i + 10] for i in range(0, len(vintage_dates), 10)]
            all_obs: List[Dict[str, Any]] = []
            for chunk in chunks:
                p = dict(params)
                p["vintage_dates"] = ",".join(chunk)
                offset = 0
                while True:
                    p["offset"] = offset
                    res = self._request("series/observations", p)
                    obs = res.get("observations", [])
                    all_obs.extend(obs)
                    batch_total = int(res.get("count", len(obs)))
                    offset += len(obs)
                    if not obs or offset >= batch_total or len(all_obs) >= limit:
                        break
        else:
            all_obs: List[Dict[str, Any]] = []
            offset = 0
            total_obs = None
            while True:
                params["offset"] = offset
                res = self._request("series/observations", params)
                obs = res.get("observations", [])
                all_obs.extend(obs)

                if total_obs is None:
                    total_obs = int(res.get("count", len(obs)))

                offset += len(obs)
                if not obs or offset >= total_obs or len(all_obs) >= limit:
                    break

        for o in all_obs:
            val_str = o.get("value", "")
            val = float(val_str) if val_str and val_str != "." else None
            records.append({
                "date": o.get("date"),
                "value": val,
                "realtime_start": o.get("realtime_start"),
                "realtime_end": o.get("realtime_end"),
            })

        df = pd.DataFrame(records)
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"])
            df.drop_duplicates(subset=["date", "realtime_start", "realtime_end", "value"], inplace=True)
            df.sort_values(by=["date", "realtime_start"], inplace=True)
            df.reset_index(drop=True, inplace=True)

        meta = {
            "count": len(df),
            "total_count": len(all_obs),
            "realtime_start": res.get("realtime_start", realtime_start or "1776-07-04"),
            "realtime_end": res.get("realtime_end", realtime_end or "9999-12-31"),
        }

        return df, meta

    def _fetch_public_csv_fallback(
        self,
        series_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Tuple[pd.DataFrame, Dict[str, Any]]:
        """Fallback method fetching raw FRED CSV endpoint when no API key is provided."""
        fred_id = resolve_series_id(series_id)
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={fred_id}"
        self._throttle()
        logger.info(f"FRED API key missing — downloading public CSV for {series_id} (FRED ID: {fred_id})")

        res = self.session.get(url, timeout=30)
        res.raise_for_status()

        from io import StringIO
        df = pd.read_csv(StringIO(res.text))

        if not df.empty and "DATE" in df.columns:
            val_cols = [c for c in df.columns if c != "DATE"]
            val_col = val_cols[0] if val_cols else series_id
            df.rename(columns={"DATE": "date", val_col: "value"}, inplace=True)
            df["value"] = pd.to_numeric(df["value"], errors="coerce")
            df["date"] = pd.to_datetime(df["date"])
            df["realtime_start"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            df["realtime_end"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            if start_date:
                df = df[df["date"] >= pd.to_datetime(start_date)]
            if end_date:
                df = df[df["date"] <= pd.to_datetime(end_date)]

            df.sort_values("date", inplace=True)
            df.reset_index(drop=True, inplace=True)

        meta = {
            "count": len(df),
            "total_count": len(df),
            "source": "FRED_PUBLIC_CSV_FALLBACK",
            "requested_series_id": series_id,
            "fred_series_id": fred_id,
        }
        return df, meta


class FredIngestionPipeline:
    """Orchestrates FRED and ALFRED dataset ingestion into the Data Lake."""

    def __init__(
        self,
        data_lake: Optional[DataLakeStore] = None,
        client: Optional[FredAlfredClient] = None,
    ):
        self.data_lake = data_lake or DataLakeStore()
        self.client = client or FredAlfredClient()

    def ingest_series(
        self,
        series_id: str,
        realtime_start: Optional[str] = None,
        realtime_end: Optional[str] = None,
        vintage_dates: Optional[List[str]] = None,
        observation_start: Optional[str] = None,
        observation_end: Optional[str] = None,
        domain: str = "fred",
        category: Optional[str] = None,
        series_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Ingest a single FRED or ALFRED series into the data lake."""
        start_time = time.time()
        logger.info(f"Starting ingestion for series '{series_id}' into domain '{domain}'")

        # 1. Metadata
        metadata = self.client.get_series_metadata(series_id)
        cat_info = _lookup_catalog_info(series_id)
        if cat_info:
            if "frequency" in cat_info:
                metadata["frequency"] = cat_info["frequency"]
            if "type" in cat_info:
                metadata["series_type"] = cat_info["type"]

        if category:
            metadata["category"] = category
        if series_type:
            metadata["series_type"] = series_type

        # Ensure required provenance fields
        if "source" not in metadata or not metadata["source"]:
            metadata["source"] = "FRED_API" if self.client.api_key else "FRED_PUBLIC_CSV_FALLBACK"
        if "last_updated" not in metadata or not metadata["last_updated"]:
            metadata["last_updated"] = datetime.now(timezone.utc).isoformat()

        # 2. Vintages (if ALFRED or vintage requested)
        vintages = []
        if domain == "alfred" or realtime_start or realtime_end or vintage_dates:
            vintages = self.client.get_vintage_dates(series_id, realtime_start, realtime_end)
            metadata["vintage_dates_count"] = len(vintages)

        # 3. Observations
        df, fetch_meta = self.client.get_series_observations(
            series_id=series_id,
            realtime_start=realtime_start,
            realtime_end=realtime_end,
            vintage_dates=vintage_dates or (vintages if vintages else None),
            observation_start=observation_start,
            observation_end=observation_end,
        )

        metadata.update(fetch_meta)
        metadata["ingested_at"] = datetime.now(timezone.utc).isoformat()
        metadata["execution_time_sec"] = round(time.time() - start_time, 3)

        # 4. Save to Data Lake
        json_path = self.data_lake.save_json(
            domain=domain,
            series_id=series_id,
            data=metadata,
            metadata=metadata,
            filename=f"{series_id}_meta.json",
        )
        parquet_path = self.data_lake.save_dataframe(
            domain=domain,
            series_id=series_id,
            df=df,
            metadata=metadata,
        )

        report = {
            "series_id": series_id,
            "fred_id": resolve_series_id(series_id),
            "domain": domain,
            "category": category or "Uncategorized",
            "series_type": series_type or "standard",
            "status": "SUCCESS",
            "row_count": len(df),
            "vintage_count": len(vintages),
            "parquet_path": str(parquet_path),
            "meta_path": str(json_path),
            "execution_time_sec": metadata["execution_time_sec"],
        }
        logger.info(f"Ingestion complete for {series_id}: {len(df)} rows in {metadata['execution_time_sec']}s")
        return report

    def ingest_key_series(self, domain: str = "fred") -> Dict[str, Dict[str, Any]]:
        """Ingest all key economic series (GDP, CPI, Unemployment, Yields, etc.)."""
        results = {}
        for s_id, name in KEY_ECONOMIC_SERIES.items():
            logger.info(f"Ingesting key series: {s_id} ({name})")
            try:
                res = self.ingest_series(series_id=s_id, domain=domain)
                results[s_id] = res
            except Exception as exc:
                logger.error(f"Failed to ingest key series {s_id}: {exc}")
                results[s_id] = {"series_id": s_id, "status": "FAILED", "error": str(exc)}
        return results

    def ingest_expanded_catalog(
        self,
        categories: Optional[List[str]] = None,
        realtime_start: Optional[str] = None,
        realtime_end: Optional[str] = None,
        observation_start: Optional[str] = None,
        observation_end: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Ingest all macro and financial series defined in EXPANDED_SERIES_CATALOG.

        - Macro series (monthly/quarterly/weekly): fetched with ALFRED vintages (batched x10)
        - Daily financial series: standard FRED observations
        - Cataloged and stored in Data Lake

        Returns aggregate summary report containing:
            total_series, total_rows, total_vintages, execution_time_sec, category summaries.
        """
        pipeline_start = time.time()
        logger.info("Executing ingestion for expanded FRED/ALFRED series catalog...")

        total_series = 0
        total_rows = 0
        total_vintages = 0
        category_summaries: Dict[str, Dict[str, Any]] = {}
        series_reports: List[Dict[str, Any]] = []

        target_cats = categories if categories else list(EXPANDED_SERIES_CATALOG.keys())

        for cat_name in target_cats:
            if cat_name not in EXPANDED_SERIES_CATALOG:
                logger.warning(f"Category '{cat_name}' not found in EXPANDED_SERIES_CATALOG")
                continue

            series_list = EXPANDED_SERIES_CATALOG[cat_name]
            cat_rows = 0
            cat_vintages = 0
            cat_series_count = 0

            logger.info(f"--- Processing Category: {cat_name} ({len(series_list)} series) ---")

            for s_info in series_list:
                s_id = s_info["series_id"]
                s_type = s_info.get("type", "macro")
                
                # Determine domain: 'alfred' for macro series vintage tracking, 'fred' for daily financial
                domain = "alfred" if s_type == "macro" else "fred"

                try:
                    rep = self.ingest_series(
                        series_id=s_id,
                        realtime_start=realtime_start,
                        realtime_end=realtime_end,
                        observation_start=observation_start,
                        observation_end=observation_end,
                        domain=domain,
                        category=cat_name,
                        series_type=s_type,
                    )
                    series_reports.append(rep)
                    
                    cat_series_count += 1
                    cat_rows += rep.get("row_count", 0)
                    cat_vintages += rep.get("vintage_count", 0)

                    total_series += 1
                    total_rows += rep.get("row_count", 0)
                    total_vintages += rep.get("vintage_count", 0)
                except Exception as exc:
                    logger.error(f"Failed ingestion for {s_id} in category '{cat_name}': {exc}")
                    series_reports.append({
                        "series_id": s_id,
                        "category": cat_name,
                        "status": "FAILED",
                        "error": str(exc),
                    })

            category_summaries[cat_name] = {
                "series_count": cat_series_count,
                "rows": cat_rows,
                "vintages": cat_vintages,
            }

        elapsed_sec = round(time.time() - pipeline_start, 3)

        summary_report = {
            "total_series": total_series,
            "total_rows": total_rows,
            "total_vintages": total_vintages,
            "execution_time_sec": elapsed_sec,
            "categories": category_summaries,
            "series_reports": series_reports,
        }

        # Print formatted executive summary report
        self._print_execution_report(summary_report)

        return summary_report

    def _print_execution_report(self, report: Dict[str, Any]) -> None:
        """Format and print ingestion execution report."""
        sep = "=" * 80
        sub_sep = "-" * 80
        print("\n" + sep)
        print("          FRED / ALFRED HIGH-VALUE MACRO INGESTION REPORT          ")
        print(sep)
        print(f" Total Series Ingested : {report['total_series']}")
        print(f" Total Rows Stored     : {report['total_rows']:,}")
        print(f" Total Vintages Fetched: {report['total_vintages']:,}")
        print(f" Execution Time        : {report['execution_time_sec']}s")
        print(sub_sep)
        print(" CATEGORY BREAKDOWN:")
        for cat_name, cat_data in report["categories"].items():
            print(
                f"  - {cat_name:<24}: {cat_data['series_count']:>2} series | "
                f"{cat_data['rows']:>8,} rows | {cat_data['vintages']:>5,} vintages"
            )
        print(sep + "\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    pipeline = FredIngestionPipeline()
    report = pipeline.ingest_expanded_catalog()

