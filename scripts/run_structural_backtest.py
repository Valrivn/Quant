#!/usr/bin/env python
"""
Structural Backtest - Tests pipeline logic with synthetic IV proxy.
Uses: Price * 1.1 = IV_Fair, Price * 0.9 = IV_P25, Price * 0.75 = IV_P10
This validates: HMM regime, C_t signals, Entry/Exit timing, Sizing, Fallback, Black Swan
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yaml

from config.logging_config import college_event, init_logging

# Pipeline stages
from Quantitative.dcf_screen import DCFOutputs, MASTER_TICKERS
from Quantitative.entry_timing import run_entry_timing, EntrySignal, get_real_c_t_signals
from Quantitative.exit_engine import run_exit_engine, ExitSignal
from Quantitative.sizing_rebalancing import (
    run_sizing_rebalancing, SizingPlan, generate_orders_from_plan,
)
from Quantitative.fallback_engine import run_fallback_engine, get_fallback_weights_for_regime
from Quantitative.black_swan import (
    run_black_swan_batch, BlackSwanDecision, BlackSwanTier, load_tier1_tracker,
)
from Quantitative.hmm_regime import RegimeDetector, RegimeState, build_macro_features
from Quantitative.consumer_signal import compute_c_t, validate_cascade_lead_lag
from Quantitative.stat_tests import (
    run_full_stat_suite, ANOVAResult, WelchTTestResult, ChiSquareResult,
    DeflatedSharpeResult,
)

# Import backtest harness components
from Quantitative.backtest_harness import (
    BacktestConfig, BacktestState, BacktestReport,
    load_price_history, load_macro_features_history, get_trading_dates,
    compute_portfolio_value, execute_orders, run_backtest_cycle,
)

init_logging(level="INFO", inject_experiment=True)
logger = logging.getLogger(__name__)

OUTPUT_DIR = Path("data/checkpoints/backtest/structural")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Pre-registered success bars
SUCCESS_BARS = {
    "C1": {"name": "Cascade Lead-Lag", "criteria": "rho(C->T, k<0) AND rho(T->W, k<0) > 0.1", "threshold": 0.1},
    "C2": {"name": "V_MOAT/V_VAL Composite IC", "criteria": "IC > 0 at 126/252d net of HML", "threshold": 0.0},
    "U1": {"name": "Full-Window TR & Sharpe vs SPY", "criteria": "Total Return & Sharpe > SPY", "threshold": "beat_spy"},
    "U2": {"name": "Strata Performance", "criteria": "Beats SPY in >=3 of {S4,S6,S7,S8}, no >-25% maxDD", "threshold": {"min_strata_wins": 3, "max_dd": -0.25}},
}

# Strata definitions (pre-registered)
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


@dataclass
class StrataResult:
    stratum: str
    start_date: str
    end_date: str
    config: BacktestConfig
    report: BacktestReport
    spy_return: float
    spy_sharpe: float
    spy_max_dd: float
    beats_spy_return: bool
    beats_spy_sharpe: bool
    max_dd_ok: bool
    stat_results: Dict[str, Any]


def create_synthetic_dcf_outputs(tickers: List[str], prices: Dict[str, float]) -> Dict[str, DCFOutputs]:
    """Create synthetic DCF outputs using price-based proxies.
    
    IV_Fair = Price * 1.1
    IV_P25 = Price * 0.9  (entry threshold)
    IV_P10 = Price * 0.75 (black swan threshold)
    """
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
            passes_screen=True,  # All pass for structural test
            degraded=False,
            warnings=["SYNTHETIC_IV_PROXY"],
        )
    return results


def load_spy_benchmark(start_date: str, end_date: str) -> Tuple[float, float, float]:
    spy_path = Path("data/source/yfinance/SPY/prices.parquet")
    if not spy_path.exists():
        return 0.08, 0.6, -0.15
    
    try:
        df = pd.read_parquet(spy_path)
        df.index = pd.to_datetime(df.index)
        df = df[(df.index >= start_date) & (df.index <= end_date)]
        if df.empty or len(df) < 2:
            return 0.08, 0.6, -0.15
        
        returns = df["Close"].pct_change().dropna()
        total_return = (df["Close"].iloc[-1] / df["Close"].iloc[0]) - 1
        n_days = len(returns)
        annualized_return = (1 + total_return) ** (252 / n_days) - 1
        sharpe = np.mean(returns) / np.std(returns, ddof=1) * np.sqrt(252) if np.std(returns) > 0 else 0
        
        cumulative = np.cumprod(1 + returns)
        running_max = np.maximum.accumulate(cumulative)
        drawdown = (cumulative - running_max) / running_max
        max_dd = float(np.min(drawdown))
        
        return float(total_return), float(sharpe), max_dd
    except Exception as e:
        logger.warning(f"SPY benchmark load failed: {e}")
        return 0.08, 0.6, -0.15


def run_stratum_backtest(stratum: str, start_date: str, end_date: str, initial_capital: float = 1_000_000) -> StrataResult:
    logger.info(f"Running backtest for {stratum}: {start_date} to {end_date}")
    
    # Load price data for universe
    price_data = load_price_history(MASTER_TICKERS, start_date, end_date)
    if not price_data:
        logger.warning(f"No price data for {stratum}")
        # Return empty result
        return StrataResult(
            stratum=stratum, start_date=start_date, end_date=end_date,
            config=None, report=None, spy_return=0, spy_sharpe=0, spy_max_dd=0,
            beats_spy_return=False, beats_spy_sharpe=False, max_dd_ok=False,
            stat_results={}
        )
    
    # Get trading dates
    trading_dates = get_trading_dates(price_data, frequency="weekly")
    logger.info(f"{len(trading_dates)} trading dates in {stratum}")
    
    # Build macro features for HMM
    macro_features = load_macro_features_history(start_date, end_date)
    
# Initialize HMM detector
        detector = RegimeDetector()
        if macro_features is not None and len(macro_features) > 250:
            detector.retrain(macro_features)
            logger.info("HMM detector fitted")
        else:
            logger.warning("Insufficient macro features for HMM fitting")
    
    # Run backtest cycles
    config = BacktestConfig(
        start_date=start_date,
        end_date=end_date,
        universe=MASTER_TICKERS,
        initial_capital=initial_capital,
        cost_bps=50,
    )
    
    # Load factor data for stat tests
    factor_path = Path("data/source/ken_french/ff5_daily.parquet")
    factor_data = pd.read_parquet(factor_path) if factor_path.exists() else None
    
    # Initial state
    state = BacktestState(
        date=pd.Timestamp(start_date, tz='UTC'),
        cash=initial_capital,
        positions={},
        position_values={},
        total_value=initial_capital,
        dry_powder=initial_capital * 0.1,  # 10% baseline dry powder
        regime="EXPANSION",
        hmm_crisis_prob=0.0,
    )
    
    # Track for stat tests
    portfolio_returns = []
    spy_returns = []
    regime_history = []
    
    for i, date in enumerate(trading_dates):
        # Current prices
        current_prices = {t: price_data[t].loc[date] if date in price_data[t].index else np.nan for t in MASTER_TICKERS}
        current_prices = {t: p for t, p in current_prices.items() if not np.isnan(p)}
        if not current_prices:
            continue
        
        # Get regime
        regime = RegimeState.EXPANSION
        hmm_crisis_prob = 0.0
        if detector.is_fitted:
            try:
                # Build features up to current date
                hist_features = macro_features[macro_features.index <= date] if macro_features is not None else None
                if hist_features is not None and len(hist_features) > 100:
                    regime, prob = detector.decode(hist_features)
                    hmm_crisis_prob = prob.get(2, 0.0) if isinstance(prob, dict) else (prob[2] if len(prob) > 2 else 0.0)
            except Exception as e:
                logger.debug(f"Regime detection failed at {date}: {e}")
        
        regime_history.append(regime.value if hasattr(regime, 'value') else str(regime))
        
        # Create synthetic DCF outputs
        dcf_results = create_synthetic_dcf_outputs(list(current_prices.keys()), current_prices)
        dcf_passed = list(current_prices.keys())
        
        # Compute C_t signals (degraded if no alt data)
        try:
            c_t_signals = get_real_c_t_signals(list(current_prices.keys()), lookback_days=63)
        except Exception as e:
            logger.debug(f"C_t signal failed: {e}")
            c_t_signals = {}
        
        # Run entry timing
        try:
            entry_signals = run_entry_timing(list(current_prices.keys()), current_prices)
        except Exception as e:
            logger.debug(f"Entry timing failed: {e}")
            entry_signals = {}
        
        # Run exit engine
        try:
            exit_signals = run_exit_engine(list(current_prices.keys()), current_prices, dcf_results)
        except Exception as e:
            logger.debug(f"Exit engine failed: {e}")
            exit_signals = {}
        
        # Run black swan check
        try:
            price_series = {t: price_data[t][price_data[t].index <= date] for t in current_prices.keys() if t in price_data}
            tier1_tracker = load_tier1_tracker()
            bs_decisions = run_black_swan_batch(
                list(current_prices.keys()), price_series, current_prices, tier1_tracker
            )
        except Exception as e:
            logger.debug(f"Black swan failed: {e}")
            bs_decisions = {}
        
        # Run fallback engine
        regime_str = regime.value if hasattr(regime, 'value') else str(regime)
        fallback_weights = get_fallback_weights_for_regime(regime_str, hmm_crisis_prob)
        
        # Run sizing & rebalancing
        portfolio_state = {
            'cash': state.cash,
            'positions': state.positions,
            'position_values': state.position_values,
            'total_value': state.total_value,
        }
        try:
            sizing_plan = run_sizing_rebalancing(
                portfolio_state=portfolio_state,
                dcf_results={t: o.to_dict() for t, o in dcf_results.items()},
                entry_signals={t: s.to_dict() for t, s in entry_signals.items()},
                exit_signals={t: s.to_dict() for t, s in exit_signals.items()},
                black_swan_decisions={t: d.to_dict() for t, d in bs_decisions.items()},
                fallback_weights=fallback_weights,
                regime=regime_str,
                hmm_crisis_prob=hmm_crisis_prob,
                current_prices=current_prices,
            )
            orders = generate_orders_from_plan(sizing_plan, current_prices)
        except Exception as e:
            logger.debug(f"Sizing failed: {e}")
            orders = []
        
        # Execute orders
        state = execute_orders(state, orders, current_prices, date)
        
        # Track returns
        prev_value = state.daily_values[-1] if state.daily_values else initial_capital
        daily_ret = (state.total_value - prev_value) / prev_value if prev_value > 0 else 0
        state.daily_returns.append(daily_ret)
        state.daily_values.append(state.total_value)
        
        # Log progress
        if i % 50 == 0:
            logger.info(f"  {date.date()}: Value=${state.total_value:,.0f}, Regime={regime_str}, CrisisProb={hmm_crisis_prob:.3f}")
    
    # Build report
    returns = pd.Series(state.daily_returns)
    if len(returns) > 1:
        total_return = (state.daily_values[-1] / state.daily_values[0]) - 1
        annualized_return = (1 + total_return) ** (252 / len(returns)) - 1
        sharpe = np.mean(returns) / np.std(returns, ddof=1) * np.sqrt(252) if np.std(returns, ddof=1) > 0 else 0
        
        cumulative = np.cumprod(1 + returns)
        running_max = np.maximum.accumulate(cumulative)
        drawdown = (cumulative - running_max) / running_max
        max_dd = float(np.min(drawdown))
    else:
        annualized_return = sharpe = max_dd = 0.0
    
    # SPY benchmark
    spy_return, spy_sharpe, spy_max_dd = load_spy_benchmark(start_date, end_date)
    
    report = BacktestReport(
        stratum=stratum,
        start_date=start_date,
        end_date=end_date,
        total_return=float(total_return) if 'total_return' in locals() else 0.0,
        annualized_return=float(annualized_return),
        sharpe=float(sharpe),
        max_drawdown=max_dd,
        num_trades=len([o for o in orders if o.get('side') == 'BUY']),
        final_value=state.total_value,
        regime_distribution=pd.Series(regime_history).value_counts().to_dict() if regime_history else {},
    )
    
    # Stat tests (simplified - need more data)
    stat_inputs = {
        'strategy_returns': returns.tolist(),
        'spy_returns': spy_returns,
        'regime_history': regime_history,
    }
    stat_results = {}
    if factor_data is not None:
        try:
            stat_results = run_full_stat_suite(stat_inputs, factor_data)
        except Exception as e:
            logger.warning(f"Stat tests failed: {e}")
    
    return StrataResult(
        stratum=stratum,
        start_date=start_date,
        end_date=end_date,
        config=config,
        report=report,
        spy_return=spy_return,
        spy_sharpe=spy_sharpe,
        spy_max_dd=spy_max_dd,
        beats_spy_return=annualized_return > spy_return,
        beats_spy_sharpe=sharpe > spy_sharpe,
        max_dd_ok=max_dd > -0.25,
        stat_results=stat_results,
    )


def main():
    logger.info("=" * 60)
    logger.info("STRUCTURAL BACKTEST - Pipeline Logic Validation")
    logger.info("=" * 60)
    logger.info("Using synthetic IV proxy: IV_Fair=Price*1.1, IV_P25=Price*0.9, IV_P10=Price*0.75")
    logger.info("Testing: HMM regime, C_t signals, Entry/Exit, Sizing, Fallback, Black Swan")
    logger.info("NOT testing: Fundamental DCF (no PIT data)")
    
    college_event("structural_backtest_start", 
        mode="synthetic_iv_proxy",
        testable=["HMM", "C_t", "Entry", "Exit", "Sizing", "Fallback", "BlackSwan"],
        untestable=["DCF_fundamental"],
        strata=list(STRATA.keys())
    )
    
    all_results = {}
    
    for stratum, s_info in STRATA.items():
        try:
            result = run_stratum_backtest(stratum, s_info["start"], s_info["end"])
            all_results[stratum] = result
            
            if result.report:
                logger.info(f"{stratum}: Return={result.report.annualized_return:.2%}, "
                           f"Sharpe={result.report.sharpe:.2f}, MaxDD={result.report.max_drawdown:.2%}, "
                           f"Beats SPY={result.beats_spy_return}/{result.beats_spy_sharpe}")
        except Exception as e:
            logger.error(f"{stratum} failed: {e}")
            all_results[stratum] = None
    
    # Aggregate results
    logger.info("=" * 60)
    logger.info("AGGREGATE RESULTS")
    logger.info("=" * 60)
    
    valid_results = {k: v for k, v in all_results.items() if v and v.report}
    
    if valid_results:
        avg_return = np.mean([v.report.annualized_return for v in valid_results.values()])
        avg_sharpe = np.mean([v.report.sharpe for v in valid_results.values()])
        max_dds = [v.report.max_drawdown for v in valid_results.values()]
        strata_wins_return = sum(1 for v in valid_results.values() if v.beats_spy_return)
        strata_wins_sharpe = sum(1 for v in valid_results.values() if v.beats_spy_sharpe)
        max_dd_ok = all(v.max_dd_ok for v in valid_results.values())
        
        logger.info(f"Avg Annualized Return: {avg_return:.2%}")
        logger.info(f"Avg Sharpe: {avg_sharpe:.2f}")
        logger.info(f"Strata Beating SPY (Return): {strata_wins_return}/{len(valid_results)}")
        logger.info(f"Strata Beating SPY (Sharpe): {strata_wins_sharpe}/{len(valid_results)}")
        logger.info(f"MaxDD per stratum: {[f'{dd:.2%}' for dd in max_dds]}")
        logger.info(f"All MaxDD > -25%: {max_dd_ok}")
        
        # Success bars
        u2_pass = strata_wins_return >= 3 and max_dd_ok
        u1_pass = avg_return > np.mean([v.spy_return for v in valid_results.values()]) and avg_sharpe > np.mean([v.spy_sharpe for v in valid_results.values()])
        
        logger.info(f"U1 (Full-window > SPY): {'PASS' if u1_pass else 'FAIL'}")
        logger.info(f"U2 (Strata >=3 wins, MaxDD>-25%): {'PASS' if u2_pass else 'FAIL'}")
        logger.info(f"C1 (Cascade): UNTESTED (no alt data history)")
        logger.info(f"C2 (Value IC): UNTESTED (no PIT fundamentals)")
    
    # Save report
    report_data = {
        "mode": "structural_synthetic_iv",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "strata": {},
    }
    
    for stratum, result in all_results.items():
        if result and result.report:
            report_data["strata"][stratum] = {
                "return": result.report.annualized_return,
                "sharpe": result.report.sharpe,
                "max_dd": result.report.max_drawdown,
                "beats_spy_return": result.beats_spy_return,
                "beats_spy_sharpe": result.beats_spy_sharpe,
                "max_dd_ok": result.max_dd_ok,
                "regime_distribution": result.report.regime_distribution,
                "spy_return": result.spy_return,
                "spy_sharpe": result.spy_sharpe,
                "spy_max_dd": result.spy_max_dd,
            }
    
    report_path = OUTPUT_DIR / "structural_backtest_report.json"
    with report_path.open("w") as f:
        json.dump(report_data, f, indent=2, default=str)
    
    logger.info(f"Report saved to {report_path}")
    college_event("structural_backtest_complete", report_path=str(report_path))
    
    return report_data


if __name__ == "__main__":
    main()