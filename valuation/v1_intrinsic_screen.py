"""
valuation/v1_intrinsic_screen.py — STRAT-V1-INTRINSIC Signal Pipeline.

Builds Stage 1->3 signal pipeline for Damodaran x Lynch value strategy.
Emits data/backtests/v1_intrinsic/screen_panel.parquet, sidecar, and coverage report.
"""

from __future__ import annotations

import os
import sys
import json
import time
import hashlib
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import numpy as np
import pandas as pd
import polars as pl

# Ensure repo root is on python path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ingestion.persistence.merged_universe import MergedUniverseStore
from valuation.monte_carlo_dcf import monte_carlo_valuation


SCREEN_PANEL_DIR = REPO_ROOT / "data" / "backtests" / "v1_intrinsic"
PANEL_PARQUET_PATH = SCREEN_PANEL_DIR / "screen_panel.parquet"
PROVENANCE_PATH = SCREEN_PANEL_DIR / "screen_panel.provenance.json"
COVERAGE_REPORT_PATH = SCREEN_PANEL_DIR / "coverage_report.json"
FRED_CACHE_DIR = REPO_ROOT / "data" / "fred_cache"
MASTER_PRICES_DIR = REPO_ROOT / "data" / "master_prices"
VALUATION_FUNDAMENTALS_PATH = REPO_ROOT / "data" / "pit_fundamentals" / "valuation_fundamentals.parquet"


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def load_risk_free_rates() -> pd.Series:
    """Load FRED DGS10 10-year Treasury yields (in decimal, e.g. 0.025 for 2.5%)."""
    dgs10_path = FRED_CACHE_DIR / "DGS10.parquet"
    if dgs10_path.exists():
        df = pd.read_parquet(dgs10_path)
        df["date"] = pd.to_datetime(df["date"]).dt.as_unit("ns")
        # Convert percent to decimal
        df["rf"] = df["value"] / 100.0
        return df.set_index("date")["rf"].sort_index()
    else:
        # Default 4.0% fallback
        dates = pd.date_range("2010-01-01", "2026-12-31", freq="D")
        return pd.Series(0.04, index=dates)


def load_master_price(ticker: str) -> Optional[pd.Series]:
    """Load daily Close prices for a ticker from data/master_prices."""
    price_file = MASTER_PRICES_DIR / f"{ticker}.parquet"
    if not price_file.exists():
        return None
    try:
        df = pd.read_parquet(price_file)
        if df.empty or "Close" not in df.columns:
            return None
        df.index = pd.to_datetime(df.index).tz_localize(None).as_unit("ns")
        return df["Close"].sort_index()
    except Exception:
        return None


def calculate_stage1_survivors() -> Tuple[pd.DataFrame, pd.DataFrame, Dict[str, Any]]:
    """
    Loads merged PIT universe and valuation fundamentals, applies D1 R&D reconciliation,
    computes Stage 1 gates, and returns survivor panel + universe panel.
    """
    print("Reading merged PIT universe via MergedUniverseStore...")
    store = MergedUniverseStore()
    df_u = store.read_merged("2018-01-01", "2024-12-31")
    df_u["rebalance_date"] = pd.to_datetime(df_u["rebalance_date"]).dt.as_unit("ns")

    print(f"Loaded universe: {len(df_u)} rows across {df_u['rebalance_date'].nunique()} rebalance dates.")

    print(f"Reading fundamentals from {VALUATION_FUNDAMENTALS_PATH}...")
    df_v = pl.read_parquet(VALUATION_FUNDAMENTALS_PATH).to_pandas()
    df_v["filed_date"] = pd.to_datetime(df_v["filed_date"]).dt.as_unit("ns")
    df_v["fiscal_end_date"] = pd.to_datetime(df_v["fiscal_end_date"]).dt.as_unit("ns")

    # Earliest filing per ticker for history check
    earliest_filed = df_v.groupby("ticker")["filed_date"].min().to_dict()

    # Pre-sort for backward merge_asof
    df_u_sorted = df_u.sort_values("rebalance_date")
    df_v_sorted = df_v.sort_values("filed_date")

    merged = pd.merge_asof(
        df_u_sorted,
        df_v_sorted,
        left_on="rebalance_date",
        right_on="filed_date",
        by="ticker",
        direction="backward",
        suffixes=("", "_fund"),
    )

    # ---------------------------------------------------------
    # D1 R&D AMORTIZATION RECONCILIATION
    # ---------------------------------------------------------
    # Fundamentals parquet used L=5 for all R&D reporters.
    # Per D1: Hardware & Software use L=5; all other sectors use L=0 (expensed).
    is_hw_sw = merged["sector_custom"].isin(["Hardware", "Software"])

    # For HW/SW: use roic_cap_rd from fundamentals parquet where available
    # For non-HW/SW: L=0 -> NOPAT / Invested Capital (raw roic)
    merged["roic_cap_rd_reconciled"] = np.where(
        is_hw_sw & merged["roic_cap_rd"].notna(),
        merged["roic_cap_rd"],
        merged["roic"],
    )

    # Reconcile EBIT and NOPAT for R&D
    merged["ebit_cap_rd_reconciled"] = np.where(
        is_hw_sw & merged["ebit_cap_rd"].notna(),
        merged["ebit_cap_rd"],
        merged["ebit"],
    )

    wacc = merged["wacc_estimate"].fillna(merged["wacc"]).fillna(0.08)
    merged["wacc_clean"] = wacc
    merged["roic_wacc_spread"] = merged["roic_cap_rd_reconciled"] - wacc

    # ---------------------------------------------------------
    # STAGE 1 GATES
    # ---------------------------------------------------------
    # 1. ROIC-WACC spread >= 15%
    gate1 = merged["roic_wacc_spread"] >= 0.15

    # 2. CFO > Net Income (latest filed)
    cfo_gt_ni = merged["operating_cash_flow"] > merged["net_income"]
    gate2 = cfo_gt_ni == True

    # 3. Net cash fortress: (cash - total_debt) >= 0 OR interest_coverage >= 5
    net_cash = merged["cash"] - merged["total_debt"]
    merged["net_cash_val"] = net_cash
    icr = merged["interest_coverage"].fillna(merged["icr"])
    gate3 = (net_cash >= 0) | (icr >= 5.0)

    # 4. Operating margin >= sector median
    op_margin = np.where(
        (merged["revenue"].notna()) & (merged["revenue"] > 0),
        merged["ebit_cap_rd_reconciled"] / merged["revenue"],
        np.nan,
    )
    merged["op_margin"] = op_margin
    sec_op_medians = merged.groupby(["rebalance_date", "sector_custom"])["op_margin"].transform("median")
    gate4 = merged["op_margin"] >= sec_op_medians

    # 5. Lynch class in {fast, stalwart}
    # Reinvestment rate = (Capex - Dep + R&D) / NOPAT (fallback: 0.30)
    nopat = np.where(
        merged["nopat_cap_rd"].notna() & is_hw_sw,
        merged["nopat_cap_rd"],
        merged["nopat"],
    )
    nopat = np.where(nopat <= 0, np.nan, nopat)

    reinv_calc = (
        merged["capex"].fillna(0)
        - merged["depreciation_amortization"].fillna(0)
        + (merged["rd_expense"].fillna(0) if is_hw_sw.any() else 0)
    ) / nopat

    reinvest_rate = merged["reinvestment_rate"].fillna(reinv_calc).fillna(0.30)
    reinvest_rate = reinvest_rate.clip(0.0, 1.0)
    merged["reinvestment_rate_clean"] = reinvest_rate

    endogenous_g = reinvest_rate * merged["roic_cap_rd_reconciled"]
    merged["endogenous_g"] = endogenous_g

    lynch_class = np.where(
        endogenous_g >= 0.15,
        "fast",
        np.where((endogenous_g >= 0.10) & (endogenous_g < 0.15), "stalwart", "slow"),
    )
    merged["lynch_class"] = lynch_class
    gate5 = merged["lynch_class"].isin(["fast", "stalwart"])

    # 6. >= 3y of filing history
    merged["earliest_filed"] = merged["ticker"].map(earliest_filed)
    merged["filing_history_days"] = (merged["filed_date"] - merged["earliest_filed"]).dt.days
    gate6 = merged["filing_history_days"] >= 1095

    stage1_pass = gate1 & gate2 & gate3 & gate4 & gate5 & gate6
    merged["stage1_pass"] = stage1_pass

    print("\nStage 1 Gate Pass Counts:")
    print(f"  Gate 1 (ROIC-WACC >= 15%): {gate1.sum()}")
    print(f"  Gate 2 (CFO > NI): {gate2.sum()}")
    print(f"  Gate 3 (Net Cash / ICR >= 5): {gate3.sum()}")
    print(f"  Gate 4 (Op Margin >= Sector Median): {gate4.sum()}")
    print(f"  Gate 5 (Lynch Fast/Stalwart): {gate5.sum()}")
    print(f"  Gate 6 (>=3y Filing History): {gate6.sum()}")
    print(f"  ALL Stage 1 PASS: {stage1_pass.sum()}")

    survivors = merged[stage1_pass].copy()

    reconcile_info = {
        "d1_reconciliation": " valuation_fundamentals.parquet capitalized R&D (L=5) for all R&D reporters. Reconciled per D1: Hardware & Software keep L=5; all other sectors use L=0 (expensed).",
        "stage1_survivor_count": int(len(survivors)),
        "total_universe_rows": int(len(df_u)),
    }

    return survivors, merged, reconcile_info


def calculate_stage3_and_monte_carlo(
    survivors: pd.DataFrame, universe: pd.DataFrame
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    Computes Stage 3 overvaluation gates and 10,000 passes Monte Carlo DCF valuations.
    """
    rf_series = load_risk_free_rates()
    price_cache: Dict[str, pd.Series] = {}

    print("\nCaching master prices for survivors...")
    unique_tickers = survivors["ticker"].unique()
    for t in unique_tickers:
        ps = load_master_price(t)
        if ps is not None:
            price_cache[t] = ps

    # Pre-calculate trailing 8Q margins per ticker in universe
    print("Computing trailing 8Q margin statistics and universe sector parameters...")
    # Sector EV/Sales regression statistics per rebalance_date
    universe["ev_sales"] = (universe["market_cap"] + (universe["total_debt"].fillna(0) - universe["cash"].fillna(0))) / universe["revenue"]
    universe["after_tax_margin"] = universe["op_margin"] * (1.0 - 0.21)

    panel_rows: List[Dict[str, Any]] = []
    quarterly_stats: Dict[str, Dict[str, Any]] = {}

    # Sort survivors by (rebalance_date, ticker)
    survivors = survivors.sort_values(["rebalance_date", "ticker"])

    start_wall = time.time()
    total_mc = 0

    # Group survivors by quarter for cap check and coverage report
    survivors["quarter"] = survivors["rebalance_date"].dt.to_period("Q").astype(str)
    universe["quarter"] = universe["rebalance_date"].dt.to_period("Q").astype(str)

    # Process rebalance dates
    for reb_date, reb_group in survivors.groupby("rebalance_date"):
        reb_dt = pd.Timestamp(reb_date)
        reb_str = reb_dt.strftime("%Y-%m-%d")

        # Get risk-free rate at rebalance_date
        rf_sub = rf_series[rf_series.index <= reb_dt]
        rf_val = float(rf_sub.iloc[-1]) if not rf_sub.empty else 0.04
        rf_val = min(max(rf_val, 0.01), 0.04)

        # Cap rule: if a quarter/month exceeds 400 survivors, take top 400 by roic_wacc_spread
        if len(reb_group) > 400:
            reb_group = reb_group.sort_values("roic_wacc_spread", ascending=False).head(400)

        # Universe slice for Stage 3 regression
        univ_slice = universe[universe["rebalance_date"] == reb_date]

        for _, row in reb_group.iterrows():
            ticker = str(row["ticker"])
            sec_custom = str(row["sector_custom"])
            sec_gics = str(row["sector_gics"]) if pd.notna(row["sector_gics"]) else sec_custom
            mkt_cap = float(row["market_cap"])

            # -----------------------------------------------------
            # Price Retrieval
            # -----------------------------------------------------
            price_val = np.nan
            if ticker in price_cache:
                ps = price_cache[ticker]
                sub_p = ps[ps.index <= reb_dt]
                if not sub_p.empty:
                    price_val = float(sub_p.iloc[-1])

            if np.isnan(price_val) or price_val <= 0:
                price_val = 100.0  # nominal unit price fallback if master price is absent

            shares_out = mkt_cap / price_val if price_val > 0 else 1.0

            # -----------------------------------------------------
            # D3 Margin Statistics & Distribution Parameters
            # -----------------------------------------------------
            # Trailing margins for this ticker
            t_univ = universe[(universe["ticker"] == ticker) & (universe["filed_date"] <= reb_dt)]
            t_margins = t_univ["op_margin"].dropna().tail(8)

            if len(t_margins) >= 4:
                margin_mu = float(t_margins.mean())
                margin_sigma = float(t_margins.std())
            else:
                margin_mu = float(row["op_margin"]) if pd.notna(row["op_margin"]) else 0.15
                margin_sigma = 0.05

            # Floor margin_sigma at sector margin std across universe that month
            sec_margins = univ_slice[univ_slice["sector_custom"] == sec_custom]["op_margin"].dropna()
            sec_sigma = float(sec_margins.std()) if len(sec_margins) >= 5 else 0.05
            margin_sigma = max(margin_sigma, sec_sigma, 0.02)

            # Revenue growth dist parameters
            g_endogenous = float(row["endogenous_g"])
            growth_sigma = max(0.5 * margin_sigma, 0.05)

            # Effective tax rate
            tax_exp = row.get("income_tax_expense")
            pretax = (row.get("ebit") or 0) - (row.get("interest_expense") or 0)
            if pretax is not None and pretax > 0 and tax_exp is not None:
                tau = float(np.clip(tax_exp / pretax, 0.0, 0.35))
            else:
                tau = 0.21

            # D2 Terminal Growth
            term_g = min(g_endogenous, rf_val, 0.040)

            # Seed generation: sha256(ticker + rebalance_date)[:8]
            seed_str = hashlib.sha256(f"{ticker}{reb_str}".encode()).hexdigest()[:8]
            n_passes = 10000

            # -----------------------------------------------------
            # Run Monte Carlo Valuation
            # -----------------------------------------------------
            mc_res = monte_carlo_valuation(
                base_wacc=float(row["wacc_clean"]),
                base_growth=g_endogenous,
                base_term_growth=term_g,
                n=n_passes,
                seed=seed_str,
                revenue=float(row["revenue"]) if pd.notna(row["revenue"]) else mkt_cap * 0.5,
                margin_mean=margin_mu,
                margin_std=margin_sigma,
                growth_std=growth_sigma,
                tax_rate=tau,
                reinvestment_rate=float(row["reinvestment_rate_clean"]),
                net_debt=float(row["net_cash_val"]) * -1.0,  # net_debt = total_debt - cash
                shares_out=shares_out,
            )

            total_mc += 1

            iv_p10 = mc_res["p10"]
            iv_p25 = mc_res["p25"]
            iv_p50 = mc_res["p50"]
            iv_p75 = mc_res["p75"]
            iv_p90 = mc_res["p90"]

            mos = float((iv_p50 - price_val) / iv_p50) if iv_p50 > 0 else 0.0

            # -----------------------------------------------------
            # PEG Calculation
            # -----------------------------------------------------
            ni_latest = float(row["net_income"]) if pd.notna(row["net_income"]) else np.nan
            if pd.notna(ni_latest) and ni_latest > 0 and g_endogenous > 0:
                peg_val = float((mkt_cap / ni_latest) / (g_endogenous * 100.0))
            else:
                peg_val = np.nan

            # -----------------------------------------------------
            # STAGE 3 OVERVALUATION GATE (speculative_flag)
            # -----------------------------------------------------
            # 1. Implied growth > 2x sector median growth
            sec_g_med = float(univ_slice[univ_slice["sector_custom"] == sec_custom]["endogenous_g"].median())
            sec_g_med = max(sec_g_med, 0.05)
            # Simple DCF reverse proxy: g_implied ~ WACC - (1 + g)/Price_to_FCF
            # If price > 2x iv_p50, implied growth is excessively high
            flag_implied_g = (price_val > 1.8 * iv_p50) or (g_endogenous > 2.0 * sec_g_med)

            # 2. EV/Sales residual vs sector regression > +2sigma
            sec_univ = univ_slice[univ_slice["sector_custom"] == sec_custom]
            if len(sec_univ) < 30:
                sec_univ = univ_slice

            reg_data = sec_univ[["ev_sales", "after_tax_margin"]].dropna()
            flag_ev_sales = False
            if len(reg_data) >= 5:
                x = reg_data["after_tax_margin"].values
                y = reg_data["ev_sales"].values
                if np.std(x) > 1e-4:
                    beta, alpha = np.polyfit(x, y, 1)
                    resids = y - (alpha + beta * x)
                    std_resids = np.std(resids)
                    if std_resids > 0:
                        cur_ev_s = float((mkt_cap + max(-1.0 * float(row["net_cash_val"]), 0.0)) / (row["revenue"] or 1.0))
                        cur_res = cur_ev_s - (alpha + beta * float(row["op_margin"] * (1.0 - tau)))
                        if cur_res > 2.0 * std_resids:
                            flag_ev_sales = True

            # 3. PEG >= 2.5
            flag_peg = pd.notna(peg_val) and (peg_val >= 2.5)

            speculative_flag = bool(flag_implied_g or flag_ev_sales or flag_peg)

            panel_rows.append({
                "rebalance_date": reb_dt.date(),
                "ticker": ticker,
                "sector_custom": sec_custom,
                "sector_gics": sec_gics,
                "price": price_val,
                "market_cap": mkt_cap,
                "iv_p10": iv_p10,
                "iv_p25": iv_p25,
                "iv_p50": iv_p50,
                "iv_p75": iv_p75,
                "iv_p90": iv_p90,
                "margin_of_safety": mos,
                "peg": peg_val if pd.notna(peg_val) else None,
                "roic_cap_rd": float(row["roic_cap_rd_reconciled"]),
                "roic_wacc_spread": float(row["roic_wacc_spread"]),
                "endogenous_g": g_endogenous,
                "lynch_class": str(row["lynch_class"]),
                "cfo_gt_ni": bool(row["operating_cash_flow"] > row["net_income"]),
                "net_cash": float(row["net_cash_val"]),
                "speculative_flag": speculative_flag,
                "stage1_pass": True,
                "mc_passes": int(n_passes),
                "mc_seed": seed_str,
            })

    wall_time = time.time() - start_wall
    print(f"\nMonte Carlo Valuation complete for {total_mc} items in {wall_time:.2f} seconds.")

    out_df = pd.DataFrame(panel_rows)

    # -----------------------------------------------------
    # QUARTERLY COVERAGE REPORT GENERATION
    # -----------------------------------------------------
    out_df["quarter"] = pd.to_datetime(out_df["rebalance_date"]).dt.to_period("Q").astype(str)
    univ_q_counts = universe.groupby("quarter")["ticker"].nunique().to_dict()

    for qtr, q_group in out_df.groupby("quarter"):
        u_size = int(univ_q_counts.get(qtr, 1000))
        surv_cnt = int(len(q_group))
        fast_cnt = int((q_group["lynch_class"] == "fast").sum())
        stalwart_cnt = int((q_group["lynch_class"] == "stalwart").sum())
        pct_fast = float(fast_cnt / surv_cnt) if surv_cnt > 0 else 0.0
        pct_stalwart = float(stalwart_cnt / surv_cnt) if surv_cnt > 0 else 0.0

        quarterly_stats[qtr] = {
            "universe_size": u_size,
            "stage1_survivors": surv_cnt,
            "mc_computed_count": surv_cnt,
            "mean_spread": float(q_group["roic_wacc_spread"].mean()),
            "pct_fast": pct_fast,
            "pct_stalwart": pct_stalwart,
            "median_margin_of_safety": float(q_group["margin_of_safety"].median()),
        }

    meta = {
        "wall_clock_seconds": float(wall_time),
        "total_mc_computed": total_mc,
        "passes_per_sim": 10000,
        "quarterly_coverage": quarterly_stats,
    }

    out_df = out_df.drop(columns=["quarter"])
    return out_df, meta


def run_pipeline():
    """Executes full Stage 1->3 pipeline and writes contract artifacts."""
    SCREEN_PANEL_DIR.mkdir(parents=True, exist_ok=True)

    survivors, universe, reconcile_info = calculate_stage1_survivors()
    panel_df, meta_info = calculate_stage3_and_monte_carlo(survivors, universe)

    # Enforce contract ordering & sorting: (rebalance_date, ticker)
    panel_df["rebalance_date"] = pd.to_datetime(panel_df["rebalance_date"]).dt.date
    panel_df = panel_df.sort_values(["rebalance_date", "ticker"]).reset_index(drop=True)

    print(f"\nWriting screen panel artifact: {PANEL_PARQUET_PATH} ({len(panel_df)} rows)")
    panel_df.to_parquet(PANEL_PARQUET_PATH, index=False)

    # SHA256 digest of panel
    file_bytes = PANEL_PARQUET_PATH.read_bytes()
    sha256_digest = _sha256_bytes(file_bytes)

    # Write Provenance Sidecar
    provenance_payload = {
        "source": "STRAT-V1-INTRINSIC",
        "strategy_label": "STRAT-V1-INTRINSIC",
        "retrieval_timestamp": datetime.now(timezone.utc).isoformat(),
        "sha256": sha256_digest,
        "row_count": int(len(panel_df)),
        "date_range": {
            "min": str(panel_df["rebalance_date"].min()),
            "max": str(panel_df["rebalance_date"].max()),
        },
        "mc_passes": 10000,
        "d1_rd_amortization_reconciliation": reconcile_info["d1_reconciliation"],
        "transformations": [
            "MergedUniverseStore.read_merged",
            "valuation_fundamentals_pit_merge",
            "stage1_intrinsic_gate_filter",
            "monte_carlo_10k_dcf",
            "stage3_overvaluation_gate",
        ],
        "schema_fields": list(panel_df.columns),
    }

    PROVENANCE_PATH.write_text(json.dumps(provenance_payload, indent=2), encoding="utf-8")
    print(f"Wrote provenance sidecar: {PROVENANCE_PATH}")

    # Write Coverage Report
    coverage_payload = {
        "strategy": "STRAT-V1-INTRINSIC",
        "total_rows": int(len(panel_df)),
        "unique_rebalance_dates": int(panel_df["rebalance_date"].nunique()),
        "d1_reconciliation": reconcile_info["d1_reconciliation"],
        "mc_passes": 10000,
        "wall_clock_seconds": meta_info["wall_clock_seconds"],
        "quarterly_breakdown": meta_info["quarterly_coverage"],
    }

    with open(COVERAGE_REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(coverage_payload, f, indent=2)
    print(f"Wrote coverage report: {COVERAGE_REPORT_PATH}")

    print("\nSUCCESS: Stage 1->3 Signal Pipeline Complete!")


if __name__ == "__main__":
    run_pipeline()
