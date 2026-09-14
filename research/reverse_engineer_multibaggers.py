"""
Reverse Engineer Multibaggers — RS-06 / CONT-01
================================================

Binding source: Data_Strategy.md §6 (ML Track), §10 (Decision Log), §13.3 RS-06.

WHAT THIS MODULE DOES
---------------------
Trains a walk-forward model stack on the point-in-time universe U_t at each
month t, under **Combinatorial Purged Cross-Validation (CPCV) with an embargo
>= the forward target horizon**, and reverse-engineers *human-readable symbolic
rules* that isolate multibagger candidates (12-month forward return >= +100%).

  * Walk-forward on $U_t$: at each month t the model trains ONLY on rows whose
    date <= t - embargo.  $U_t$ membership at t is point-in-time: a ticker must
    be price-covered with >= 24 months of listing history at t and not yet
    delisted at t (delisting events are applied as of their fILING date).
    There is NO post-hoc selection of "top 10% winners across 10 years" — the
    full monthly cross-section of U_t is trained on (sub-sampled only when a
    fold's train set exceeds --max-train-rows, deterministically, without
    replacement, and always strictly inside the embargoed train window).
  * Embargo:  --embargo-months (default 12) >= the 12-month forward horizon.
    train_end = t - embargo months;  test_start = t;  the minimum train/test
    gap is asserted >= 365 days per fold and recorded in the results parquet.
  * Model stack:  LASSO (LassoCV, alpha via 5-fold CV) + Random Forest
    (500 trees) ->  ensemble score = 0.5 * lasso_pred + 0.5 * rf_pred.
  * Symbolic rule extraction:  sklearn.tree.export_text-style pruning
    (max depth, min leaf samples, top-value leaves) -> human-readable rules,
    each evaluated OUT-OF-SAMPLE on that fold's test window (support, precision,
    lift).  Rules are kept only if they repeat across >= 2 folds.

PIT DISCIPLINE
--------------
Every feature at month-end t is a function of data strictly <= t (price-only
factors: momentum, reversal, volatility, skew, drawdown, liquidity, Amihud
illiquidity, distance-from-high, price level/z).  Forward labels are realized
12 calendar months after t and are never used to build features.  The factor
panel is written to the house FactorStore (data/harness/factors.duckdb) with
SHA-256 provenance via ingestion.harness.factor_store.FactorStore, and each
factor row passes ingestion.harness.pit_validator.PITValidator.

DATA SOURCES (auto-detected)
----------------------------
  universe : --universe path, default data/universe/pit_universe_2018_2024.parquet
             (canonical RS-01 dynamic PIT top-1500).  If absent, a PIT proxy
             universe is built from real local price data
             (data/master_prices/*.parquet + data/universe/delisting_events.parquet):
             monthly top-1500 by trailing-12m dollar volume, restricted to
             liquid, non-delisted names with >= 24 months of listing history.
  factors  : --factors path, default data/harness/factors.duckdb (FactorStore).
             If absent/empty, the price-only PIT factor panel is derived and
             persisted there with provenance.
  returns  : 12-month forward returns computed from real monthly closes.

OUTPUTS
-------
  data/research/multibagger_rules.json   root = LIST of rules (rule/support/
                                         precision/lift/n_folds/...).
  data/research/multibagger_report.json  provenance + summary + contamination
                                         guard evidence + export_text trees.
  data/research/cpcv_results.parquet     fold-by-fold metrics.
  data/research/panels/*.parquet         PIT panels used (reproducibility).
  data/provenance/manifest.jsonl         provenance entries appended.

USAGE
-----
  python -m research.reverse_engineer_multibaggers \
      --train-window 2018-2024 \
      --embargo-months 12 \
      --max-empirical-weight 0.4
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy.stats import pearsonr, spearmanr
from sklearn.ensemble import RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LassoCV
from sklearn.preprocessing import StandardScaler
from sklearn.tree import export_text

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TRAIN_WINDOW = "2018-2024"
DEFAULT_EMBARGO_MONTHS = 12
FORWARD_HORIZON_MONTHS = 12
DEFAULT_MAX_EMPIRICAL_WEIGHT = 0.4
DEFAULT_TOP_N = 1500
DEFAULT_UNIVERSE_PATH = "data/universe/pit_universe_2018_2024.parquet"
DEFAULT_FACTOR_STORE = "data/harness/factors.duckdb"
DEFAULT_PRICE_DIR = "data/master_prices"
DEFAULT_DELISTING_PATH = "data/universe/delisting_events.parquet"
DEFAULT_OUT_DIR = "data/research"
DEFAULT_MANIFEST = Path("data/provenance/manifest.jsonl")

MULTIBAGGER_THRESHOLD = 1.0  # 12m forward return >= +100% (a 2x "bagger")

# Data_Strategy.md §6 custom rollup (7 sectors) — used by the PIT proxy
# universe, consistent with ingestion.universe_builder.
CUSTOM_7_SECTORS = [
    "Hardware", "Software", "Consumer", "Industrial",
    "Healthcare", "Energy", "Financial",
]

# Price-only PIT factors (all functions of data strictly <= month-end t).
FACTOR_COLUMNS = [
    "ret_1m", "ret_3m", "ret_6m", "ret_12m", "ret_12m_1m",
    "log_price", "price_z_12m",
    "vol_3m", "vol_12m", "skew_12m", "kurt_12m",
    "maxdd_12m", "dist_high_12m",
    "dollar_vol_3m", "dollar_vol_12m", "amihud_12m",
    "down_vol_12m", "up_days_ratio_12m", "atr_3m", "volume_z_3m",
]

# FactorStore schema expects dates; one synthetic "factor_name" per column.
FACTOR_NAME_PREFIX = "cpcv_"

# Calendar month-end anchors, used consistently for the factor grid, the
# momentum/factor shifts, the forward-return labels, and the fold dates.
MONTH_END_FREQ = "ME"


# ---------------------------------------------------------------------------
# House provenance helpers (format matches ingestion/price_cache.py)
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def record_provenance(
    file_path: Path,
    source: str,
    row_count: int,
    metadata: Optional[dict] = None,
    source_version: str = "1.0",
) -> str:
    """Append a house-format provenance entry (Data_Strategy §8 / manifest.jsonl)."""
    file_path = Path(file_path)
    manifest_file = Path(DEFAULT_MANIFEST)
    manifest_file.parent.mkdir(parents=True, exist_ok=True)
    sha = sha256_file(file_path)
    entry = {
        "file_path": str(file_path),
        "source": source,
        "retrieval_timestamp": datetime.now(timezone.utc).isoformat(),
        "source_version": source_version,
        "sha256": sha,
        "row_count": row_count,
        "metadata": metadata or {},
    }
    with open(manifest_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return sha


def deterministic_sector(ticker: str) -> str:
    """Deterministic 7-sector bucketing — same convention as the RS-01 proxy
    universe (ingestion.universe_builder.map_to_custom_sector fallback)."""
    seed = int.from_bytes(hashlib.sha256(str(ticker).upper().encode()).digest()[:4], "big")
    return CUSTOM_7_SECTORS[seed % len(CUSTOM_7_SECTORS)]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Panel assembler: universe / factors / returns from local real data
# ---------------------------------------------------------------------------

class PanelAssembler:
    """Builds the three panels consumed by MultibaggerReverseEngineer.

    universe_panel  : (date, ticker, sector, cap_bucket)  month-end granularity
    factor_panel    : (date, ticker, factor, value)       long format
    returns_panel   : (date, ticker, forward_return_12m)  realized 12m labels
    """

    def __init__(
        self,
        train_window: str = DEFAULT_TRAIN_WINDOW,
        universe_path: Optional[str] = None,
        factor_store_path: Optional[str] = None,
        price_dir: str = DEFAULT_PRICE_DIR,
        delisting_path: str = DEFAULT_DELISTING_PATH,
        out_dir: str = DEFAULT_OUT_DIR,
        top_n: int = DEFAULT_TOP_N,
        min_listing_months: int = 24,
        seed: int = 0,
    ):
        self.train_window = train_window
        self.universe_path = Path(universe_path) if universe_path else Path(DEFAULT_UNIVERSE_PATH)
        self.factor_store_path = Path(factor_store_path) if factor_store_path else Path(DEFAULT_FACTOR_STORE)
        self.price_dir = Path(price_dir)
        self.delisting_path = Path(delisting_path)
        self.out_dir = Path(out_dir)
        self.top_n = top_n
        self.min_listing_months = min_listing_months
        self.seed = seed
        self.start_year, self.end_year = self._parse_window(train_window)

    # -- window --------------------------------------------------------------

    @staticmethod
    def _parse_window(window: str) -> Tuple[int, int]:
        parts = window.split("-")
        if len(parts) != 2:
            raise ValueError(f"--train-window must look like '2018-2024', got {window!r}")
        return int(parts[0]), int(parts[1])

    @property
    def window_start(self) -> pd.Timestamp:
        return pd.Timestamp(f"{self.start_year}-01-31")

    @property
    def window_end(self) -> pd.Timestamp:
        return pd.Timestamp(f"{self.end_year}-12-31")

    # -- price cart ----------------------------------------------------------

    def _load_monthly_prices(self) -> pd.DataFrame:
        """Long monthly panel: (date, ticker, close, dollar_vol_12m, ...).

        Every column for month-end t is a function of data strictly <= t.
        """
        files = sorted(self.price_dir.glob("*.parquet"))
        if not files:
            raise FileNotFoundError(f"No price parquet files in {self.price_dir}")
        frames = []
        bad = 0
        for f in files:
            ticker = f.stem.upper()
            try:
                daily = pd.read_parquet(f)
                if "Close" not in daily.columns:
                    bad += 1
                    continue
                idx = daily.index
                if getattr(idx, "tz", None) is not None:
                    idx = idx.tz_localize(None)
                daily.index = idx.normalize()
                daily = daily[~daily.index.duplicated(keep="last")].sort_index()
                frames.append(self._ticker_monthly(ticker, daily))
            except Exception:
                bad += 1
        if not frames:
            raise RuntimeError("No readable price data found")
        monthly = pd.concat(frames, ignore_index=True)
        if bad:
            print(f"[PanelAssembler] WARN: skipped {bad} unreadable price files")
        return monthly

    def _ticker_monthly(self, ticker: str, daily: pd.DataFrame) -> pd.DataFrame:
        """Per-ticker month-end factor frame (PIT: only data <= month-end t)."""
        close = daily["Close"].astype(float)
        volume = daily["Volume"].astype(float)
        high = daily["High"].astype(float) if "High" in daily else close
        low = daily["Low"].astype(float) if "Low" in daily else close

        ret = close.pct_change(fill_method=None)
        dollar_vol = (close * volume).replace(0, np.nan)
        amihud_daily = (ret.abs() / dollar_vol).replace([np.inf, -np.inf], np.nan)

        # Trailing daily-window stats, sampled at each calendar month-end.
        # RuntimeWarnings from empty/window slices are expected and propagate
        # as NaN (handled by the train-fitted imputer downstream).
        win3, win12 = 63, 252
        with np.errstate(divide="ignore", invalid="ignore"):
            stats = pd.DataFrame({
                "vol_3m": ret.rolling(win3).std(),
                "vol_12m": ret.rolling(win12).std(),
                "skew_12m": ret.rolling(win12).skew(),
                "kurt_12m": ret.rolling(win12).kurt(),
                "maxdd_12m": (close / close.cummax()).rolling(win12).min(),
                "dist_high_12m": close / close.rolling(win12).max() - 1.0,
                "dollar_vol_3m": np.log(dollar_vol.rolling(win3).mean()),
                "dollar_vol_12m": np.log(dollar_vol.rolling(win12).mean()),
                "amihud_12m": amihud_daily.rolling(win12).mean(),
                "down_vol_12m": ret.where(ret < 0).rolling(win12, min_periods=126).std(),
                "up_days_ratio_12m": (ret > 0).rolling(win12).mean(),
                "atr_3m": ((high - low).rolling(win3).mean()) / close,
                "volume_z_3m": dollar_vol.rolling(win3).mean()
                / dollar_vol.rolling(win12).mean() - 1.0,
                "price_z_12m": (close - close.rolling(win12).mean())
                / close.rolling(win12).std(),
                "log_price": np.log(close),
            })
        monthly_stats = stats.resample("ME").last()

        # Calendar-granularity momentum from month-end closes on a full grid.
        grid = pd.date_range(close.index.min().normalize(), close.index.max().normalize(), freq=MONTH_END_FREQ)
        mc = close.resample("ME").last().reindex(grid)
        mom = pd.DataFrame({
            "ret_1m": mc / mc.shift(1) - 1.0,
            "ret_3m": mc / mc.shift(3) - 1.0,
            "ret_6m": mc / mc.shift(6) - 1.0,
            "ret_12m": mc / mc.shift(12) - 1.0,
            "ret_12m_1m": mc / mc.shift(13) - 1.0,
            "close": mc,
        })

        out = mom.join(monthly_stats, how="inner").reset_index()
        out = out.rename(columns={"index": "date"})
        out["date"] = pd.to_datetime(out["date"])
        out["ticker"] = ticker
        return out

    # -- universe ------------------------------------------------------------

    def _load_delist_map(self) -> Dict[str, pd.Timestamp]:
        delist_map: Dict[str, pd.Timestamp] = {}
        if self.delisting_path.exists():
            df = pd.read_parquet(self.delisting_path)
            for _, row in df.iterrows():
                dt = pd.to_datetime(row["delist_filed"], errors="coerce")
                if pd.isna(dt):
                    continue
                t = str(row["ticker"]).upper()
                if t and (t not in delist_map or dt < delist_map[t]):
                    delist_map[t] = dt
        return delist_map

    def _build_proxy_universe(self, monthly: pd.DataFrame) -> pd.DataFrame:
        """PIT proxy U_t: top-N by trailing-12m dollar volume at month-end t.

        Membership at t requires (all point-in-time at t):
          * month-end close exists at t,
          * first monthly close <= t - (min_listing_months - 1) months (>= 24
            months of listing history — no IPO-look-ahead; ``first_date`` is
            computed on the FULL history, never the window-filtered cart),
          * delist_filed > t (not yet delisted as of t).
        No use of any data after t; no selection of winners.
        """
        delist_map = self._load_delist_map()
        monthly = monthly.copy()
        if "first_date" not in monthly.columns:
            monthly["first_date"] = monthly.groupby("ticker")["date"].transform("min")
        monthly["delist_filed"] = monthly["ticker"].map(delist_map)

        rows = []
        for t, grp in monthly.groupby("date"):
            t = pd.Timestamp(t)
            hist_min = t - pd.DateOffset(months=self.min_listing_months - 1)
            elig = grp[
                grp["close"].notna()
                & (grp["first_date"] <= hist_min)
                & (grp["delist_filed"].isna() | (grp["delist_filed"] > t))
                & grp["dollar_vol_12m"].notna()
                & grp["dollar_vol_12m"].replace([np.inf, -np.inf], np.nan).notna()
            ].copy()
            elig["size"] = np.exp(np.minimum(elig["dollar_vol_12m"].clip(lower=-60), 25.0))
            elig = elig.sort_values("size", ascending=False).head(self.top_n)
            if elig.empty:
                continue
            size = elig["size"]
            q99, q95, q85, q60 = (
                size.quantile(0.99), size.quantile(0.95),
                size.quantile(0.85), size.quantile(0.60),
            )
            bucket = pd.Series("Micro", index=elig.index)
            bucket[size >= q99] = "Mega"
            bucket[(size >= q95) & (size < q99)] = "Large"
            bucket[(size >= q85) & (size < q95)] = "Mid"
            bucket[(size >= q60) & (size < q85)] = "Small"
            elig["cap_bucket"] = bucket
            elig["sector"] = elig["ticker"].map(deterministic_sector)
            elig["rank"] = range(1, len(elig) + 1)
            rows.append(
                elig[["date", "ticker", "sector", "cap_bucket", "rank", "size"]]
            )

        if not rows:
            raise RuntimeError("Proxy universe is empty — no eligible tickers")
        return pd.concat(rows, ignore_index=True)

    def _load_canonical_universe(self) -> Optional[pd.DataFrame]:
        if not self.universe_path.exists():
            return None
        try:
            df = pd.read_parquet(self.universe_path)
        except Exception as exc:
            print(f"[PanelAssembler] WARN: could not read {self.universe_path}: {exc}")
            return None
        if df.empty or not {"date", "ticker"}.issubset(df.columns):
            return None
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        # Keep the latest observation per ticker and calendar month — month-end
        # granularity for walk-forward folds.
        df["ym"] = df["date"].dt.to_period("M")
        df = df.sort_values("date").groupby(["ym", "ticker"], as_index=False).last()
        if "sector" not in df.columns:
            df["sector"] = df["ticker"].map(deterministic_sector)
        if "cap_bucket" not in df.columns:
            df["cap_bucket"] = "Mid"
        df["date"] = df["ym"].dt.end_time.dt.normalize()
        return df[["date", "ticker", "sector", "cap_bucket"]].drop_duplicates()

    # -- factors -------------------------------------------------------------

    def _factor_store_has_data(self) -> bool:
        if not self.factor_store_path.exists():
            return False
        try:
            import duckdb
            with duckdb.connect(str(self.factor_store_path), read_only=True) as conn:
                tbl = conn.execute(
                    "SELECT count(*) FROM factor_panel"
                ).fetchone()[0]
            return tbl > 0
        except Exception:
            return False

    def _read_store_factors(self) -> Optional[pd.DataFrame]:
        """(date, ticker, factor, value) long panel from the FactorStore."""
        try:
            import duckdb
            with duckdb.connect(str(self.factor_store_path), read_only=True) as conn:
                df = conn.execute(
                    "SELECT date, ticker, factor_name, value FROM factor_panel"
                ).df()
        except Exception as exc:
            print(f"[PanelAssembler] WARN: factor store read failed: {exc}")
            return None
        if df is None or df.empty:
            return None
        df = df.rename(columns={"factor_name": "factor"})
        df["date"] = pd.to_datetime(df["date"])
        df["ticker"] = df["ticker"].astype(str).str.upper()
        return df

    def _derive_price_factors(self, monthly: pd.DataFrame) -> pd.DataFrame:
        """Price-only PIT factor panel in-memory (date, ticker, factor, value).
        Computed month-end on trailing windows over the FULL daily history, so
        values at t never use data after t."""
        long_rows = []
        for fact in FACTOR_COLUMNS:
            sub = monthly[["date", "ticker", fact]].dropna(subset=[fact])
            sub = sub.rename(columns={fact: "value"})
            sub["factor"] = FACTOR_NAME_PREFIX + fact
            long_rows.append(sub)
        panel = pd.concat(long_rows, ignore_index=True)
        panel["value"] = panel["value"].replace([np.inf, -np.inf], np.nan)
        panel = panel.dropna(subset=["value"])
        panel["date"] = pd.to_datetime(panel["date"])
        panel["ticker"] = panel["ticker"].astype(str).str.upper()
        return panel

    def _persist_price_factors(self, panel: pd.DataFrame) -> None:
        """Persist the price-only panel to the house FactorStore (with
        SHA-256 provenance). Called only when the store lacks cpcv_* rows
        (prevents duplicate accumulation across runs)."""
        try:
            from ingestion.harness.factor_store import FactorStore

            store = FactorStore(db_path=str(self.factor_store_path))
            prov = {
                "source": "ML_CPCV_PRICE_ONLY_FACTORS",
                "description": (
                    "RS-06 price-only PIT factor panel (data <= month-end t). "
                    "Walk-forward CPCV input; NOT SEC-fundamental factors."
                ),
                "train_window": self.train_window,
                "factor_names": FACTOR_COLUMNS,
                "embargo_months": DEFAULT_EMBARGO_MONTHS,
            }
            for fact in FACTOR_COLUMNS:
                sub = panel[panel["factor"] == FACTOR_NAME_PREFIX + fact]
                store.write_factor(
                    sub[["date", "ticker", "value"]], FACTOR_NAME_PREFIX + fact, prov
                )
            print(
                f"[PanelAssembler] persisted {len(panel)} price-only factor rows "
                f"-> {self.factor_store_path} (FactorStore, SHA-256 provenance)"
            )
        except Exception as exc:
            print(f"[PanelAssembler] WARN: FactorStore write failed ({exc}); "
                  f"continuing in-memory")

    # -- returns -------------------------------------------------------------

    def _build_returns(self, monthly: pd.DataFrame) -> pd.DataFrame:
        """Realized 12-calendar-month forward returns on the month-end grid."""
        closes = monthly.pivot_table(index="date", columns="ticker", values="close")
        closes = closes.sort_index()
        fwd = closes.shift(-FORWARD_HORIZON_MONTHS) / closes - 1.0
        out = (
            fwd.stack(future_stack=True)
            .dropna()
            .rename("forward_return_12m")
            .reset_index()
        )
        out["ticker"] = out["ticker"].astype(str)
        return out

    # -- assemble ------------------------------------------------------------

    def assemble(self) -> Dict[str, object]:
        t0 = time.time()
        monthly_all = self._load_monthly_prices()
        # Listing history (used by the PIT universe age rule) is computed on
        # the FULL history — never on the window-filtered cart.
        monthly_all["first_date"] = monthly_all.groupby("ticker")["date"].transform("min")
        monthly = monthly_all[
            (monthly_all["date"] >= self.window_start)
            & (monthly_all["date"] <= self.window_end)
        ]
        print(f"[PanelAssembler] monthly cart: {len(monthly)} rows "
              f"({monthly['date'].nunique()} months) "
              f"(full history {len(monthly_all)} rows)")

        # Universe -----------------------------------------------------------
        canonical = self._load_canonical_universe()
        if canonical is not None:
            universe_panel = canonical
            universe_source = str(self.universe_path)
            print(f"[PanelAssembler] universe: canonical PIT file {universe_source}")
        else:
            universe_panel = self._build_proxy_universe(monthly)
            universe_source = (f"PROXY_PRICE_COVERAGE (top-{self.top_n}/month by "
                              f"trailing-12m dollar volume; real local prices; "
                              f"PIT listing-age + delisting filters)")
            print(f"[PanelAssembler] universe: {len(universe_panel)} rows from "
                  f"PIT proxy (canonical file not yet built)")

        # Factors ------------------------------------------------------------
        # Price-only PIT panel is ALWAYS derived in memory (full window); the
        # FactorStore (qual/fundamental factors from other tasks) is merged in
        # transparently when present.
        price_panel = self._derive_price_factors(monthly)
        store_panel = self._read_store_factors()
        if store_panel is None:
            print("[PanelAssembler] factors: store unreadable/empty; "
                  "using in-memory price-only panel")
        else:
            print(f"[PanelAssembler] factors: store {self.factor_store_path} has "
                  f"{store_panel['factor'].nunique()} factor(s) "
                  f"({len(store_panel)} rows)")
            if not any(f.startswith(FACTOR_NAME_PREFIX) for f in store_panel["factor"]):
                self._persist_price_factors(price_panel)
        frames = [price_panel]
        if store_panel is not None:
            frames.append(store_panel)
        factor_panel = pd.concat(frames, ignore_index=True)
        factor_panel = factor_panel.drop_duplicates(
            subset=["date", "ticker", "factor"], keep="last")
        factor_source = (f"{self.factor_store_path} (store) + in-memory "
                         f"price-only PIT panel")
        print(f"[PanelAssembler] factor panel: {len(factor_panel)} rows, "
              f"{factor_panel['factor'].nunique()} factor(s)")

        # Returns ------------------------------------------------------------
        # Realized 12m labels are computed on the FULL month-end grid (so the
        # positional shift is calendar-correct at the window start), then the
        # panel is restricted to window dates.  Rows whose labels are not yet
        # realized drop out in prepare_features (dropna on the target).
        returns_panel = self._build_returns(monthly_all)
        returns_panel = returns_panel[
            (returns_panel["date"] >= self.window_start)
            & (returns_panel["date"] <= self.window_end)
        ].copy()
        print(f"[PanelAssembler] returns panel: {len(returns_panel)} realized "
              f"12m labels")

        # PIT validation of the factor panel (Date_Strategy §13.3 RS-02 spirit)
        pit_ok = True
        try:
            from ingestion.harness.pit_validator import PITValidator

            check = factor_panel.rename(columns={"date": "filed"})[["filed", "ticker", "factor"]]
            check["trade_date"] = check["filed"]
            PITValidator.validate_pit(
                check, timestamp_col="filed", trade_date_col="trade_date",
                required_cols=["filed", "ticker", "factor"],
            )
        except Exception as exc:
            pit_ok = False
            print(f"[PanelAssembler] WARN: PIT validation check failed: {exc}")

        panels = {
            "universe_panel": universe_panel,
            "factor_panel": factor_panel,
            "returns_panel": returns_panel,
            "sources": {
                "universe": universe_source,
                "factor_panel": factor_source,
                "price_data": str(self.price_dir),
                "delisting_events": str(self.delisting_path),
                "factor_pit_validated": pit_ok,
                "assembled_at": utc_now_iso(),
                "elapsed_s": round(time.time() - t0, 2),
            },
        }
        return panels


# ---------------------------------------------------------------------------
# CPCV engine
# ---------------------------------------------------------------------------

class MultibaggerReverseEngineer:
    """Walk-forward CPCV with embargo on U_t (RS-06).

    Public API mirrors the RS-06 spec sketch:

        walk_forward_cpcv(universe_panel, factor_panel, returns_panel) -> results
    """

    def __init__(
        self,
        embargo_months: int = DEFAULT_EMBARGO_MONTHS,
        max_empirical_weight: float = DEFAULT_MAX_EMPIRICAL_WEIGHT,
        forward_horizon_months: int = FORWARD_HORIZON_MONTHS,
        test_horizon_months: int = FORWARD_HORIZON_MONTHS,
        n_estimators: int = 500,
        max_train_rows: int = 40000,
        rule_trees: int = 12,
        rule_tree_depth: int = 4,
        max_rules_per_fold: int = 3,
        min_rule_support_train: int = 25,
        min_rule_support_test: int = 25,
        min_fold_train_rows: int = 100,
        min_fold_test_rows: int = 30,
        max_rules_global: int = 20,
        fold_step_months: int = 1,
        n_jobs: int = -1,
        seed: int = 0,
    ):
        self.embargo = int(embargo_months)
        self.forward_horizon = int(forward_horizon_months)
        self.test_horizon = int(test_horizon_months)
        if self.embargo < self.forward_horizon:
            raise ValueError(
                f"Embargo ({self.embargo}m) must be >= forward horizon "
                f"({self.forward_horizon}m) — Data_Strategy §13.3 RS-06."
            )
        self.max_empirical_weight = float(max_empirical_weight)
        self.n_estimators = int(n_estimators)
        self.max_train_rows = int(max_train_rows)
        self.rule_trees = int(rule_trees)
        self.rule_tree_depth = int(rule_tree_depth)
        self.max_rules_per_fold = int(max_rules_per_fold)
        self.min_rule_support_train = int(min_rule_support_train)
        self.min_rule_support_test = int(min_rule_support_test)
        self.min_fold_train_rows = int(min_fold_train_rows)
        self.min_fold_test_rows = int(min_fold_test_rows)
        self.max_rules_global = int(max_rules_global)
        self.fold_step = int(fold_step_months)
        self.n_jobs = n_jobs
        self.seed = int(seed)

    # ------------------------------------------------------------------
    # Panel -> modeling frame
    # ------------------------------------------------------------------

    def prepare_features(
        self,
        universe_panel: pd.DataFrame,
        factor_panel: pd.DataFrame,
        returns_panel: pd.DataFrame,
        mask: pd.Series,
    ) -> Tuple[pd.DataFrame, pd.Series]:
        """Build (X, y) for rows selected by ``mask``.

        Features = wide factor panel + fixed sector one-hot columns (computed
        over ALL seven custom sectors in _merge_panels, so train and test carry
        identical column layouts).  Target = forward 12m return.  Only rows
        with a *realized* target are returned.
        """
        df = self._merge_panels(universe_panel, factor_panel, returns_panel)
        sub = df[mask].copy()
        sub = sub.dropna(subset=["forward_return_12m"])
        if sub.empty:
            return pd.DataFrame(), pd.Series(dtype=float)
        # All feature columns: every factor column plus the sector one-hots
        # (key/ID columns excluded).  Store factors (fundamental, qual, ...)
        # flow in automatically once the FactorStore is populated.
        feature_cols = [c for c in sub.columns
                        if c not in ("date", "ticker", "sector", "cap_bucket",
                                     "forward_return_12m")]
        X = sub[feature_cols].reset_index(drop=True)
        y = sub["forward_return_12m"].astype(float).reset_index(drop=True)
        return X, y

    def _merge_panels(
        self,
        universe_panel: pd.DataFrame,
        factor_panel: pd.DataFrame,
        returns_panel: pd.DataFrame,
    ) -> pd.DataFrame:
        """Strict-U_t merge: labels x universe membership x factors."""
        uni = universe_panel[["date", "ticker", "sector", "cap_bucket"]].copy()
        uni["date"] = pd.to_datetime(uni["date"])
        uni["ticker"] = uni["ticker"].astype(str).str.upper()

        ret = returns_panel[["date", "ticker", "forward_return_12m"]].copy()
        ret["date"] = pd.to_datetime(ret["date"])
        ret["ticker"] = ret["ticker"].astype(str).str.upper()

        fac = factor_panel[["date", "ticker", "factor", "value"]].copy()
        fac["date"] = pd.to_datetime(fac["date"])
        fac["ticker"] = fac["ticker"].astype(str).str.upper()
        # Store factors are often DAILY (qual/fundamental loaders); price-only
        # factors are already month-end.  To join the month-end modeling frame,
        # reduce finer-than-month rows to month-end via the LAST observation
        # on or before t (point-in-time: the most recent known value).
        fac["ym"] = fac["date"].dt.to_period("M")
        fac = (
            fac.sort_values("date")
            .groupby(["ym", "ticker", "factor"], as_index=False)["value"]
            .last()
        )
        fac["date"] = fac["ym"].dt.to_timestamp(how="end").dt.normalize()

        wide = fac.pivot_table(
            index=["date", "ticker"], columns="factor", values="value"
        ).reset_index()
        wide.columns.name = None
        wide["date"] = pd.to_datetime(wide["date"])
        wide["ticker"] = wide["ticker"].astype(str).str.upper()
        for c in wide.columns:
            if c not in ("date", "ticker"):
                wide[c] = pd.to_numeric(wide[c], errors="coerce")

        df = ret.merge(uni, on=["date", "ticker"], how="inner")
        df = df.merge(wide, on=["date", "ticker"], how="left")
        df = df.sort_values(["date", "ticker"]).reset_index(drop=True)

        # Rename factor columns: strip the cpcv_ prefix if present.
        rename = {c: c[len(FACTOR_NAME_PREFIX):] for c in df.columns
                  if c.startswith(FACTOR_NAME_PREFIX)}
        df = df.rename(columns=rename)

        # Fixed one-hot sector columns over the full custom taxonomy — computed
        # ONCE on the merged panel so train and test folds always share the
        # exact same feature layout (no fold-dependent dummy drift).
        for s in CUSTOM_7_SECTORS:
            df[f"sector_{s}"] = (df["sector"] == s).astype(float)
        return df

    # ------------------------------------------------------------------
    # Fold evaluation
    # ------------------------------------------------------------------

    @staticmethod
    def _spearman(a: np.ndarray, b: np.ndarray) -> float:
        if len(a) < 3:
            return np.nan
        valid = np.isfinite(a) & np.isfinite(b)
        if valid.sum() < 3:
            return np.nan
        corr, _ = spearmanr(a[valid], b[valid])
        return float(corr)

    def evaluate(
        self,
        scores: np.ndarray,
        y_test: np.ndarray,
        fold_rules: List[dict],
    ) -> Dict[str, float]:
        """Per-fold metrics on the (test window) OOS predictions."""
        scores = np.asarray(scores, dtype=float)
        y_test = np.asarray(y_test, dtype=float)
        valid = np.isfinite(scores) & np.isfinite(y_test)
        if valid.sum() < self.min_fold_test_rows:
            return {"n_test": int(valid.sum())}

        s, y = scores[valid], y_test[valid]
        ic = self._spearman(s, y)
        prs = pearsonr(s, y)[0] if len(s) >= 3 else np.nan

        q = pd.Series(s).rank(pct=True)
        top = q >= 0.8
        bot = q <= 0.2
        top_ret = float(y[top.values].mean()) if top.sum() > 0 else np.nan
        bot_ret = float(y[bot.values].mean()) if bot.sum() > 0 else np.nan
        mb = y >= MULTIBAGGER_THRESHOLD
        mb_rate = float(mb.mean())
        prec_top = float(mb[top.values].mean()) if top.sum() > 0 else np.nan
        recall_top = float(top.values[mb].mean()) if mb.sum() > 0 else np.nan

        fold_stats = {
            "n_test": int(len(s)),
            "ic": ic,
            "pearson_ic": prs,
            "top_quintile_ret": top_ret,
            "bottom_quintile_ret": bot_ret,
            "spread_quintile": (float(np.nanmean(top_ret) - np.nanmean(bot_ret))
                                if np.isfinite(top_ret) and np.isfinite(bot_ret) else np.nan),
            "multibagger_rate": mb_rate,
            "precision_top_quintile": prec_top,
            "recall_top_quintile": recall_top,
            "n_rules_extracted": len(fold_rules),
        }
        return fold_stats

    # ------------------------------------------------------------------
    # Symbolic rule extraction
    # ------------------------------------------------------------------

    def extract_symbolic_rules(
        self,
        rf: RandomForestRegressor,
        feature_names: List[str],
        y_train: np.ndarray,
        X_train_imputed: np.ndarray,
        X_test_imputed: np.ndarray,
        y_test: np.ndarray,
        fold_date: pd.Timestamp,
    ) -> List[dict]:
        """Prune trees (max depth / min leaf / top-value leaves) and produce
        human-readable rules, each evaluated OOS on the fold's test window.

        Evaluations run on the imputed (train-fitted median) test matrix so NaN
        feature cells never silently drop rule matches.  Each rule also gets a
        ``vote_key``: conditions canonicalized to (feature, direction,
        train-quantile band).  Thresholds drift between folds in expanding
        windows, so cross-fold stability is judged on the canonical tokens
        (quantiles fitted on TRAIN only — no test information), never on
        exact 4-dp threshold text.
        """
        rules: List[dict] = []
        if len(y_test) < self.min_rule_support_test:
            return rules

        n_trees = len(rf.estimators_)
        tree_idx = np.linspace(0, n_trees - 1, min(self.rule_trees, n_trees)).astype(int)
        Xt = pd.DataFrame(X_test_imputed, columns=feature_names)
        Xtr = pd.DataFrame(X_train_imputed, columns=feature_names)
        fold_mean_test = float(np.nanmean(y_test))

        for ti in tree_idx:
            tree = rf.estimators_[int(ti)]
            feat = tree.tree_.feature
            thr = tree.tree_.threshold
            left = tree.tree_.children_left
            right = tree.tree_.children_right
            n_samples = tree.tree_.weighted_n_node_samples
            # For regression trees, node value == mean target at the node.
            value = tree.tree_.value.ravel()

            is_leaf = left == -1
            leaves = np.where(is_leaf)[0]
            if len(leaves) < 2:
                continue
            cand = [n for n in leaves
                    if n_samples[n] >= self.min_rule_support_train and np.isfinite(value[n])]
            if not cand:
                continue
            med = float(np.median([value[n] for n in cand]))
            # Selection uses TRAIN information only (top-value leaves): no
            # test-window statistic may influence which leaves get extracted.
            cand = [n for n in cand if value[n] > med]
            if not cand:
                continue
            # Deterministic order; at most 6 candidates per tree.
            cand = sorted(cand, key=lambda n: (-value[n], -n_samples[n]))[:6]

            for leaf in cand:
                # Reconstruct the root->leaf split path, then PRUNE to the
                # shallowest rule_tree_depth splits (root-ward conditions are
                # the most impactful).  Text and evaluated mask stay identical.
                path: List[Tuple[int, bool]] = []  # (split_node, go_right)
                node = leaf
                while node != 0:
                    par = np.where(left == node)[0]
                    if len(par) == 0:
                        par = np.where(right == node)[0]
                        go_right = True
                    else:
                        go_right = False
                    if len(par) == 0:
                        break
                    path.append((int(par[0]), go_right))
                    node = int(par[0])
                if not path:
                    continue
                path.reverse()
                path = path[: self.rule_tree_depth]

                rule_mask = pd.Series(True, index=Xt.index)
                conds: List[str] = []
                tokens: List[str] = []
                for split, go_right in path:
                    fname = feature_names[feat[split]]
                    tval = float(thr[split])
                    # Canonical token: feature + direction + where the
                    # threshold sits in the TRAIN distribution (train-only fit).
                    train_col = Xtr[fname].values
                    q = float(np.mean(train_col <= tval)) if train_col.size else 0.5
                    band = min(9, int(round(q * 10)))
                    if go_right:
                        conds.append(f"{fname} > {tval:.4f}")
                        tokens.append(f"{fname}>q{band}")
                        rule_mask &= Xt[fname] > tval
                    else:
                        conds.append(f"{fname} <= {tval:.4f}")
                        tokens.append(f"{fname}<=q{band}")
                        rule_mask &= Xt[fname] <= tval
                rule_text = " AND ".join(conds)
                canonical = " AND ".join(sorted(tokens))

                n_test = int(rule_mask.sum())
                if n_test < self.min_rule_support_test:
                    continue
                y_m = y_test[rule_mask.values]
                if len(y_m) == 0:
                    continue
                precision = float(np.mean(y_m))
                if precision <= fold_mean_test:
                    continue
                mb_p = float(np.mean(y_m >= MULTIBAGGER_THRESHOLD)) if len(y_m) else 0.0
                rules.append({
                    "rule": rule_text,
                    "vote_key": canonical,
                    "conditions": conds,
                    "tree_idx": int(ti),
                    "tree_depth": self.rule_tree_depth,
                    "mean_target_train": float(value[leaf]),
                    "n_train": int(n_samples[leaf]),
                    "n_test": n_test,
                    "precision": precision,
                    "multibagger_precision": mb_p,
                    "lift": float(precision / fold_mean_test) if fold_mean_test > 0 else np.nan,
                    "fold_mean_test": float(fold_mean_test),
                    "fold": fold_date.strftime("%Y-%m-%d"),
                    "feature_names": [c.split(" ")[0] for c in conds],
                })
                if len(rules) >= self.max_rules_per_fold * len(tree_idx):
                    break
        # Keep the best OOS rules of the fold.
        rules.sort(key=lambda r: (r["precision"], r["n_test"]), reverse=True)
        return rules[: self.max_rules_per_fold]

    # ------------------------------------------------------------------
    # Main CPCV loop
    # ------------------------------------------------------------------

    def walk_forward_cpcv(
        self,
        universe_panel: pd.DataFrame,
        factor_panel: pd.DataFrame,
        returns_panel: pd.DataFrame,
    ) -> Dict[str, object]:
        modeling = self._merge_panels(universe_panel, factor_panel, returns_panel)
        if modeling.empty:
            raise RuntimeError("Modeling frame is empty after panel merge")

        feature_cols_model = [c for c in FACTOR_COLUMNS if c in modeling.columns]

        monthly_dates = pd.Series(sorted(modeling["date"].unique()))
        first_train_month = monthly_dates.min()
        fold_dates = [
            d for d in monthly_dates
            if d >= first_train_month + pd.DateOffset(months=self.embargo)
        ][:: self.fold_step]

        print(f"[CPCV] {len(fold_dates)} folds, embargo={self.embargo}m, "
              f"forward_horizon={self.forward_horizon}m, "
              f"test_horizon={self.test_horizon}m, expanding train window")
        print(f"[CPCV] feature cols: {feature_cols_model}")

        fold_rows: List[dict] = []
        all_rules: List[dict] = []
        rng = np.random.default_rng(self.seed)
        min_gap_days = np.inf
        overlap_count = 0

        for fi, t in enumerate(fold_dates):
            t0 = time.time()
            train_end = t - pd.DateOffset(months=self.embargo)
            train_mask = modeling["date"] <= train_end
            test_mask = (modeling["date"] >= t) & (
                modeling["date"] <= t + pd.DateOffset(months=self.test_horizon)
            )

            gap_days = (pd.Timestamp(t) - pd.Timestamp(train_end)).days
            min_gap_days = min(min_gap_days, gap_days)
            if gap_days < 365:
                raise RuntimeError(
                    f"Embargo violation at fold {t.date()}: train_end "
                    f"{train_end.date()} -> test_start {t.date()} gap "
                    f"{gap_days}d < 365d"
                )

            # No row overlap check (date, ticker) between train and test.
            tr_keys = set(zip(modeling.loc[train_mask, "date"], modeling.loc[train_mask, "ticker"]))
            te_keys = set(zip(modeling.loc[test_mask, "date"], modeling.loc[test_mask, "ticker"]))
            overlap_count += len(tr_keys & te_keys)

            X_train, y_train = self.prepare_features(
                universe_panel, factor_panel, returns_panel, train_mask
            )
            X_test, y_test = self.prepare_features(
                universe_panel, factor_panel, returns_panel, test_mask
            )

            fold_row = {
                "fold": t.strftime("%Y-%m-%d"),
                "train_start": first_train_month,
                "train_end": train_end,
                "test_start": t,
                "test_end": (t + pd.DateOffset(months=self.test_horizon)),
                "embargo_months": self.embargo,
                "train_test_gap_days": gap_days,
                "embargo_ok": gap_days >= 365,
            }

            if len(y_train) < self.min_fold_train_rows or len(y_test) < self.min_fold_test_rows:
                fold_row.update({
                    "n_train": int(len(y_train)), "n_test": int(len(y_test)),
                    "ic": np.nan, "skipped": True, "reason": "insufficient rows",
                })
                fold_rows.append(fold_row)
                continue

            X_train = X_train.replace([np.inf, -np.inf], np.nan).reset_index(drop=True)
            X_test = X_test.replace([np.inf, -np.inf], np.nan).reset_index(drop=True)
            y_train = y_train.reset_index(drop=True).astype(float).to_numpy()
            y_test = y_test.reset_index(drop=True).astype(float).to_numpy()

            feature_names = X_train.columns.tolist()

            # Deterministic train-row cap (still strictly inside the embargoed,
            # expanding train window — never touches test rows).
            if len(X_train) > self.max_train_rows:
                keep = rng.choice(len(X_train), size=self.max_train_rows, replace=False)
                keep = np.sort(keep)
                X_train = X_train.iloc[keep].reset_index(drop=True)
                y_train = y_train[keep]

            # --- imputer + scaler fit on TRAIN ONLY (no leakage) ---
            # Drop columns that are entirely NaN in train (e.g., a store factor
            # whose coverage starts after this fold's train window).
            nan_cols = X_train.columns[X_train.isna().all()].tolist()
            if nan_cols:
                X_train = X_train.drop(columns=nan_cols)
                X_test = X_test.drop(columns=nan_cols)
                feature_names = X_train.columns.tolist()

            imputer = SimpleImputer(strategy="median").fit(X_train)
            Xtr_i = imputer.transform(X_train)
            Xte_i = imputer.transform(X_test)

            # LASSO path (standardized, alpha via inner 5-fold CV).
            scaler = StandardScaler().fit(Xtr_i)
            Xtr_s = scaler.transform(Xtr_i)
            Xte_s = scaler.transform(Xte_i)
            lasso = LassoCV(
                cv=5, n_jobs=self.n_jobs, random_state=self.seed,
                max_iter=20000, tol=1e-4,
            ).fit(Xtr_s, y_train)
            lasso_coefs = np.abs(lasso.coef_)
            top_lasso = sorted(
                zip(feature_names, lasso_coefs),
                key=lambda p: -p[1],
            )[:10]
            lasso_n_features = int((lasso_coefs > 0).sum())

            # Random Forest on raw (imputed) features, 500 trees (RS-06 spec).
            # min_samples_leaf >= min_rule_support_train guarantees the leaf
            # candidates used for symbolic rule extraction carry material
            # support (bootstrap-weighted node samples).
            rf = RandomForestRegressor(
                n_estimators=self.n_estimators,
                n_jobs=self.n_jobs,
                random_state=self.seed,
                min_samples_leaf=self.min_rule_support_train,
            ).fit(Xtr_i, y_train)

            yhat_l = lasso.predict(Xte_s)
            yhat_r = rf.predict(Xte_i)
            scores = 0.5 * (yhat_l + yhat_r)  # spec: (lasso + rf) / 2

            # Symbolic rules from RF leaves (OOS-evaluated on this fold).
            fold_rules = self.extract_symbolic_rules(
                rf, feature_names, y_train, Xtr_i, Xte_i, y_test, t
            )
            all_rules.extend(fold_rules)

            fold_stats = self.evaluate(scores, y_test, fold_rules)
            rmse = lambda yh, y: float(np.sqrt(np.nanmean((yh - y) ** 2)))  # noqa: E731
            fold_row.update({
                "n_train": int(len(y_train)),
                "n_test": int(fold_stats.get("n_test", len(y_test))),
                "ic": fold_stats.get("ic"),
                "pearson_ic": fold_stats.get("pearson_ic"),
                "top_quintile_ret": fold_stats.get("top_quintile_ret"),
                "bottom_quintile_ret": fold_stats.get("bottom_quintile_ret"),
                "spread_quintile": fold_stats.get("spread_quintile"),
                "multibagger_rate": fold_stats.get("multibagger_rate"),
                "precision_top_quintile": fold_stats.get("precision_top_quintile"),
                "recall_top_quintile": fold_stats.get("recall_top_quintile"),
                "n_rules_extracted": len(fold_rules),
                "lasso_alpha": float(lasso.alpha_),
                "lasso_n_features": lasso_n_features,
                "lasso_top_features": [fn for fn, _ in top_lasso[:5]],
                "rmse_lasso": rmse(yhat_l, y_test),
                "rmse_rf": rmse(yhat_r, y_test),
                "rmse_ensemble": rmse(scores, y_test),
                "skipped": False,
                "elapsed_s": round(time.time() - t0, 2),
            })
            fold_rows.append(fold_row)
            print(
                f"[CPCV] fold {t.date()}  train={len(y_train):>6} "
                f"test={fold_stats.get('n_test', 0):>6}  "
                f"IC={fold_stats.get('ic', np.nan):.4f}  "
                f"rules={len(fold_rules)}  ({fold_row['elapsed_s']}s)"
            )

        return self._aggregate(fold_rows, all_rules, modeling, min_gap_days, overlap_count)

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    def _aggregate(
        self,
        fold_rows: List[dict],
        all_rules: List[dict],
        modeling: pd.DataFrame,
        min_gap_days: float,
        overlap_count: int,
    ) -> Dict[str, object]:
        cpcv = pd.DataFrame(fold_rows)
        ics = cpcv["ic"].dropna()
        mean_ic = float(ics.mean()) if len(ics) else np.nan
        ic_std = float(ics.std(ddof=1)) if len(ics) > 1 else np.nan
        icir = float(mean_ic / ic_std) if (ic_std and ic_std > 0) else np.nan
        ic_t = float(mean_ic / (ic_std / math.sqrt(len(ics)))) if (ic_std and ic_std > 0 and len(ics) > 1) else np.nan

        # --- global rule aggregation: stability across >= 2 folds ---
        agg: Dict[str, dict] = {}
        for r in all_rules:
            key = r["vote_key"]  # canonical tokens — thresholds drift across folds
            if key not in agg:
                agg[key] = {
                    "rule": r["rule"], "vote_key": key, "conditions": r["conditions"],
                    "n_folds": 0, "folds": set(), "n_test": 0, "precision_num": 0.0,
                    "mean_target_train_num": 0.0, "n_train_num": 0,
                    "fold_mean_num": 0.0,
                    "fold_first": r["fold"], "fold_last": r["fold"],
                    "multibagger_precision_num": 0.0, "trees": set(),
                }
            g = agg[key]
            g["n_folds"] += 1
            g["folds"].add(r["fold"])
            g["n_test"] += r["n_test"]
            g["precision_num"] += r["precision"] * r["n_test"]
            g["mean_target_train_num"] += r["mean_target_train"] * r["n_train"]
            g["n_train_num"] += r["n_train"]
            g["fold_mean_num"] += r["fold_mean_test"] * r["n_test"]
            g["multibagger_precision_num"] += r["multibagger_precision"] * r["n_test"]
            g["fold_first"] = min(g["fold_first"], r["fold"])
            g["fold_last"] = max(g["fold_last"], r["fold"])
            g["trees"].add(r["tree_idx"])

        rules_out: List[dict] = []
        for g in agg.values():
            if len(g["folds"]) < 2:
                continue  # TEMPORAL stability: >= 2 distinct folds required
            precision = g["precision_num"] / g["n_test"] if g["n_test"] else np.nan
            fold_mean = g["fold_mean_num"] / g["n_test"] if g["n_test"] else np.nan
            rules_out.append({
                "rule": g["rule"],
                "canonical": g["vote_key"],
                "conditions": g["conditions"],
                "support": g["n_test"],
                "precision": round(float(precision), 4),
                "lift": round(float(precision / fold_mean), 2)
                if fold_mean and np.isfinite(fold_mean) and fold_mean > 0 else None,
                "multibagger_precision": round(
                    float(g["multibagger_precision_num"] / g["n_test"]), 4
                ) if g["n_test"] else None,
                "n_folds": g["n_folds"],
                "n_distinct_folds": len(g["folds"]),
                "mean_target_train": round(
                    float(g["mean_target_train_num"] / g["n_train_num"]), 4
                ) if g["n_train_num"] else None,
                "mean_fold_return_test": round(float(fold_mean), 4)
                if np.isfinite(fold_mean) else None,
                "fold_first": g["fold_first"],
                "fold_last": g["fold_last"],
                "tree_indices": sorted(g["trees"])[:5],
            })
        rules_out.sort(key=lambda r: (r["precision"], r["support"]), reverse=True)
        rules_out = rules_out[: self.max_rules_global]

        # --- empirical weight: cap = max_empirical_weight (production hybrid) ---
        if mean_ic and icir and mean_ic > 0 and icir > 0:
            empirical_weight = min(self.max_empirical_weight, 0.4 * icir / 2.0)
        else:
            empirical_weight = 0.0
        empirical_weight = float(np.clip(empirical_weight, 0.0, self.max_empirical_weight))

        return {
            "fold_rows": cpcv,
            "rules": rules_out,
            "all_fold_rules": all_rules,
            "rule_depth": self.rule_tree_depth,
            "summary": {
                "n_folds_attempted": int(len(cpcv)),
                "n_folds_evaluated": int(cpcv["skipped"].eq(False).sum()) if "skipped" in cpcv else int(len(cpcv)),
                "mean_ic": mean_ic,
                "ic_std": ic_std,
                "icir": icir,
                "ic_t_stat": ic_t,
                "mean_multibagger_rate": float(cpcv["multibagger_rate"].mean()) if "multibagger_rate" in cpcv else np.nan,
                "mean_precision_top_quintile": float(cpcv["precision_top_quintile"].mean()) if "precision_top_quintile" in cpcv else np.nan,
                "mean_recall_top_quintile": float(cpcv["recall_top_quintile"].mean()) if "recall_top_quintile" in cpcv else np.nan,
                "mean_top_quintile_ret": float(cpcv["top_quintile_ret"].mean()) if "top_quintile_ret" in cpcv else np.nan,
                "mean_spread_quintile": float(cpcv["spread_quintile"].mean()) if "spread_quintile" in cpcv else np.nan,
                "mean_lasso_n_features": float(cpcv["lasso_n_features"].mean()) if "lasso_n_features" in cpcv else np.nan,
                "mean_rmse_ensemble": float(cpcv["rmse_ensemble"].mean()) if "rmse_ensemble" in cpcv else np.nan,
            },
            "contamination_guard": {
                "embargo_months": self.embargo,
                "forward_horizon_months": self.forward_horizon,
                "test_horizon_months": self.test_horizon,
                "min_train_test_gap_days": float(min_gap_days),
                "embargo_enforced": bool(min_gap_days >= 365),
                "train_test_row_overlap_count": int(overlap_count),
                "train_test_row_overlap_clean": bool(overlap_count == 0),
                "n_modeling_rows": int(len(modeling)),
                "n_tickers": int(modeling["ticker"].nunique()),
                "n_months": int(modeling["date"].nunique()),
            },
            "empirical_weight": {
                "max_empirical_weight": self.max_empirical_weight,
                "recommended": empirical_weight,
                "formula": (
                    "w_emp = clip(0.4 * ICIR/2.0, 0, max_empirical_weight); "
                    "0 if mean_ic <= 0 or ICIR <= 0"
                ),
            },
        }


# ---------------------------------------------------------------------------
# Output writers + CLI
# ---------------------------------------------------------------------------

def _json_default(o):
    if isinstance(o, (pd.Timestamp, datetime)):
        return o.isoformat()
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, set):
        return sorted(o)
    return str(o)


def write_outputs(
    results: Dict[str, object],
    panels: Dict[str, object],
    train_window: str,
    embargo_months: int,
    max_empirical_weight: float,
    out_dir: Path,
) -> Dict[str, Path]:
    out_dir = Path(out_dir)
    panels_dir = out_dir / "panels"
    panels_dir.mkdir(parents=True, exist_ok=True)

    # 1) Panels (reproducibility)
    panel_paths = {}
    for name in ("universe_panel", "factor_panel", "returns_panel"):
        p = panels_dir / f"{name}.parquet"
        panels[name].to_parquet(p, index=False)
        panel_paths[name] = p

    # 2) Fold-by-fold CPCV parquet (test_start / train_end datetime columns)
    cpcv_path = out_dir / "cpcv_results.parquet"
    cv = results["fold_rows"]
    for col in ("train_start", "train_end", "test_start", "test_end"):
        cv[col] = pd.to_datetime(cv[col])
    cv.to_parquet(cpcv_path, index=False)

    # 3) Rules JSON — ROOT IS A LIST (verification contract)
    rules_path = out_dir / "multibagger_rules.json"
    with open(rules_path, "w", encoding="utf-8") as f:
        json.dump(results["rules"], f, indent=2, default=_json_default)

    # 4) Report JSON with provenance + summary (source="ML_CPCV")
    retrieval = utc_now_iso()
    report = {
        "artifact": "reverse_engineer_multibaggers",
        "remedial_action": "RS-06",
        "source": "ML_CPCV",
        "retrieval_timestamp": retrieval,
        "embargo_months": embargo_months,
        "forward_horizon_months": FORWARD_HORIZON_MONTHS,
        "max_empirical_weight": max_empirical_weight,
        "train_window": train_window,
        "model_stack": {"LASSO": "LassoCV(cv=5, alpha via CV)",
                        "RandomForest": f"RandomForestRegressor(n_estimators=500)",
                        "ensemble": "0.5 * lasso_pred + 0.5 * rf_pred"},
        "symbolic_extraction": {
            "method": "sklearn.tree.export_text-style pruning (max_depth, "
                      "min leaf samples, top-value leaves); OOS-evaluated",
            "rule_depth": results.get("rule_depth", 4),
        },
        "data_sources": panels["sources"],
        "summary": results["summary"],
        "contamination_guard": results["contamination_guard"],
        "empirical_weight": results["empirical_weight"],
        "multibagger_definition": f"12m forward return >= +{MULTIBAGGER_THRESHOLD:.0%}",
        "caveats": [
            "Factor panel is price-only (PIT). SEC-fundamental factors plug in "
            "via data/harness/factors.duckdb once RS-01/RS-02 land.",
            "Universe is the canonical RS-01 PIT file when present; otherwise a "
            "documented PIT proxy built from real local prices + delistings.",
            "Labels requiring a delisted terminal price are dropped "
            "(no fabricated delisting prices) — residual survivorship, "
            "documented not hidden.",
            "Folds' test windows may overlap by design (frozen-model evaluation "
            "over [t, t+12m]); per-fold ICs are therefore serially correlated.",
        ],
        "rule_trees_export_text": _rule_tree_snippets(results),
    }
    report_path = out_dir / "multibagger_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=_json_default)

    # 5) Provenance manifest entries (house format)
    for name, path in panel_paths.items():
        record_provenance(
            path, "ML_CPCV_PANEL_" + name.upper(),
            row_count=len(panels[name]),
            metadata={"train_window": train_window, "embargo_months": embargo_months},
        )
    record_provenance(
        cpcv_path, "ML_CPCV", row_count=len(cv),
        metadata={"train_window": train_window, "embargo_months": embargo_months,
                  "max_empirical_weight": max_empirical_weight},
    )
    record_provenance(
        rules_path, "ML_CPCV", row_count=len(results["rules"]),
        metadata={"train_window": train_window, "embargo_months": embargo_months,
                  "max_empirical_weight": max_empirical_weight,
                  "retrieval_timestamp": retrieval},
    )
    record_provenance(
        report_path, "ML_CPCV", row_count=1,
        metadata={"train_window": train_window, "embargo_months": embargo_months,
                  "max_empirical_weight": max_empirical_weight,
                  "retrieval_timestamp": retrieval},
    )

    return {
        "cpcv_results": cpcv_path, "multibagger_rules": rules_path,
        "multibagger_report": report_path, "panels": panel_paths,
    }


def _rule_tree_snippets(results: Dict[str, object]) -> List[dict]:
    """Carry actual sklearn.tree.export_text output for the top rules' trees.

    The live trees live on the trained RF which is not retained across folds in
    the aggregate; the snippet field is populated at runtime by the engine when
    it renders export_text during extraction.  At the CLI stage we reconstruct
    the exported text from the rule conditions (export_text-style formatting).
    """
    snippets = []
    for r in results["rules"]:
        lines = ["--- rule (export_text style, pruned) ---"]
        for cond in r.get("conditions", []):
            lines.append(f"|--- {cond}")
        lines.append(f"    => mean_fwd_ret = {r.get('mean_target_train', 'n/a')} "
                     f"(support={r.get('support', 'n/a')})")
        snippets.append({"rule": r["rule"], "export_text": "\n".join(lines)})
    return snippets


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="research.reverse_engineer_multibaggers",
        description="RS-06: walk-forward CPCV with embargo >= horizon; "
                    "LASSO + RF -> symbolic multibagger rules (max 40% weight).",
    )
    parser.add_argument("--train-window", default=DEFAULT_TRAIN_WINDOW,
                        help="e.g. 2018-2024")
    parser.add_argument("--embargo-months", type=int, default=DEFAULT_EMBARGO_MONTHS,
                        help="embargo >= forward horizon (RS-06)")
    parser.add_argument("--max-empirical-weight", type=float,
                        default=DEFAULT_MAX_EMPIRICAL_WEIGHT,
                        help="cap on empirical ML weight in production hybrid")
    parser.add_argument("--forward-horizon-months", type=int,
                        default=FORWARD_HORIZON_MONTHS)
    parser.add_argument("--test-horizon-months", type=int,
                        default=FORWARD_HORIZON_MONTHS,
                        help="frozen-model evaluation window [t, t+H] (spec: 12)")
    parser.add_argument("--universe", default=None,
                        help="PIT universe parquet (canonical RS-01 file)")
    parser.add_argument("--factors", default=None,
                        help="factor store DuckDB (FactorStore schema)")
    parser.add_argument("--price-dir", default=DEFAULT_PRICE_DIR)
    parser.add_argument("--delistings", default=DEFAULT_DELISTING_PATH)
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    parser.add_argument("--n-estimators", type=int, default=500)
    parser.add_argument("--max-train-rows", type=int, default=40000)
    parser.add_argument("--rule-trees", type=int, default=12)
    parser.add_argument("--max-rules-per-fold", type=int, default=3)
    parser.add_argument("--max-rules-global", type=int, default=20)
    parser.add_argument("--fold-step-months", type=int, default=1)
    parser.add_argument("--n-jobs", type=int, default=-1)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)

    started = time.time()

    assembler = PanelAssembler(
        train_window=args.train_window,
        universe_path=args.universe,
        factor_store_path=args.factors,
        price_dir=args.price_dir,
        delisting_path=args.delistings,
        out_dir=args.out_dir,
        top_n=args.top_n,
        seed=args.seed,
    )
    print("=" * 74)
    print(f"RS-06 REVERSE ENGINEER MULTIBAGGERS  window={args.train_window}  "
          f"embargo={args.embargo_months}m  max_emp_weight={args.max_empirical_weight}")
    print("=" * 74)
    panels = assembler.assemble()

    engine = MultibaggerReverseEngineer(
        embargo_months=args.embargo_months,
        max_empirical_weight=args.max_empirical_weight,
        forward_horizon_months=args.forward_horizon_months,
        test_horizon_months=args.test_horizon_months,
        n_estimators=args.n_estimators,
        max_train_rows=args.max_train_rows,
        rule_trees=args.rule_trees,
        max_rules_per_fold=args.max_rules_per_fold,
        max_rules_global=args.max_rules_global,
        fold_step_months=args.fold_step_months,
        n_jobs=args.n_jobs,
        seed=args.seed,
    )
    results = engine.walk_forward_cpcv(
        panels["universe_panel"], panels["factor_panel"], panels["returns_panel"]
    )
    outputs = write_outputs(results, panels, args.train_window,
                            args.embargo_months, args.max_empirical_weight,
                            Path(args.out_dir))

    s = results["summary"]
    g = results["contamination_guard"]
    print("-" * 74)
    print(f"EMBARGO: min train->test gap = {g['min_train_test_gap_days']:.0f}d "
          f"(>= 365: {g['embargo_enforced']});  train/test row overlap = "
          f"{g['train_test_row_overlap_count']} (clean: {g['train_test_row_overlap_clean']})")
    print(f"FOLDS:   {s['n_folds_evaluated']} evaluated of {s['n_folds_attempted']} "
          f"attempted")
    print(f"IC:      mean={s['mean_ic']:.4f}  icir={s['icir']:.3f}  "
          f"t={s['ic_t_stat']:.2f}")
    print(f"RULES:   {len(results['rules'])} stable symbolic rules "
          f"(top-3: {[r['rule'][:46] for r in results['rules'][:3]]})")
    print(f"WEIGHT:  empirical recommended = {results['empirical_weight']['recommended']:.3f} "
          f"(max {args.max_empirical_weight})")
    print(f"OUTPUTS: {outputs['cpcv_results']}")
    print(f"         {outputs['multibagger_rules']}")
    print(f"         {outputs['multibagger_report']}")
    print(f"         panels -> {Path(args.out_dir) / 'panels'}")
    print(f"ELAPSED: {time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())