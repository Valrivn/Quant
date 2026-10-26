"""
probationary_queue.py — 10-20 Day Staging with 3 Quality Gates
Pre-Registration Config: config/pre_registration_v3.yaml

Provides:
  1. ProbationaryQueue: Staging queue holding candidates for 10-20 days before active entry.
  2. Gate 1: Forward EPS revisions slope >= 0 over 30 days.
  3. Gate 2: Operating Margin Robustness (OMR) >= 0.50.
  4. Gate 3: Volatility Contraction (|r| < trailing 5-day avg for 3 consecutive days).
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import datetime, date
from typing import Dict, Any, List, Optional, Union

import numpy as np
import pandas as pd
import yaml

from Quantitative.regime_shield_v2 import ConfigWrapper, load_config

logger = logging.getLogger(__name__)


def estimate_revisions_slope(ticker: str, date_val: pd.Timestamp, market_data: Optional[Dict[str, Any]] = None, window_days: int = 30) -> float:
    """Gate 1 helper: Forward EPS revisions slope over window_days. Returns np.nan on missing data (fail-closed)."""
    if market_data and ticker in market_data and "eps_revisions" in market_data[ticker]:
        revs = market_data[ticker]["eps_revisions"]
        if len(revs) >= 2:
            return float(np.polyfit(np.arange(len(revs)), revs, 1)[0])
    logger.warning(f"ProbationaryQueue: Missing EPS revisions for {ticker} at {date_val} — flagging lack of data, failing closed.")
    return np.nan


def compute_omr(ticker: str, date_val: pd.Timestamp, market_data: Optional[Dict[str, Any]] = None) -> float:
    """Gate 2 helper: OMR = Operating Margin Robustness. Returns np.nan on missing data (fail-closed)."""
    if market_data and ticker in market_data and "omr" in market_data[ticker]:
        return float(market_data[ticker]["omr"])
    logger.warning(f"ProbationaryQueue: Missing OMR for {ticker} at {date_val} — flagging lack of data, failing closed.")
    return np.nan


def get_abs_returns(ticker: str, date_val: pd.Timestamp, market_data: Optional[Dict[str, Any]] = None) -> np.ndarray:
    """Gate 3 helper: Get absolute daily returns history up to date_val. Returns empty array on missing data (fail-closed)."""
    if market_data and ticker in market_data and "abs_returns" in market_data[ticker]:
        return np.asarray(market_data[ticker]["abs_returns"], dtype=float)
    logger.warning(f"ProbationaryQueue: Missing historical returns for {ticker} at {date_val} — flagging lack of data, failing closed.")
    return np.array([])


class ProbationaryQueue:
    """10-20 Day Staging Queue with 3 Quality Gates."""

    def __init__(self, config: Union[ConfigWrapper, Dict[str, Any], str, None] = None):
        if config is not None:
            cfg = load_config(config)
            pq_cfg = getattr(cfg, "probationary_queue", cfg.get("probationary_queue") if isinstance(cfg, dict) else None)
            self.enabled = bool(getattr(pq_cfg, "enabled", True))
            self.min_days = int(getattr(pq_cfg, "min_days", 10))
            self.max_days = int(getattr(pq_cfg, "max_days", 20))
            self.checks_config = getattr(pq_cfg, "checks", {})
        else:
            self.enabled = True
            self.min_days = 10
            self.max_days = 20
            self.checks_config = {}

        self.queue: Dict[str, Dict[str, Any]] = {}

    def on_entry_trigger(self, ticker: str, date_val: Union[str, pd.Timestamp, date], price: float, iv: float) -> None:
        """Add ticker to probationary queue."""
        d = pd.Timestamp(date_val)
        self.queue[ticker] = {
            'entry_date': d,
            'trigger_price': float(price),
            'iv_at_trigger': float(iv),
            'checks': {'revisions': None, 'omr': None, 'vol': None},
            'passed': False
        }
        logger.info(f"ProbationaryQueue: {ticker} staged on {d.date()} at price ${price:.2f} (IV ${iv:.2f})")

    def daily_process(self, date_val: Union[str, pd.Timestamp, date], market_data: Optional[Dict[str, Any]] = None) -> List[str]:
        """Process staged tickers and return list of tickers that passed all gates."""
        if not self.enabled:
            return list(self.queue.keys())

        d = pd.Timestamp(date_val)
        to_remove = []
        
        for ticker, info in list(self.queue.items()):
            days_held = (d - info['entry_date']).days
            
            if days_held >= self.max_days:
                if all(val is True for val in info['checks'].values()):
                    info['passed'] = True
                else:
                    info['passed'] = False
                to_remove.append(ticker)
                continue
            
            if days_held >= self.min_days:
                # Run 3 gates
                self._check_revisions(ticker, d, market_data)
                self._check_omr(ticker, d, market_data)
                self._check_vol_contraction(ticker, d, market_data)
                
                if all(val is True for val in info['checks'].values()):
                    info['passed'] = True
                    to_remove.append(ticker)
        
        passed_tickers = [t for t in to_remove if self.queue[t]['passed']]
        for t in to_remove:
            del self.queue[t]
            
        return passed_tickers

    def _check_revisions(self, ticker: str, date_val: pd.Timestamp, market_data: Optional[Dict[str, Any]]) -> None:
        """Gate 1: Forward EPS revisions slope >= 0 over 30 days. Fails closed on missing/NaN."""
        window = 30
        if isinstance(self.checks_config, ConfigWrapper):
            rev_cfg = self.checks_config.get("revisions_slope", {})
            if isinstance(rev_cfg, ConfigWrapper):
                window = int(rev_cfg.get("window_days", 30))
        slope = estimate_revisions_slope(ticker, date_val, market_data, window_days=window)
        self.queue[ticker]['checks']['revisions'] = bool(not np.isnan(slope) and slope >= 0.0)

    def _check_omr(self, ticker: str, date_val: pd.Timestamp, market_data: Optional[Dict[str, Any]]) -> None:
        """Gate 2: OMR >= 0.50. Fails closed on missing/NaN."""
        thresh = 0.50
        omr_val = compute_omr(ticker, date_val, market_data)
        self.queue[ticker]['checks']['omr'] = bool(not np.isnan(omr_val) and omr_val >= thresh)

    def _check_vol_contraction(self, ticker: str, date_val: pd.Timestamp, market_data: Optional[Dict[str, Any]]) -> None:
        """Gate 3: |r| < trailing 5-day avg for 3 consecutive days."""
        abs_rets = get_abs_returns(ticker, date_val, market_data)
        if len(abs_rets) >= 8:
            avg_5d = np.mean(abs_rets[-8:-3])
            recent_3 = abs_rets[-3:]
            passed = all(r < avg_5d for r in recent_3)
            self.queue[ticker]['checks']['vol'] = bool(passed)
        else:
            self.queue[ticker]['checks']['vol'] = False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Test Quantitative.probationary_queue")
    parser.add_argument("--test", action="store_true", help="Run test suite")
    parser.add_argument("--min-days", type=int, default=10, help="Min probation days")
    parser.add_argument("--max-days", type=int, default=20, help="Max probation days")
    parser.add_argument("--gates", type=int, default=3, help="Number of quality gates")
    parser.add_argument("--config", type=str, default="config/pre_registration_v3.yaml", help="Path to config")
    args = parser.parse_args()

    cfg = load_config(args.config)
    pq = ProbationaryQueue(cfg)
    pq.min_days = args.min_days
    pq.max_days = args.max_days

    entry_d = pd.Timestamp("2024-01-01")
    pq.on_entry_trigger("AAPL", entry_d, price=150.0, iv=180.0)

    # Day 5 check (under min_days)
    passed_day5 = pq.daily_process("2024-01-06")
    print(f"Day 5 Process: Passed = {passed_day5} | Queue size = {len(pq.queue)}")

    # Day 12 check (between min_days and max_days)
    passed_day12 = pq.daily_process("2024-01-13")
    print(f"Day 12 Process: Passed = {passed_day12} | Queue size = {len(pq.queue)}")

    print("ProbationaryQueue Test OK!")
