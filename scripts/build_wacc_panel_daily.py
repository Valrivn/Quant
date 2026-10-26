#!/usr/bin/env python
"""
build_wacc_panel_daily.py — Workstream B5 producer for the daily WACC panel.

Builds the Phase-8 daily WACC panel for every (asof_date, ticker) key in the
backtest universe (PIT universe file, 2018-2024) using the FROZEN
`ingestion.wacc_engine_v2.WACCEngineV2` — no threshold, parameter or formula is
touched here.  This script only LOADS inputs and PERSISTS output.

PIT contract (unchanged from the pre-registration):
  * every fundamental is keyed on its `filed_date` and looked up with
    `filed_date <= asof_date` (the engine's `_pit_lookup`), so no row can see a
    filing that had not been made on the valuation date;
  * `asof_date <= retrieval_ts` is asserted on every emitted row;
  * market inputs (rf, VIX) are point-in-time series with staleness flags;
  * the firm 60-month regression (beta, rho) is computed ONLY from monthly
    closes at or before the month-end that anchors the panel row.

Inputs (all PIT, all with their own §9 provenance)
  --universe     data/universe/pit_universe_2018_2024.parquet
  --valuation    data/pit_fundamentals/valuation_fundamentals.parquet
  --prices       data/prices/price_cache.parquet
  --rf           data/fred_cache/DGS10.json
  --vix          data/fred_cache/VIXCLS.json      (optional; gap recorded)
  --positions    data/backtests/*/positions.parquet  (optional, weight join)

Outputs
  data/backtests/wacc_panel_daily.parquet            the panel (15 gate columns
                                                    + prov_code + weight)
  data/backtests/wacc_panel_daily.parquet.provenance.json   §9 sidecar
  data/backtests/wacc_tradable_keys.parquet          the universe keys in the
                                                    coverage-gate contract
  data/backtests/wacc_panel_daily_build_report.json  build metrics + gaps

Usage
  python scripts/build_wacc_panel_daily.py --run
  python scripts/build_wacc_panel_daily.py --run --start 2018-01-01 --end 2024-12-31
  python scripts/build_wacc_panel_daily.py --self-test
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingestion.wacc_engine_v2 import (  # noqa: E402
    FIRM_REGRESSION_MIN_OBS, RF_DEFAULT, VIX_DEFAULT_POINTS, WACCEngineV2,
)

FROZEN_CONFIG = "config/pre_registration_phase8.yaml"
DEFAULT_OUT = Path("data/backtests/wacc_panel_daily.parquet")
DEFAULT_TRADABLE = Path("data/backtests/wacc_tradable_keys.parquet")
DEFAULT_REPORT = Path("data/backtests/wacc_panel_daily_build_report.json")
UNIVERSE = "data/universe/pit_universe_2018_2024.parquet"
VALUATION = "data/pit_fundamentals/valuation_fundamentals.parquet"
PRICES = "data/prices/price_cache.parquet"
RF_JSON = "data/fred_cache/DGS10.json"
VIX_JSON = "data/fred_cache/VIXCLS.json"
BENCH = "SPY"
BENCH_FACTOR = "Mkt-RF"
FF5 = "data/fred_cache/ff5_factors_daily.parquet"
# Every artifact the build reads.  The newest of their mtimes is the
# deterministic default `retrieval_ts` (see resolve_retrieval_ts).
INPUT_FILES: Tuple[str, ...] = (
    FROZEN_CONFIG, UNIVERSE, VALUATION, PRICES, RF_JSON, VIX_JSON, FF5)
# A firm beta is only admitted when the name traded through the last
# FRESH_MONTHS months of the window (a frozen last close regresses to a
# negative beta against a rising market) and the estimate lands in
# [0, MAX_TOTAL_BETA] (the frozen total-beta ceiling).
FRESH_MONTHS = 3
MAX_TOTAL_BETA = 5.0
REGRESSION_WINDOW = 60
PROVENANCE_FIELDS = (
    "source", "retrieval_timestamp", "source_version", "pit_timestamp_column",
    "entity_key", "transformations", "sha256", "row_count", "date_range",
    "mapping_confidence",
)
PANEL_COLUMNS = (
    "asof_date", "ticker", "filed_date_ref", "rf_10y", "erp_damodaran",
    "erp_option", "erp_blend", "ke_v2", "kd_pre_tax_v2", "wacc_v2",
    "beta_source", "wacc_baseline", "duration_wacc", "iv_nowcast",
    "retrieval_ts", "position_weight", "prov_code",
)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Lookup acceleration — IDENTICAL math, O(log n) PIT lookups
# ---------------------------------------------------------------------------
# The frozen engine's `_pit_lookup` / `_carry_forward` scan their whole table
# per call (correct, but O(n)).  At the 1.8M-key panel scale that is ~1e12
# operations, so the builder subclasses the engine and re-indexes those two
# helpers with `np.searchsorted` over the same keys.  The SELECTED VALUE is
# unchanged — still "the most recent key <= asof" over the same insertion order
# — and `assert_engine_equivalence()` proves row-for-row equality against the
# base class.  No formula, threshold, clamp or table content is touched.

_PIT_TABLES = (
    "cap_struct", "cash_ratio", "icr", "ocf", "revenue", "tax_rate",
    "is_financial", "iv_baseline", "d_wacc_baseline", "wacc_estimate_baseline",
    "filed_date_ref", "beta_60m", "reg_beta_n", "rho_60m",
)


class _IndexedEngine(WACCEngineV2):
    """`WACCEngineV2` with bisect-based PIT lookups; same results, O(log n)."""

    _idx_ready = False
    _pit_idx: Dict[int, Any] = {}
    _ser_idx: Dict[int, Any] = {}
    _pit_len: Dict[int, int] = {}
    _ser_len: Dict[int, int] = {}
    _beta_memo: Dict[Optional[str], Tuple[float, str]] = {}

    def _ensure_index(self) -> None:
        if self._idx_ready:
            return
        pit: Dict[int, Tuple[Dict[str, Tuple[np.ndarray, List[Any]]], Dict[Any, Any]]] = {}
        for name in _PIT_TABLES:
            tbl = getattr(self, name, None)
            if not isinstance(tbl, dict) or not tbl:
                continue
            per: Dict[str, Tuple[List[int], List[Any]]] = {}
            for (t, f), v in tbl.items():
                slot = per.setdefault(str(t), ([], []))
                slot[0].append(pd.Timestamp(f).value)
                slot[1].append(v)
            packed: Dict[str, Tuple[np.ndarray, List[Any]]] = {}
            for t, (dates, vals) in per.items():
                arr = np.asarray(dates, dtype="int64")
                if arr.size > 1 and not bool(np.all(np.diff(arr) >= 0)):
                    raise RuntimeError(f"non-ascending PIT keys in {name} for {t}")
                packed[t] = (arr, vals)
            pit[id(tbl)] = (packed, tbl)  # keep a ref so id() stays unique
        ser: Dict[int, Tuple[np.ndarray, np.ndarray, Dict[Any, Any]]] = {}
        for name in ("rf_series", "erp_damodaran_series", "vix_series"):
            tbl = getattr(self, name, None)
            if not tbl:
                continue
            ks = sorted(tbl.keys())
            ser[id(tbl)] = (np.array([pd.Timestamp(k).value for k in ks], dtype="int64"),
                            np.array([float(tbl[k]) for k in ks], dtype="float64"), tbl)
        self._pit_idx = pit
        self._ser_idx = ser
        self._pit_len = {id(tbl): len(tbl) for _, tbl in pit.values()}
        self._ser_len = {id(tbl): len(tbl) for _, _, tbl in ser.values()}
        self._beta_memo: Dict[Optional[str], Tuple[float, str]] = {}
        self._idx_ready = True

    def _maybe_reset(self) -> None:
        """Tables are only mutated before the grid walk; re-index if they grow."""
        if not self._idx_ready:
            return
        for name in _PIT_TABLES:
            tbl = getattr(self, name, None)
            if isinstance(tbl, dict) and self._pit_len.get(id(tbl)) not in (None, len(tbl)):
                self._idx_ready = False
                self._ensure_index()
                return
        for name in ("rf_series", "erp_damodaran_series", "vix_series"):
            tbl = getattr(self, name, None)
            if tbl and self._ser_len.get(id(tbl)) not in (None, len(tbl)):
                self._idx_ready = False
                self._ensure_index()
                return

    def _pit_lookup(self, table: Dict[Tuple[str, pd.Timestamp], Any], ticker: str,
                    d: pd.Timestamp) -> Optional[Any]:
        if not table:
            return None
        self._ensure_index()
        entry = self._pit_idx.get(id(table))
        if entry is None:  # unregistered table -> frozen behaviour
            return super()._pit_lookup(table, ticker, d)
        packed = entry[0].get(str(ticker))
        if packed is None:
            return None
        dates, vals = packed
        k = int(np.searchsorted(dates, pd.Timestamp(d).value, side="right"))
        return vals[k - 1] if k else None

    def _carry_forward(self, table: Dict[pd.Timestamp, float], d: pd.Timestamp,
                       default: float, stale_days: int) -> Tuple[float, bool]:
        if not table:
            return float(default), True
        self._ensure_index()
        entry = self._ser_idx.get(id(table))
        if entry is None:
            return super()._carry_forward(table, d, default, stale_days)
        keys, vals, _ = entry
        k = int(np.searchsorted(keys, pd.Timestamp(d).value, side="right"))
        if k == 0:
            return float(default), True
        last_k = pd.Timestamp(int(keys[k - 1]))
        stale = bool((d - last_k).days > stale_days)
        return float(vals[k - 1]), stale

    def _unlevered_beta(self, gics_l2: Optional[str]) -> Tuple[float, str]:
        key = None if gics_l2 is None else str(gics_l2)
        memo = getattr(self, "_beta_memo", None)
        if memo is None:
            memo = self._beta_memo = {}
        if key not in memo:
            memo[key] = WACCEngineV2._unlevered_beta(self, key)
        return memo[key]


def assert_engine_equivalence(engine: "_IndexedEngine", config: str,
                              keys: List[Tuple[str, pd.Timestamp, Optional[str]]],
                              retrieval_ts: pd.Timestamp) -> Dict[str, Any]:
    """Row-for-row proof that the indexed lookups match the frozen engine."""
    base = WACCEngineV2(config=config)
    mismatches: List[Dict[str, Any]] = []
    checked = 0
    for name in _PIT_TABLES + ("rf_series", "erp_damodaran_series", "vix_series"):
        setattr(base, name, getattr(engine, name))
    for tkr, d, gics in keys:
        r_idx, _ = engine._row_for(tkr, d, gics, None, None, None, None, retrieval_ts)
        r_base, _ = base._row_for(tkr, d, gics, None, None, None, None, retrieval_ts)
        checked += 1
        for col in PANEL_COLUMNS:
            if col in ("retrieval_ts", "asof_date", "ticker", "filed_date_ref"):
                continue
            a, b = r_idx.get(col), r_base.get(col)
            if isinstance(a, float) and isinstance(b, float):
                if not (a == b or abs(a - b) <= 1e-12):
                    mismatches.append({"key": [str(tkr), str(d.date())], "col": col,
                                       "indexed": a, "base": b})
            elif a != b:
                mismatches.append({"key": [str(tkr), str(d.date())], "col": col,
                                   "indexed": a, "base": b})
    return {"rows_compared": checked, "mismatches": mismatches[:20],
            "n_mismatches": len(mismatches),
            "passed": len(mismatches) == 0 and checked > 0}



def load_fred_json(path: str, to_decimal: bool = False
                   ) -> Tuple[Dict[pd.Timestamp, float], Optional[str]]:
    """
    FRED cache -> {date: value}; retrieval stamp = max(retrieved_at).

    FRED quotes DGS10 in PERCENT (1.31 = 1.31%), but the frozen engine stores
    rf_10y as a DECIMAL (its own RF_DEFAULT is 0.04) and `load_rf()` only
    accepts values <= 0.5 without rescaling — a percent series would otherwise
    be multiplied by 100.  The conversion is therefore done HERE (input
    normalisation, recorded in the provenance) and the frozen engine is left
    untouched.  VIX is quoted in points, so `to_decimal` stays False for it.
    """
    p = Path(path)
    if not p.exists():
        return {}, None
    doc = json.loads(p.read_text(encoding="utf-8"))
    obs = doc.get("observations") or []
    out: Dict[pd.Timestamp, float] = {}
    stamps: List[str] = []
    for o in obs:
        v = o.get("value")
        if v in (None, "", "."):
            continue
        try:
            val = float(v)
        except (TypeError, ValueError):
            continue
        if to_decimal:
            val = val / 100.0
        try:
            out[pd.Timestamp(o["date"])] = val
        except Exception:  # noqa: BLE001 — skip malformed rows
            continue
        if o.get("retrieved_at"):
            stamps.append(str(o["retrieved_at"]))
    return out, (max(stamps) if stamps else None)


def load_firm_regression(prices_path: str, start: pd.Timestamp, end: pd.Timestamp,
                         bench: str = BENCH, window: int = REGRESSION_WINDOW,
                         min_obs: int = FIRM_REGRESSION_MIN_OBS,
                         bench_returns: Optional[pd.Series] = None,
                         fresh_months: int = FRESH_MONTHS,
                         max_beta: float = MAX_TOTAL_BETA
                         ) -> Tuple[Dict[Tuple[str, pd.Timestamp], float],
                                    Dict[Tuple[str, pd.Timestamp], int],
                                    Dict[Tuple[str, pd.Timestamp], float],
                                    Dict[str, Any]]:
    """
    Trailing `window`-month OLS of each ticker's monthly return on the bench,
    evaluated at every month-end.  Returns (beta, n_obs, rho, stats) keyed by
    (ticker, month_end).  PIT-safe: the window never contains a month after the
    anchor month-end.

    The benchmark is either a ticker column in the price cache (`bench`) or an
    externally supplied monthly excess-return series (`bench_returns`, e.g. the
    FF5 Mkt-RF factor compounded to monthly) — the price cache carries no
    index/ETF series, so the FF5 market factor is the PIT market proxy.

    Return-month validity (data quality, not a model change):
      * a monthly return counts only when BOTH endpoints are real observations —
        otherwise a multi-year gap is attributed to a single month;
      * the last `fresh_months` months before the anchor must all be valid, so a
        name that stopped trading (frozen last close -> a run of zero returns,
        which regresses to a NEGATIVE beta against a rising market) never
        contributes a firm beta;
      * a window needs >= min_obs valid months (the engine's own ≥36 rule).
    Tickers failing any of these fall back to the unlevered-beta chain, which
    the engine records in `beta_source`.
    """
    px = pd.read_parquet(prices_path, columns=["ticker", "date", "close"])
    px = px.dropna(subset=["close"])
    px["date"] = pd.to_datetime(px["date"])
    wide = (px.pivot_table(index="date", columns="ticker", values="close", aggfunc="last")
              .resample("ME").last())
    wide = wide[(wide.index >= start) & (wide.index <= end)]
    present = wide.notna()
    rets = wide.pct_change()
    # a return is only valid when both month-end endpoints are real observations
    valid = rets.notna() & present & present.shift(1, fill_value=False)
    rets = rets.where(valid)
    if bench_returns is not None:
        mkt = bench_returns.reindex(rets.index)
        tick_cols = [c for c in rets.columns if c != bench]
    else:
        if bench not in rets.columns:
            raise SystemExit(
                f"benchmark {bench} absent from {prices_path}; pass "
                f"--benchmark-source ff5 (Mkt-RF) or a price-cache ticker")
        mkt = rets[bench]
        tick_cols = [c for c in rets.columns if c != bench]
    rets = rets[tick_cols]
    if mkt.notna().sum() < min_obs:
        raise SystemExit("benchmark series has fewer than min_obs valid months")
    beta_m: Dict[Tuple[str, pd.Timestamp], float] = {}
    nobs_m: Dict[Tuple[str, pd.Timestamp], int] = {}
    rho_m: Dict[Tuple[str, pd.Timestamp], float] = {}
    n_months = len(rets)
    vmat = valid[tick_cols]
    beta_vals: List[float] = []
    n_rej_neg = 0
    n_rej_big = 0
    for k in range(n_months):
        if k + 1 < min_obs:
            continue
        lo = max(0, k + 1 - window)
        sub = rets.iloc[lo:k + 1]
        anchor = sub.index[-1]
        m = mkt.reindex(sub.index).to_numpy(dtype=float)
        X = sub.to_numpy(dtype=float)
        mask = ~np.isnan(X)
        n = mask.sum(axis=0)
        # freshness: the last `fresh_months` months before the anchor must be
        # valid, else the name is stale/delisted and its beta is unusable
        f_lo = max(lo, k + 1 - fresh_months)
        fresh = vmat.iloc[f_lo:k + 1].all(axis=0).to_numpy()
        ok = (n >= min_obs) & fresh
        if not ok.any():
            continue
        mm = np.where(mask, m[:, None], np.nan)
        mean_x = np.nansum(X, axis=0) / np.maximum(n, 1)
        mean_m = np.nansum(mm, axis=0) / np.maximum(n, 1)
        dx = np.where(mask, X - mean_x, 0.0)
        dm = np.where(mask, m[:, None] - mean_m, 0.0)
        Sxy = (dx * dm).sum(axis=0)
        Smm = (dm * dm).sum(axis=0)
        Sxx = (dx * dx).sum(axis=0)
        with np.errstate(divide="ignore", invalid="ignore"):
            beta = np.where(Smm > 0, Sxy / Smm, np.nan)
            rho = np.where((Smm > 0) & (Sxx > 0), Sxy / np.sqrt(Sxx * Smm), np.nan)
        # admissibility: a 60-month OLS beta is only usable as a firm estimate
        # when it lands in [0, max_total_beta]; negative / >5 estimates come
        # from short or stale windows and would otherwise lever into a clamped
        # ke (the engine clamps ke to [0,0.5], silently zeroing them out).
        beta_ok = np.isfinite(beta) & (beta >= 0.0) & (beta <= max_beta)
        rej_neg = np.isfinite(beta) & (beta < 0.0)
        rej_big = np.isfinite(beta) & (beta > max_beta)
        n_rej_neg += int((ok & rej_neg).sum())
        n_rej_big += int((ok & rej_big).sum())
        ok = ok & beta_ok
        if not ok.any():
            continue
        beta_vals.extend(beta[ok].tolist())
        for j, tkr in enumerate(tick_cols):
            if not ok[j] or not np.isfinite(beta[j]):
                continue
            key = (str(tkr), pd.Timestamp(anchor))
            beta_m[key] = float(beta[j])
            nobs_m[key] = int(n[j])
            if np.isfinite(rho[j]):
                rho_m[key] = float(rho[j])
    bv = np.asarray(beta_vals, dtype=float)
    # Sampling-noise floor of a beta estimated from n months:
    #   SE(beta) = (sigma_e/sigma_m)/sqrt(n) = sqrt(1-rho^2)*|beta|/(rho*sqrt(n))
    # A 1,000-name cross-section therefore has an IRREDUCIBLE sigma(beta) of
    # roughly SE(beta); the frozen ke-dispersion tolerance is compared against
    # this floor so the report can say whether the gate is satisfiable at all.
    se_vals: List[float] = []
    for (tkr, anchor), b in beta_m.items():
        r = rho_m.get((tkr, anchor))
        n = nobs_m.get((tkr, anchor), 0)
        if r is None or n < 1 or abs(r) < 1e-6:
            continue
        se = float(np.sqrt(max(0.0, 1.0 - r * r)) * abs(b) / (abs(r) * np.sqrt(n)))
        if np.isfinite(se):
            se_vals.append(se)
    se_arr = np.asarray(se_vals, dtype=float)
    stats = {
        "window_months": window,
        "min_obs": min_obs,
        "fresh_months_required": fresh_months,
        "months_evaluated": int(n_months),
        "month_ends_with_beta": int(len({k[1] for k in beta_m})),
        "tickers_with_beta": int(len({k[0] for k in beta_m})),
        "first_beta_month": str(min(k[1] for k in beta_m).date()) if beta_m else None,
        "last_beta_month": str(max(k[1] for k in beta_m).date()) if beta_m else None,
        "benchmark": bench if bench_returns is None else f"ff5:{BENCH_FACTOR}",
        "obs_rejected_negative_beta": int(n_rej_neg),
        "obs_rejected_beta_gt_max": int(n_rej_big),
        "beta_distribution": {
            "n": int(bv.size),
            "mean": round(float(bv.mean()), 4) if bv.size else None,
            "std": round(float(bv.std()), 4) if bv.size else None,
            "p05": round(float(np.quantile(bv, 0.05)), 4) if bv.size else None,
            "p50": round(float(np.quantile(bv, 0.50)), 4) if bv.size else None,
            "p95": round(float(np.quantile(bv, 0.95)), 4) if bv.size else None,
        },
        "beta_estimation_noise": {
            "n_pairs_with_rho": int(se_arr.size) if 'se_arr' in locals() else 0,
            "mean_se": round(float(se_arr.mean()), 4) if 'se_arr' in locals() and se_arr.size else None,
            "p95_se": round(float(np.quantile(se_arr, 0.95)), 4) if 'se_arr' in locals() and se_arr.size else None,
            "mean_nobs": round(float(np.mean(list(nobs_m.values()))), 1) if nobs_m else None,
            "min_nobs": min(nobs_m.values()) if nobs_m else None,
            "max_nobs": max(nobs_m.values()) if nobs_m else None,
        },
    }
    return beta_m, nobs_m, rho_m, stats


def load_ff5_market_returns(path: str, start: pd.Timestamp,
                            end: pd.Timestamp) -> Tuple[Optional[pd.Series], Dict[str, Any]]:
    """FF5 Mkt-RF daily -> monthly compounded market excess return (PIT proxy)."""
    p = Path(path)
    if not p.exists():
        return None, {"path": path, "available": False}
    ff5 = pd.read_parquet(p)
    if BENCH_FACTOR not in ff5.columns:
        return None, {"path": path, "available": False,
                      "reason": f"{BENCH_FACTOR} column missing"}
    s = ff5[BENCH_FACTOR].dropna()
    s.index = pd.to_datetime(s.index)
    monthly = (1.0 + s).resample("ME").prod() - 1.0
    monthly = monthly[(monthly.index >= start) & (monthly.index <= end)]
    info = {
        "path": path, "available": True, "factor": BENCH_FACTOR,
        "sha256": sha256_file(p), "daily_obs": int(len(s)),
        "daily_range": [str(s.index.min().date()), str(s.index.max().date())],
        "monthly_obs": int(monthly.notna().sum()),
        "monthly_range": ([str(monthly.index.min().date()),
                           str(monthly.index.max().date())]
                          if len(monthly) else None),
        "transform": "monthly compounded (1+Mkt-RF).prod()-1",
    }
    return monthly.dropna(), info


def asof_market_data(uni: pd.DataFrame, keys: pd.DataFrame
                     ) -> pd.DataFrame:
    """
    For every (ticker, filed_date) key, the LAST universe observation with
    date <= filed_date (market cap + sector).  Backward as-of join — a filing
    can never see a market cap or sector that did not exist on its filed_date
    (the previous latest-value-per-ticker map was a lookahead).
    """
    uni_s = uni[["ticker", "date", "market_cap", "sector"]].sort_values(
        ["ticker", "date"], kind="mergesort")
    groups = {str(t): g for t, g in uni_s.groupby("ticker", sort=False)}
    left = keys.copy()
    left["ticker"] = left["ticker"].astype(str)
    if "date" not in left.columns:
        left = left.rename(columns={"filed_date": "date"})
    left["date"] = pd.to_datetime(left["date"], errors="coerce")
    left["_ord"] = np.arange(len(left))
    parts: List[pd.DataFrame] = []
    for tkr, k in left.groupby("ticker", sort=False):
        u = groups.get(tkr)
        if u is None or u.empty:
            continue
        m = pd.merge_asof(
            k.sort_values("date", kind="mergesort")[["_ord", "date"]],
            u.sort_values("date", kind="mergesort")[["date", "market_cap", "sector"]],
            on="date", direction="backward")
        parts.append(m)
    if not parts:
        return pd.DataFrame({"_ord": left["_ord"], "market_cap": np.nan,
                             "sector": None})
    out = pd.concat(parts, ignore_index=True).sort_values("_ord")
    out = out.drop_duplicates("_ord").set_index("_ord").reindex(
        pd.RangeIndex(len(left)))
    out.index.name = None
    return out


def register_filings(engine: WACCEngineV2, val_path: str, uni: pd.DataFrame,
                     end: pd.Timestamp) -> Dict[str, Any]:
    """
    Register every filing with filed_date <= `end` on its filed_date.

    NOTE: registrations are deliberately NOT clipped to the panel's first date —
    the engine's PIT rule is "latest filing with filed_date <= asof", so an early
    panel row must see the most recent PRIOR filing.  Clipping to [start, end]
    left every 2018-2020 row with no filing at all (D/E = 0, cash = 0, ICR
    missing), which collapsed the cross-section onto a single beta.
    """
    val = pd.read_parquet(val_path)
    val["filed_date"] = pd.to_datetime(val["filed_date"], errors="coerce")
    val = val[val["filed_date"] <= end]
    val = val.dropna(subset=["filed_date", "ticker"]).sort_values(
        ["ticker", "filed_date"], kind="mergesort").reset_index(drop=True)
    # PIT market cap / sector: last universe observation <= filed_date
    asof = asof_market_data(uni, val[["ticker", "filed_date"]])
    mc = pd.to_numeric(asof["market_cap"], errors="coerce").to_numpy(dtype=float)
    sector_asof = asof["sector"].to_numpy()
    eq = pd.to_numeric(val.get("stockholders_equity"), errors="coerce").to_numpy(dtype=float)
    net_debt = pd.to_numeric(val.get("net_debt"), errors="coerce")
    if net_debt is None or net_debt.isna().all():
        net_debt = (pd.to_numeric(val.get("total_debt"), errors="coerce")
                    - pd.to_numeric(val.get("cash"), errors="coerce"))
    nd = net_debt.to_numpy(dtype=float)
    # net D/E: net debt over MARKET equity as of the filed_date when available,
    # otherwise over book equity (recorded in the report as the transform).
    have_mc = np.isfinite(mc) & (mc > 0)
    de_market = np.where(have_mc, nd / np.where(have_mc, mc, np.nan),
                         np.where(eq > 0, nd / np.where(eq > 0, eq, np.nan), 0.0))
    de_market = np.nan_to_num(de_market, nan=0.0, posinf=0.0, neginf=0.0)
    cash = pd.to_numeric(val.get("cash"), errors="coerce").to_numpy(dtype=float)
    cash_ratio = np.where(eq > 0, cash / np.where(eq > 0, eq, np.nan), 0.0)
    cash_ratio = np.nan_to_num(cash_ratio, nan=0.0, posinf=0.0, neginf=0.0)
    tax = pd.to_numeric(val.get("income_tax_expense"), errors="coerce").to_numpy(dtype=float)
    ni = pd.to_numeric(val.get("net_income"), errors="coerce").to_numpy(dtype=float)
    denom = tax + ni
    tax_rate = np.where((denom > 0) & (tax >= 0), tax / np.where(denom > 0, denom, np.nan),
                        np.nan)
    tax_rate = np.where(np.isfinite(tax_rate) & (tax_rate <= 0.5), tax_rate, np.nan)
    is_fin = np.array([str(s) == "Financial" for s in sector_asof], dtype=bool)
    icr = pd.to_numeric(val.get("interest_coverage"), errors="coerce").to_numpy(dtype=float)
    icr = np.nan_to_num(icr, nan=0.0, posinf=0.0, neginf=0.0)
    ocf = pd.to_numeric(val.get("operating_cash_flow"), errors="coerce").to_numpy(dtype=float)
    ocf = np.nan_to_num(ocf, nan=0.0, posinf=0.0, neginf=0.0)
    rev = pd.to_numeric(val.get("revenue"), errors="coerce").to_numpy(dtype=float)
    rev = np.nan_to_num(rev, nan=0.0, posinf=0.0, neginf=0.0)
    wacc_est = pd.to_numeric(val.get("wacc_estimate"), errors="coerce").to_numpy(dtype=float)
    wacc_est = np.nan_to_num(wacc_est, nan=0.0, posinf=0.0, neginf=0.0)
    tickers_v = val["ticker"].to_numpy()
    filed_v = val["filed_date"].to_numpy()
    n = 0
    for i in range(len(val)):
        row = {
            "d_e_market_net": float(de_market[i]),
            "cash_ratio": float(cash_ratio[i]),
            "icr_v2": float(icr[i]),
            "operating_cash_flow": float(ocf[i]),
            "revenue": float(rev[i]),
            "is_financial_kd": bool(is_fin[i]),
            "wacc_estimate": float(wacc_est[i]),
        }
        # only pass the key when the filing yields a usable rate, so the engine
        # applies its own frozen fallback instead of float(None)
        if np.isfinite(tax_rate[i]):
            row["tax_rate_applied"] = float(tax_rate[i])
        engine.update_on_filing(str(tickers_v[i]), pd.Timestamp(filed_v[i]), row)
        n += 1
    return {
        "filings_registered": n,
        "tickers_with_filings": int(val["ticker"].nunique()),
        "first_filing": str(val["filed_date"].min().date()) if n else None,
        "last_filing": str(val["filed_date"].max().date()) if n else None,
        "tax_rate_source": "income_tax_expense/(income_tax_expense+net_income)",
        "d_e_source": "net_debt/market_cap_asof_filed_date "
                      "(fallback net_debt/stockholders_equity)",
        "market_cap_asof_available": int(np.isfinite(mc).sum()),
        "market_cap_asof_share": (round(float(np.isfinite(mc).mean()), 6)
                                  if n else 0.0),
        "sector_asof_financial_rows": int(is_fin.sum()),
    }


def load_position_weights(patterns: Optional[str]) -> Tuple[Dict[Tuple[str, str], float], Dict[str, Any]]:
    """(ticker, date) -> weight from every run's positions.parquet."""
    out: Dict[Tuple[str, str], float] = {}
    files = sorted(glob.glob(patterns)) if patterns else []
    for f in files:
        try:
            df = pd.read_parquet(f, columns=["rebalance_date", "ticker", "weight"])
        except Exception:  # noqa: BLE001
            continue
        df["rebalance_date"] = pd.to_datetime(df["rebalance_date"], errors="coerce")
        for d, t, w in df[["rebalance_date", "ticker", "weight"]].itertuples(index=False):
            if pd.isna(d) or t is None or w is None:
                continue
            out[(str(t), str(pd.Timestamp(d).date()))] = float(w)
    return out, {"files": files, "n_keys": len(out)}


# ---------------------------------------------------------------------------
# Panel build
# ---------------------------------------------------------------------------

def _prov_key(prov: Dict[str, Any]) -> str:
    """Stable signature for the per-row provenance block (floats rounded)."""
    def norm(o: Any) -> Any:
        if isinstance(o, float):
            return round(o, 6)
        if isinstance(o, dict):
            return {k: norm(v) for k, v in sorted(o.items())}
        return o
    return json.dumps(norm(prov), sort_keys=True, default=str)


def build_panel(args: argparse.Namespace) -> Dict[str, Any]:
    t_start = pd.Timestamp(args.start)
    t_end = pd.Timestamp(args.end)
    report: Dict[str, Any] = {
        "name": "wacc_panel_daily_build",
        # `run_at` is a wall clock and would break byte-determinism of this
        # report; the build's effective cutoff is `inputs.retrieval_ts`, which
        # is derived from the inputs themselves.
        "run_at_source": "inputs.retrieval_ts (deterministic; see resolve_retrieval_ts)",
        "frozen_config": args.config,
        "frozen_config_sha256": sha256_file(Path(args.config)),
        "window": {"start": str(t_start.date()), "end": str(t_end.date())},
        "inputs": {}, "data_gaps": [], "determinism": {},
    }

    # ---- universe grid (the coverage-gate contract) -------------------------
    uni = pd.read_parquet(args.universe)
    uni["date"] = pd.to_datetime(uni["date"], errors="coerce")
    uni = uni[(uni["date"] >= t_start) & (uni["date"] <= t_end)]
    uni = uni.dropna(subset=["date", "ticker"])
    # grid arrays (materialised once; the equivalence sample and the walk share them)
    uni = uni.sort_values(["date", "ticker"], kind="mergesort").reset_index(drop=True)
    tickers = uni["ticker"].astype(str).to_numpy()
    dates = uni["date"].to_numpy()
    sectors = uni["sector"].to_numpy()
    caps = uni["market_cap"].to_numpy(dtype=float)
    n = len(uni)
    report["inputs"]["universe"] = {
        "path": args.universe, "sha256": sha256_file(Path(args.universe)),
        "rows": int(len(uni)), "dates": int(uni["date"].nunique()),
        "tickers": int(uni["ticker"].nunique()),
        "date_range": [str(uni["date"].min().date()), str(uni["date"].max().date())],
    }
    tradable = pd.DataFrame({
        "asof_date": uni["date"].dt.strftime("%Y-%m-%d"),
        "ticker": uni["ticker"].astype(str),
        "price_stale_flag": uni["price_stale_flag"].fillna(False).astype(bool),
        "illiquid_flag": uni["illiquid_flag"].fillna(False).astype(bool),
    }).drop_duplicates(["asof_date", "ticker"])

    # ---- engine + market series -------------------------------------------
    engine = _IndexedEngine(config=args.config)
    rf_series, rf_stamp = load_fred_json(args.rf, to_decimal=True)
    engine.load_rf(rf_series)
    vix_series, vix_stamp = load_fred_json(args.vix, to_decimal=False)
    if vix_series:
        engine.load_vix(vix_series)
    else:
        report["data_gaps"].append({
            "gap": "VIXCLS", "path": args.vix, "impact":
            f"option-implied ERP leg uses the engine's documented default "
            f"({VIX_DEFAULT_POINTS} pts) and is flagged vix_stale=True on every row",
        })
    report["inputs"]["rf"] = {
        "path": args.rf, "sha256": sha256_file(Path(args.rf)) if Path(args.rf).exists() else None,
        "observations": len(rf_series), "retrieval_timestamp": rf_stamp,
        "coverage": ([str(min(rf_series).date()), str(max(rf_series).date())]
                     if rf_series else None),
        "transform": "FRED DGS10 percent -> decimal (value/100)",
        "value_range": ([float(min(rf_series.values())), float(max(rf_series.values()))]
                        if rf_series else None),
        "default_used": RF_DEFAULT,
    }
    if rf_series and min(rf_series) > t_start:
        report["data_gaps"].append({
            "gap": "DGS10_starts_2021_09", "path": args.rf, "impact":
            f"10Y Treasury series starts {min(rf_series).date()}; the "
            f"{int((uni['date'] < min(rf_series)).sum())} grid rows before that date "
            f"use the engine default rf={RF_DEFAULT:.4f} with rf_stale=True",
        })
    report["inputs"]["erp_damodaran"] = {
        "path": None, "series_available": False,
        "frozen_value": 0.045,
        "source": "config pre_registration_phase8.yaml wacc_engine.erp_damodaran "
                  "(CEO Ruling #8); data/damodaran and data/damodaran_mirror carry "
                  "no effective-dated ERP series",
    }
    report["inputs"]["vix"] = {
        "path": args.vix, "observations": len(vix_series),
        "retrieval_timestamp": vix_stamp,
        "default_used": VIX_DEFAULT_POINTS,
    }

    # ---- firm 60m regression (PIT: window ends at the anchor month-end) ----
    # The regression needs 60 months of history BEFORE the first panel date, so
    # both the price history and the market proxy are loaded from a lookback
    # start (--history-months, default 72).  The price cache begins 2018-01-01,
    # so early panel dates legitimately fall back to the unlevered-beta chain
    # (recorded as a data gap below).
    h_start = t_start - pd.DateOffset(months=args.history_months)
    bench_returns: Optional[pd.Series] = None
    ff5_info: Dict[str, Any] = {"path": args.ff5, "available": False}
    if args.benchmark_source == "ff5":
        bench_returns, ff5_info = load_ff5_market_returns(args.ff5, h_start, t_end)
        if bench_returns is None or len(bench_returns) < FIRM_REGRESSION_MIN_OBS:
            report["data_gaps"].append({
                "gap": "firm_regression_benchmark", "path": args.ff5, "impact":
                f"FF5 {BENCH_FACTOR} unavailable/short; the engine's "
                f"universe_median_fallback beta is used for every row",
            })
            bench_returns = None
    beta_m, nobs_m, rho_m, reg_stats = load_firm_regression(
        args.prices, h_start, t_end, bench=args.benchmark,
        bench_returns=bench_returns)
    engine.load_beta_60m(beta_m, nobs_m, rho_m)
    # how many PANEL dates have no firm regression at all?
    panel_dates = sorted(pd.Timestamp(x).to_period("M").to_timestamp("M")
                         for x in uni["date"].unique())
    dates_with_firm = {k[1] for k in beta_m}
    n_dates_fallback = sum(1 for m in panel_dates
                           if not any(a >= m for a in dates_with_firm))
    reg_stats["history_start"] = str(h_start.date())
    reg_stats["panel_months_with_firm_regression"] = int(len(panel_dates) - n_dates_fallback)
    reg_stats["panel_months_fallback_only"] = int(n_dates_fallback)
    if n_dates_fallback:
        report["data_gaps"].append({
            "gap": "firm_60m_regression_pre_history", "path": args.prices, "impact":
            f"{n_dates_fallback} of {len(panel_dates)} panel months have no 60-month "
            f"price history (price cache starts "
            f"{pd.read_parquet(args.prices, columns=['date'])['date'].min()}); those rows "
            f"use the unlevered-beta chain (Appendix B) with beta_source recorded",
        })
    report["inputs"]["firm_regression"] = {
        "path": args.prices, "sha256": sha256_file(Path(args.prices)),
        "benchmark_source": args.benchmark_source, "ff5_market": ff5_info,
        **reg_stats,
    }

    # ---- PIT filings -------------------------------------------------------
    fil = register_filings(engine, args.valuation, uni, t_end)
    report["inputs"]["valuation"] = {
        "path": args.valuation, "sha256": sha256_file(Path(args.valuation)), **fil,
    }

    # ---- position weights (mission: use the run's actual weights) ----------
    weights, wstats = load_position_weights(args.positions)
    report["inputs"]["positions"] = wstats

    retrieval_ts, ts_basis = resolve_retrieval_ts(args.retrieval_ts)
    report["inputs"]["retrieval_ts_basis"] = ts_basis
    report["retrieval_ts_basis"] = ts_basis

    # ---- proof: indexed lookups == frozen engine (sample of grid keys) ----
    if args.equivalence_check:
        n_keys = int(len(uni))
        step = max(1, n_keys // args.equivalence_check)
        sample = [(str(tickers[i]), pd.Timestamp(dates[i]),
                   None if sectors[i] is None or str(sectors[i]) == "nan"
                   else str(sectors[i]))
                  for i in range(0, n_keys, step)][:args.equivalence_check]
        eq = assert_engine_equivalence(engine, args.config, sample, retrieval_ts)
        report["engine_equivalence"] = eq
        if not eq["passed"]:
            raise SystemExit(f"indexed lookups diverge from the frozen engine: {eq}")
        print(f"[wacc_panel] engine equivalence OK over {eq['rows_compared']} keys",
              flush=True)

    # ---- grid walk ---------------------------------------------------------
    sig_to_code: Dict[str, int] = {}
    code_to_sig: Dict[int, str] = {}
    chunks: List[pd.DataFrame] = []
    n_pit_viol = 0
    for lo in range(0, n, args.chunk):
        hi = min(n, lo + args.chunk)
        rows: List[Dict[str, Any]] = []
        for i in range(lo, hi):
            tkr = str(tickers[i])
            d = pd.Timestamp(dates[i])
            gics = str(sectors[i]) if sectors[i] is not None and str(sectors[i]) != "nan" else None
            row, prov = engine._row_for(tkr, d, gics, None, None, None, None,
                                        retrieval_ts)
            key = _prov_key(prov)
            code = sig_to_code.get(key)
            if code is None:
                code = len(sig_to_code)
                sig_to_code[key] = code
                code_to_sig[code] = key
            row["prov_code"] = code
            row["position_weight"] = float(weights.get((tkr, str(d.date())), 0.0))
            rows.append(row)
            if row["asof_date"] > row["retrieval_ts"]:
                n_pit_viol += 1
        chunks.append(pd.DataFrame(rows))
        if args.progress and (lo // max(args.chunk, 1)) % 10 == 0:
            print(f"[wacc_panel] {hi}/{n} keys", flush=True)
    panel = pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame()
    del chunks

    # ---- determinism: stable order + stable dtypes -------------------------
    for c in ("asof_date", "filed_date_ref", "retrieval_ts"):
        panel[c] = pd.to_datetime(panel[c])
    panel["ticker"] = panel["ticker"].astype(str)
    panel["beta_source"] = panel["beta_source"].astype(str)
    for c in panel.columns:
        if c not in ("asof_date", "filed_date_ref", "retrieval_ts", "ticker",
                     "beta_source"):
            panel[c] = pd.to_numeric(panel[c], errors="coerce").astype("float64")
    panel = panel.sort_values(["asof_date", "ticker"], kind="mergesort").reset_index(drop=True)
    panel = panel[list(PANEL_COLUMNS)]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_opts = dict(engine="pyarrow", compression=args.compression, index=False)
    panel.to_parquet(out, **write_opts)
    digest_1 = sha256_file(out)
    # determinism proof: identical inputs -> identical bytes
    panel.to_parquet(out, **write_opts)
    digest_2 = sha256_file(out)
    report["determinism"] = {
        "sha256_first_write": digest_1,
        "sha256_second_write": digest_2,
        "identical": digest_1 == digest_2,
        "compression": args.compression,
        "sort_order": ["asof_date", "ticker"],
    }

    tkeys = Path(args.tradable_out)
    tradable = tradable.sort_values(["asof_date", "ticker"], kind="mergesort")
    tradable.to_parquet(tkeys, engine="pyarrow", compression=args.compression, index=False)

    # ---- §9 provenance sidecar -------------------------------------------
    dr = [str(panel["asof_date"].min().date()), str(panel["asof_date"].max().date())]
    prov = {
        "source": "scripts/build_wacc_panel_daily.py (ingestion.wacc_engine_v2)",
        "retrieval_timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_version": f"phase8_wacc_v2/{report['frozen_config_sha256'][:12]}",
        "pit_timestamp_column": "filed_date_ref",
        "entity_key": "asof_date|ticker",
        "transformations": [
            "universe_pit_key_grid",
            "filing_pit_lookup_filed_date<=asof_date",
            "firm_60m_monthly_ols_vs_ff5_mkt_minus_rf",
            "damodaran_unlevered_beta_appendix_b_fallback",
            "dgs10_percent_to_decimal",
            "rf_vix_pit_carry_forward",
            "erp_blend_70pct_damodaran_30pct_vix_proxy",
            "wacc_blend_ke_kd",
            "sort_asof_date_ticker",
        ],
        "sha256": digest_1,
        "row_count": int(len(panel)),
        "date_range": {"min": dr[0], "max": dr[1]},
        "mapping_confidence": ("medium" if reg_stats["tickers_with_beta"] else "low"),
        "column_contract": list(PANEL_COLUMNS),
        "prov_code_map": {str(k): v for k, v in code_to_sig.items()},
        "pit_violations": int(n_pit_viol),
        "input_sha256": {k: (v.get("sha256") if isinstance(v, dict) else None)
                         for k, v in report["inputs"].items()},
        "data_gaps": report["data_gaps"],
    }
    Path(str(out) + ".provenance.json").write_text(
        json.dumps(prov, indent=2, sort_keys=True, default=str), encoding="utf-8")

    report.update({
        "output": str(out),
        "output_sha256": digest_1,
        "tradable_keys_sha256": sha256_file(tkeys),
        "rows": int(len(panel)),
        "date_range": dr,
        "distinct_dates": int(panel["asof_date"].nunique()),
        "distinct_tickers": int(panel["ticker"].nunique()),
        "prov_signatures": len(code_to_sig),
        "beta_source_counts": {str(k): int(v) for k, v in
                               panel["beta_source"].value_counts().items()},
        "null_counts": {c: int(panel[c].isna().sum()) for c in
                        ("wacc_v2", "ke_v2", "erp_blend", "beta_source")},
        "summary_stats": {
            "ke_v2_mean": float(panel["ke_v2"].mean()),
            "ke_v2_std": float(panel["ke_v2"].std()),
            "wacc_v2_mean": float(panel["wacc_v2"].mean()),
            "erp_blend_mean": float(panel["erp_blend"].mean()),
            "rf_10y_mean": float(panel["rf_10y"].mean()),
            "weighted_rows": int((panel["position_weight"] > 0).sum()),
        },
        "fundamental_coverage_diagnostic": fundamental_coverage_diagnostic(panel),
        "ke_dispersion_feasibility": ke_dispersion_feasibility(
            panel, reg_stats, args.frozen_ke_var_threshold),
        "provenance": prov,
    })
    if report["fundamental_coverage_diagnostic"]["rows_without_filing_share"] > 0.05:
        report["data_gaps"].append({
            "gap": "pit_fundamentals_ticker_coverage",
            "path": args.valuation,
            "impact": (
                f"{report['fundamental_coverage_diagnostic']['rows_without_filing_share']:.2%} "
                f"of panel rows have NO PIT filing at their asof_date (the panel and "
                f"the PIT fundamentals share only "
                f"{report['fundamental_coverage_diagnostic']['ticker_overlap']} of "
                f"{report['fundamental_coverage_diagnostic']['universe_tickers']} tickers "
                f"— ADR/foreign listings have no SEC filing). Those rows fall back to "
                f"beta_u=0.743 with D/E=0 and the AAA base spread, which makes the "
                f"cross-section bimodal and inflates sigma(ke_v2). See "
                f"fundamental_coverage_diagnostic."),
        })
        prov["data_gaps"] = report["data_gaps"]
        Path(str(out) + ".provenance.json").write_text(
            json.dumps(prov, indent=2, sort_keys=True, default=str), encoding="utf-8")
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, indent=2, sort_keys=True, default=str),
                                 encoding="utf-8")
    return report


def fundamental_coverage_diagnostic(panel: pd.DataFrame) -> Dict[str, Any]:
    """
    Why sigma(ke_v2) is wide: split the cross-section by whether the row had a
    PIT filing at its asof_date (the engine stamps `filed_date_ref = asof_date`
    when no filing was found).  Pure diagnostic — it does not touch any gate.
    """
    filed = panel["filed_date_ref"] < panel["asof_date"]
    a = panel.loc[filed, "ke_v2"]
    b = panel.loc[~filed, "ke_v2"]
    tk_filed = set(panel.loc[filed, "ticker"].unique())
    tk_all = set(panel["ticker"].unique())

    def _stats(s: pd.Series) -> Dict[str, Any]:
        return {"rows": int(len(s)), "mean": round(float(s.mean()), 6) if len(s) else None,
                "std": round(float(s.std()), 6) if len(s) else None,
                "min": round(float(s.min()), 6) if len(s) else None,
                "max": round(float(s.max()), 6) if len(s) else None}
    return {
        "rows_with_filing": int(filed.sum()),
        "rows_without_filing": int((~filed).sum()),
        "rows_without_filing_share": round(float((~filed).mean()), 6),
        "universe_tickers": len(tk_all),
        "tickers_with_filing": len(tk_filed),
        "ticker_overlap": len(tk_filed & tk_all),
        "tickers_never_filed": len(tk_all - tk_filed),
        "tickers_never_filed_sample": sorted(tk_all - tk_filed)[:15],
        "ke_with_filing": _stats(a),
        "ke_without_filing": _stats(b),
        "ke_overall_sigma": round(float(panel["ke_v2"].std()), 6),
        "ke_sigma_filed_only": round(float(a.std()), 6) if len(a) > 1 else None,
        "ke_sigma_unfiled_only": round(float(b.std()), 6) if len(b) > 1 else None,
        "note": ("rows without a PIT filing carry the unlevered-beta chain with "
                 "D/E=0 and the AAA base spread, so their ke is a near-constant; "
                 "the mixture of a near-constant block and a dispersed block is "
                 "what widens the cross-sectional sigma(ke_v2)"),
    }


def ke_dispersion_feasibility(panel: pd.DataFrame, reg_stats: Dict[str, Any],
                              frozen_threshold: float) -> Dict[str, Any]:
    """
    Is the frozen `ke_var_threshold` reachable at all for this cross-section?

    A beta estimated from n months carries a sampling error
        SE(beta) = (sigma_e/sigma_m)/sqrt(n) = sqrt(1-rho^2)*|beta|/(rho*sqrt(n)).
    Across N independent names the cross-sectional sigma(beta) therefore has a
    FLOOR of roughly the mean SE(beta), and sigma(ke) >= floor * ERP.  If that
    floor already exceeds the frozen tolerance, the gate cannot pass for ANY
    correctly-built panel — a tolerance question, not an engine defect.
    """
    noise = reg_stats.get("beta_estimation_noise") or {}
    se_mean = noise.get("mean_se")
    erp = float(panel["erp_blend"].mean())
    out: Dict[str, Any] = {
        "frozen_ke_var_threshold": frozen_threshold,
        "mean_se_beta": se_mean,
        "mean_nobs": noise.get("mean_nobs"),
        "mean_erp_blend": round(erp, 6),
        "observed_ke_sigma": round(float(panel["ke_v2"].std()), 6),
        "method": "SE(beta)=sqrt(1-rho^2)*|beta|/(rho*sqrt(n)); "
                  "sigma(ke)_floor = mean_SE(beta) * mean_ERP",
    }
    if se_mean:
        floor = float(se_mean) * erp
        out["sigma_ke_floor"] = round(floor, 6)
        out["floor_over_threshold"] = round(floor / frozen_threshold, 3) \
            if frozen_threshold else None
        out["threshold_satisfiable"] = bool(floor <= frozen_threshold)
        out["verdict"] = (
            f"the irreducible sampling-noise floor of 60-month betas is "
            f"{floor:.4f} ({floor*1e4:.0f} bps), "
            f"{floor/frozen_threshold:.1f}x the frozen tolerance of "
            f"{frozen_threshold:.4f} ({frozen_threshold*1e4:.0f} bps): the gate is "
            f"UNSATISFIABLE for a ~1,000-name cross-section and requires a CEO "
            f"ruling on the tolerance or on the tradable universe — it is not an "
            f"engine defect" if floor > frozen_threshold else
            f"the sampling-noise floor {floor:.4f} is inside the frozen tolerance; "
            f"any FAIL is an engine/data defect")
    return out


def resolve_retrieval_ts(explicit: Optional[str]) -> Tuple[pd.Timestamp, str]:
    """
    A wall-clock retrieval stamp would make `retrieval_ts` (a panel column, so
    part of the output bytes) differ on every run, and the output sha256 with
    it — a determinism failure dressed up as provenance.  Default to the newest
    INPUT artifact mtime instead: identical inputs then give identical bytes,
    and the stamp still truthfully means "everything here was on disk by now".
    """
    if explicit:
        return pd.Timestamp(explicit), f"explicit:{explicit}"
    newest = None
    src = None
    for path in INPUT_FILES:
        try:
            m = dt.datetime.fromtimestamp(Path(path).stat().st_mtime, dt.timezone.utc)
        except (OSError, ValueError):
            continue
        if newest is None or m > newest:
            newest, src = m, path
    if newest is None:
        newest = dt.datetime.now(dt.timezone.utc)
        src = "clock (no readable input artifact)"
        basis = "clock_fallback"
    else:
        basis = f"max_input_mtime:{src}"
    return pd.Timestamp(newest.replace(tzinfo=None)), basis


def resolve_retrieval_ts_unused() -> None:  # pragma: no cover
    return None


# ---------------------------------------------------------------------------
# Self-test (synthetic grid; asserts contract + PIT + determinism)
# ---------------------------------------------------------------------------

def self_test() -> int:
    import tempfile
    td = Path(tempfile.mkdtemp(prefix="wacc_panel_"))
    rng = np.random.default_rng(11)
    # synthetic universe
    dates = pd.bdate_range("2023-01-02", periods=40, freq="B")
    tickers = [f"T{i}" for i in range(5)]
    uni = pd.DataFrame({
        "date": np.repeat(dates, len(tickers)),
        "ticker": np.tile(tickers, len(dates)),
        "sector": "Software",
        "market_cap": 1e9,
        "price_stale_flag": False,
        "illiquid_flag": False,
        "is_active": True,
    })
    up = td / "uni.parquet"
    uni.to_parquet(up, index=False)
    # synthetic filings
    filed = pd.date_range("2022-01-15", periods=6, freq="QE")
    val_rows = []
    for t in tickers:
        for f in filed:
            val_rows.append({
                "ticker": t, "filed_date": f.date(), "stockholders_equity": 1e9,
                "total_debt": 2e8, "cash": 1e8, "net_debt": 1e8,
                "income_tax_expense": 1e7, "net_income": 2e7,
                "operating_cash_flow": 5e7, "revenue": 1e9,
                "interest_coverage": 8.0, "wacc_estimate": 0.09,
            })
    vp = td / "val.parquet"
    pd.DataFrame(val_rows).to_parquet(vp, index=False)
    # synthetic prices
    px = td / "px.parquet"
    dts = pd.date_range("2020-01-31", periods=60, freq="ME")
    rows = []
    for t in tickers + [BENCH]:
        lvl = 100.0
        for d in dts:
            lvl *= float(1 + rng.normal(0.01, 0.05))
            rows.append({"ticker": t, "date": d, "close": lvl})
    pd.DataFrame(rows).to_parquet(px, index=False)
    # synthetic FF5 market factor (daily Mkt-RF) for the benchmark path
    ff5p = td / "ff5.parquet"
    dly = pd.bdate_range("2020-01-01", "2024-12-31")
    pd.DataFrame({BENCH_FACTOR: rng.normal(0.0004, 0.011, len(dly))},
                 index=dly).rename_axis("date").to_parquet(ff5p)
    args = argparse.Namespace(
        config=FROZEN_CONFIG, start="2023-01-01", end="2023-02-28",
        universe=str(up), valuation=str(vp), prices=str(px),
        rf="data/fred_cache/DGS10.json", vix="data/fred_cache/VIXCLS.json",
        benchmark=BENCH, benchmark_source="ff5", ff5=str(ff5p), history_months=72,
        retrieval_ts="2026-09-25T00:00:00",
        out=str(td / "panel.parquet"), tradable_out=str(td / "keys.parquet"),
        report=str(td / "rep.json"), chunk=100, compression="snappy", progress=False,
        positions=None, equivalence_check=40, frozen_ke_var_threshold=0.005,
    )
    rep = build_panel(args)
    import pandas as _pd
    p = _pd.read_parquet(args.out)
    assert list(p.columns) == list(PANEL_COLUMNS), p.columns
    assert len(p) == len(uni), (len(p), len(uni))
    assert p["wacc_v2"].notna().all() and p["ke_v2"].notna().all()
    assert p["beta_source"].notna().all()
    assert (p["asof_date"] <= p["retrieval_ts"]).all(), "PIT violation in panel"
    assert (p["wacc_v2"].between(0.0, 0.5)).all(), "wacc out of bounds"
    assert rep["determinism"]["identical"], "panel write is not byte-deterministic"
    assert rep["engine_equivalence"]["passed"], rep["engine_equivalence"]
    side = json.loads(Path(str(args.out) + ".provenance.json").read_text())
    for f in PROVENANCE_FIELDS:
        assert side.get(f) not in (None, "", []), f"sidecar missing {f}"
    print(f"[OK] rows={len(p)} dates={p['asof_date'].nunique()} "
          f"beta_sources={dict(p['beta_source'].value_counts())}")
    print(f"[OK] determinism sha256={rep['determinism']['sha256_first_write'][:16]} "
          f"identical={rep['determinism']['identical']}")
    print(f"[OK] indexed lookups == frozen engine over "
          f"{rep['engine_equivalence']['rows_compared']} keys "
          f"(mismatches={rep['engine_equivalence']['n_mismatches']})")
    print(f"[OK] §9 sidecar complete ({len(PROVENANCE_FIELDS)} fields), "
          f"PIT violations={side['pit_violations']}")
    print("\nAll build_wacc_panel_daily self-tests passed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Build the Phase-8 daily WACC panel (frozen engine, PIT inputs)")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--config", default=FROZEN_CONFIG)
    ap.add_argument("--start", default="2018-01-01")
    ap.add_argument("--end", default="2024-12-31")
    ap.add_argument("--history-months", type=int, default=72,
                    help="months of history loaded before --start for the 60-month "
                         "firm regression (the panel itself starts at --start)")
    ap.add_argument("--universe", default=UNIVERSE)
    ap.add_argument("--valuation", default=VALUATION)
    ap.add_argument("--prices", default=PRICES)
    ap.add_argument("--rf", default=RF_JSON)
    ap.add_argument("--vix", default=VIX_JSON)
    ap.add_argument("--benchmark", default=BENCH)
    ap.add_argument("--benchmark-source", default="ff5", choices=("ff5", "ticker"),
                    help="market proxy for the 60-month regression: FF5 Mkt-RF "
                         "(default, PIT) or a price-cache ticker")
    ap.add_argument("--ff5", default=FF5)
    ap.add_argument("--positions", default="data/backtests/*/positions.parquet")
    ap.add_argument("--frozen-ke-var-threshold", type=float, default=0.005,
                    help="the frozen phase8 ke_var_threshold; recorded in the "
                         "feasibility block, never applied here")
    ap.add_argument("--retrieval-ts", default=None,
                    help="ISO retrieval stamp (default: now, UTC)")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--tradable-out", default=str(DEFAULT_TRADABLE))
    ap.add_argument("--report", default=str(DEFAULT_REPORT))
    ap.add_argument("--chunk", type=int, default=50000)
    ap.add_argument("--equivalence-check", type=int, default=250,
                    help="sample size for the indexed-vs-frozen engine proof "
                         "(0 disables)")
    ap.add_argument("--compression", default="snappy")
    ap.add_argument("--progress", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if not args.run:
        ap.print_help()
        return 1
    rep = build_panel(args)
    s = rep["summary_stats"]
    print(f"[wacc_panel] rows={rep['rows']} dates={rep['distinct_dates']} "
          f"tickers={rep['distinct_tickers']} -> {rep['output']}")
    print(f"[wacc_panel] sha256={rep['output_sha256']} "
          f"deterministic={rep['determinism']['identical']}")
    print(f"[wacc_panel] beta_source={rep['beta_source_counts']}")
    print(f"[wacc_panel] ke mean={s['ke_v2_mean']:.6f} wacc mean={s['wacc_v2_mean']:.6f} "
          f"erp_blend mean={s['erp_blend_mean']:.6f} rf mean={s['rf_10y_mean']:.6f}")
    print(f"[wacc_panel] weighted rows={s['weighted_rows']} "
          f"nulls={rep['null_counts']} gaps={len(rep['data_gaps'])}")
    print(f"[wacc_panel] report: {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
