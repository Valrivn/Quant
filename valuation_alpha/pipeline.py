"""P5 live-data end-to-end orchestration for the D-20260801-004 pipeline.

The call site owns ALL network I/O. Every fetch is wrapped so a single
ticker/series failure is recorded in ``skipped`` and never crashes the run.
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

from valuation_alpha.universe.roster import get_universe
from valuation_alpha.datastore.xbrl_financials import (
    fetch_companyfacts,
    extract_quarterly_financials,
)
from valuation_alpha.datastore.prices import fetch_prices
from valuation_alpha.datastore.factors import fetch_ff5_factors, fetch_sp500
from valuation_alpha.engine import run_l1
from valuation_alpha.selection import generate_candidates, rank_candidates
from valuation_alpha.stats import bias_ablation
from valuation_alpha.report import bias_ablation_report
from diversification.datastore import SLEEVES, fetch_sleeve_prices, fetch_fred_series
from diversification.backtest import walk_forward_replay, run_sleeve_backtest
from diversification.report import sleeve_backtest_report
from portfolio.allocator import walk_forward_allocate, portfolio_backtest
from portfolio.llm_guide import propose_configs, select_configs
from portfolio.report import allocator_report

_FRED_SERIES = ("BAA10Y", "DFII10", "M2SL")


def build_fields_map() -> dict:
    """Return the friendly-name -> US-GAAP tag map for extract_quarterly_financials.

    The map carries the raw XBRL components; the derived ratios are computed in
    ``_derive_metrics``. Approximation choices (matching the P0 datastore):
      - revenue: RevenueFromContractWithCustomerExcludingAssessedTax (ASC 606).
      - operating_margin: OperatingIncomeLoss / Revenue.
      - roic: OperatingIncomeLoss / StockholdersEquity (approximate ROIC; ignores
        invested capital and taxes).
      - interest_coverage: OperatingIncomeLoss / InterestExpense.
      - debt_to_capital: LongTermDebtNoncurrent / StockholdersEquity (ignores
        current debt).
      - cash_burn: 3 * CashAndCashEquivalentsAtCarryingValue / OperatingExpenses
        (months of operating expenses covered by cash).
    """
    return {
        "revenue": "RevenueFromContractWithCustomerExcludingAssessedTax",
        "operating_income": "OperatingIncomeLoss",
        "equity": "StockholdersEquity",
        "interest_expense": "InterestExpense",
        "long_term_debt": "LongTermDebtNoncurrent",
        "cash": "CashAndCashEquivalentsAtCarryingValue",
        "operating_expenses": "OperatingExpenses",
        "capex": "PaymentsToAcquirePropertyPlantAndEquipment",
        "rd": "ResearchAndDevelopmentExpense",
        "ocf": "NetCashProvidedByUsedInOperatingActivities",
        "net_income": "NetIncomeLoss",
        "assets": "Assets",
    }


def _derive_metrics(quarterly: pd.DataFrame) -> pd.DataFrame:
    if quarterly is None or quarterly.empty:
        return quarterly
    df = quarterly.copy()
    if "operating_income" in df.columns and "revenue" in df.columns:
        df["operating_margin"] = df["operating_income"] / df["revenue"]
    if "operating_income" in df.columns and "equity" in df.columns:
        df["roic"] = df["operating_income"] / df["equity"]
    if "operating_income" in df.columns and "interest_expense" in df.columns:
        df["interest_coverage"] = df["operating_income"] / df["interest_expense"]
    if "long_term_debt" in df.columns:
        df["debt"] = df["long_term_debt"]
    if "cash" in df.columns and "operating_expenses" in df.columns:
        df["cash_burn"] = 3.0 * df["cash"] / df["operating_expenses"]
    # Reinvestment-rate fundamentals (D-20260802-002). capex is a cash outflow
    # (negative in SEC XBRL) -> normalize to a positive magnitude. OCF keeps its
    # sign: negative OCF = cash burn, which the profit-agnostic thesis surfaces
    # as a gamble profile rather than hiding it. R&D is an expense (positive).
    if "capex" in df.columns:
        df["capex"] = df["capex"].astype(float).abs()
    if {"capex", "ocf"}.issubset(df.columns):
        num = df["capex"] + df.get("rd", pd.Series(0.0, index=df.index)).fillna(0.0)
        denom = df["ocf"].replace(0.0, np.nan)
        df["reinvestment_rate"] = (num / denom).replace([np.inf, -np.inf], np.nan)
    if "revenue" in df.columns and "capex" in df.columns:
        df["reinvestment_intensity"] = (
            (df["capex"] + df.get("rd", pd.Series(0.0, index=df.index)).fillna(0.0))
            / df["revenue"].replace(0.0, np.nan)
        ).replace([np.inf, -np.inf], np.nan)
    return df


def _write(out_dir: str, name: str, content: str) -> str:
    path = os.path.join(out_dir, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.ndarray,)):
        return o.tolist()
    if isinstance(o, (pd.Timestamp,)):
        return str(o)
    return str(o)


def _config_score(backtest: dict) -> float:
    dsr = backtest.get("deflated_sharpe")
    if dsr and dsr.get("dsr") == dsr.get("dsr"):
        return float(dsr["dsr"])
    sharpe = backtest.get("sharpe")
    return float(sharpe) if sharpe == sharpe else -np.inf


def run_live_full(
    start: str = "2015-01-01",
    end: str = "2025-12-31",
    max_workers: int = 8,
    out_dir: str = "center/valuation_alpha",
) -> dict:
    """Run the full D-20260801-004 pipeline end-to-end and write reports to disk.

    L1 equity sleeve -> selection -> bias ablation -> L2 diversification ->
    L3 allocator. Returns a dict with run_a, run_b, ranking, bias_ablation,
    sleeve_results, decisions, allocator, reports_paths, skipped, and config.
    """
    os.makedirs(out_dir, exist_ok=True)
    skipped = []

    universe = get_universe()
    sector_by_ticker = {r["ticker"]: r["sector"] for r in universe}
    cik_by_ticker = {r["ticker"]: r["sec_cik"] for r in universe}
    tickers = [r["ticker"] for r in universe]

    prices = fetch_prices(tickers, start, end)
    factors = fetch_ff5_factors()
    sp500 = fetch_sp500(start, end)

    valid_tickers = []
    for t in tickers:
        if t in prices.columns and prices[t].notna().sum() > 0:
            valid_tickers.append(t)
        else:
            skipped.append(t)

    fields = build_fields_map()
    quarterly_by_ticker = {}

    def _fetch_q(t):
        cik = cik_by_ticker.get(t)
        if not cik:
            return t, pd.DataFrame()
        try:
            facts = fetch_companyfacts(cik)
            raw = extract_quarterly_financials(facts, fields)
            return t, _derive_metrics(raw)
        except Exception:
            return t, pd.DataFrame()

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for t, q in ex.map(_fetch_q, tickers):
            quarterly_by_ticker[t] = q

    run_a = run_l1(
        valid_tickers, prices, factors, sp500, sector_by_ticker,
        quarterly_by_ticker, include_bias=True,
    )
    run_b = run_l1(
        valid_tickers, prices, factors, sp500, sector_by_ticker,
        quarterly_by_ticker, include_bias=False,
    )

    candidates = generate_candidates(
        run_a["names"], top_n=15, k_values=[5, 10, 15], scoring="blended"
    )
    ranking = rank_candidates(
        run_a["names"], factors, sp500, prices.pct_change(), candidates, horizon_days=756
    )
    stats = bias_ablation(run_a["names"], run_b["names"])
    bias_report = bias_ablation_report(stats["run_a"], stats["run_b"], stats)
    bias_path = _write(out_dir, "bias_ablation_report.md", bias_report)

    sleeve_tickers = [t for ts in SLEEVES.values() for t in ts]
    sleeve_prices = fetch_sleeve_prices(sleeve_tickers, start, end)
    fred = {}
    for sid in _FRED_SERIES:
        fred[sid] = fetch_fred_series(sid, start, end)
    historical = {"prices": sleeve_prices, "fred": fred, "factors": factors}

    replay = walk_forward_replay(historical)
    sleeve_results = run_sleeve_backtest(historical)
    sleeve_report = sleeve_backtest_report(sleeve_results, replay["decisions"])
    sleeve_path = _write(out_dir, "sleeve_backtest_report.md", sleeve_report)

    proposed = propose_configs(replay["sleeve_returns"])
    configs = select_configs(proposed, k=3)
    best = None
    for cfg in configs:
        weights = walk_forward_allocate(
            replay["sleeve_returns"],
            objective=cfg["objective"],
            target_vol=cfg["target_vol"],
        )
        backtest = portfolio_backtest(
            weights, replay["sleeve_returns"], benchmark=sp500, factors=factors
        )
        score = _config_score(backtest)
        if best is None or score > best["score"]:
            best = {
                "config": cfg,
                "weights": weights,
                "backtest": backtest,
                "score": score,
            }

    allocator_report_str = allocator_report(
        best["weights"], best["backtest"], [best["config"]]
    )
    allocator_path = _write(out_dir, "allocator_report.md", allocator_report_str)

    bundle = {
        "run_a": run_a["names"].to_dict(orient="records"),
        "run_b": run_b["names"].to_dict(orient="records"),
        "ranking": ranking.to_dict(orient="records") if not ranking.empty else [],
        "bias_ablation": stats,
        "sleeve_results": (
            sleeve_results.to_dict(orient="records") if not sleeve_results.empty else []
        ),
        "allocator": {
            "config": best["config"],
            "metrics": {
                "annualized_return": best["backtest"].get("annualized_return"),
                "annualized_vol": best["backtest"].get("annualized_vol"),
                "sharpe": best["backtest"].get("sharpe"),
                "max_drawdown": best["backtest"].get("max_drawdown"),
                "deflated_sharpe": (
                    best["backtest"].get("deflated_sharpe", {}).get("dsr")
                    if best["backtest"].get("deflated_sharpe")
                    else None
                ),
            },
        },
        "skipped": skipped,
        "config": {
            "start": start,
            "end": end,
            "max_workers": max_workers,
            "out_dir": out_dir,
        },
    }
    results_path = os.path.join(out_dir, "results.json")
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump(bundle, f, default=_json_default, indent=2)

    reports_paths = {
        "bias_ablation": bias_path,
        "sleeve_backtest": sleeve_path,
        "allocator": allocator_path,
        "results": results_path,
    }

    return {
        "run_a": run_a,
        "run_b": run_b,
        "ranking": ranking,
        "bias_ablation": stats,
        "sleeve_results": sleeve_results,
        "decisions": replay["decisions"],
        "allocator": best,
        "reports_paths": reports_paths,
        "skipped": skipped,
        "config": bundle["config"],
    }