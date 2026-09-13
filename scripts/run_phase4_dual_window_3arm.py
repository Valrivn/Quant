"""Phase 4 — Dual-Window 3-Arm Backtest Driver (Workstream B, big-pickle-worker).

Executes the canonical 3-arm attribution backtest (Data_Strategy.md section 6)
across the two pre-registered windows using the REAL factor estate:

    Primary   : 2018-01-01 -> 2024-12-31  (7y; RPO-clean, rpo_is_imputed=False)
    Secondary : 2020-01-01 -> 2026-12-31  (extended horizon, RPO-clean)

NOTE (2026-09-13 provenance audit): the primary window definition changed from
2014-2024 (which relied on RPO imputations; validation FAIL, OOS R2=0.157) to
2018-2024 per the RS-07 pre-registered ruling.  `compute_rpo_growth` now drops
rows where rpo_is_imputed=True, so the RPO factor is clean by construction.

Pipeline:
    1. Quant factors (roic / wacc / icr / fcf_yield) derived PIT from
       data/pit_fundamentals/pit_fundamentals.parquet (SEC companyfacts).
    2. Universe_T  = PIT month-end expansion of the tradable names out of the
       SEC estate (names with local price files AND complete quant rows).
    3. Qual factors_T = PIT month-end expansion of rpo_growth (SEC, RPO-clean)
       and the 2023-sample alt-data velocity z-scores (GH/ATS/patents/CFPB/
       NHTSA) read from the canonical data/qual/ tree.  Out-of-window factors
       (mda_tone 2026, app_store 2026) stay excluded; missing velocity months
       are coded as neutral z=0 (documented).
    4. ThreeArmRunner (A quant / B qual / C hybrid, monthly rebalance, 30%
       turnover cap) produces the six-contract result dict.
    5. GateEngine evaluates all 10 pre-registered gates (Bonferroni
       alpha=0.0071).

Artifacts (per window):
    .agents/project/org/backtests/20260910-phase4-3arm-<window>.md
    .agents/project/org/backtests/20260910-phase4-3arm-<window>-gates.json

CLI:
    python scripts/run_phase4_dual_window_3arm.py [--window primary|secondary|both]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import pandas as pd

from ingestion.harness.gate_engine import GateEngine, GATE_REGISTRY
from ingestion.harness.three_arm_runner import (
    QUAL_FACTOR_COLS,
    QUANT_FACTOR_COLS,
    TOP_N_DEFAULT,
    ThreeArmRunner,
)

DATA = PROJECT_ROOT / "data"
FUNDAMENTALS = DATA / "pit_fundamentals" / "pit_fundamentals.parquet"
PRICES = DATA / "master_prices"
# RPO stays canonical in data/factors/; all alt-data qual reads data/qual/.
RPO = DATA / "factors" / "rpo.parquet"
GH = DATA / "qual" / "gh_velocity.parquet"
ATS = DATA / "qual" / "ats_velocity.parquet"
PATENTS = DATA / "qual" / "patents_signals.parquet"
CFPB = DATA / "qual" / "cfpb_velocity.parquet"
NHTSA = DATA / "qual" / "nhtsa_signals.parquet"
INST = DATA / "qual" / "inst_ownership.parquet"

OUT_DIR = PROJECT_ROOT / ".agents" / "project" / "org" / "backtests"

WINDOWS = {
    "primary": {"start": "2018-01-01", "end": "2024-12-31",
                "label": "PRIMARY 2018-2024 (7y, RPO-clean)"},
    "secondary": {"start": "2020-01-01", "end": "2026-12-31",
                  "label": "SECONDARY 2020-2026 (7y, extended, RPO-clean)"},
}

WACC_CONSTANT = 0.10      # pre-registered hurdle for ROIC > WACC screen
COST_OF_DEBT = 0.05       # ICR proxy base: ebit / (total_debt * 5%)


# --------------------------------------------------------------------------
# Loaders
# --------------------------------------------------------------------------

def load_prices() -> Dict[str, pd.DataFrame]:
    out: Dict[str, pd.DataFrame] = {}
    for f in PRICES.glob("*.parquet"):
        df = pd.read_parquet(f)
        if not isinstance(df.index, pd.DatetimeIndex):
            continue
        idx = pd.to_datetime(df.index)
        if idx.tz is not None:
            idx = idx.tz_localize(None)
        close = df["Close"].astype(float)
        close.index = idx
        out[f.stem] = close.sort_index()
    return out


def load_fundamentals() -> pd.DataFrame:
    df = pd.read_parquet(FUNDAMENTALS)
    df = df.rename(columns={"as_of": "date"})
    df["date"] = pd.to_datetime(df["date"])
    piv = df.pivot_table(index=["date", "ticker"], columns="field",
                         values="value", aggfunc="first").reset_index()
    return piv


def compute_quant_factors(fund: pd.DataFrame, prices: Dict[str, pd.DataFrame]
                          ) -> pd.DataFrame:
    """Per-filing PIT quant factors (SEC-derived; see module docstring)."""
    rows: List[Dict[str, Any]] = []
    for (d, ticker), g in fund.groupby(["date", "ticker"]):
        row = g.iloc[0]
        ebit = row.get("ebit")
        tax = row.get("tax_rate")
        if not np.isfinite(ebit) or not np.isfinite(tax):
            continue
        tax = float(np.clip(tax, 0.0, 0.6))
        nopat = ebit * (1.0 - tax)
        debt = row.get("total_debt") or 0.0
        cash = row.get("cash") or 0.0
        sh = row.get("shares_outstanding")
        px = _price_at_or_before(prices, ticker, d)
        if not np.isfinite(sh) or sh <= 0 or px is None:
            continue
        mcap = sh * px
        inv_cap = max(debt + mcap - cash, 1e6)
        roic = nopat / inv_cap

        dep = row.get("depreciation") or 0.0
        capex = row.get("capex") or 0.0
        nwc = row.get("change_nwc") or 0.0
        fcf = nopat + dep - capex - nwc
        fcf_yield = fcf / mcap if mcap > 0 else np.nan

        icr = ebit / max(debt * COST_OF_DEBT, 1e3)
        rows.append({
            "date": d, "ticker": ticker, "roic": roic, "wacc": WACC_CONSTANT,
            "icr": icr, "fcf_yield": fcf_yield,
            "mcap": mcap, "nopat": nopat,
        })
    return pd.DataFrame(rows)


def _price_at_or_before(prices: Dict[str, pd.DataFrame], ticker: str,
                        d: pd.Timestamp) -> Optional[float]:
    px = prices.get(ticker)
    if px is None:
        return None
    hist = px[px.index <= d]
    return float(hist.iloc[-1]) if len(hist) else None


def load_13f_sponsor() -> pd.DataFrame:
    """Last-known sponsor status: ticker has >=1 13F holder at filing date."""
    inst = pd.read_parquet(INST)
    inst["date"] = pd.to_datetime(inst["date"])
    inst = inst.dropna(subset=["ticker"])
    sponsor = (inst[inst.get("inst_holders", pd.Series(dtype=float)).fillna(0) > 0]
               [["date", "ticker"]].copy())
    sponsor["has_13f_sponsor"] = True
    return sponsor


# --------------------------------------------------------------------------
# PIT expansion helpers
# --------------------------------------------------------------------------

def month_ends(start: str, end: str) -> pd.DatetimeIndex:
    return pd.date_range(start, end, freq="ME")


def pit_expand_long(df: pd.DataFrame, tickers: List[str],
                    start: str, end: str,
                    factor_col: str) -> pd.DataFrame:
    """
    For each ticker x month-end: last observed value with date <= month end
    (PIT forward-fill on the raw factor dates).
    """
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    ends = month_ends(start, end)
    out: List[Dict[str, Any]] = []
    for t in tickers:
        g = df[(df["ticker"] == t)].sort_values("date")
        if g.empty:
            continue
        last_val: Optional[float] = None
        for d in ends:
            sub = g[g["date"] <= d]
            if not sub.empty:
                last_val = float(sub.iloc[-1][factor_col])
            if last_val is not None and np.isfinite(last_val):
                out.append({"date": d, "ticker": t, factor_col: last_val})
    return pd.DataFrame(out)


def pit_expand_quant(qf: pd.DataFrame, tickers: List[str],
                     start: str, end: str) -> pd.DataFrame:
    """PIT expansion for the wide quant frame (per-ticker monotone cols)."""
    qf = qf.copy()
    qf["date"] = pd.to_datetime(qf["date"])
    ends = month_ends(start, end)
    keep = ["roic", "wacc", "icr", "fcf_yield", "has_13f_sponsor", "mcap"]
    out: List[Dict[str, Any]] = []
    for t in tickers:
        g = qf[qf["ticker"] == t].sort_values("date")
        if g.empty:
            continue
        last: Dict[str, Any] = {}
        for d in ends:
            sub = g[g["date"] <= d]
            if not sub.empty:
                last = sub.iloc[-1].to_dict()
            if last:
                r = {"date": d, "ticker": t}
                for c in keep:
                    r[c] = last.get(c)
                out.append(r)
    return pd.DataFrame(out)


def compute_rpo_growth() -> pd.DataFrame:
    """Quarterly YoY growth of rpo (PIT date = filed_date), RPO-CLEAN.

    Imputed rows (rpo_is_imputed=True) are DROPPED per RS-07: the rpo_imputed
    validation FAILED (OOS R2=0.157) so imputed values must never reach the
    backtest panel.  `rpo_clean_share` is carried on each emitted growth row.
    """
    rpo = pd.read_parquet(RPO)
    rpo["filed_date"] = pd.to_datetime(rpo["filed_date"])
    rpo["date"] = pd.to_datetime(rpo["date"])
    rpo = rpo[rpo["rpo_is_imputed"] == False]  # noqa: E712 — RPO-clean
    out: List[Dict[str, Any]] = []
    for t, g in rpo.groupby("ticker"):
        g = g.dropna(subset=["rpo"]).sort_values("date").reset_index(drop=True)
        if len(g) < 2:
            continue
        g["_growth"] = g["rpo"].pct_change(4).fillna(
            g["rpo"].pct_change(1))
        for _, r in g.iterrows():
            v = r["_growth"]
            if np.isfinite(v):
                out.append({"date": r["filed_date"], "ticker": t,
                            "rpo_growth": float(v),
                            "rpo_is_imputed": bool(r["rpo_is_imputed"])})
    return pd.DataFrame(out)


# --------------------------------------------------------------------------
# Qual panel
# --------------------------------------------------------------------------

def build_qual_factors(tickers: List[str], start: str, end: str
                       ) -> pd.DataFrame:
    """
    Wide PIT monthly qual panel: rpo_growth + 2023-sample velocity z-scores.
    Velocity z = 0 (historical norm) on months without observations (neutral).
    mda_tone (2026) and app_velocity (2026) are OUT OF WINDOW -> excluded.
    """
    panels: List[pd.DataFrame] = []

    rg = compute_rpo_growth()
    rg_p = pit_expand_long(rg, tickers, start, end, "rpo_growth")
    if len(rg_p):
        panels.append(rg_p[["date", "ticker", "rpo_growth"]])

    velocity_map = {
        "gh_velocity": (GH, "gh_commits_90d"),
        "ats_velocity": (ATS, "ats_posted_90d"),
        "patent_accel": (PATENTS, "patent_citations_1y"),
        "cfpb_vel": (CFPB, "cfpb_complaints_90d"),
        "nhtsa_vel": (NHTSA, "nhtsa_complaints_90d"),
    }
    for col, (path, src_col) in velocity_map.items():
        if not path.exists():
            continue
        raw = pd.read_parquet(path)
        raw["date"] = pd.to_datetime(raw["date"])
        exp = pit_expand_long(raw[["date", "ticker", src_col]], tickers,
                              start, end, src_col).rename(
                                  columns={src_col: col})
        if len(exp):
            exp[col] = exp[col].fillna(0.0)  # neutral norm on gaps
            panels.append(exp[["date", "ticker", col]])

    if not panels:
        raise RuntimeError("no qual factor panels could be built")
    wide = panels[0]
    for p in panels[1:]:
        wide = wide.merge(p, on=["date", "ticker"], how="outer")
    wide = wide.fillna(0.0)
    return wide.sort_values(["date", "ticker"]).reset_index(drop=True)


# --------------------------------------------------------------------------
# Returns
# --------------------------------------------------------------------------

def build_returns_t(prices: Dict[str, pd.DataFrame], tickers: List[str],
                    start: str, end: str) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    for t in tickers:
        px = prices.get(t)
        if px is None:
            continue
        px = px[(px.index >= start) & (px.index <= end)]
        ret = px.pct_change().dropna()
        for d, v in ret.items():
            rows.append({"date": d, "ticker": t, "ret": float(v)})
    return pd.DataFrame(rows)


def build_spy(prices: Dict[str, pd.DataFrame], start: str, end: str
              ) -> pd.Series:
    px = prices["SPY"]
    px = px[(px.index >= start) & (px.index <= end)]
    return px.pct_change().dropna()


# --------------------------------------------------------------------------
# Runner + gates
# --------------------------------------------------------------------------

def run_window(window: str, top_n: int = TOP_N_DEFAULT) -> Dict[str, Any]:
    w = WINDOWS[window]
    start, end = w["start"], w["end"]

    prices = load_prices()
    fund = load_fundamentals()
    quant = compute_quant_factors(fund, prices)

    # Tradable universe: names with quant factors AND price coverage.
    # Full 27-name SEC PIT estate -> 9 without local price files -> 5 with
    # prices but no complete quant rows (ebit/tax_rate/shares co-observed)
    # -> 13 tradable.
    all_names = sorted(fund["ticker"].unique())
    no_price = sorted(t for t in all_names if t not in prices)
    tickers = sorted(t for t in quant["ticker"].unique() if t in prices)
    no_factor_rows = sorted(set(all_names) - set(no_price) - set(tickers))
    with_price = tickers
    print(f"[phase4] {window}: {len(with_price)}/{len(all_names)} PIT names tradable "
          f"({len(no_price)} no price, {len(no_factor_rows)} no factor rows excluded)")

    sponsor = load_13f_sponsor()
    quant = quant.merge(sponsor, on=["date", "ticker"], how="left")

    universe_T = pit_expand_quant(quant, with_price, start, end)
    qual_T = build_qual_factors(with_price, start, end)
    returns_T = build_returns_t(prices, with_price, start, end)
    spy = build_spy(prices, start, end)

    # coverage stats
    n_reb_months = universe_T["date"].nunique()
    n_qual = qual_T["date"].nunique() if len(qual_T) else 0

    runner = ThreeArmRunner(
        universe_T=universe_T,
        qual_factors_T=qual_T,
        returns_T=returns_T,
        top_n=top_n,
        composite_method="pca",
    )
    res = runner.run()
    res["spy"] = spy
    engine = GateEngine()
    gates = engine.run_all_gates(res)
    gate_dict = gates.to_dict()

    # Screen degeneracy check: pre-registered screen (ROIC>WACC, ICR>3,
    # FCF_Yield>5%) may admit a singleton qualified set -> Arm C collapses
    # onto Arm A.  Reported as a DATA finding, thresholds are NOT tuned.
    from ingestion.harness.three_arm_runner import _as_of_month_snapshots
    q_sizes: List[int] = []
    a_eq_c = 0
    n_targets = 0
    for as_of, _ in _as_of_month_snapshots(universe_T):
        snap = runner._merge_snapshot(as_of)
        if snap.empty:
            continue
        q = runner.screen_quant(snap)
        if q.empty:
            continue
        q_sizes.append(int(len(q)))
        n_targets += 1
        if len(q) > 1:
            wa = runner.arm_a_weights(snap)
            wc = runner.arm_c_weights(snap)
            a_eq_c += int(wa.equals(wc))
    screen_stats = {
        "n_qualified_months": len(q_sizes),
        "qualified_size_min": int(min(q_sizes)) if q_sizes else 0,
        "qualified_size_mean": round(float(np.mean(q_sizes)), 4) if q_sizes else 0.0,
        "qualified_size_max": int(max(q_sizes)) if q_sizes else 0,
        "n_months_qualified_gt1": int(sum(1 for s in q_sizes if s > 1)),
        "arm_a_equals_arm_c_when_gt1": a_eq_c,
    }
    screen_note = (
        "Screen degeneracy: the pre-registered quant screen "
        "(ROIC>WACC=0.10, ICR>3, FCF_Yield>5%) admits a singleton qualified "
        "set in most months on this 13-name real universe, so Arm C (Tier-2 "
        "qual tilt within the qualified set) collapses onto Arm A. Reported "
        "as-is per pre-registration; screen thresholds NOT tuned."
    )

    # arm summary
    summary: Dict[str, Any] = {}
    for arm in ("a", "b", "c"):
        r = res.get(f"arm_{arm}_returns")
        summary[f"arm_{arm}"] = {
            "n_returns": int(len(r)) if r is not None else 0,
            "ann_return": float(r.mean() * 252) if r is not None and len(r) else None,
            "ann_vol": float(r.std(ddof=1) * np.sqrt(252)) if r is not None and len(r) > 1 else None,
            "sharpe": float(r.mean() / r.std(ddof=1) * np.sqrt(252)) if r is not None and len(r) > 1 and r.std(ddof=1) > 0 else None,
            "n_rebalances": int(len(res[f"arm_{arm}_weights"])),
        }

    n_imputed_in_window = 0
    n_rpo_in_window = 0
    rpo = pd.read_parquet(RPO)
    rpo["date"] = pd.to_datetime(rpo["date"])
    in_win = rpo[(rpo["date"] >= start) & (rpo["date"] <= end)]
    n_rpo_in_window = int(len(in_win))
    n_imputed_in_window = int(in_win["rpo_is_imputed"].sum())

    artifact = {
        "run_name": f"phase4-3arm-{window}",
        "window": {"label": w["label"], "start": start, "end": end},
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "universe": {
            "n_all_pit": len(all_names),
            "n_tradable": len(with_price),
            "tickers": with_price,
            "excluded_no_price": no_price,
            "excluded_no_factor_rows": no_factor_rows,
            "quant_sources": ["SEC_EDGAR companyfacts -> pit_fundamentals.parquet"],
            "wacc_hurdle": WACC_CONSTANT,
            "icr_proxy": "ebit / (total_debt * 5%%)",
        },
        "qual_factors": {
            "columns_in_runner": [c for c in QUAL_FACTOR_COLS if c in qual_T.columns],
            "rpo_imputed_rows_in_window": n_imputed_in_window,
            "rpo_total_rows_in_window": n_rpo_in_window,
            "rpo_clean": True,
            "note": ("RPO growth uses CLEAN rows only (rpo_is_imputed=False); "
                     "velocity z=0 neutral on months without observations; "
                     "mda_tone (2026) and app_store (2026) out of window, excluded; "
                     "cfpb_vel/nhtsa_vel (2023 sample) cover cohort tickers "
                     "NOT in the tradable universe -> columns absent, no tilt contributed"),
            "alt_data_state": "2023 test/sample cohorts (GH/NHTSA/CFPB/ATS/patents); RPO clean 2018+",
        },
        "coverage": {
            "rebalance_months": int(n_reb_months),
            "qual_months": int(n_qual),
            "returns_rows": int(len(returns_T)),
            "spy_rows": int(len(spy)),
            "avg_universe_per_month": float(universe_T.groupby("date")["ticker"].count().mean()),
            "screen_stats": screen_stats,
        },
        "screen_note": screen_note,
        "schedule_note": "monthly rebalance day-1, PIT prior month-end data, 30% one-way turnover cap",
        "cost_note": "transaction costs (10bp/side) NOT modeled in returns; turnover gate 8 monitors drag",
        "oos_note": ("Gate 5 oos_sharpe scores the full in-window Arm C series "
                     "(no separate in-window OOS split; scored_oos=false). "
                     "Cross-window OOS robustness is provided by running the "
                     "SAME pre-registered windows (primary vs secondary)."),
        "rpo_validation_status": "FAIL (OOS R2=0.157, see data/validation/rpo_imputation_report.json) -> RPO factor is RPO-CLEAN: imputed rows dropped, no imputation reaches the panel",
        "gates_summary": {
            "passed": bool(gates.passed),
            "n_passed": int(gates.n_passed),
            "n_gates": int(gates.n_gates),
            "alpha_bonferroni": gates.alpha,
            "failed_gates": [name for name, d in gate_dict["gates"].items()
                             if not d.get("passed")],
        },
        "arm_summary": summary,
        "gates": gate_dict["gates"],
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    md = OUT_DIR / f"20260910-phase4-3arm-{window}.md"
    md.write_text(render_md(artifact), encoding="utf-8")
    jp = OUT_DIR / f"20260910-phase4-3arm-{window}-gates.json"
    jp.write_text(json.dumps(artifact, indent=2, sort_keys=True, default=str),
                  encoding="utf-8")

    print(f"[phase4] {window}: gates passed {gates.n_passed}/{gates.n_gates} "
          f"-> {'PASS' if gates.passed else 'FAIL'}")
    for name, d in gate_dict["gates"].items():
        mk = "PASS" if d.get("passed") else "FAIL"
        print(f"  [{mk}] {name:24s} value={d.get('value')} "
              f"threshold={d.get('threshold')}")
    print(f"[phase4] artifacts: {md} ; {jp}")
    return artifact


def render_md(artifact: Dict[str, Any]) -> str:
    lines = [f"# Backtest Run: {artifact['run_name']}"]
    w = artifact["window"]
    lines += ["## window", "`json", json.dumps(w), "`"]
    lines += ["## data_readiness", "`json",
              json.dumps({
                  "rpo_validation_status": artifact["rpo_validation_status"],
                  "qual_factor_note": artifact["qual_factors"]["note"],
                  "alt_data_state": artifact["qual_factors"]["alt_data_state"],
                  "cost_note": artifact["cost_note"],
              }, indent=2), "`"]
    lines += ["## universe", "`json",
              json.dumps(artifact["universe"], indent=2), "`"]
    lines += ["## coverage", "`json",
              json.dumps(artifact["coverage"], indent=2), "`"]
    lines += ["## gates_summary", "`json",
              json.dumps(artifact["gates_summary"], indent=2), "`"]
    lines += ["## arm_summary", "`json",
              json.dumps(artifact["arm_summary"], indent=2), "`"]
    lines += ["## gates", "`json",
              json.dumps(artifact["gates"], indent=2, default=str), "`"]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 4 dual-window 3-arm backtest")
    parser.add_argument("--window", choices=["primary", "secondary", "both"],
                        default="both")
    parser.add_argument("--top-n", type=int, default=TOP_N_DEFAULT)
    args = parser.parse_args()

    windows = ["primary", "secondary"] if args.window == "both" else [args.window]
    artifacts = []
    for w in windows:
        artifacts.append(run_window(w, top_n=args.top_n))

    index = OUT_DIR / "20260910-phase4-3arm-index.json"
    index.write_text(json.dumps(
        {"run": "phase4-3arm", "windows": windows,
         "artifacts": [a["run_name"] for a in artifacts],
         "gates_passed": {a["window"]["label"]: a["gates_summary"] for a in artifacts},
         "ran_at": datetime.now(timezone.utc).isoformat()},
        indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(f"[phase4] index: {index}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())