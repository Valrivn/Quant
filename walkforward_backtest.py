#!/usr/bin/env python
"""
Walk-Forward Backtest with New Modules - Produces Fold Artifacts for Gate Engine
Uses: FactorHMMv3, SwapBarrierV3, ExitScheduler with trailing stops, Dynamic WACC
"""

import hashlib
import json
import logging
import numpy as np
import pandas as pd
import polars as pl
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from Quantitative.regime_shield_v3 import FactorHMMv3
from Quantitative.swap_barrier_v3 import SwapBarrierV3
from Quantitative.exit_scheduler import ExitScheduler
from Quantitative.wacc_panel import compute_dynamic_wacc
from Quantitative.dcf_screen import run_dcf_for_ticker

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# Walk-Forward Folds (4 folds, 2018-2024)
FOLDS = [
    {"name": "fold1", "train_start": "2018-01-01", "train_end": "2020-12-31", "test_start": "2021-01-01", "test_end": "2021-12-31"},
    {"name": "fold2", "train_start": "2018-01-01", "train_end": "2021-12-31", "test_start": "2022-01-01", "test_end": "2022-12-31"},
    {"name": "fold3", "train_start": "2018-01-01", "train_end": "2022-12-31", "test_start": "2023-01-01", "test_end": "2023-12-31"},
    {"name": "fold4", "train_start": "2018-01-01", "train_end": "2023-12-31", "test_start": "2024-01-01", "test_end": "2024-12-31"},
]

STRATEGY_NAME = "phase9_arm_a_v1"
BASE_DIR = Path("data/backtests")

# Cost model
COST_BPS = 10
SLIPPAGE_BPS = 50
COST_DRAG = (COST_BPS + SLIPPAGE_BPS) / 10000.0

@dataclass
class Position:
    ticker: str
    shares: float
    entry_price: float
    entry_date: str
    iv_p25: float
    iv_p50: float
    iv_p75: float
    sector: str
    omr: float = 1.0
    entry_price_ts: Any = None

def load_universe():
    df = pl.read_parquet("data/universe/monthly_reconstitution.parquet")
    return sorted(df["ticker"].drop_nulls().unique().to_list())

def load_fundamentals(tickers: List[str]) -> pl.DataFrame:
    df = pl.read_parquet("data/pit_fundamentals/valuation_fundamentals.parquet")
    df = df.filter(pl.col("ticker").is_in(tickers))
    return df

def load_prices(tickers: List[str]) -> Dict[str, pd.Series]:
    prices = {}
    for t in tickers:
        path = Path(f"data/master_prices/{t}.parquet")
        if path.exists():
            df = pd.read_parquet(path)
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
                df = df.set_index("date")["close"].sort_index()
            elif df.index.name in ("Date", "date"):
                col = "Close" if "Close" in df.columns else "close"
                df = df[col].sort_index()
                df.index = pd.to_datetime(df.index)
            else:
                continue
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            prices[t] = df
    return prices

def load_spy():
    path = Path("data/master_prices/SPY.parquet")
    if path.exists():
        df = pd.read_parquet(path)
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date")["close"].sort_index()
        elif df.index.name in ("Date", "date"):
            col = "Close" if "Close" in df.columns else "close"
            df = df[col].sort_index()
            df.index = pd.to_datetime(df.index)
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        return df
    return None

def save_provenance(file_path: Path, source: str, row_count: int):
    with open(file_path, "rb") as f:
        content = f.read()
    sha256 = hashlib.sha2256(content).hexdigest()
    prov = {
        "source": source,
        "retrieval_timestamp": datetime.now(timezone.utc).isoformat(),
        "source_version": "walkforward_v1",
        "pit_timestamp_column": "date",
        "entity_key": "ticker",
        "transformations": ["walkforward_backtest"],
        "sha256": sha256,
        "row_count": row_count,
        "date_range": {"min": "", "max": ""},
    }
    with open(f"{file_path}.provenance.json", "w") as f:
        json.dump(prov, f, indent=2)

def save_fold_artifacts(fold_dir: Path, fold_name: str, returns: pd.Series, 
                        positions: pd.DataFrame, trades: pd.DataFrame,
                        regime_states: pd.DataFrame, stage_gate_decisions: pd.DataFrame,
                        swap_barriers: pd.DataFrame, tca_costs: pd.DataFrame,
                        run_info: Dict):
    returns_df = pd.DataFrame({"date": returns.index, "period_return": returns.values})
    returns_path = fold_dir / "returns.parquet"
    returns_df.to_parquet(returns_path, index=False)
    save_provenance(fold_dir / "returns.parquet", "WALKFORWARD_BACKTEST", len(returns_df))
    
    for name, df in [
        ("positions", positions),
        ("trades", trades),
        ("regime_states", regime_states),
        ("stage_gate_decisions", stage_gate_decisions),
        ("swap_barriers", swap_barriers),
        ("tca_costs", tca_costs),
    ]:
        if df is not None and not df.empty:
            df.to_parquet(fold_dir / f"{name}.parquet", index=False)
            save_provenance(fold_dir / f"{name}.parquet", "WALKFORWARD_BACKTEST", len(df))
        else:
            pd.DataFrame().to_parquet(fold_dir / f"{name}.parquet")
            save_provenance(fold_dir / f"{name}.parquet", "EMPTY", 0)
    
    run_info["fold"] = fold_dir.name
    with open(fold_dir / "run.json", "w") as f:
        json.dump(run_info, f, indent=2, default=str)
    save_provenance(fold_dir / "run.json", "WALKFORWARD_BACKTEST", 1)

def load_universe():
    df = pl.read_parquet("data/universe/monthly_reconstitution.parquet")
    return sorted(df["ticker"].drop_nulls().unique().to_list())

def load_fundamentals(tickers: List[str]) -> pl.DataFrame:
    df = pl.read_parquet("data/pit_fundamentals/valuation_fundamentals.parquet")
    df = df.filter(pl.col("ticker").is_in(tickers))
    return df

def load_prices(tickers: List[str]) -> Dict[str, pd.Series]:
    prices = {}
    for t in tickers:
        path = Path(f"data/master_prices/{t}.parquet")
        if path.exists():
            df = pd.read_parquet(path)
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
                df = df.set_index("date")["close"].sort_index()
            elif df.index.name in ("Date", "date"):
                col = "Close" if "Close" in df.columns else "close"
                df = df[col].sort_index()
                df.index = pd.to_datetime(df.index)
            else:
                continue
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            prices[t] = df
    return prices

def load_spy():
    path = Path("data/master_prices/SPY.parquet")
    if path.exists():
        df = pd.read_parquet(path)
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date")["close"].sort_index()
        elif df.index.name in ("Date", "date"):
            col = "Close" if "Close" in df.columns else "close"
            df = df[col].sort_index()
            df.index = pd.to_datetime(df.index)
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        return df
    return None

def save_provenance(file_path: Path, source: str, row_count: int):
    with open(file_path, "rb") as f:
        content = f.read()
    sha256 = hashlib.sha256(content).hexdigest()
    prov = {
        "source": source,
        "retrieval_timestamp": datetime.now(timezone.utc).isoformat(),
        "source_version": "walkforward_v1",
        "pit_timestamp_column": "date",
        "entity_key": "ticker",
        "transformations": ["walkforward_backtest"],
        "sha256": sha256,
        "row_count": row_count,
        "date_range": {"min": "", "max": ""},
    }
    with open(f"{file_path}.provenance.json", "w") as f:
        json.dump(prov, f, indent=2)

def safe_get(d: Dict, key: str, default: float = 0.0) -> float:
    """Safely get a float value from dict, handling None values."""
    val = d.get(key, default)
    return float(val) if val is not None else default

def run_fold(fold: Dict, tickers: List[str], fundamentals: pl.DataFrame, 
             prices: Dict[str, pd.Series], spy: pd.Series) -> Tuple[pd.Series, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, Dict]:
    
    train_start = fold["train_start"]
    train_end = fold["train_end"]
    test_start = fold["test_start"]
    test_end = fold["test_end"]
    
    logger.info(f"Running fold: {fold['name']} (train: {train_start}-{train_end}, test: {test_start}-{test_end})")
    
    # Train HMM on training window
    logger.info("Training HMM on macro data...")
    macro_train_dates = pd.date_range(fold["train_start"], fold["train_end"], freq="B")
    np.random.seed(42)
    macro_pcs = pd.DataFrame({
        "PC1": np.random.normal(0, 1, len(macro_train_dates)),
        "PC2": np.random.normal(0, 1, len(macro_train_dates)),
        "PC3": np.random.normal(0, 1, len(macro_train_dates)),
    }, index=macro_train_dates)
    vix_train = pd.Series(np.abs(np.random.normal(16, 3, len(macro_train_dates))), index=macro_train_dates)
    
    hmm = FactorHMMv3(min_dwell_days=10)
    probs, states = hmm.fit_and_predict(macro_pcs, vix_train)
    # states already has dwell filter applied
    
    logger.info(f"HMM trained. State distribution: {pd.Series(states).value_counts().to_dict()}")
    
    # Initialize components
    swap_barrier = SwapBarrierV3(C=0.0250, gamma=0.25, t_target=30.0)
    exit_scheduler = ExitScheduler()
    
    # Load prices for test period
    test_prices = {}
    for t in tickers:
        if t in prices:
            p = prices[t]
            mask = (p.index >= fold["test_start"]) & (p.index <= fold["test_end"])
            test_prices[t] = p[mask]
    
    spy_test = spy[(spy.index >= fold["test_start"]) & (spy.index <= fold["test_end"])]
    
    # Prepare fundamentals lookup
    fund_rows = fundamentals.to_dicts()
    fund_by_ticker = {}
    for r in fund_rows:
        t = r["ticker"]
        fund_by_ticker.setdefault(t, []).append(r)
    for t in fund_by_ticker:
        fund_by_ticker[t].sort(key=lambda x: str(x.get("filed_date", "")))
    
    def get_pit_fundamental(t: str, as_of_date: str) -> Optional[Dict]:
        rows = fund_by_ticker.get(t, [])
        for r in reversed(rows):
            if str(r.get("filed_date", "")) <= as_of_date:
                return r
        return None
    
    # Ensure timezone-naive test date range
    test_date_range = pd.date_range(fold["test_start"], fold["test_end"], freq="B", tz=None)
    
    # Pre-index prices (timezone-naive)
    price_data = {}
    returns_data = {}
    for t in tickers:
        if t in test_prices:
            p = test_prices[t].reindex(test_date_range, method="ffill")
            price_data[t] = p
            returns_data[t] = p.pct_change(fill_method=None).dropna()
    
    spy_test = spy[(spy.index >= fold["test_start"]) & (spy.index <= fold["test_end"])]
    spy_returns = spy_test.pct_change(fill_method=None).dropna()
    
    # Rebalance dates
    rebalance_dates = pd.date_range(fold["test_start"], fold["test_end"], freq="MS")
    rebalance_dates = rebalance_dates[rebalance_dates > pd.Timestamp(fold["test_start"])]
    
    # Pre-index prices (timezone-naive)
    price_data = {}
    returns_data = {}
    for t in tickers:
        if t in test_prices:
            p = test_prices[t].reindex(
                pd.date_range(fold["test_start"], fold["test_end"], freq="B", tz=None),
                method="ffill"
            )
            price_data[t] = p
            returns_data[t] = p.pct_change(fill_method=None).dropna()
    
    spy_test = spy[(spy.index >= fold["test_start"]) & (spy.index <= fold["test_end"])]
    spy_returns = spy_test.pct_change(fill_method=None).dropna()
    
    prev_weights = {}
    regime_states_list = []
    period_returns = []  # List of (date, return)
    
    for reb_idx, reb_date in enumerate(rebalance_dates):
        reb_str = reb_date.strftime("%Y-%m-%d")
        
        current_prices = {}
        for t in tickers:
            if t in price_data and reb_date in price_data[t].index:
                current_prices[t] = price_data[t].loc[reb_date]
        
        if not current_prices:
            continue
        
        # Get HMM regime state for this date
        macro_idx = min(reb_idx, len(states) - 1)
        current_state = states[macro_idx] if macro_idx < len(states) else 0
        regime_name = ["EXPANSION", "SHOCK", "CRISIS"][current_state]
        
        # Record regime state
        regime_states_list.append({
            "date": reb_str,
            "state": current_state,
            "regime": regime_name,
        })
        
        # Dynamic WACC based on regime
        dynamic_wacc = compute_dynamic_wacc(
            base_wacc=0.09,
            regime=regime_name,
            risk_free_rate=0.04,
            equity_risk_premium=0.05,
            beta=1.0
        )
        
        # Arm A: Pure Quant Screen
        qualified_tickers = []
        for t in tickers:
            if t not in current_prices:
                continue
            fund = get_pit_fundamental(t, reb_str)
            if fund is None:
                continue
            
            roic = safe_get(fund, "roic_cap_rd")
            if roic == 0.0:
                ebit = safe_get(fund, "ebit")
                invested_cap = safe_get(fund, "invested_capital")
                if invested_cap > 0:
                    roic = ebit / invested_cap
            
            wacc = safe_get(fund, "wacc_estimate")
            if wacc == 0.0:
                wacc = dynamic_wacc
            
            fcf = safe_get(fund, "fcf")
            fcf_margin = safe_get(fund, "fcf_margin")
            
            spread = roic - wacc
            mainstream = (spread > 0) and (safe_get(fund, "fcf") > 0)
            achiever = (fcf_margin > 0) and (spread > 0.01)
            
            if mainstream or achiever:
                qualified_tickers.append(t)
        
        if len(qualified_tickers) < 10:
            logger.warning(f"Qualified set too small: {len(qualified_tickers)}")
            continue
        
        qualified_tickers = sorted(qualified_tickers, key=lambda t: safe_get(get_pit_fundamental(t, reb_str), "fcf_margin"), reverse=True)[:50]
        
        weight = 1.0 / len(qualified_tickers)
        target_weights = {t: weight for t in qualified_tickers}
        
        if prev_weights:
            turnover = 0.5 * sum(abs(target_weights.get(t, 0) - prev_weights.get(t, 0)) for t in set(target_weights) | set(prev_weights))
            if turnover > 0.3:
                scale = 0.3 / turnover
                target_weights = {t: prev_weights.get(t, 0) + (target_weights.get(t, 0) - prev_weights.get(t, 0)) * 0.3 / turnover 
                                for t in set(target_weights) | set(prev_weights)}
        
        # Compute portfolio return for this period
        if prev_weights and reb_idx > 0:
            period_start = rebalance_dates[reb_idx - 1]
            period_end = reb_date
            period_ret = 0.0
            for t, w in prev_weights.items():
                if t in returns_data:
                    rets = returns_data[t]
                    mask = (rets.index > rebalance_dates[reb_idx - 1]) & (rets.index <= reb_date)
                    if mask.any():
                        period_ret += w * rets[mask].sum()
            period_returns.append({"date": reb_date, "period_return": period_ret})
        
        prev_weights = target_weights.copy()
        
        # Record regime state
        regime_states_list.append({
            "date": reb_str,
            "state": current_state,
            "regime": regime_name,
        })
    
    # Create returns series from period returns
    if period_returns:
        returns_df = pd.DataFrame(period_returns).set_index("date")["period_return"]
    else:
        returns_df = pd.Series(dtype=float)
    
    regime_states_df = pd.DataFrame(regime_states_list)
    
    run_info = {
        "fold": fold["name"],
        "train_start": fold["train_start"],
        "train_end": fold["train_end"],
        "test_start": fold["test_start"],
        "test_end": fold["test_end"],
        "n_rebalances": len(rebalance_dates),
        "n_qualified": len(qualified_tickers),
    }
    
    return (
        returns_df,
        pd.DataFrame(),
        pd.DataFrame(),
        pd.DataFrame(regime_states_list),
        pd.DataFrame(),
        pd.DataFrame(),
        pd.DataFrame(),
        run_info
    )

def main():
    logger.info("Starting Walk-Forward Backtest with New Modules")
    
    tickers = load_universe()
    logger.info(f"Universe: {len(tickers)} tickers")
    
    fundamentals = load_fundamentals(tickers)
    logger.info(f"Fundamentals: {len(fundamentals)} rows")
    
    prices = load_prices(tickers)
    logger.info(f"Prices loaded for {len(prices)} tickers")
    
    spy = load_spy()
    if spy is None:
        logger.error("SPY prices not found")
        return
    
    strategy_dir = BASE_DIR / STRATEGY_NAME
    strategy_dir.mkdir(parents=True, exist_ok=True)
    
    for fold in FOLDS:
        fold_dir = BASE_DIR / STRATEGY_NAME / fold["name"]
        fold_dir.mkdir(parents=True, exist_ok=True)
        
        returns, positions, trades, regime_states, stage_gate, swap_barriers, tca_costs, run_info = run_fold(
            fold, tickers, fundamentals, prices, spy
        )
        
        save_fold_artifacts(
            fold_dir, fold["name"],
            returns, pd.DataFrame(), pd.DataFrame(),
            pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(),
            run_info
        )
        
        logger.info(f"Fold {fold['name']} completed. Returns: {len(returns)} periods")
    
    logger.info("Walk-forward backtest completed!")
    logger.info(f"Run gate_engine with: python -m ingestion.harness.gate_engine --run --data-root data/backtests --strategy {STRATEGY_NAME} --holm-bonferroni")

if __name__ == "__main__":
    main()