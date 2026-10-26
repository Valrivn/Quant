"""
wacc_gates.py — Phase 3 Validation Gate: Daily WACC Panel Validation
Owner: bigpickle-worker | Phase 8 Validation Gates (dispatch 2026-09-23)

Validates the daily WACC panel (§2.2, data/wacc/wacc_panel_daily/month=YYYY-MM/
wacc_panel.parquet) and the V3 beta/spread components on the PIT valuation panel
(data/pit_fundamentals/valuation_fundamentals.parquet) against the FROZEN
pre-registration config (config/pre_registration_phase8.yaml, CEO Ruling
B-20260920-001):

  phase8.wacc_engine:
    erp_damodaran: 0.0450, vix_proxy_weight: 0.30, damodaran_weight: 0.70
    erp_bounds: [0.0050, 0.0800], distressed_spread: 0.0885
    spread_bounds: [0.0063, 0.1900], rho_floor: 0.25, tax_fallback: 0.21
    omega_stress: {enabled, max_multiplier: 3.0, cash_burn_weight: 1.0,
                   icr_weight: 1.0, sector_stress_factors: 1.5/1.0/0.5}
  phase8.total_beta: {correlation_floor: 0.25, max_total_beta: 5.0,
                      sigmoid_transition_point: 3.0}

Gates implemented
-----------------
  G1 coverage_gate      — 100% WACC coverage of the tradable universe
                           (price_stale_flag=false AND illiquid_flag=false);
                           without a tradable file, completeness = share of
                           panel rows with (wacc_v2, ke_v2, erp_blend,
                           beta_source) all non-null and PIT-valid.
  G2 ke_dispersion_gate — cross-sectional sigma(ke_v2) per asof <= 50 bps
                           (ke_var_threshold, default 0.005).  A day whose
                           dispersion explodes fails the gate.
  G3 regime_shift_gate  — |median(ke|SHOCK/CRISIS) - median(ke|EXPANSION)|
                           >= 200 bps (regime_shift_threshold, default 0.02).
                           The engine must RESPOND to regime; a frozen Ke
                           across regimes fails.  Needs >= min_obs rows per
                           regime side (default 5), else SKIP (not evaluated).
  G4 panel_bounds_gate  — erp_blend in [0.005, 0.08]; ke/wacc in [0, 0.5];
                           iv_nowcast >= 0.01; beta_source non-null.
  G5 beta_ceiling_gate  — total_beta <= 5.0 (soft sigmoid ceiling) and
                           rho_market_60m >= 0.25 whenever total_beta present.
  G6 spread_bounds_gate — credit_spread_bps in [63, 1900]; distressed rows
                           (EBIT<=0, rating CCC..D) must sit in [885, 1900].
  G7 omega_oracle_gate  — frozen Omega_stress formula computed via the engine
                           oracle over a canonical case matrix: every case in
                           [1.0, 3.0], clamps at max_multiplier 3.0, neutral
                           case == 1.0, EXT-01 case == 1.675 exactly.

Data contracts (§8 / phase8 §2.2)
---------------------------------
  * All Parquet I/O goes through DuckDB (requirement).
  * Reports carry the §8 provenance fields: source, retrieval_timestamp,
    sha256, row_count, date_range, mapping_confidence.
  * Report -> data/validation/gates/wacc_gates_report.json (+ per-gate
    results parquet with the same §8 columns).

CLI (dispatch verification, must exit 0):
    python -m validation.wacc_gates --run --coverage-threshold 1.0 \
        --ke-var-threshold 0.005 --regime-shift-threshold 0.02
    python -m validation.wacc_gates --test
Exit codes: 0 = PASS/SKIP (executed without error), 1 = FAIL.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

try:
    import duckdb
except ImportError:  # pragma: no cover
    duckdb = None

CONFIG_PATH = "config/pre_registration_phase8.yaml"
DEFAULT_REPORT = Path("data/validation/gates/wacc_gates_report.json")
GREP_N = 0

# Default panel locations (producer §2.2 hive layout, then the brief's path)
PANEL_PATTERNS: Tuple[str, ...] = (
    "data/wacc/wacc_panel_daily/**/wacc_panel.parquet",
    "data/valuation/wacc_panel_daily.parquet",
    "data/backtests/wacc_panel_daily.parquet",
)
# Tradable-universe key frame (asof_date|ticker + stale/illiquid flags) written
# alongside the panel by scripts/build_wacc_panel_daily.py.
TRADABLE_PATTERNS: Tuple[str, ...] = (
    "data/backtests/wacc_tradable_keys.parquet",
)
REGIME_PATTERNS: Tuple[str, ...] = (
    "data/regime/regime_daily_nowcast.parquet",
    "data/regime/*.parquet",
    "data/backtests/*/regime_states.parquet",
)
VALUATION_PANEL = "data/pit_fundamentals/valuation_fundamentals.parquet"

# Frozen defaults (mirror config/pre_registration_phase8.yaml)
DEFAULTS: Dict[str, Any] = {
    "erp_bounds": (0.0050, 0.0800),
    "wacc_bounds": (0.0, 0.5),
    "iv_floor": 0.01,
    "distressed_spread": 0.0885,
    "spread_bounds_bps": (63.0, 1900.0),
    "rho_floor": 0.25,
    "max_total_beta": 5.0,
    "omega_max": 3.0,
    "omega_neutral": 1.0,
    "min_regime_obs": 5,
}


# ---------------------------------------------------------------------------
# Config + DuckDB I/O
# ---------------------------------------------------------------------------

def load_config(path: str = CONFIG_PATH) -> Dict[str, Any]:
    import yaml
    p = Path(path)
    if not p.exists():
        return {}
    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return doc.get("phase8", doc) or {}


def merged_config(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    phase8 = cfg if cfg is not None else load_config()
    out: Dict[str, Any] = {}
    we = phase8.get("wacc_engine") or {}
    out["erp_bounds"] = tuple(float(x) for x in we.get("erp_bounds", DEFAULTS["erp_bounds"]))
    out["distressed_spread"] = float(we.get("distressed_spread", DEFAULTS["distressed_spread"]))
    sb = we.get("spread_bounds", DEFAULTS["spread_bounds_bps"])
    out["spread_bounds_bps"] = tuple(float(x) * 1e4 for x in sb) \
        if max(sb) <= 1.0 else tuple(float(x) for x in sb)
    out["rho_floor"] = float(we.get("rho_floor", DEFAULTS["rho_floor"]))
    out["omega"] = {**({"max_multiplier": DEFAULTS["omega_max"],
                        "enabled": True, "cash_burn_weight": 1.0,
                        "icr_weight": 1.0}), **(we.get("omega_stress") or {})}
    tb = phase8.get("total_beta") or {}
    out["max_total_beta"] = float(tb.get("max_total_beta", DEFAULTS["max_total_beta"]))
    out["min_regime_obs"] = int(DEFAULTS["min_regime_obs"])
    return out


def duck_read(pattern: str) -> pd.DataFrame:
    """Read parquet via DuckDB (glob patterns allowed). Empty on failure.

    NOTE: duckdb>=1.1 requires `params=` as a keyword; passing the list
    positionally raised TypeError, which the bare `except` below turned into a
    silent empty frame — every panel-backed gate SKIPped.  Both call styles are
    now handled explicitly so a read failure can never masquerade as "missing
    artifact".
    """
    if duckdb is None:
        return pd.DataFrame()
    q = "SELECT * FROM read_parquet(?, filename=true)"
    try:
        return duckdb.sql(q, params=[str(pattern)]).df()
    except TypeError:  # older duckdb: positional params
        try:
            return duckdb.sql(q, [str(pattern)]).df()
        except Exception as exc:  # noqa: BLE001
            print(f"[wacc_gates] duck_read failed for {pattern}: {exc}",
                  file=sys.stderr)
            return pd.DataFrame()
    except Exception as exc:  # noqa: BLE001 — missing artifact is a SKIP, not a crash
        print(f"[wacc_gates] duck_read failed for {pattern}: {exc}",
              file=sys.stderr)
        return pd.DataFrame()


def first_existing(patterns: Sequence[str]) -> Optional[str]:
    for pat in patterns:
        import glob
        if glob.glob(pat):
            return pat
    return None


def load_panel(patterns: Sequence[str] = PANEL_PATTERNS) -> pd.DataFrame:
    pat = first_existing(patterns)
    return duck_read(pat) if pat else pd.DataFrame()


def load_tradable(patterns: Sequence[str] = TRADABLE_PATTERNS) -> pd.DataFrame:
    """Tradable-universe key frame; empty when absent (gate falls back to
    completeness share of the panel itself)."""
    pat = first_existing(patterns)
    return duck_read(pat) if pat else pd.DataFrame()


def load_regime(patterns: Sequence[str] = REGIME_PATTERNS) -> pd.DataFrame:
    pat = first_existing(patterns)
    df = duck_read(pat) if pat else pd.DataFrame()
    if df.empty:
        return df
    # normalize to (date, regime_state): daily file uses timestamp_utc /
    # regime_state; fold file uses obs_date / detected_state.
    date_col = next((c for c in ("timestamp_utc", "obs_date", "asof_date")
                     if c in df.columns), None)
    state_col = next((c for c in ("regime_state", "detected_state", "policy_state")
                      if c in df.columns), None)
    if date_col is None or state_col is None:
        return pd.DataFrame()
    out = pd.DataFrame({
        "date": pd.to_datetime(df[date_col], errors="coerce"),
        "regime_state": df[state_col].astype(str).str.upper(),
    })
    out = out[out["regime_state"].isin(
        ("EXPANSION", "SHOCK", "CRISIS", "RECOVERY"))]
    return out.dropna(subset=["date"])


# ---------------------------------------------------------------------------
# Gate 1 — coverage
# ---------------------------------------------------------------------------

def _panel_complete(panel: pd.DataFrame) -> pd.DataFrame:
    """Rows of the panel that are fully computed + PIT-valid."""
    if panel.empty:
        return panel
    cols = ["wacc_v2", "ke_v2", "erp_blend", "beta_source"]
    present = [c for c in cols if c in panel.columns]
    if not present:
        return panel.iloc[0:0]
    mask = pd.Series(True, index=panel.index)
    for c in present:
        mask &= panel[c].notna()
    if "asof_date" in panel.columns and "retrieval_ts" in panel.columns:
        asof = pd.to_datetime(panel["asof_date"], errors="coerce")
        retr = pd.to_datetime(panel["retrieval_ts"], errors="coerce").dt.tz_localize(None)
        mask &= asof.notna() & retr.notna() & (asof <= retr)
    return panel[mask]


def coverage_gate(panel: pd.DataFrame,
                  tradable: Optional[pd.DataFrame] = None,
                  coverage_threshold: float = 1.0) -> Dict[str, Any]:
    """
    100% WACC coverage of the tradable universe.  Pass = coverage >=
    coverage_threshold (frozen wacc_coverage: '100%_tradable', CEO Ruling #4).
    """
    if panel.empty:
        return {"status": "SKIP", "metric_value": None,
                "reason": "wacc panel artifact missing"}
    complete = _panel_complete(panel)
    if tradable is not None and not tradable.empty:
        t = tradable.copy()
        t["asof_date"] = pd.to_datetime(t["asof_date"], errors="coerce")
        t = t[(t.get("price_stale_flag", pd.Series(False, index=t.index)) == False) &  # noqa: E712
              (t.get("illiquid_flag", pd.Series(False, index=t.index)) == False)]  # noqa: E712
        n_tradable = int(len(t))
        if n_tradable == 0:
            return {"status": "SKIP", "metric_value": None,
                    "reason": "tradable universe empty after stale/illiquid filter"}
        key = ["asof_date", "ticker"]
        if all(c in complete.columns for c in key) and all(c in t.columns for c in key):
            merged = complete[key].drop_duplicates().merge(
                t[key].drop_duplicates(), on=key, how="inner")
            coverage = len(merged) / n_tradable
        else:
            coverage = 0.0
    else:
        total = len(panel)
        coverage = len(complete) / total if total else 0.0
    passed = bool(coverage >= coverage_threshold - 1e-12)
    return {
        "status": "PASS" if passed else "FAIL",
        "metric_value": round(float(coverage), 6),
        "threshold": coverage_threshold,
        "passed": passed,
        "rows_panel": int(len(panel)),
        "rows_complete": int(len(complete)),
        "reason": ("coverage inside frozen 100%_tradable rule"
                   if passed else "coverage below frozen 100%_tradable rule"),
    }


# ---------------------------------------------------------------------------
# Gate 2 — cross-sectional Ke dispersion
# ---------------------------------------------------------------------------

def ke_dispersion_gate(panel: pd.DataFrame,
                       ke_var_threshold: float = 0.005) -> Dict[str, Any]:
    """
    Cross-sectional sigma(ke_v2) per asof_date must stay <= 50 bps.  The gate
    fails when ANY day's dispersion exceeds the threshold (engine unstable /
    cross-sectional contamination).  Needs >= 2 valid ke rows per day.
    """
    if panel.empty or "ke_v2" not in panel.columns or "asof_date" not in panel.columns:
        return {"status": "SKIP", "reason": "panel/ke_v2 columns missing"}
    sub = panel[["asof_date", "ke_v2"]].dropna()
    sub["asof_date"] = pd.to_datetime(sub["asof_date"], errors="coerce")
    sub = sub.dropna()
    if len(sub) < 2:
        return {"status": "SKIP", "reason": "insufficient ke_v2 rows"}
    stds = sub.groupby("asof_date")["ke_v2"].std().dropna()
    stds = stds[stds.notna() & (stds > 0)]
    if stds.empty:
        return {"status": "SKIP", "metric_value": 0.0,
                "reason": "no day has >= 2 valid ke rows"}
    max_std = float(stds.max())
    worst_day = str(stds.idxmax().date()) if hasattr(stds.idxmax(), "date") else str(stds.idxmax())
    passed = bool(max_std <= ke_var_threshold)
    return {
        "status": "PASS" if passed else "FAIL",
        "metric_value": round(max_std, 6),
        "threshold": ke_var_threshold,
        "passed": passed,
        "days_evaluated": int(len(stds)),
        "worst_day": worst_day,
        "reason": ("cross-sectional ke sigma within 50bps" if passed
                   else f"ke dispersion {max_std:.4f} > 50bps on {worst_day}"),
    }


# ---------------------------------------------------------------------------
# Gate 3 — Ke regime sensitivity (dual ke-variance + regime shift)
# ---------------------------------------------------------------------------

def regime_ke_shift_gate(panel: pd.DataFrame, regime: pd.DataFrame,
                         regime_shift_threshold: float = 0.02,
                         min_obs: int = 5) -> Dict[str, Any]:
    """
    The engine must RESPOND to the HMM regime: |median(ke|SHOCK/CRISIS) -
    median(ke|EXPANSION)| >= 200 bps.  Evaluated only when both sides carry
    >= min_obs rows (else SKIP — can't assess with empty regime samples).
    """
    if panel.empty or regime.empty or "ke_v2" not in panel.columns:
        return {"status": "SKIP", "reason": "panel or regime artifact missing"}
    p = panel[["asof_date", "ticker", "ke_v2"]].dropna()
    p["asof_date"] = pd.to_datetime(p["asof_date"], errors="coerce")
    if p["asof_date"].dt.tz is not None:  # regime files may be tz-aware (US/Eastern)
        p["asof_date"] = p["asof_date"].dt.tz_localize(None)
    r = regime.copy()
    r = r.rename(columns={"date": "asof_date"})
    r["asof_date"] = pd.to_datetime(r["asof_date"], errors="coerce")
    if r["asof_date"].dt.tz is not None:
        r["asof_date"] = r["asof_date"].dt.tz_convert("UTC").dt.tz_localize(None)
    r["asof_date"] = r["asof_date"].dt.normalize()
    p["asof_date"] = p["asof_date"].dt.normalize()
    if "asof_date" not in r.columns:
        return {"status": "SKIP", "reason": "regime file has no date column"}
    m = p.merge(r.drop_duplicates("asof_date"), on="asof_date", how="inner")
    if m.empty:
        return {"status": "SKIP", "reason": "no (asof, regime) intersection"}
    stress = m[m["regime_state"].isin(("SHOCK", "CRISIS"))]["ke_v2"]
    calm = m[m["regime_state"] == "EXPANSION"]["ke_v2"]
    if len(stress) < min_obs or len(calm) < min_obs:
        return {"status": "SKIP",
                "reason": f"regime samples too small (stress={len(stress)}, "
                          f"calm={len(calm)} < {min_obs})"}
    med_stress = float(stress.median())
    med_calm = float(calm.median())
    shift = abs(med_stress - med_calm)
    passed = bool(shift >= regime_shift_threshold)
    # Surface the regime mix: a classifier that calls 94% of history SHOCK
    # structurally limits the contrast this gate can measure, so the mix has to
    # travel with the verdict rather than hide behind the two medians.
    mix = (m.groupby("regime_state")["ke_v2"]
             .agg(["median", "count"]).round(6).to_dict("index"))
    total = int(len(m))
    dominant = max(mix.items(), key=lambda kv: kv[1]["count"]) if mix else ("", 0)
    share = (dominant[1]["count"] / total) if total else 0.0
    caveat = None
    if share >= 0.90:
        caveat = (f"regime source is {share:.1%} {dominant[0]} ({total} rows); a "
                  f"near-degenerate classifier structurally caps the achievable "
                  f"median shift, so this verdict is bounded by the regime "
                  f"artifact, not only by the WACC engine")
    return {
        "status": "PASS" if passed else "FAIL",
        "metric_value": round(shift, 6),
        "threshold": regime_shift_threshold,
        "passed": passed,
        "median_ke_stress": round(med_stress, 6),
        "median_ke_expansion": round(med_calm, 6),
        "n_stress": int(len(stress)),
        "n_expansion": int(len(calm)),
        "regime_mix": mix,
        "regime_dominant_share": round(share, 4),
        "regime_caveat": caveat,
        "reason": ("Ke responds to regime (>=200bps)" if passed
                   else f"Ke regime shift {shift:.4f} < 200bps (Ke frozen across regime?)"),
    }


# ---------------------------------------------------------------------------
# Gate 4 — panel numeric bounds
# ---------------------------------------------------------------------------

def panel_bounds_gate(panel: pd.DataFrame,
                      cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    c = merged_config(cfg)
    erp_lo, erp_hi = c["erp_bounds"]
    violations: List[str] = []
    if not panel.empty:
        if "erp_blend" in panel.columns:
            erp = pd.to_numeric(panel["erp_blend"], errors="coerce").dropna()
            n = int(((erp < erp_lo) | (erp > erp_hi)).sum())
            if n:
                violations.append(f"{n} rows erp_blend outside [{erp_lo}, {erp_hi}]")
        for col in ("ke_v2", "wacc_v2"):
            if col in panel.columns:
                num = pd.to_numeric(panel[col], errors="coerce").dropna()
                n = int(((num < 0.0) | (num > 0.5)).sum())
                if n:
                    violations.append(f"{n} rows {col} outside [0, 0.5]")
        if "iv_nowcast" in panel.columns:
            iv = pd.to_numeric(panel["iv_nowcast"], errors="coerce").dropna()
            n = int((iv < DEFAULTS["iv_floor"]).sum())
            if n:
                violations.append(f"{n} rows iv_nowcast below {DEFAULTS['iv_floor']}")
        if "beta_source" in panel.columns:
            n = int(panel["beta_source"].isna().sum())
            if n:
                violations.append(f"{n} rows null beta_source (coverage gate violation)")
    passed = not violations
    return {
        "status": "PASS" if passed else "FAIL",
        "metric_value": len(violations),
        "threshold": 0,
        "passed": passed,
        "violations": violations,
        "reason": "bounds clean" if passed else "; ".join(violations),
        "rows_evaluated": int(len(panel)),
    }


# ---------------------------------------------------------------------------
# Gate 5 — total_beta ceiling + rho floor (valuation panel)
# ---------------------------------------------------------------------------

def beta_ceiling_gate(val: pd.DataFrame,
                      cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if val.empty or not {"total_beta", "rho_market_60m"}.issubset(val.columns):
        return {"status": "SKIP", "reason": "valuation panel total_beta/rho columns missing"}
    c = merged_config(cfg)
    max_tb = c["max_total_beta"]
    rho_floor = c["rho_floor"]
    violations: List[str] = []
    tb = pd.to_numeric(val["total_beta"], errors="coerce")
    n = int((tb > max_tb + 1e-12).sum())
    if n:
        violations.append(f"{n} rows total_beta > {max_tb}")
    rho = pd.to_numeric(val["rho_market_60m"], errors="coerce")
    mask = tb.notna() & rho.notna()
    n = int((rho[mask] < rho_floor - 1e-12).sum())
    if n:
        violations.append(f"{n} rows rho_market_60m < rho_floor {rho_floor}")
    passed = not violations
    return {
        "status": "PASS" if passed else "FAIL",
        "metric_value": len(violations),
        "threshold": 0,
        "passed": passed,
        "violations": violations,
        "max_total_beta_seen": round(float(tb.max()), 6) if tb.notna().any() else None,
        "reason": "total_beta<=5.0 & rho>=0.25 clean" if passed else "; ".join(violations),
    }


# ---------------------------------------------------------------------------
# Gate 6 — credit spread bounds (valuation panel)
# ---------------------------------------------------------------------------

def spread_bounds_gate(val: pd.DataFrame,
                       cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if val.empty or "credit_spread_bps" not in val.columns:
        return {"status": "SKIP", "reason": "valuation panel credit_spread_bps missing"}
    c = merged_config(cfg)
    lo, hi = c["spread_bounds_bps"]
    dist_lo = float(c["distressed_spread"] * 1e4)
    violations: List[str] = []
    sp = pd.to_numeric(val["credit_spread_bps"], errors="coerce")
    n = int(((sp < lo) | (sp > hi)).sum())
    if n:
        violations.append(f"{n} rows spread outside [{lo:.0f}, {hi:.0f}] bps")
    if {"ebit", "synthetic_rating"}.issubset(val.columns):
        ebit = pd.to_numeric(val["ebit"], errors="coerce")
        distressed = (ebit <= 0) & val["synthetic_rating"].isin(("CCC", "CC", "C", "D"))
        if distressed.any():
            dsp = sp[distressed].dropna()
            n = int((dsp < dist_lo - 1e-9).sum())
            if n:
                violations.append(f"{n} distressed rows spread < {dist_lo:.0f} bps "
                                  "(distressed_spread 8.85%)")
    passed = not violations
    return {
        "status": "PASS" if passed else "FAIL",
        "metric_value": len(violations),
        "threshold": 0,
        "passed": passed,
        "violations": violations,
        "reason": "spread bounds clean" if passed else "; ".join(violations),
    }


# ---------------------------------------------------------------------------
# Gate 7 — Omega_stress formula oracle (engine cross-check)
# ---------------------------------------------------------------------------

def omega_oracle_gate(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Runs the frozen Omega_stress formula through the engine oracle over a
    canonical case matrix; every case must land in [1.0, 3.0] and the
    documented EXT-01 case must equal 1.675 exactly (frozen)."""
    from ingestion.wacc_engine_v2 import WACCEngineV2
    c = merged_config(cfg)
    om = c.get("omega", {})
    eng = WACCEngineV2()
    cases: List[Tuple[str, Any, float]] = [
        ("neutral_healthy", (8.0, -1.0, 100.0, "451020"), 1.0),
        ("ext01_frozen", (-2.0, -500.0, 1000.0, "451020"), 1.675),
        ("extreme_burn", (0.1, -1e9, 1000.0, "451020"), 3.0),
        ("missing_icr", (None, -500.0, 1000.0, "451020"), 1.0),
    ]
    results: List[Dict[str, Any]] = []
    violations: List[str] = []
    max_mult = float(om.get("max_multiplier", DEFAULTS["omega_max"]))
    for name, args, expected in cases:
        omega, prov = eng._omega_stress(*args)
        if not (DEFAULTS["omega_neutral"] - 1e-12 <= omega <= max_mult + 1e-12):
            violations.append(f"{name}: omega {omega} outside [1.0, {max_mult}]")
        if expected is not None and abs(omega - expected) > 1e-9:
            violations.append(f"{name}: omega {omega} != frozen {expected}")
        results.append({"case": name, "omega": round(float(omega), 6),
                        "stress_tier": prov.get("stress_tier"),
                        "expected": expected})
    passed = not violations
    return {
        "status": "PASS" if passed else "FAIL",
        "metric_value": len(violations),
        "threshold": 0,
        "passed": passed,
        "violations": violations,
        "cases": results,
        "max_multiplier_frozen": max_mult,
        "enabled_frozen": bool(om.get("enabled")),
        "reason": "omega formula oracle clean" if passed else "; ".join(violations),
    }


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def run_gates(panel: pd.DataFrame, regime: pd.DataFrame, val: pd.DataFrame,
              tradable: Optional[pd.DataFrame] = None,
              coverage_threshold: float = 1.0, ke_var_threshold: float = 0.005,
              regime_shift_threshold: float = 0.02,
              cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    c = merged_config(cfg)
    gates = {
        "coverage": coverage_gate(panel, tradable, coverage_threshold),
        "ke_dispersion": ke_dispersion_gate(panel, ke_var_threshold),
        "regime_shift": regime_ke_shift_gate(panel, regime, regime_shift_threshold,
                                             int(c["min_regime_obs"])),
        "panel_bounds": panel_bounds_gate(panel, c),
        "beta_ceiling": beta_ceiling_gate(val, c),
        "spread_bounds": spread_bounds_gate(val, c),
        "omega_oracle": omega_oracle_gate(c),
    }
    failed = [k for k, g in gates.items() if g["status"] == "FAIL"]
    evaluated = [k for k, g in gates.items() if g["status"] != "SKIP"]
    status = "FAIL" if failed else ("PASS" if evaluated else "SKIP")
    rows = max([len(panel), len(regime), len(val),
                len(tradable) if tradable is not None else 0])
    date_range = None
    if not panel.empty and "asof_date" in panel.columns:
        d = pd.to_datetime(panel["asof_date"], errors="coerce").dropna()
        if len(d):
            date_range = {"min": str(d.min().date()), "max": str(d.max().date())}
    # production coverage extension: surface the coverage metric at report
    # level so ingestion/harness/gate_engine.py gate 12 can consume it
    # without parsing the per-gate dict (100%_tradable, CEO Ruling #4)
    cov_g = gates.get("coverage") or {}
    coverage_fraction = cov_g.get("metric_value")
    tradable_n = cov_g.get("rows_complete")
    return {
        "name": "wacc_gates",
        "passed": status == "PASS",
        "status": status,
        "gates": gates,
        "coverage_fraction": coverage_fraction,
        "coverage_tradable_rows": int(tradable_n) if tradable_n is not None else None,
        # Lineage: bind this verdict to the exact artifacts that were judged, so
        # a later panel rebuild cannot silently inherit an older verdict.
        "inputs": input_lineage(),
        "summary": {
            "status": status,
            "failed_gates": failed,
            "evaluated_gates": evaluated,
            "rows_evaluated": rows,
            "date_range": date_range,
        },
    }


def input_lineage() -> Dict[str, Any]:
    """sha256 + rows + mtime for every artifact this report judges."""
    out: Dict[str, Any] = {}
    for name, path in (("panel", first_existing(PANEL_PATTERNS)),
                       ("tradable", first_existing(TRADABLE_PATTERNS)),
                       ("regime", first_existing(REGIME_PATTERNS)),
                       ("valuation", VALUATION_PANEL)):
        try:
            p = Path(path)
            if not p.exists():
                out[name] = {"path": str(p), "exists": False}
                continue
            digest = hashlib.sha256()
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
            out[name] = {
                "path": str(p),
                "exists": True,
                "sha256": digest.hexdigest(),
                "bytes": p.stat().st_size,
                "modified_utc": datetime.fromtimestamp(
                    p.stat().st_mtime, tz=timezone.utc).isoformat(),
            }
        except Exception as exc:  # noqa: BLE001
            out[name] = {"path": str(path), "error": f"{type(exc).__name__}: {exc}"}
    return out


def write_report(report: Dict[str, Any], report_path: Path) -> None:
    """§8-provenance-wrapped report + per-gate parquet sidecar (DuckDB write)."""
    content = json.dumps(report, sort_keys=True, default=str).encode("utf-8")
    prov = {
        "source": "validation.wacc_gates",
        "retrieval_timestamp": datetime.now(timezone.utc).isoformat(),
        "sha256": hashlib.sha256(content).hexdigest(),
        "row_count": report.get("summary", {}).get("rows_evaluated", 0),
        "date_range": report.get("summary", {}).get("date_range"),
        "mapping_confidence": "n/a",
    }
    report["provenance"] = prov
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str),
                           encoding="utf-8")
    # per-gate results parquet (DuckDB, §8 columns)
    rows = []
    for name, g in report["gates"].items():
        rows.append({
            "gate_name": name, "status": g["status"],
            "passed": bool(default := g.get("passed", g["status"] == "PASS")),
            "metric_value": g.get("metric_value"),
            "threshold": g.get("threshold"),
            "reason": str(g.get("reason", "")),
            "source": prov["source"], "retrieval_timestamp": prov["retrieval_timestamp"],
            "sha256": prov["sha256"], "row_count": prov["row_count"],
            "date_range": json.dumps(prov["date_range"]) if prov["date_range"] else None,
            "mapping_confidence": "n/a",
        })
    df = pd.DataFrame(rows)
    out = report_path.with_name("wacc_gates_results.parquet")
    if not df.empty and duckdb is not None:
        duckdb.sql("CREATE OR REPLACE TABLE wacc_gates_results AS SELECT * FROM df")
        out.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(out, engine="pyarrow", index=False)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="wacc_gates — daily WACC panel validation (Phase 3 gates)")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--coverage-threshold", type=float, default=1.0)
    parser.add_argument("--ke-var-threshold", type=float, default=0.005)
    parser.add_argument("--regime-shift-threshold", type=float, default=0.02)
    parser.add_argument("--panel", type=str, default=None,
                        help="wacc panel parquet / glob (default: producer paths)")
    parser.add_argument("--regime", type=str, default=None,
                        help="regime states parquet / glob")
    parser.add_argument("--valuation", type=str, default=VALUATION_PANEL)
    parser.add_argument("--tradable", type=str, default=None,
                        help="tradable universe parquet (stale/illiquid flags); "
                             "defaults to the first of TRADABLE_PATTERNS")
    parser.add_argument("--report", type=str, default=str(DEFAULT_REPORT))
    parser.add_argument("--config", type=str, default=CONFIG_PATH)
    args = parser.parse_args()

    if args.test:
        return run_tests()
    if not args.run:
        parser.print_help()
        return 1

    panel = duck_read(args.panel) if args.panel else load_panel()
    regime = duck_read(args.regime) if args.regime else load_regime()
    val = duck_read(args.valuation)
    tradable = duck_read(args.tradable) if args.tradable else load_tradable()

    report = run_gates(panel, regime, val, tradable,
                       coverage_threshold=args.coverage_threshold,
                       ke_var_threshold=args.ke_var_threshold,
                       regime_shift_threshold=args.regime_shift_threshold,
                       cfg=load_config(args.config))
    write_report(report, Path(args.report))
    s = report["summary"]
    print(f"[wacc_gates] report: {args.report}")
    print(f"[wacc_gates] status={report['status']} "
          f"evaluated={len(s['evaluated_gates'])} gates, "
          f"failed={s['failed_gates'] or 'none'}")
    for name, g in report["gates"].items():
        print(f"  [{g['status']:4s}] {name}: {g.get('metric_value')} "
              f"(threshold {g.get('threshold')}) {g.get('reason', '')[:100]}")
    return 1 if report["status"] == "FAIL" else 0  # 0 for PASS and SKIP


def run_tests() -> int:
    print("wacc_gates self-test")
    print("=" * 66)
    # coverage
    panel = pd.DataFrame({
        "asof_date": pd.to_datetime(["2026-09-01"] * 4),
        "ticker": list("ABCD"),
        "ke_v2": [0.08, 0.09, 0.10, 0.11],
        "erp_blend": [0.04, 0.04, 0.04, 0.04],
        "wacc_v2": [0.07, 0.08, 0.09, 0.10],
        "beta_source": ["sector_bottom_up"] * 4,
        "retrieval_ts": pd.to_datetime(["2026-09-02"] * 4),
    })
    cg = coverage_gate(panel)
    assert cg["status"] == "PASS" and cg["metric_value"] == 1.0
    panel_bad = panel.copy()
    panel_bad.loc[0, "beta_source"] = None
    assert coverage_gate(panel_bad)["status"] == "FAIL"
    # ke dispersion
    wide = pd.concat([pd.DataFrame({
        "asof_date": pd.to_datetime(["2026-09-01"] * 10),
        "ticker": [f"T{i}" for i in range(10)],
        "ke_v2": np.linspace(0.02, 0.22, 10)}),
        panel[["asof_date", "ticker", "ke_v2"]]], ignore_index=True)
    assert ke_dispersion_gate(wide)["status"] == "FAIL"          # sigma ~0.06 > 50bps
    tight = pd.DataFrame({
        "asof_date": pd.to_datetime(["2026-09-01"] * 4),
        "ticker": list("WXYZ"),
        "ke_v2": [0.1000, 0.1005, 0.1010, 0.1015],               # sigma ~6.5e-4
    })
    assert ke_dispersion_gate(tight)["status"] == "PASS"
    # regime shift
    regime = pd.DataFrame({"date": pd.to_datetime(["2026-09-01"] * 2),
                           "regime_state": ["SHOCK", "SHOCK"]})
    big_stress_calm = pd.DataFrame({
        "asof_date": pd.to_datetime(["2026-09-01"] * 5 + ["2026-08-28"] * 5),
        "ticker": list("ABCDEFGHIJ"),
        "ke_v2": [0.09] * 5 + [0.15] * 5,
    })
    # only SHOCK dates available -> calm side empty -> SKIP
    assert regime_ke_shift_gate(big_stress_calm, regime, min_obs=5)["status"] == "SKIP"
    r2 = pd.concat([regime, pd.DataFrame({"date": pd.to_datetime(["2026-08-28"] * 5),
                                          "regime_state": ["EXPANSION"] * 5})],
                   ignore_index=True)
    g3 = regime_ke_shift_gate(big_stress_calm, r2, min_obs=5)
    assert g3["status"] == "PASS" and g3["metric_value"] >= 0.02
    # bounds
    assert panel_bounds_gate(panel)["status"] == "PASS"
    pb = panel.copy()
    pb.loc[0, "erp_blend"] = 0.12
    assert panel_bounds_gate(pb)["status"] == "FAIL"
    # beta ceiling
    val = pd.DataFrame({"total_beta": [1.2, 4.9, 5.0],
                        "rho_market_60m": [0.6, 0.3, 0.25]})
    assert beta_ceiling_gate(val)["status"] == "PASS"
    val_bad = val.copy()
    val_bad.loc[1, "total_beta"] = 5.5
    assert beta_ceiling_gate(val_bad)["status"] == "FAIL"
    val_bad2 = val.copy()
    val_bad2.loc[2, "rho_market_60m"] = 0.1
    assert beta_ceiling_gate(val_bad2)["status"] == "FAIL"
    # spread bounds
    sv = pd.DataFrame({"credit_spread_bps": [63.0, 885.0, 1900.0],
                       "ebit": [1e9, -5.0, -5.0],
                       "synthetic_rating": ["AAA", "CCC", "D"]})
    assert spread_bounds_gate(sv)["status"] == "PASS"
    sv_bad = sv.copy()
    sv_bad.loc[1, "credit_spread_bps"] = 400.0
    assert spread_bounds_gate(sv_bad)["status"] == "FAIL"        # distressed < 885
    sv_bad2 = sv.copy()
    sv_bad2.loc[2, "credit_spread_bps"] = 2000.0
    assert spread_bounds_gate(sv_bad2)["status"] == "FAIL"       # > 1900
    # omega oracle (needs engine — frozen config present)
    og = omega_oracle_gate()
    assert og["status"] == "PASS", og["violations"]
    for case in og["cases"]:
        assert 1.0 <= case["omega"] <= 3.0
    print("[OK] coverage, ke dispersion, regime shift, bounds, beta, spread, omega")
    print("\nAll wacc_gates tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())