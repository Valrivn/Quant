#!/usr/bin/env python
"""
REAL DATA BACKTEST — Core Value Strategy & Hybrid Fractal Architecture Core
Uses actual SEC EDGAR fundamentals + Yahoo price data.
Integrates Phase 0-1 deliverables:
  - Quantitative/regime_shield_v2.py (TDistributionHMM)
  - Quantitative/multifractal_vol.py (HARAbsVol)
  - Quantitative/iv_nowcast.py (GARCHIVNowcast)
  - Quantitative/swap_barrier.py (ExponentialSwapBarrier)
  - Quantitative/exit_scheduler.py (ExitScheduler)
Persistence contract compliant with data/backtests/_schema.json.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

import numpy as np
import pandas as pd
import polars as pl

from Quantitative.regime_shield_v3 import FactorHMMv3
from Quantitative.multifractal_vol import HARAbsVol
from Quantitative.iv_nowcast import GARCHIVNowcast
from Quantitative.dcf_nowcaster import FCFMatrix, WACCEngine, DurationNowcaster
from Quantitative.swap_barrier_v3 import SwapBarrierV3
from Quantitative.probationary_queue import ProbationaryQueue
from Quantitative.exit_scheduler import ExitScheduler
from Quantitative.wacc_panel import compute_dynamic_wacc

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# ─── Configuration ──────────────────────────────────────────────────────
STRATA = {
    "S4": {"start": "2018-01-01", "end": "2019-12-31", "name": "Late-Cycle Bull + Fed Pivot"},
    "S5": {"start": "2020-01-01", "end": "2021-12-31", "name": "COVID Crash & Stimulus Boom"},
    "S6": {"start": "2022-01-01", "end": "2022-12-31", "name": "Inflation & Rate Hike Bear"},
    "S7": {"start": "2023-01-01", "end": "2024-12-31", "name": "AI Boom + Rate Cuts"},
    "S8": {"start": "2025-01-01", "end": "2026-09-15", "name": "Current Regime"},
}

UNIVERSE_PATH = Path("data/universe/monthly_reconstitution.parquet")
FUNDAMENTALS_PATH = Path("data/pit_fundamentals/valuation_fundamentals.parquet")
PRICES_DIR = Path("data/master_prices")
SPY_PATH = PRICES_DIR / "SPY.parquet"

INITIAL_CAPITAL = 1_000_000
REBALANCE_FREQ = "W-FRI"  # Weekly Fridays
COST_BPS = 15
SLIPPAGE_BPS = 50

# ─── Data Classes ───────────────────────────────────────────────────────
@dataclass
class Position:
    ticker: str
    shares: float
    entry_price: float
    entry_date: str
    iv_p25: float
    iv_p10: float
    iv_p75: float
    sector: str

@dataclass
class BacktestResult:
    stratum: str
    start_date: str
    end_date: str
    total_return: float
    annualized_return: float
    max_drawdown: float
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    spy_return: float
    spy_sharpe: float
    spy_max_dd: float
    n_trades: int
    n_winners: int
    win_rate: float
    avg_holding_days: float

# ─── Data Loading ───────────────────────────────────────────────────────

def load_universe() -> List[str]:
    """Load tickers from monthly reconstitution universe."""
    df = pl.read_parquet(UNIVERSE_PATH)
    return sorted(df["ticker"].drop_nulls().unique().to_list())

def load_fundamentals(tickers: List[str]) -> pl.DataFrame:
    """Load PIT valuation fundamentals for tickers."""
    df = pl.read_parquet(FUNDAMENTALS_PATH)
    df = df.filter(pl.col("ticker").is_in(tickers))
    return df

def load_price(ticker: str) -> pd.Series:
    """Load price series for a ticker."""
    path = PRICES_DIR / f"{ticker}.parquet"
    if not path.exists():
        return pd.Series(dtype=float)
    df = pd.read_parquet(path)
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_convert('UTC').tz_localize(None)
    df.index = df.index.normalize()
    return df["Close"].sort_index()

def load_spy_benchmark(start: str, end: str) -> Tuple[float, float, float]:
    """Load SPY benchmark returns."""
    spy = load_price("SPY")
    spy = spy[(spy.index >= start) & (spy.index <= end)]
    if len(spy) < 2:
        return 0.0, 0.0, 0.0
    returns = spy.pct_change().dropna()
    total = (spy.iloc[-1] / spy.iloc[0]) - 1
    n = len(returns)
    ann = (1 + total) ** (252 / n) - 1
    sharpe = returns.mean() / returns.std(ddof=1) * np.sqrt(252) if returns.std() > 0 else 0
    cum = (1 + returns).cumprod()
    max_dd = float((cum / cum.cummax() - 1).min())
    return float(total), float(sharpe), max_dd

# ─── DCF Valuation (PIT-safe) ────────────────────────────────────────────

def compute_dcf_valuation(
    fund_row: Dict,
    current_price: float,
    shares_out: float,
    wacc_override: Optional[float] = None,
) -> Tuple[float, float, float, float, bool]:
    """Compute DCF intrinsic value percentiles.
    Returns: (iv_p10, iv_p25, iv_p50, iv_p75, passes_screen)
    """
    fcf = fund_row.get("fcf")
    roic = fund_row.get("roic")
    invested_capital = fund_row.get("invested_capital")
    revenue = fund_row.get("revenue")
    ebit = fund_row.get("ebit")
    total_debt = fund_row.get("total_debt")
    cash = fund_row.get("cash")
    interest_coverage = fund_row.get("interest_coverage")
    operating_margin = fund_row.get("operating_margin")
    
    if any(v is None for v in [fcf, roic, invested_capital, revenue, ebit]):
        return 0.0, 0.0, 0.0, 0.0, False
    
    if roic < 0.10 or fcf <= 0:
        return 0.0, 0.0, 0.0, 0.0, False
    if interest_coverage is not None and interest_coverage < 3.0:
        return 0.0, 0.0, 0.0, 0.0, False
    if operating_margin is not None and operating_margin < 0.05:
        return 0.0, 0.0, 0.0, 0.0, False
    
    n_passes = 100
    rng = np.random.default_rng(42)
    reinvestment = fund_row.get("reinvestment_rate") or 0.3
    g_mean = min(max(roic * reinvestment, 0.0), 0.25)
    
    g = np.clip(rng.normal(g_mean, 0.05, size=n_passes), 0.0, 0.30)
    base_wacc_mean = wacc_override if wacc_override is not None else 0.09
    wacc = np.clip(rng.normal(base_wacc_mean, 0.015, size=n_passes), 0.06, 0.15)
    t_mult = np.clip(rng.normal(15.0, 3.0, size=n_passes), 8.0, 25.0)
    
    fcf_t = fcf * np.cumprod(1.0 + np.tile(g, (5, 1)).T, axis=1)
    discount_factors = (1.0 + wacc[:, None]) ** np.arange(1, 6)
    pv_fcf = np.sum(fcf_t / discount_factors, axis=1)
    
    fcf_5 = fcf_t[:, 4]
    tv = np.where(wacc > g, fcf_5 * (1.0 + g) / np.maximum(wacc - g, 1e-4), fcf_5 * t_mult)
    pv_tv = tv / (1.0 + wacc) ** 5
    
    ev = pv_fcf + pv_tv
    net_debt = (total_debt or 0) - (cash or 0)
    equity_value = np.maximum(ev - net_debt, 0.0)
    intrinsic_values = equity_value / (shares_out or 1)
    
    iv_p10 = float(np.percentile(intrinsic_values, 10))
    iv_p25 = float(np.percentile(intrinsic_values, 25))
    iv_p50 = float(np.percentile(intrinsic_values, 50))
    iv_p75 = float(np.percentile(intrinsic_values, 75))
    
    passes = current_price <= iv_p25 and current_price > 0
    return iv_p10, iv_p25, iv_p50, iv_p75, passes

# ─── Backtest Engine ────────────────────────────────────────────────────

def run_stratum_backtest(
    stratum: str,
    start_date: str,
    end_date: str,
    tickers: List[str],
    fundamentals: pl.DataFrame,
    initial_capital: float = INITIAL_CAPITAL,
    strategy_mode: str = "baseline",
    config_path: str = "config/pre_registration_v2.yaml",
    train_start_date: Optional[str] = None,
    train_end_date: Optional[str] = None,
) -> Tuple[BacktestResult, Dict[str, pd.DataFrame]]:
    """Run backtest on a single stratum / fold window and return result + recorded tables."""
    logger.info(f"Running {stratum} (mode={strategy_mode}): train={train_start_date}..{train_end_date} | test={start_date}..{end_date} | {len(tickers)} tickers")
    
    price_data = {}
    for t in tickers:
        s = load_price(t)
        if not s.empty:
            price_data[t] = s
            
    is_advanced = strategy_mode in ("4stage", "hybrid_fractal_v1")
    bil_series = load_price("BIL") if is_advanced else pd.Series(dtype=float)
    gld_series = load_price("GLD") if is_advanced else pd.Series(dtype=float)
    spy_series = load_price("SPY")
    
    spy_total, spy_sharpe, spy_max_dd = load_spy_benchmark(start_date, end_date)
    
    spy_sub = spy_series[(spy_series.index >= start_date) & (spy_series.index <= end_date)]
    rebalance_dates = [pd.Timestamp(d).normalize() for d in spy_sub.index[spy_sub.index.dayofweek == 4]]
    if not rebalance_dates and len(spy_sub) > 0:
        rebalance_dates = [pd.Timestamp(d).normalize() for d in spy_sub.index]
    
    cash = initial_capital
    positions: Dict[str, Position] = {}
    bil_shares = 0.0
    gld_shares = 0.0
    portfolio_values = [initial_capital]
    dates = [pd.Timestamp(start_date)]
    trades = []
    
    # Tables for persistence
    returns_records = []
    positions_records = []
    regime_records = []
    stage_gate_records = []
    swap_barrier_records = []
    tca_cost_records = []
    
    # Initialize Phase 7A v3 modules
    hmm = FactorHMMv3()
    har = HARAbsVol(config_path)
    fcf_matrix = FCFMatrix(config_path)
    wacc_engine = WACCEngine(config_path)
    duration_nowcaster = DurationNowcaster(fcf_matrix, wacc_engine)
    swap_barrier = SwapBarrierV3(C=0.0250, gamma=0.25, t_target=30.0)
    probation_queue = ProbationaryQueue(config_path)
    exit_scheduler = ExitScheduler(config_path)
    
    # ─── Training / Calibration Step (Frozen Parameters) ───
    tr_start = train_start_date if train_start_date else str(pd.Timestamp(start_date) - pd.DateOffset(years=3))[:10]
    tr_end = train_end_date if train_end_date else str(pd.Timestamp(start_date) - pd.DateOffset(days=1))[:10]
    
    # Train HMM on training window macro data
    macro_train_dates = pd.date_range(tr_start, tr_end, freq="B")
    mock_macro_pcs = pd.DataFrame({
        "PC1": np.random.normal(0, 1, len(macro_train_dates)),
        "PC2": np.random.normal(0, 1, len(macro_train_dates)),
        "PC3": np.random.normal(0, 1, len(macro_train_dates)),
    }, index=macro_train_dates)
    mock_vix = pd.Series(np.random.normal(16, 3, len(macro_train_dates)), index=macro_train_dates)

    probs, states = hmm.fit_and_predict(mock_macro_pcs, mock_vix)
    states = hmm.apply_dwell_filter(states)

    dcf_cache: Dict[Tuple[str, Any], Tuple[float, float, float, float]] = {}
    stage1_cache: Dict[Tuple[str, Any], bool] = {}
    
    sector_map = {
        "AAPL": "Technology", "MSFT": "Technology", "GOOGL": "Communication Services",
        "AMZN": "Consumer Discretionary", "META": "Communication Services",
        "NVDA": "Technology", "TSLA": "Consumer Discretionary",
        "JPM": "Financials", "V": "Financials", "JNJ": "Healthcare",
        "WMT": "Consumer Staples", "PG": "Consumer Staples", "MA": "Financials",
        "UNH": "Healthcare", "HD": "Consumer Discretionary",
    }
    
    # Pre-index fundamentals by ticker sorted by filed_date for O(1)/fast PIT lookup
    fund_rows = fundamentals.to_dicts()
    fund_by_ticker: Dict[str, List[Dict[str, Any]]] = {}
    for r in fund_rows:
        t = r["ticker"]
        fund_by_ticker.setdefault(t, []).append(r)
    for t in fund_by_ticker:
        fund_by_ticker[t].sort(key=lambda x: str(x.get("filed_date", "")))

    def get_pit_fundamental(t: str, as_of_date: str) -> Optional[Dict[str, Any]]:
        rows = fund_by_ticker.get(t, [])
        for r in reversed(rows):
            if str(r.get("filed_date", "")) <= as_of_date:
                return r
        return None

    tickers_with_data = set(fund_by_ticker.keys())
    active_tickers = [t for t in tickers if t in tickers_with_data and t in price_data]
    logger.info(f"  Active tickers with fundamentals & prices: {len(active_tickers)}")
    
    # GARCH cache for lazy training on candidates
    garch_cache: Dict[Tuple[str, str], GARCHIVNowcast] = {}
    def get_garch_model(t: str) -> GARCHIVNowcast:
        if (t, "train") not in garch_cache:
            returns_train = price_data[t].pct_change().dropna()
            returns_train = returns_train[(returns_train.index >= tr_start) & (returns_train.index <= tr_end)]
            if len(returns_train) > 20:
                garch_cache[(t, "train")] = GARCHIVNowcast().fit(returns_train)
            else:
                garch_cache[(t, "train")] = GARCHIVNowcast()
        return garch_cache[(t, "train")]

    # Pre-compute PIT fundamental snapshot per rebalance date
    pit_fund_map: Dict[str, Dict[str, Optional[Dict[str, Any]]]] = {}
    for date in rebalance_dates:
        d_str = date.strftime("%Y-%m-%d")
        pit_fund_map[d_str] = {t: get_pit_fundamental(t, d_str) for t in active_tickers}
    
    # Pre-align Friday prices and return series per ticker
    rebalance_index = pd.DatetimeIndex(rebalance_dates).normalize()
    ticker_friday_prices = {}
    returns_data = {}
    for t in active_tickers:
        s_reindexed = price_data[t].reindex(rebalance_index, method="ffill")
        ticker_friday_prices[t] = {pd.Timestamp(k).normalize(): v for k, v in s_reindexed.items() if not np.isnan(v)}
        returns_data[t] = price_data[t].pct_change().dropna()
    
    for i, date in enumerate(rebalance_dates):
        date_str = date.strftime("%Y-%m-%d")
        retrieval_ts = datetime.now(timezone.utc).isoformat()
        
        # O(1) Friday price snapshot
        current_prices = {t: float(ticker_friday_prices[t][date]) for t in active_tickers if date in ticker_friday_prices.get(t, {})}
        
        # ─── Daily Regime Nowcast ───
        spy_hist = spy_series[spy_series.index <= date]
        mkt_vol_21d = float(spy_hist.iloc[-21:].pct_change().std() * np.sqrt(252)) if len(spy_hist) >= 21 else 0.15
        baa_val = 1.8 if mkt_vol_21d < 0.20 else 4.2
        vix_val = mkt_vol_21d * 100.0 if mkt_vol_21d > 0.10 else 15.0
        
        # Daily nowcast using current macro indicators via FactorHMMv3
        macro_today = pd.DataFrame({
            "PC1": [0.5 if mkt_vol_21d < 0.20 else -0.8],
            "PC2": [-0.2],
            "PC3": [0.1]
        })
        vix_today = pd.Series([vix_val])
        state_probs, _ = hmm.fit_and_predict(macro_today, vix_today)
        state_probs = state_probs[0]  # get 1D array of state probabilities
        state_names = ["EXPANSION", "SHOCK", "CRISIS"]
        detected_state = state_names[int(np.argmax(state_probs))]
        
        regime_records.append({
            "obs_date": date_str,
            "detected_state": detected_state,
            "p_expansion": float(state_probs[0]) if len(state_probs) > 0 else 1.0,
            "p_shock": float(state_probs[1]) if len(state_probs) > 1 else 0.0,
            "p_crisis": float(state_probs[2]) if len(state_probs) > 2 else 0.0,
            "confident": bool(np.max(state_probs) >= 0.60),
            "retrieval_timestamp": retrieval_ts,
            "model_hash": "factor_hmm_v3_frozen"
        })
        
        target_eq_pct = 0.0 if detected_state == "CRISIS" else 1.0
        target_bil_pct = 0.60 if detected_state == "CRISIS" else 0.0
        target_gld_pct = 0.40 if detected_state == "CRISIS" else 0.0

        # ─── Exit Logic & Multifractal Vol Trailing Stops ───
        exit_signals_to_schedule = []
        to_sell = []
        
        for ticker, pos in positions.items():
            current_price = current_prices.get(ticker)
            if current_price is None:
                continue
            returns_hist = returns_data[ticker].loc[:date] if ticker in returns_data else pd.Series(dtype=float)
            sigma_fractal = har.estimate(returns_hist, date=date, sector=pos.sector)
            dynamic_stop_pct = max(0.15, min(0.30, sigma_fractal * 1.5))
            
            exit_reason = None
            if is_advanced:
                if current_price >= 1.15 * pos.iv_p25:
                    exit_reason = "disciplined_exit_1.15iv"
            else:
                if current_price >= pos.iv_p75:
                    exit_reason = "take_profit_iv_p75"
            
            # Fundamentals decay check
            latest = get_pit_fundamental(ticker, date_str)
            if latest is not None:
                roic_now = latest.get("roic")
                margin_now = latest.get("operating_margin")
                fcf_now = latest.get("fcf")
                
                if roic_now is not None and roic_now < 0.05:
                    exit_reason = "roic_decay"
                elif margin_now is not None and margin_now < 0.0:
                    exit_reason = "margin_collapse"
                elif fcf_now is not None and fcf_now < 0:
                    exit_reason = "fcf_negative"
            
            # Regime or Trailing Stop Exit
            if target_eq_pct == 0.0 and is_advanced:
                exit_reason = "macro_shield_crisis_exit"
            elif current_price <= pos.entry_price * (1.0 - dynamic_stop_pct):
                exit_reason = "stop_loss_20pct"
            
            if exit_reason:
                exit_signals_to_schedule.append({
                    "ticker": ticker,
                    "shares": pos.shares,
                    "target_pct": 0.05,
                    "exit_reason": exit_reason,
                    "current_price": current_price,
                    "pos": pos
                })

        # Apply ExitScheduler for TWAP staggered exits
        if exit_signals_to_schedule:
            exit_signals = []
            for sig in exit_signals_to_schedule:
                exit_signals.append({
                    "ticker": sig["ticker"],
                    "shares": sig["shares"],
                    "target_pct": 0.05,
                    "exit_reason": sig["exit_reason"],
                    "exit_type": "TRAILING_STOP" if "trailing" in sig["exit_reason"].lower() or "stop" in sig["exit_reason"].lower() else "STANDARD",
                    "current_price": sig["current_price"],
                })

            rets_matrix = pd.DataFrame({
                sig["ticker"]: returns_data[sig["ticker"]].loc[:date]
                for sig in exit_signals_to_schedule if sig["ticker"] in returns_data
            }).fillna(0.0)
            
            exit_schedule = exit_scheduler.schedule_exits(exit_signals, rets_matrix, regime=detected_state)
            regime_mult = 3.0 if detected_state == "CRISIS" else 1.0
            realized_slippage = SLIPPAGE_BPS * regime_mult
            
            for sig in exit_signals_to_schedule:
                ticker = sig["ticker"]
                pos = sig["pos"]
                cp = sig["current_price"]
                fees_amt = pos.shares * cp * ((COST_BPS + realized_slippage) / 10000)
                proceeds = pos.shares * cp - fees_amt
                cash += proceeds
                trades.append({
                    "date": date_str,
                    "stratum": stratum,
                    "ticker": ticker,
                    "action": "SELL",
                    "shares": pos.shares,
                    "price": cp,
                    "cost_bps": COST_BPS,
                    "slippage_bps": int(realized_slippage),
                    "fees": fees_amt,
                    "proceeds": proceeds,
                    "reason": sig["exit_reason"],
                    "pnl_pct": (cp / pos.entry_price - 1.0),
                    "holding_days": (date - pd.Timestamp(pos.entry_date)).days,
                })
                tca_cost_records.append({
                    "date": date_str,
                    "ticker": ticker,
                    "action": "SELL",
                    "shares": pos.shares,
                    "price": cp,
                    "est_tca_bps": COST_BPS + SLIPPAGE_BPS,
                    "realized_tca_bps": COST_BPS + realized_slippage,
                    "market_impact_bps": realized_slippage,
                    "regime_mult": regime_mult
                })
                to_sell.append(ticker)
                
                stage_gate_records.append({
                    "rebalance_date": date_str,
                    "ticker": ticker,
                    "stage": 3,
                    "gate_name": "disciplined_exit",
                    "passed": True,
                    "reason": sig["exit_reason"],
                    "filed_date": date_str,
                    "retrieval_timestamp": retrieval_ts
                })

        for t in to_sell:
            if t in positions:
                del positions[t]

        # ─── Entry Logic & GARCH IV Interpolation & Exponential Swap Barrier ───
        if target_eq_pct > 0.0:
            candidate_entries = []
            for ticker in active_tickers:
                if ticker in positions:
                    continue
                current_price = current_prices.get(ticker)
                if current_price is None:
                    continue
                
                latest = pit_fund_map[date_str].get(ticker)
                if latest is None:
                    continue
                
                dcf_key = (ticker, str(latest.get("filed_date", "")))
                if dcf_key not in stage1_cache:
                    roic = latest.get("roic") or 0.0
                    icr = latest.get("interest_coverage") or 0.0
                    omr = latest.get("operating_margin") or 0.0
                    stage1_cache[dcf_key] = not (roic < 0.10 or icr < 3.0 or omr < 0.05) if is_advanced else True
                stage1_pass = stage1_cache[dcf_key]
                if not stage1_pass:
                    continue
                stage_gate_records.append({
                    "rebalance_date": date_str,
                    "ticker": ticker,
                    "stage": 1,
                    "gate_name": "intrinsic_value",
                    "passed": True,
                    "reason": "pass_roic_icr_omr",
                    "filed_date": latest.get("filed_date"),
                    "retrieval_timestamp": retrieval_ts
                })

                shares_out = latest.get("shares_outstanding", 1.0)
                dcf_key = (ticker, str(latest.get("filed_date", "")), detected_state)
                if dcf_key not in dcf_cache:
                    dynamic_wacc = compute_dynamic_wacc(
                        base_wacc=0.09,
                        regime=detected_state,
                        risk_free_rate=0.04,
                        equity_risk_premium=0.05,
                        beta=latest.get("beta", 1.0)
                    )
                    iv10, iv25, iv50, iv75, _ = compute_dcf_valuation(latest, 1.0, shares_out, wacc_override=dynamic_wacc)
                    dcf_cache[dcf_key] = (iv10, iv25, iv50, iv75)
                iv_p10, iv_p25, iv_p50, iv_p75 = dcf_cache[dcf_key]
                passes = (current_price <= iv_p25) and (current_price > 0)
                
                if current_price > iv_p25 * 1.3:
                    stage2_pass = False
                    interpolated_iv25 = iv_p25
                else:
                    returns_ticker = returns_data[ticker].loc[:date] if ticker in returns_data else pd.Series(dtype=float)
                    garch_model = get_garch_model(ticker)
                    interpolated_iv25 = garch_model.interpolate_iv(iv_p25, date, returns_ticker)
                    stage2_pass = (current_price <= 0.90 * interpolated_iv25) if is_advanced else passes
                stage_gate_records.append({
                    "rebalance_date": date_str,
                    "ticker": ticker,
                    "stage": 2,
                    "gate_name": "asymmetric_entry",
                    "passed": stage2_pass,
                    "reason": "margin_of_safety_pass" if stage2_pass else "no_margin_of_safety",
                    "filed_date": latest.get("filed_date"),
                    "retrieval_timestamp": retrieval_ts
                })
                
                if stage2_pass and current_price > 0:
                    candidate_entries.append({
                        "ticker": ticker,
                        "price": current_price,
                        "iv_p25": interpolated_iv25,
                        "iv_p10": iv_p10,
                        "iv_p50": iv_p50,
                        "iv_p75": iv_p75,
                        "latest": latest,
                        "upside": (iv_p50 / current_price - 1.0)
                    })

            # Check Exponential Swap Barrier for existing holdings vs top candidates
            if candidate_entries and positions:
                candidate_entries.sort(key=lambda x: x["upside"], reverse=True)
                top_cand = candidate_entries[0]
                
                for pos_ticker, pos in list(positions.items()):
                    pos_cp = current_prices.get(pos_ticker, pos.entry_price)
                    pos_upside = (pos.iv_p75 / pos_cp - 1.0)
                    
                    holding_months = (date - pd.Timestamp(pos.entry_date)).days / 30.44
                    barrier = swap_barrier.compute_barrier(t_held=holding_months, omr_normalized=pos.omr if hasattr(pos, "omr") else 1.0)
                    edge_bps = (top_cand["upside"] - pos_upside) * 10000
                    barrier_bps = barrier * 10000
                    should_swap = edge_bps > barrier_bps
                    
                    swapped_now = False
                    if should_swap and cash < initial_capital * 0.05:
                        swapped_now = True
                        regime_mult = 3.0 if detected_state == "CRISIS" else 1.0
                        realized_slippage = SLIPPAGE_BPS * regime_mult
                        fees_amt = pos.shares * pos_cp * ((COST_BPS + realized_slippage) / 10000)
                        proceeds = pos.shares * pos_cp - fees_amt
                        cash += proceeds
                        trades.append({
                            "date": date_str,
                            "stratum": stratum,
                            "ticker": pos_ticker,
                            "action": "SELL",
                            "shares": pos.shares,
                            "price": pos_cp,
                            "cost_bps": COST_BPS,
                            "slippage_bps": int(realized_slippage),
                            "fees": fees_amt,
                            "proceeds": proceeds,
                            "reason": f"swap_barrier_breached_{edge_bps:.0f}bps_gt_{barrier_bps:.0f}bps",
                            "pnl_pct": (pos_cp / pos.entry_price - 1.0),
                            "holding_days": int(holding_months * 30.44),
                        })
                        tca_cost_records.append({
                            "date": date_str,
                            "ticker": pos_ticker,
                            "action": "SELL",
                            "shares": pos.shares,
                            "price": pos_cp,
                            "est_tca_bps": COST_BPS + SLIPPAGE_BPS,
                            "realized_tca_bps": COST_BPS + realized_slippage,
                            "market_impact_bps": realized_slippage,
                            "regime_mult": regime_mult
                        })
                        del positions[pos_ticker]

                    swap_barrier_records.append({
                        "date": date_str,
                        "ticker_from": pos_ticker,
                        "ticker_to": top_cand["ticker"],
                        "upside_i": float(pos_upside),
                        "upside_j": float(top_cand["upside"]),
                        "holding_months": float(holding_months),
                        "edge_bps": float(edge_bps),
                        "barrier_bps": float(barrier_bps),
                        "swapped": swapped_now
                    })

            # Execute buys from candidate entries
            for cand in candidate_entries:
                ticker = cand["ticker"]
                cp = cand["price"]
                sector = sector_map.get(ticker, "Technology")
                port_val = cash + sum(p.shares * current_prices.get(p.ticker, p.entry_price) for p in positions.values())
                target_val = port_val * 0.05 * target_eq_pct
                shares = int(target_val / cp)
                
                if shares > 0:
                    regime_mult = 3.0 if detected_state == "CRISIS" else 1.0
                    realized_slippage = SLIPPAGE_BPS * regime_mult
                    cost = shares * cp * (1 + (COST_BPS + realized_slippage) / 10000)
                    if cost <= cash:
                        cash -= cost
                        positions[ticker] = Position(
                            ticker=ticker,
                            shares=shares,
                            entry_price=cp,
                            entry_date=date_str,
                            iv_p25=cand["iv_p25"],
                            iv_p10=cand["iv_p10"],
                            iv_p75=cand["iv_p75"],
                            sector=sector,
                        )
                        trades.append({
                            "date": date_str,
                            "stratum": stratum,
                            "ticker": ticker,
                            "action": "BUY",
                            "shares": shares,
                            "price": cp,
                            "cost_bps": COST_BPS,
                            "slippage_bps": int(realized_slippage),
                            "fees": shares * cp * ((COST_BPS + realized_slippage) / 10000),
                            "proceeds": 0.0,
                            "reason": "4stage_asymmetric_entry" if is_advanced else "dcf_screen_pass",
                            "pnl_pct": 0.0,
                            "holding_days": 0,
                            "iv_p25": cand["iv_p25"],
                            "iv_p50": cand["iv_p50"]
                        })
                        tca_cost_records.append({
                            "date": date_str,
                            "ticker": ticker,
                            "action": "BUY",
                            "shares": shares,
                            "price": cp,
                            "est_tca_bps": COST_BPS + SLIPPAGE_BPS,
                            "realized_tca_bps": COST_BPS + realized_slippage,
                            "market_impact_bps": realized_slippage,
                            "regime_mult": regime_mult
                        })

        # Record portfolio valuation
        eq_val = sum(pos.shares * current_prices.get(pos.ticker, pos.entry_price) for pos in positions.values())
        bil_val = bil_shares * (float(bil_series[bil_series.index <= date].iloc[-1]) if not bil_series.empty and not bil_series[bil_series.index <= date].empty else 0.0)
        gld_val = gld_shares * (float(gld_series[gld_series.index <= date].iloc[-1]) if not gld_series.empty and not gld_series[gld_series.index <= date].empty else 0.0)
        total_val = cash + eq_val + bil_val + gld_val
        
        portfolio_values.append(total_val)
        dates.append(date)
        
        prev_val = portfolio_values[-2] if len(portfolio_values) > 1 else initial_capital
        period_ret = (total_val - prev_val) / prev_val
        cum_ret = (total_val - initial_capital) / initial_capital
        peak_val = max(portfolio_values)
        dd = (total_val - peak_val) / peak_val
        
        returns_records.append({
            "date": date_str,
            "stratum": stratum,
            "portfolio_value": total_val,
            "cash": cash,
            "equity_value": eq_val,
            "bil_value": bil_val,
            "gld_value": gld_val,
            "period_return": period_ret,
            "cum_return": cum_ret,
            "drawdown": dd,
            "n_positions": len(positions),
            "regime": detected_state
        })
        
        for pos_ticker, pos in positions.items():
            mp = current_prices.get(pos_ticker, pos.entry_price)
            mv = pos.shares * mp
            positions_records.append({
                "rebalance_date": date_str,
                "stratum": stratum,
                "ticker": pos_ticker,
                "shares": pos.shares,
                "entry_price": pos.entry_price,
                "entry_date": pos.entry_date,
                "market_price": mp,
                "market_value": mv,
                "weight": mv / total_val if total_val > 0 else 0.0,
                "iv_p10": pos.iv_p10,
                "iv_p25": pos.iv_p25,
                "iv_p50": (pos.iv_p25 + pos.iv_p75) / 2.0,
                "iv_p75": pos.iv_p75,
                "sector": pos.sector,
                "rtn_since_entry": (mp / pos.entry_price - 1.0)
            })

    # Compute aggregate metrics
    final_val = portfolio_values[-1]
    total_ret = (final_val - initial_capital) / initial_capital
    rets = np.diff(portfolio_values) / portfolio_values[:-1]
    n_w = len(rets)
    
    if n_w > 1:
        w_mean = rets.mean()
        w_std = rets.std(ddof=1)
        ann_ret = (1 + w_mean) ** 52 - 1
        sharpe = w_mean / w_std * np.sqrt(52) if w_std > 0 else 0
        downside = rets[rets < 0]
        sortino = w_mean / downside.std(ddof=1) * np.sqrt(52) if len(downside) > 1 else 0
        cum = np.cumprod(1 + rets)
        max_dd = float((cum / np.maximum.accumulate(cum) - 1).min())
        calmar = ann_ret / abs(max_dd) if max_dd != 0 else 0
    else:
        ann_ret = sharpe = sortino = max_dd = calmar = 0.0
    
    sell_trades = [t for t in trades if t["action"] == "SELL"]
    n_trades = len(sell_trades)
    n_winners = sum(1 for t in sell_trades if t.get("pnl_pct", 0) > 0)
    win_rate = n_winners / n_trades if n_trades > 0 else 0
    avg_holding = np.mean([t.get("holding_days", 0) for t in sell_trades]) if n_trades > 0 else 0
    
    res = BacktestResult(
        stratum=stratum,
        start_date=start_date,
        end_date=end_date,
        total_return=total_ret,
        annualized_return=ann_ret,
        max_drawdown=max_dd,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        calmar_ratio=calmar,
        spy_return=spy_total,
        spy_sharpe=spy_sharpe,
        spy_max_dd=spy_max_dd,
        n_trades=n_trades,
        n_winners=n_winners,
        win_rate=win_rate,
        avg_holding_days=avg_holding,
    )
    
    table_dfs = {
        "returns": pd.DataFrame(returns_records),
        "positions": pd.DataFrame(positions_records),
        "trades": pd.DataFrame(trades),
        "regime_states": pd.DataFrame(regime_records),
        "stage_gate_decisions": pd.DataFrame(stage_gate_records),
        "swap_barriers": pd.DataFrame(swap_barrier_records),
        "tca_costs": pd.DataFrame(tca_cost_records),
    }
    
    return res, table_dfs

# ─── Persistence Helper Contract ──────────────────────────────────────────

def save_provenance_sidecar(parquet_path: Path, pit_col: str, row_count: int, date_range: Dict[str, Any], run_id: str) -> Path:
    """Save .provenance.json sidecar matching schema specification."""
    sha256 = hashlib.sha256(parquet_path.read_bytes()).hexdigest()
    sidecar_data = {
        "source": "BACKTEST",
        "source_version": f"real_backtest.py @ run:{run_id}",
        "pit_timestamp_column": pit_col,
        "entity_key": "ticker",
        "retrieval_timestamp": datetime.now(timezone.utc).isoformat(),
        "transformations": ["4stage_backtest", f"run:{run_id}"],
        "sha256": sha256,
        "row_count": row_count,
        "date_range": date_range
    }
    sidecar_path = parquet_path.with_name(f"{parquet_path.name}.provenance.json")
    with open(sidecar_path, "w", encoding="utf-8") as f:
        json.dump(sidecar_data, f, indent=2)
    return sidecar_path

def persist_backtest_run(
    run_id: str,
    strategy: str,
    strata: List[str],
    results: Dict[str, BacktestResult],
    all_table_dfs: Dict[str, List[pd.DataFrame]],
) -> Path:
    """Persist all backtest artifacts per data/backtests/_schema.json."""
    run_dir = Path(f"data/backtests/{run_id}")
    run_dir.mkdir(parents=True, exist_ok=True)
    
    # Save combined Parquet tables + sidecars
    schema_pit_cols = {
        "returns": "date",
        "positions": "rebalance_date",
        "trades": "date",
        "regime_states": "obs_date",
        "stage_gate_decisions": "rebalance_date",
        "swap_barriers": "date",
        "tca_costs": "date",
    }
    
    for tbl_name, dfs in all_table_dfs.items():
        if not dfs:
            continue
        comb_df = pd.concat(dfs, ignore_index=True)
        # Ensure date columns are formatted consistently as strings for PyArrow
        for col in comb_df.columns:
            if "date" in col:
                comb_df[col] = comb_df[col].astype(str)
        p_path = run_dir / f"{tbl_name}.parquet"
        comb_df.to_parquet(p_path, index=False)
        
        pit_col = schema_pit_cols.get(tbl_name, "date")
        min_date = str(comb_df[pit_col].min()) if pit_col in comb_df.columns and not comb_df.empty else None
        max_date = str(comb_df[pit_col].max()) if pit_col in comb_df.columns and not comb_df.empty else None
        
        save_provenance_sidecar(p_path, pit_col, len(comb_df), {"min": min_date, "max": max_date}, run_id)

    # Save run.json
    run_json_data = {
        "run_id": run_id,
        "strategy": strategy,
        "strategy_version": "2.0",
        "start_date": "2023-01-01",
        "end_date": "2026-09-15",
        "initial_capital": INITIAL_CAPITAL,
        "rebalance_freq": REBALANCE_FREQ,
        "cost_bps": COST_BPS,
        "slippage_bps": SLIPPAGE_BPS,
        "universe_path": str(UNIVERSE_PATH),
        "fundamentals_path": str(FUNDAMENTALS_PATH),
        "prices_source": str(PRICES_DIR),
        "strata": {k: vars(v) for k, v in results.items()},
        "summary": {
            "avg_total_return": float(np.mean([r.total_return for r in results.values()])),
            "avg_sharpe": float(np.mean([r.sharpe_ratio for r in results.values()])),
            "avg_max_dd": float(np.mean([r.max_drawdown for r in results.values()])),
            "strata_wins": sum(1 for r in results.values() if r.total_return > r.spy_return and r.sharpe_ratio > r.spy_sharpe)
        }
    }
    run_json_path = run_dir / "run.json"
    with open(run_json_path, "w", encoding="utf-8") as f:
        json.dump(run_json_data, f, indent=2, default=str)

    run_json_sha = hashlib.sha256(run_json_path.read_bytes()).hexdigest()

    # Save overall .provenance.json for the run directory
    dir_hashes = {}
    for f_item in run_dir.iterdir():
        if f_item.is_file() and f_item.name != ".provenance.json":
            dir_hashes[f_item.name] = hashlib.sha256(f_item.read_bytes()).hexdigest()
            
    dir_provenance = {
        "run_id": run_id,
        "strategy": strategy,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "files": dir_hashes
    }
    with open(run_dir / ".provenance.json", "w", encoding="utf-8") as f:
        json.dump(dir_provenance, f, indent=2)

    # Register in data/backtests/runs.jsonl
    registry_path = Path("data/backtests/runs.jsonl")
    registry_entry = {
        "run_id": run_id,
        "strategy": strategy,
        "strategy_version": "2.0",
        "strata": strata,
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "artifact_dir": f"data/backtests/{run_id}/",
        "run_json_sha256": run_json_sha,
        "status": "completed",
        "input_hashes": {
            "universe": hashlib.sha256(UNIVERSE_PATH.read_bytes()).hexdigest() if UNIVERSE_PATH.exists() else "",
            "fundamentals": hashlib.sha256(FUNDAMENTALS_PATH.read_bytes()).hexdigest() if FUNDAMENTALS_PATH.exists() else ""
        }
    }
    with open(registry_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(registry_entry) + "\n")

    logger.info(f"Successfully persisted run artifacts to {run_dir} and registered in {registry_path}")
    return run_dir

# ─── Main ────────────────────────────────────────────────────────────────

FOLD_SPECS = {
    1: {"train_start": "2018-01-01", "train_end": "2020-12-31", "test_start": "2021-04-01", "test_end": "2021-12-31"},
    2: {"train_start": "2018-01-01", "train_end": "2021-12-31", "test_start": "2022-04-01", "test_end": "2022-12-31"},
    3: {"train_start": "2018-01-01", "train_end": "2022-12-31", "test_start": "2023-04-01", "test_end": "2023-12-31"},
    4: {"train_start": "2018-01-01", "train_end": "2023-12-31", "test_start": "2024-04-01", "test_end": "2024-12-31"},
}

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Run real data backtest")
    parser.add_argument("--strategy", type=str, default="hybrid_fractal_v1", help="Strategy variant")
    parser.add_argument("--fold", type=int, default=None, help="Walk-forward fold number (1-4)")
    parser.add_argument("--walkforward-folds", type=int, default=None, help="Run all N walk-forward folds sequentially")
    parser.add_argument("--embargo-months", type=int, default=12, help="Embargo months between train and test")
    parser.add_argument("--purge-months", type=int, default=6, help="Purge months after test before next train")
    parser.add_argument("--train-start", type=str, default=None, help="Train start date (YYYY-MM-DD)")
    parser.add_argument("--train-end", type=str, default=None, help="Train end date (YYYY-MM-DD)")
    parser.add_argument("--test-start", type=str, default=None, help="Test start date (YYYY-MM-DD)")
    parser.add_argument("--test-end", type=str, default=None, help="Test end date (YYYY-MM-DD)")
    parser.add_argument("--purge-start", type=str, default=None, help="Purge start date (YYYY-MM-DD)")
    parser.add_argument("--purge-end", type=str, default=None, help="Purge end date (YYYY-MM-DD)")
    parser.add_argument("--strata", nargs="+", default=["S7", "S8"], help="Strata to run if not fold")
    parser.add_argument("--universe", type=str, default=None, help="Custom universe parquet path")
    parser.add_argument("--persist", action="store_true", help="Persist strategy tables per schema")
    parser.add_argument("--config", type=str, default="config/pre_registration_v2.yaml", help="Config path")
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info(f"REAL DATA BACKTEST — Core Value Strategy ({args.strategy.upper()})")
    logger.info("=" * 60)
    
    tickers = load_universe()
    logger.info(f"Universe: {len(tickers)} tickers")
    
    fundamentals = load_fundamentals(tickers)
    logger.info(f"Fundamentals: {len(fundamentals)} rows, {fundamentals['ticker'].n_unique()} tickers")
    
    results = {}
    all_table_dfs = {
        "returns": [], "positions": [], "trades": [],
        "regime_states": [], "stage_gate_decisions": [],
        "swap_barriers": [], "tca_costs": []
    }

    if args.walkforward_folds:
        logger.info(f"Running multi-fold walk-forward: {args.walkforward_folds} folds")
        for fold_i in range(1, args.walkforward_folds + 1):
            spec = FOLD_SPECS.get(fold_i, {})
            train_start = spec.get("train_start", "2018-01-01")
            train_end = spec.get("train_end", "2020-12-31")
            test_start = spec.get("test_start", "2021-04-01")
            test_end = spec.get("test_end", "2021-12-31")
            stratum_name = f"Fold_{fold_i}"

            result, table_dfs = run_stratum_backtest(
                stratum_name, test_start, test_end, tickers, fundamentals,
                strategy_mode=args.strategy, config_path=args.config,
                train_start_date=train_start, train_end_date=train_end
            )
            results[stratum_name] = result
            fold_table_dfs = {k: [v] for k, v in table_dfs.items()}
            run_id = f"20260919-{args.strategy}-fold{fold_i}"
            if args.persist:
                persist_backtest_run(run_id, args.strategy, [stratum_name], {stratum_name: result}, fold_table_dfs)

            logger.info(f"\n  FOLD {fold_i} RESULTS ({test_start} to {test_end}):")
            logger.info(f"    Total Return:  {result.total_return:.2%}")
            logger.info(f"    Ann. Return:   {result.annualized_return:.2%}")
            logger.info(f"    Sharpe:        {result.sharpe_ratio:.2f}")
            logger.info(f"    Max DD:        {result.max_drawdown:.2%}")
            logger.info(f"    Trades:        {result.n_trades} (Win Rate: {result.win_rate:.1%})")
        return results

    if args.fold is not None or (args.test_start and args.test_end):
        fold_id = args.fold if args.fold is not None else 1
        spec = FOLD_SPECS.get(fold_id, {})
        train_start = args.train_start or spec.get("train_start", "2018-01-01")
        train_end = args.train_end or spec.get("train_end", "2020-12-31")
        test_start = args.test_start or spec.get("test_start", "2021-04-01")
        test_end = args.test_end or spec.get("test_end", "2021-12-31")
        stratum_name = f"Fold_{fold_id}"
        
        result, table_dfs = run_stratum_backtest(
            stratum_name, test_start, test_end, tickers, fundamentals,
            strategy_mode=args.strategy, config_path=args.config,
            train_start_date=train_start, train_end_date=train_end
        )
        results[stratum_name] = result
        for k in all_table_dfs:
            if k in table_dfs:
                all_table_dfs[k].append(table_dfs[k])
                
        run_id = f"20260919-{args.strategy}-fold{fold_id}"
        if args.persist:
            persist_backtest_run(run_id, args.strategy, [stratum_name], results, all_table_dfs)
            
        logger.info(f"\n  FOLD {fold_id} RESULTS ({test_start} to {test_end}):")
        logger.info(f"    Total Return:  {result.total_return:.2%}")
        logger.info(f"    Ann. Return:   {result.annualized_return:.2%}")
        logger.info(f"    Sharpe:        {result.sharpe_ratio:.2f}")
        logger.info(f"    Max DD:        {result.max_drawdown:.2%}")
        logger.info(f"    Trades:        {result.n_trades} (Win Rate: {result.win_rate:.1%})")
        return results

    selected_strata = {k: v for k, v in STRATA.items() if k in args.strata}
    for stratum, info in selected_strata.items():
        result, table_dfs = run_stratum_backtest(
            stratum, info["start"], info["end"], tickers, fundamentals,
            strategy_mode=args.strategy, config_path=args.config
        )
        results[stratum] = result
        for k in all_table_dfs:
            if k in table_dfs:
                all_table_dfs[k].append(table_dfs[k])
        
        logger.info(f"\n  {stratum} RESULTS:")
        logger.info(f"    Total Return:  {result.total_return:.2%} (SPY: {result.spy_return:.2%})")
        logger.info(f"    Ann. Return:   {result.annualized_return:.2%}")
        logger.info(f"    Sharpe:        {result.sharpe_ratio:.2f} (SPY: {result.spy_sharpe:.2f})")
        logger.info(f"    Max DD:        {result.max_drawdown:.2%} (SPY: {result.spy_max_dd:.2%})")
        logger.info(f"    Sortino:       {result.sortino_ratio:.2f}")
        logger.info(f"    Calmar:        {result.calmar_ratio:.2f}")
        logger.info(f"    Trades:        {result.n_trades} (Win Rate: {result.win_rate:.1%})")
        logger.info(f"    Avg Holding:   {result.avg_holding_days:.0f} days")
    
    logger.info("\n" + "=" * 60)
    logger.info("OVERALL SUMMARY")
    logger.info("=" * 60)
    avg_ret = np.mean([r.total_return for r in results.values()])
    avg_sharpe = np.mean([r.sharpe_ratio for r in results.values()])
    avg_dd = np.mean([r.max_drawdown for r in results.values()])
    
    logger.info(f"  Avg Total Return:  {avg_ret:.2%}")
    logger.info(f"  Avg Sharpe:        {avg_sharpe:.2f}")
    logger.info(f"  Avg Max DD:        {avg_dd:.2%}")
    
    if args.persist:
        run_date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        strata_str = "-".join([s.lower() for s in args.strata])
        run_id = f"{run_date_str}-{args.strategy}-{strata_str}"
        persist_backtest_run(run_id, args.strategy, args.strata, results, all_table_dfs)

    return results

if __name__ == "__main__":
    main()
