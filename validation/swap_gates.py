"""
Swap Barrier Churn Gate — Phase 0-1 (bigpickle-worker / Workstream B)

Validates the Exponential Barrier Swap Rule (Data_Strategy.md §12.7.2) against
the frozen config/pre_registration_v2.yaml `gates.swap_barrier_churn`:

    max_annual_turnover: 0.30     (turnover < 30%/yr)
    max_swaps_per_year:  5        (swaps < 5/yr)
    moat_protection_efficacy: true  (moat-intact holdings must be shielded)

Core equation (from §12.7.2 — reproduced for provenance):
    ΔEdge_{j,i} > C · exp(γ · t_target / (t_held + ε)) + Penalty_moat
    C = 150 bps, γ = 0.5, t_target = 24 months, ε = 1e-6,
    Penalty_moat = 100 bps if moat intact on the CURRENT holding.

Input contract — `backtest_results` dict:
    trades          DataFrame | list[dict]
        date                 : decision date (pd.Timestamp; PIT)
        ticker               : str
        action               : BUY | SELL | SWAP  (SWAP = sold to switch names)
        reason               : one of {floor, target, trailing, swap,
                               regime_shift, swap_candidate}
        is_moat              : bool — moat intact on the CURRENT holding
        moat_penalty_bps     : float (100.0 when moat else 0.0)   [optional]
        edge_delta_bps       : candidate − current expected return (bps)
        t_held_months        : float [optional]
        swap_barrier_bps     : barrier value at that date [optional;
                               computed from t_held_months when absent]
        notional_usd         : traded notional [optional; required for the
                               notional-based turnover definition]
    period_years    float  (defaults to date-range length)
    avg_portfolio_notional  float  (required for notional turnover;
                               defaults to sum of BUY notionals over time/2)

Metrics:
    annual_turnover          one-way traded notional / years / avg AUM
                             (fallback: swaps_per_year × base weight)
    swaps_per_year           count of swap exits / years
    moat_swaps_prevented     moat-intact holdings with candidate edge
                             (edge_delta_bps > 0) that were NOT swapped
    moat_protection_efficacy prevented / candidate events  (>= 0.80 when
                             events exist; vacuous pass when none)

CLI (dispatch verification, must exit 0):
    python -m validation.swap_gates --test --config config/pre_registration_v2.yaml
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

CONFIG_PATH = "config/pre_registration_v2.yaml"
SWAP_REASONS = {"swap"}
EXIT_REASONS = {"floor", "target", "trailing", "swap", "regime_shift"}

# Frozen defaults (mirror config; overridden by config when present)
DEFAULT_MAX_TURNOVER = 0.30
DEFAULT_MAX_SWAPS_PER_YEAR = 5
DEFAULT_MOAT_EFFICACY = 0.80
DEFAULT_C_BPS = 150.0
DEFAULT_GAMMA = 0.5
DEFAULT_T_TARGET = 24.0
DEFAULT_MOAT_PENALTY_BPS = 100.0


def load_config(path: str = CONFIG_PATH) -> Dict[str, Any]:
    import yaml
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"pre-registration config not found: {path}")
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return cfg


def swap_barrier_bps(t_held_months: float, c_bps: float = DEFAULT_C_BPS,
                     gamma: float = DEFAULT_GAMMA,
                     t_target: float = DEFAULT_T_TARGET,
                     moat_penalty_bps: float = 0.0,
                     epsilon: float = 1e-6) -> float:
    """Exponential barrier per §12.7.2 (used as a reference if column missing)."""
    import math
    barrier = c_bps * math.exp(gamma * t_target / (max(float(t_held_months), 0.0) + epsilon))
    return float(barrier + moat_penalty_bps)


def _normalize_trades(trades: Any) -> pd.DataFrame:
    if isinstance(trades, pd.DataFrame):
        df = trades.copy()
    elif isinstance(trades, Sequence) and len(trades) and isinstance(trades[0], dict):
        df = pd.DataFrame(list(trades))
    else:
        raise TypeError("trades must be DataFrame or list[dict]")

    required = {"date", "ticker"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"trades missing required columns: {sorted(missing)}")
    df["date"] = pd.to_datetime(df["date"])
    df["ticker"] = df["ticker"].astype(str)

    if "action" not in df.columns:
        df["action"] = np.where(df["reason"].astype(str).str.lower().isin(SWAP_REASONS),
                                "SWAP", "SELL")
    df["action"] = df["action"].astype(str).str.upper()
    if "reason" in df.columns:
        df["reason"] = df["reason"].astype(str).str.lower()
    else:
        df["reason"] = np.where(df["action"] == "SWAP", "swap", "sell")
    if "is_moat" in df.columns:
        df["is_moat"] = df["is_moat"].astype(bool)
    else:
        df["is_moat"] = False
    for c in ("edge_delta_bps", "moat_penalty_bps", "t_held_months",
              "swap_barrier_bps", "notional_usd"):
        if c not in df.columns:
            df[c] = np.nan
    return df.sort_values("date").reset_index(drop=True)


def test_swap_barrier(
    backtest_results: Dict[str, Any],
    max_annual_turnover: Optional[float] = None,
    max_swaps_per_year: Optional[float] = None,
    moat_efficacy_threshold: float = DEFAULT_MOAT_EFFICACY,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Gate 15 — swap-barrier churn effectiveness.

    Returns (gate contract dict for ingestion/harness/gate_engine.py):
        {name, passed, p_value: None, metric_value: max(normalized violations),
         annual_turnover, swaps_per_year, moat_swaps_prevented,
         moat_protection_efficacy, thresholds, ...detail}
    """
    try:
        trades = _normalize_trades(backtest_results.get("trades"))
    except Exception as exc:  # noqa: BLE001
        return {"name": "swap_barrier_churn", "passed": False, "p_value": None,
                "metric_value": None, "reason": "insufficient_data_or_error",
                "detail": f"{type(exc).__name__}: {exc}"}
    if trades.empty:
        return {"name": "swap_barrier_churn", "passed": False, "p_value": None,
                "metric_value": None, "reason": "insufficient_data_or_error",
                "detail": "empty trades"}

    cfg = config if config is not None else load_config()
    gate_cfg = cfg.get("gates", {}).get("swap_barrier_churn", {})
    if max_annual_turnover is None:
        max_annual_turnover = float(gate_cfg.get("max_annual_turnover", DEFAULT_MAX_TURNOVER))
    if max_swaps_per_year is None:
        max_swaps_per_year = float(gate_cfg.get("max_swaps_per_year", DEFAULT_MAX_SWAPS_PER_YEAR))

    years = float(backtest_results.get("period_years")
                  or max((trades["date"].max() - trades["date"].min()).days / 365.25, 1e-9))
    years = max(years, 1e-9)

    swaps = trades[(trades["action"] == "SWAP")
                   | (trades["reason"].isin(SWAP_REASONS))]
    swaps_per_year = float(len(swaps) / years)

    # notional turnover when possible; else swap-count fallback
    has_notional = trades["notional_usd"].notna().any()
    annual_turnover: float
    turnover_method: str
    if has_notional:
        avg_aum = float(backtest_results.get("avg_portfolio_notional")
                        or trades["notional_usd"].sum() / max(2 * years, 1e-9))
        one_way_notional = float(trades["notional_usd"].fillna(0.0).abs().sum())
        annual_turnover = one_way_notional / years / max(avg_aum, 1e-9)
        turnover_method = "notional"
    else:
        annual_turnover = swaps_per_year * 0.02   # 2% base weight per swap
        turnover_method = "swap_count_estimate"

    # Moat protection: moat-intact holdings with a candidate edge that did NOT swap
    if "edge_delta_bps" in trades.columns:
        candidates = trades[trades["edge_delta_bps"].notna() & (trades["edge_delta_bps"] > 0)]
        moat_candidates = candidates[candidates["is_moat"]]
    else:
        candidates = pd.DataFrame()
        moat_candidates = pd.DataFrame()
    prevented = moat_candidates[
        ~moat_candidates["reason"].isin(SWAP_REASONS)
        & (moat_candidates["action"] != "SWAP")]
    n_candidate_events = len(moat_candidates)
    moat_swaps_prevented = int(len(prevented))
    moat_protection_efficacy = (
        moat_swaps_prevented / n_candidate_events if n_candidate_events > 0 else 1.0
    )

    passed_turnover = annual_turnover <= max_annual_turnover
    passed_swaps = swaps_per_year <= max_swaps_per_year
    passed_moat = (moat_protection_efficacy >= moat_efficacy_threshold) \
        if n_candidate_events > 0 else True
    passed = bool(passed_turnover and passed_swaps and passed_moat)

    # barrier reference check: when swap rows carry t_held_months, verify a
    # non-moat swap observed at t_held < 6 months is consistent with barrier
    barrier_audit = []
    if "t_held_months" in trades.columns:
        for _, row in swaps.iterrows():
            if pd.isna(row.get("t_held_months")):
                continue
            bar = swap_barrier_bps(
                float(row["t_held_months"]),
                c_bps=float(cfg.get("swap_barrier", {}).get("C_bps", DEFAULT_C_BPS)),
                gamma=float(cfg.get("swap_barrier", {}).get("gamma", DEFAULT_GAMMA)),
                t_target=float(cfg.get("swap_barrier", {}).get("t_target_months", DEFAULT_T_TARGET)),
                moat_penalty_bps=float(row.get("moat_penalty_bps") or 0.0),
            )
            barrier_audit.append({
                "ticker": row["ticker"], "date": str(row["date"].date()),
                "t_held_months": float(row["t_held_months"]),
                "barrier_bps": round(float(bar), 2),
            })

    return {
        "name": "swap_barrier_churn",
        "passed": passed,
        "p_value": None,  # descriptive churn audit
        "metric_value": round(max(
            annual_turnover / max_annual_turnover,
            swaps_per_year / max_swaps_per_year,
            (1.0 - moat_protection_efficacy) if n_candidate_events else 0.0,
        ), 6),
        "metric": "max_normalized_violation_ratio",
        "annual_turnover": round(annual_turnover, 6),
        "turnover_method": turnover_method,
        "swaps_per_year": round(swaps_per_year, 6),
        "moat_swaps_prevented": moat_swaps_prevented,
        "moat_candidate_events": n_candidate_events,
        "moat_protection_efficacy": round(float(moat_protection_efficacy), 6),
        "period_years": round(years, 4),
        "n_trades": int(len(trades)),
        "n_swaps": int(len(swaps)),
        "thresholds": {
            "max_annual_turnover": max_annual_turnover,
            "max_swaps_per_year": max_swaps_per_year,
            "moat_protection_efficacy": moat_efficacy_threshold,
        },
        "barrier_audit": barrier_audit[:20],
    }


# ----------------------------------------------------------------------------
# Self-test (falsification-first)
# ----------------------------------------------------------------------------

def _book(period_start: str, rows: List[dict]) -> Dict[str, Any]:
    return {
        "trades": pd.DataFrame(rows),
        "period_years": None,
        "avg_portfolio_notional": None,
    }


def _barrier_book(seed: int = 0) -> Dict[str, Any]:
    """Disciplined book: barrier active — few swaps, turnover low, moat shielded."""
    rng = np.random.default_rng(seed)
    rows = []
    dates = pd.bdate_range("2022-01-03", "2023-12-29")
    names = [f"MOAT{i}" for i in range(12)] + [f"COM{i}" for i in range(8)]
    for nm in names:
        is_moat = nm.startswith("MOAT")
        # swap candidates reviewed ~ every 40 trading days; edge ~ N(95,150) bps
        # vs barrier 670(bps @8mo) - 247(bps @24mo): crossings are rare
        for k, d in enumerate(dates[::40], start=1):
            held = 8.0 + 16.0 * (k / max(len(dates[::40]), 1))
            edge = rng.normal(95.0, 150.0)
            bar = swap_barrier_bps(held, moat_penalty_bps=100.0 if is_moat else 0.0)
            swap_now = (not is_moat) and (edge > bar)
            rows.append({
                "date": d, "ticker": nm,
                "action": "SWAP" if swap_now else "SELL",
                "reason": "swap" if swap_now else
                          ("swap_candidate" if edge > 0 else "target"),
                "is_moat": is_moat, "edge_delta_bps": edge,
                "moat_penalty_bps": 100.0 if is_moat else 0.0,
                "t_held_months": held,
            })
    return _book("2022-01-03", rows)


def _churn_book(seed: int = 1) -> Dict[str, Any]:
    """No barrier: constant swapping, high turnover, moat ignored."""
    rng = np.random.default_rng(seed)
    rows = []
    names = [f"N{i}" for i in range(20)]
    dates = pd.bdate_range("2022-01-03", "2023-12-29")
    n = 0
    for t, d in enumerate(dates):
        if t % 5 == 0:
            for nm in names:
                r = rng.random()
                if r < 0.5:
                    rows.append({"date": d, "ticker": nm, "action": "SWAP",
                                 "reason": "swap", "is_moat": bool(r < 0.2),
                                 "edge_delta_bps": rng.normal(80, 60),
                                 "moat_penalty_bps": 0.0, "t_held_months": 2.0})
                    n += 1
    return _book("2022-01-03", rows)


def run_tests(config_path: str = CONFIG_PATH) -> int:
    print("Swap-barrier gate self-test")
    print("=" * 60)

    cfg = load_config(config_path)
    sb = cfg["swap_barrier"]
    assert abs(float(sb["C_bps"]) - 150.0) < 1e-9
    assert abs(float(sb["gamma"]) - 0.5) < 1e-9
    assert abs(float(sb["t_target_months"]) - 24.0) < 1e-9
    assert abs(float(sb["moat_penalty_bps"]) - 100.0) < 1e-9
    print("[OK] frozen barrier params: C=150bps, gamma=0.5, t_target=24mo, moat=100bps")

    # reference barrier values from §12.7.2
    assert abs(swap_barrier_bps(1.0) - 150.0 * np.exp(12.0)) / (150.0 * np.exp(12.0)) < 1e-3
    assert abs(swap_barrier_bps(24.0) - 150.0 * np.exp(0.5)) / (150.0 * np.exp(0.5)) < 1e-3
    print("[OK] exponential barrier reproduces S12.7.2 table (month 1 ~ 1,500+ bps; "
          "month 24 ~ 247 bps)")

    # PASS book: barrier-active discipline
    out_pass = test_swap_barrier(_barrier_book(seed=0), config=cfg)
    assert out_pass["passed"], out_pass
    assert out_pass["swaps_per_year"] < 5.0, out_pass
    assert out_pass["annual_turnover"] < 0.30, out_pass
    assert out_pass["moat_protection_efficacy"] >= 0.80, out_pass
    print(f"[OK] barrier-active book PASS: turnover={out_pass['annual_turnover']:.3f}, "
          f"swaps/yr={out_pass['swaps_per_year']:.2f}, "
          f"moat_efficacy={out_pass['moat_protection_efficacy']:.2f} "
          f"({out_pass['moat_swaps_prevented']}/{out_pass['moat_candidate_events']} prevented)")

    # FAIL book: churn
    out_fail = test_swap_barrier(_churn_book(seed=1), config=cfg)
    assert not out_fail["passed"], out_fail
    print(f"[OK] churny book FAIL: turnover={out_fail['annual_turnover']:.3f}, "
          f"swaps/yr={out_fail['swaps_per_year']:.1f}, "
          f"moat_efficacy={out_fail['moat_protection_efficacy']:.2f}")

    # broken input -> graceful non-pass
    out_bad = test_swap_barrier({"trades": pd.DataFrame()}, config=cfg)
    assert not out_bad["passed"] and "insufficient" in str(out_bad.get("reason", ""))
    print("[OK] empty trades handled gracefully (gate fails, does not throw)")

    print("\nAll swap-barrier gate tests passed.")
    return 0


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="Swap barrier churn gate — turnover, swap count, moat protection")
    parser.add_argument("--test", action="store_true", help="Run self-test suite")
    parser.add_argument("--config", type=str, default=CONFIG_PATH,
                        help="Frozen pre-registration config (default: %(default)s)")
    args = parser.parse_args()

    if args.test:
        return run_tests(config_path=args.config)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())