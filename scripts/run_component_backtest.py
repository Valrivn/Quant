#!/usr/bin/env python
"""
Component Validation Backtest - Tests each pipeline component per stratum.
Simpler than full backtest loop, validates component APIs work correctly.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from config.logging_config import college_event, init_logging

# Pipeline stages
from Quantitative.dcf_screen import DCFOutputs, MASTER_TICKERS
from Quantitative.entry_timing import run_entry_timing, get_real_c_t_signals
from Quantitative.exit_engine import run_exit_engine
from Quantitative.sizing_rebalancing import run_sizing_rebalancing, generate_orders_from_plan
from Quantitative.fallback_engine import run_fallback_engine, get_fallback_weights_for_regime
from Quantitative.black_swan import run_black_swan_batch, load_tier1_tracker
from Quantitative.hmm_regime import RegimeDetector, RegimeState, build_macro_features

init_logging(level="INFO", inject_experiment=True)
logger = logging.getLogger(__name__)

OUTPUT_DIR = Path("data/checkpoints/backtest/component")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

STRATA = {
    "S4": {"start": "1999-03-01", "end": "2003-09-30", "name": "Dot-com Boom->Bust"},
    "S5": {"start": "2008-01-01", "end": "2009-06-30", "name": "GFC Bear"},
    "S6": {"start": "2010-01-01", "end": "2018-12-31", "name": "Neutral/Bull Mix"},
    "S7": {"start": "2019-01-01", "end": "2020-02-29", "name": "Pre-COVID Bull"},
    "S8": {"start": "2020-03-01", "end": "2020-05-31", "name": "COVID Crash"},
    "S9": {"start": "2020-06-01", "end": "2021-12-31", "name": "Meme Bull"},
    "S10": {"start": "2022-01-01", "end": "2022-12-31", "name": "Rate-Hike Bear"},
    "S11": {"start": "2023-01-01", "end": "2026-07-31", "name": "AI Boom Bull"},
}


def load_price_data(tickers: List[str], start_date: str, end_date: str) -> Dict[str, pd.Series]:
    """Load price data with timezone handling."""
    from pathlib import Path
    price_data = {}
    start = pd.Timestamp(start_date, tz='UTC')
    end = pd.Timestamp(end_date, tz='UTC')
    
    for ticker in tickers:
        path = Path(f"data/source/yfinance/{ticker}/prices.parquet")
        if path.exists():
            try:
                df = pd.read_parquet(path)
                df.index = pd.to_datetime(df.index)
                if df.index.tz is not None:
                    df.index = df.index.tz_convert('UTC')
                else:
                    df.index = df.index.tz_localize('UTC')
                df = df[(df.index >= start) & (df.index <= end)]
                if not df.empty:
                    price_data[ticker] = df["Close"]
            except Exception as e:
                logger.warning(f"Price load failed for {ticker}: {e}")
    return price_data


def create_synthetic_dcf(tickers: List[str], prices: Dict[str, float]) -> Dict[str, DCFOutputs]:
    """Create synthetic DCF outputs using price-based proxies."""
    results = {}
    for t in tickers:
        price = prices.get(t, 100.0)
        results[t] = DCFOutputs(
            ticker=t,
            iv_mean=price * 1.1,
            iv_p25=price * 0.9,
            iv_p10=price * 0.75,
            iv_p50=price * 1.1,
            iv_p75=price * 1.2,
            iv_p90=price * 1.3,
            iv_std=price * 0.15,
            entry_price=price * 0.9,
            crisis_entry_price=price * 0.75,
            discount_sector=0.10,
            roic=0.15,
            solvency_ratio=5.0,
            margin_health=0.25,
            rd_capitalized=0.0,
            current_price=price,
            passes_screen=True,
            degraded=False,
            warnings=["SYNTHETIC_IV_PROXY"],
        )
    return results


def test_stratum_components(stratum: str, start_date: str, end_date: str) -> Dict[str, Any]:
    logger.info(f"Testing components for {stratum}: {start_date} to {end_date}")
    
    # Load price data
    price_data = load_price_data(MASTER_TICKERS, start_date, end_date)
    if not price_data:
        return {"stratum": stratum, "error": "No price data"}
    
    # Get latest date with data
    latest_date = max(df.index[-1] for df in price_data.values())
    current_prices = {t: price_data[t].loc[latest_date] for t in price_data if latest_date in price_data[t].index}
    if not current_prices:
        return {"stratum": stratum, "error": "No current prices"}
    
    results = {"stratum": stratum, "start": start_date, "end": end_date, "components": {}}
    
    # 1. HMM Regime Detection
    try:
        macro_features = build_macro_features()
        if macro_features is not None:
            start = pd.Timestamp(start_date, tz='UTC')
            end = pd.Timestamp(end_date, tz='UTC')
            hist_features = macro_features[(macro_features.index >= start) & (macro_features.index <= end)]
            
            if len(hist_features) > 250:
                detector = RegimeDetector()
                detector.retrain(hist_features)
                regime, prob = detector.decode(hist_features)
                crisis_prob = prob.get(2, 0.0) if isinstance(prob, dict) else (prob[2] if len(prob) > 2 else 0.0)
                results["components"]["hmm"] = {
                    "status": "OK",
                    "regime": regime.value if hasattr(regime, 'value') else str(regime),
                    "crisis_prob": crisis_prob,
                    "n_obs": len(hist_features)
                }
            else:
                results["components"]["hmm"] = {"status": "INSUFFICIENT_DATA", "n_obs": len(hist_features)}
        else:
            results["components"]["hmm"] = {"status": "NO_MACRO_FEATURES"}
    except Exception as e:
        results["components"]["hmm"] = {"status": "ERROR", "error": str(e)}
    
    # 2. C_t Consumer Signals
    try:
        c_t = get_real_c_t_signals(list(current_prices.keys()), lookback_days=63)
        results["components"]["c_t"] = {"status": "OK", "tickers": len(c_t)}
    except Exception as e:
        results["components"]["c_t"] = {"status": "ERROR", "error": str(e)}
    
    # 3. DCF Screen (synthetic)
    try:
        dcf_results = create_synthetic_dcf(list(current_prices.keys()), current_prices)
        dcf_passed = [t for t, o in dcf_results.items() if o.passes_screen]
        results["components"]["dcf"] = {"status": "OK", "passed": len(dcf_passed)}
    except Exception as e:
        results["components"]["dcf"] = {"status": "ERROR", "error": str(e)}
    
    # 4. Entry Timing
    try:
        entry = run_entry_timing(list(current_prices.keys()), current_prices)
        entry_triggered = [t for t, s in entry.items() if s.triggered]
        results["components"]["entry"] = {"status": "OK", "triggered": len(entry_triggered)}
    except Exception as e:
        results["components"]["entry"] = {"status": "ERROR", "error": str(e)}
    
    # 5. Exit Engine
    try:
        exit_sigs = run_exit_engine(list(current_prices.keys()), current_prices, dcf_results)
        exit_triggered = [t for t, s in exit_sigs.items() if s.triggered]
        results["components"]["exit"] = {"status": "OK", "triggered": len(exit_triggered)}
    except Exception as e:
        results["components"]["exit"] = {"status": "ERROR", "error": str(e)}
    
    # 6. Black Swan
    try:
        price_series = {t: price_data[t] for t in current_prices.keys() if t in price_data}
        tier1_tracker = load_tier1_tracker()
        bs = run_black_swan_batch(list(current_prices.keys()), price_series, current_prices, tier1_tracker)
        bs_tier1 = [t for t, d in bs.items() if d.tier.value == "TIER1_PROVISIONAL"]
        bs_tier2 = [t for t, d in bs.items() if d.tier.value == "TIER2_STRUCTURAL"]
        results["components"]["black_swan"] = {"status": "OK", "tier1": len(bs_tier1), "tier2": len(bs_tier2)}
    except Exception as e:
        results["components"]["black_swan"] = {"status": "ERROR", "error": str(e)}
    
    # 7. Fallback Engine
    try:
        regime_str = results["components"]["hmm"].get("regime", "EXPANSION") if results["components"]["hmm"].get("status") == "OK" else "EXPANSION"
        crisis_prob = results["components"]["hmm"].get("crisis_prob", 0.0) if results["components"]["hmm"].get("status") == "OK" else 0.0
        fallback = get_fallback_weights_for_regime(regime_str, crisis_prob)
        results["components"]["fallback"] = {"status": "OK", "weights": fallback}
    except Exception as e:
        results["components"]["fallback"] = {"status": "ERROR", "error": str(e)}
    
    # 8. Sizing & Rebalancing
    try:
        portfolio_state = {
            'cash': 1_000_000,
            'positions': {},
            'position_values': {},
            'total_value': 1_000_000,
        }
        sizing = run_sizing_rebalancing(
            portfolio_state=portfolio_state,
            dcf_results={t: o.to_dict() for t, o in dcf_results.items()},
            entry_signals={t: s.to_dict() for t, s in entry.items()},
            exit_signals={t: s.to_dict() for t, s in exit_sigs.items()},
            black_swan_decisions={t: d.to_dict() for t, d in bs.items()},
            fallback_weights=fallback,
            regime=regime_str,
            hmm_crisis_prob=crisis_prob,
            current_prices=current_prices,
        )
        orders = generate_orders_from_plan(sizing, current_prices)
        buy_orders = [o for o in orders if o.get('side') == 'BUY']
        sell_orders = [o for o in orders if o.get('side') == 'SELL']
        results["components"]["sizing"] = {"status": "OK", "buy_orders": len(buy_orders), "sell_orders": len(sell_orders)}
    except Exception as e:
        results["components"]["sizing"] = {"status": "ERROR", "error": str(e)}
    
    return results


def main():
    logger.info("=" * 60)
    logger.info("COMPONENT VALIDATION BACKTEST")
    logger.info("=" * 60)
    
    college_event("component_backtest_start", strata=list(STRATA.keys()))
    
    all_results = {}
    
    for stratum, s_info in STRATA.items():
        try:
            result = test_stratum_components(stratum, s_info["start"], s_info["end"])
            all_results[stratum] = result
            
            comp_status = {k: v.get("status", "UNKNOWN") for k, v in result.get("components", {}).items()}
            logger.info(f"{stratum}: {comp_status}")
        except Exception as e:
            logger.error(f"{stratum} failed: {e}")
            all_results[stratum] = {"error": str(e)}
    
    # Summary
    logger.info("=" * 60)
    logger.info("COMPONENT VALIDATION SUMMARY")
    logger.info("=" * 60)
    
    for comp in ["hmm", "c_t", "dcf", "entry", "exit", "black_swan", "fallback", "sizing"]:
        ok_count = sum(1 for r in all_results.values() if r.get("components", {}).get(comp, {}).get("status") == "OK")
        total = len(all_results)
        logger.info(f"  {comp}: {ok_count}/{total} strata OK")
    
    # Save report
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": "component_validation",
        "strata": all_results,
    }
    
    report_path = OUTPUT_DIR / "component_validation_report.json"
    with report_path.open("w") as f:
        json.dump(report, f, indent=2, default=str)
    
    logger.info(f"Report saved to {report_path}")
    college_event("component_backtest_complete", report_path=str(report_path))
    
    return report


if __name__ == "__main__":
    from datetime import datetime, timezone
    main()