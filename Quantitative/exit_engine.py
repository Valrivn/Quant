"""
Stage 3: Exit Engine — Disciplined Profit Harvest
Pure functions, type hints, dataclasses. No global state.
College-audited logging via college_event().
Degraded-registry pattern: missing data → degraded flag, not hard stop.
Checkpointed: save intermediate state for resume.

TAKE PROFIT: Market Price > 1.15 * IV_Fair (+15% premium)
HYSTERIA EXIT: Bullish Hype P95 OR T_t/W_t Swarm
STRUCTURAL BREAK: Gross Margin decay > 300bps YoY OR EVA <= 0
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yaml

from config.logging_config import college_event

logger = logging.getLogger(__name__)

CHECKPOINT_DIR = Path("data/checkpoints/exit_engine")
CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)

SECTOR_MAP = {
    "AAPL": "Technology", "MSFT": "Technology", "GOOGL": "Communication Services",
    "AMZN": "Consumer Discretionary", "META": "Communication Services",
    "NVDA": "Technology", "TSLA": "Consumer Discretionary",
    "JPM": "Financials", "V": "Financials", "JNJ": "Healthcare",
    "WMT": "Consumer Staples", "PG": "Consumer Staples", "MA": "Financials",
    "UNH": "Healthcare", "HD": "Consumer Discretionary",
    "DIS": "Communication Services", "PYPL": "Financials", "ADBE": "Technology",
    "NFLX": "Communication Services", "CRM": "Technology", "INTC": "Technology",
    "CSCO": "Technology", "PFE": "Healthcare", "TMO": "Healthcare",
    "ABBV": "Healthcare", "ACN": "Technology", "COST": "Consumer Staples",
}

@dataclass
class ExitSignal:
    """Exit signal for a single position."""
    ticker: str
    timestamp: datetime
    current_price: float
    iv_fair: float              # IV_P50 (fair value)
    iv_p25: float               # Entry reference
    
    # Take Profit
    take_profit_triggered: bool = False
    take_profit_level: float = 0.0
    take_profit_pct: float = 0.0
    
    # Hysteria Exit
    hysteria_triggered: bool = False
    hype_p95: Optional[float] = None
    t_t_swarm: bool = False
    w_t_swarm: bool = False
    
    # Structural Break
    structural_break_triggered: bool = False
    margin_decay_bps: Optional[float] = None
    eva: Optional[float] = None
    eva_positive: bool = True
    
    # Final decision
    exit_triggered: bool = False
    exit_reason: str = ""
    exit_type: str = ""         # "TAKE_PROFIT", "HYSTERIA", "STRUCTURAL_BREAK"
    degraded: bool = False
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "timestamp": self.timestamp.isoformat(),
            "current_price": self.current_price,
            "iv_fair": self.iv_fair,
            "iv_p25": self.iv_p25,
            "take_profit_triggered": self.take_profit_triggered,
            "take_profit_level": self.take_profit_level,
            "take_profit_pct": self.take_profit_pct,
            "hysteria_triggered": self.hysteria_triggered,
            "hype_p95": self.hype_p95,
            "t_t_swarm": self.t_t_swarm,
            "w_t_swarm": self.w_t_swarm,
            "structural_break_triggered": self.structural_break_triggered,
            "margin_decay_bps": self.margin_decay_bps,
            "eva": self.eva,
            "eva_positive": self.eva_positive,
            "exit_triggered": self.exit_triggered,
            "exit_reason": self.exit_reason,
            "exit_type": self.exit_type,
            "degraded": self.degraded,
            "warnings": self.warnings,
        }

@dataclass
class ExitCheckpoint:
    tickers_done: List[str] = field(default_factory=list)
    signals: Dict[str, Dict] = field(default_factory=dict)
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

def load_position_sizing_config() -> Dict[str, Any]:
    path = Path("config/position_sizing.yaml")
    with path.open() as f:
        return yaml.safe_load(f)

def load_dcf_results() -> Dict[str, Any]:
    path = Path("data/checkpoints/dcf_screen/dcf_screen_results.json")
    if not path.exists():
        return {}
    with path.open() as f:
        return json.load(f)

def load_entry_signals() -> Dict[str, Any]:
    path = Path("data/checkpoints/entry_timing/entry_signals.json")
    if not path.exists():
        return {}
    with path.open() as f:
        return json.load(f)

def load_financial_inputs(ticker: str) -> Dict[str, Any]:
    """Load latest financial data for margin/EVA computation."""
    sec_path = Path(f"data/source/sec/companyfacts/{ticker}.json")
    if not sec_path.exists():
        return {"degraded": True, "warnings": ["SEC data missing"]}
    
    try:
        with sec_path.open() as f:
            data = json.load(f)
        facts = data.get("facts", {}).get("us-gaap", {})
        
        # Gross Profit
        gp = facts.get("GrossProfit", {}).get("units", {}).get("USD", [])
        gross_profit = gp[-1]["val"] if gp else None
        
        # Revenue
        rev = facts.get("Revenues", {}).get("units", {}).get("USD", [])
        revenue = rev[-1]["val"] if rev else None
        
        # Prior year gross profit (for YoY)
        if len(gp) >= 2:
            gross_profit_prior = gp[-2]["val"]
        else:
            gross_profit_prior = None
        if len(rev) >= 2:
            revenue_prior = rev[-2]["val"]
        else:
            revenue_prior = None
        
        # EBIT (for EVA)
        ebit = facts.get("OperatingIncomeLoss", {}).get("units", {}).get("USD", [])
        ebit_val = ebit[-1]["val"] if ebit else None
        
        # Tax rate
        tax = facts.get("IncomeTaxExpenseBenefit", {}).get("units", {}).get("USD", [])
        pretax = facts.get("IncomeLossFromContinuingOperationsBeforeTax", {}).get("units", {}).get("USD", [])
        tax_rate = None
        if tax and pretax and pretax[-1]["val"] != 0:
            tax_rate = abs(tax[-1]["val"] / pretax[-1]["val"])
            tax_rate = min(max(tax_rate, 0.15), 0.35)
        
        # Invested Capital (approximation)
        debt = facts.get("LongTermDebt", {}).get("units", {}).get("USD", [])
        debt_short = facts.get("ShortTermDebt", {}).get("units", {}).get("USD", [])
        total_debt = 0.0
        if debt:
            total_debt += debt[-1]["val"]
        if debt_short:
            total_debt += debt_short[-1]["val"]
        
        cash = facts.get("CashAndCashEquivalentsAtCarryingValue", {}).get("units", {}).get("USD", [])
        cash_val = cash[-1]["val"] if cash else 0.0
        
        shares = facts.get("CommonStockSharesOutstanding", {}).get("units", {}).get("shares", [])
        shares_val = shares[-1]["val"] if shares else None
        
        return {
            "gross_profit": gross_profit,
            "revenue": revenue,
            "gross_profit_prior": gross_profit_prior,
            "revenue_prior": revenue_prior,
            "ebit": ebit_val,
            "tax_rate": tax_rate,
            "total_debt": total_debt,
            "cash": cash_val,
            "shares_outstanding": shares_val,
            "degraded": False,
            "warnings": [],
        }
    except Exception as e:
        return {"degraded": True, "warnings": [f"SEC parse error: {e}"]}

def compute_gross_margin_decay_bps(
    gross_profit: Optional[float],
    revenue: Optional[float],
    gross_profit_prior: Optional[float],
    revenue_prior: Optional[float],
) -> Optional[float]:
    """Compute gross margin decay in basis points YoY.
    
    Margin = Gross Profit / Revenue
    Decay = (Margin_prior - Margin_current) * 10000
    """
    if not all(v is not None and v > 0 for v in [gross_profit, revenue, gross_profit_prior, revenue_prior]):
        return None
    
    margin_current = gross_profit / revenue
    margin_prior = gross_profit_prior / revenue_prior
    decay_bps = (margin_prior - margin_current) * 10000
    return float(decay_bps)

def compute_eva(
    ebit: Optional[float],
    tax_rate: Optional[float],
    total_debt: float,
    cash: float,
    shares_outstanding: Optional[float],
    current_price: float,
    wacc: float = 0.09,
) -> Optional[float]:
    """Economic Value Added = NOPAT - (Invested Capital * WACC)
    
    NOPAT = EBIT * (1 - tax_rate)
    Invested Capital ≈ Total Debt + Equity - Cash
    Equity ≈ Market Cap = Price * Shares
    """
    if not all(v is not None for v in [ebit, tax_rate, shares_outstanding]):
        return None
    
    nopat = ebit * (1 - tax_rate)
    market_cap = current_price * shares_outstanding
    invested_capital = total_debt + market_cap - cash
    
    if invested_capital <= 0:
        return None
    
    eva = nopat - (invested_capital * wacc)
    return float(eva)

def load_hype_data(ticker: str) -> Tuple[Optional[float], bool, bool]:
    """Load hype/swarm indicators.
    
    Returns: (hype_p95, t_t_swarm, w_t_swarm)
    - hype_p95: 95th percentile of grassroots sentiment (proxy for bubble)
    - t_t_swarm: Top-down macro swarm (e.g., analyst upgrade cluster)
    - w_t_swarm: Wall Street swarm (e.g., media coverage spike)
    """
    # Placeholder - would integrate with sentiment/macro data
    # For now, return None/False/False
    return None, False, False

def evaluate_take_profit(
    current_price: float,
    iv_fair: float,
    iv_p25: float,
) -> Tuple[bool, float, float]:
    """TAKE PROFIT: Market Price > 1.15 * IV_Fair (+15% premium).
    
    Returns: (triggered, level, pct_above_fair)
    """
    take_profit_level = iv_fair * 1.15
    pct_above_fair = (current_price - iv_fair) / iv_fair if iv_fair > 0 else 0
    triggered = current_price > take_profit_level
    return triggered, take_profit_level, pct_above_fair

def evaluate_hysteria_exit(
    hype_p95: Optional[float],
    t_t_swarm: bool,
    w_t_swarm: bool,
    current_price: float,
    iv_fair: float,
) -> Tuple[bool, str]:
    """HYSTERIA EXIT: Bullish Hype P95 OR T_t/W_t Swarm.
    
    Also check if price is significantly above fair value (bubble territory).
    """
    if hype_p95 is not None and hype_p95 > 0.95:
        return True, f"Hype P95 exceeded: {hype_p95:.2f}"
    
    if t_t_swarm or w_t_swarm:
        return True, f"Swarm detected: T_t={t_t_swarm}, W_t={w_t_swarm}"
    
    # Also trigger if price > 1.25 * IV_Fair (extreme overvaluation)
    if current_price > iv_fair * 1.25:
        return True, f"Extreme overvaluation: {current_price/iv_fair:.2f}x IV_Fair"
    
    return False, ""

def evaluate_structural_break(
    margin_decay_bps: Optional[float],
    eva: Optional[float],
) -> Tuple[bool, str]:
    """STRUCTURAL BREAK: Gross Margin decay > 300bps YoY OR EVA <= 0."""
    reasons = []
    
    if margin_decay_bps is not None and margin_decay_bps > 300:
        reasons.append(f"Gross margin decay {margin_decay_bps:.0f}bps > 300bps")
    
    if eva is not None and eva <= 0:
        reasons.append(f"EVA <= 0 ({eva:,.0f})")
    
    triggered = len(reasons) > 0
    reason = "; ".join(reasons) if triggered else ""
    return triggered, reason

def evaluate_exit_signal(
    ticker: str,
    current_price: float,
    dcf_result: Dict[str, Any],
    entry_signal: Optional[Dict[str, Any]] = None,
) -> ExitSignal:
    """Evaluate complete exit signal for a position."""
    iv_fair = dcf_result.get("iv_p50", 0)  # Fair value = IV_P50
    iv_p25 = dcf_result.get("iv_p25", 0)
    
    warnings = []
    
    # Take Profit
    tp_triggered, tp_level, tp_pct = evaluate_take_profit(current_price, iv_fair, iv_p25)
    
    # Hysteria Exit
    hype_p95, t_t_swarm, w_t_swarm = load_hype_data(ticker)
    hysteria_triggered, hysteria_reason = evaluate_hysteria_exit(
        hype_p95, t_t_swarm, w_t_swarm, current_price, iv_fair
    )
    
    # Structural Break
    financials = load_financial_inputs(ticker)
    margin_decay = None
    eva_val = None
    
    if not financials.get("degraded", True):
        margin_decay = compute_gross_margin_decay_bps(
            financials.get("gross_profit"),
            financials.get("revenue"),
            financials.get("gross_profit_prior"),
            financials.get("revenue_prior"),
        )
        eva_val = compute_eva(
            financials.get("ebit"),
            financials.get("tax_rate"),
            financials.get("total_debt", 0),
            financials.get("cash", 0),
            financials.get("shares_outstanding"),
            current_price,
        )
    else:
        warnings.extend(financials.get("warnings", []))
    
    structural_triggered, structural_reason = evaluate_structural_break(margin_decay, eva_val)
    
    # Determine final exit
    exit_triggered = False
    exit_reason = ""
    exit_type = ""
    
    # Priority: Structural Break > Hysteria > Take Profit
    if structural_triggered:
        exit_triggered = True
        exit_reason = structural_reason
        exit_type = "STRUCTURAL_BREAK"
    elif hysteria_triggered:
        exit_triggered = True
        exit_reason = hysteria_reason
        exit_type = "HYSTERIA"
    elif tp_triggered:
        exit_triggered = True
        exit_reason = f"Take profit at {tp_pct:.1%} above IV_Fair"
        exit_type = "TAKE_PROFIT"
    else:
        exit_reason = "No exit condition met"
    
    degraded = financials.get("degraded", False) or hype_p95 is None
    
    signal = ExitSignal(
        ticker=ticker,
        timestamp=datetime.now(timezone.utc),
        current_price=current_price,
        iv_fair=iv_fair,
        iv_p25=iv_p25,
        take_profit_triggered=tp_triggered,
        take_profit_level=tp_level,
        take_profit_pct=tp_pct,
        hysteria_triggered=hysteria_triggered,
        hype_p95=hype_p95,
        t_t_swarm=t_t_swarm,
        w_t_swarm=w_t_swarm,
        structural_break_triggered=structural_triggered,
        margin_decay_bps=margin_decay,
        eva=eva_val,
        eva_positive=eva_val > 0 if eva_val is not None else True,
        exit_triggered=exit_triggered,
        exit_reason=exit_reason,
        exit_type=exit_type,
        degraded=degraded,
        warnings=warnings,
    )
    
    college_event(
        "exit_signal_evaluated",
        ticker=ticker,
        price=current_price,
        iv_fair=iv_fair,
        exit_triggered=exit_triggered,
        exit_type=exit_type,
        take_profit_pct=tp_pct,
        margin_decay_bps=margin_decay,
        eva=eva_val,
        degraded=degraded,
    )
    
    return signal

def run_exit_engine(
    tickers: List[str],
    current_prices: Dict[str, float],
) -> Dict[str, ExitSignal]:
    """Main entry point: run exit engine on current positions."""
    dcf_results = load_dcf_results()
    entry_signals = load_entry_signals()
    
    config = load_position_sizing_config()
    checkpoint = ExitCheckpoint()
    signals = {}
    
    for ticker in tickers:
        if ticker not in current_prices:
            logger.warning(f"No current price for {ticker}")
            continue
        
        if ticker not in dcf_results:
            logger.warning(f"No DCF result for {ticker}")
            continue
        
        entry_data = entry_signals.get(ticker)
        signal = evaluate_exit_signal(
            ticker,
            current_prices[ticker],
            dcf_results[ticker],
            entry_data,
        )
        signals[ticker] = signal
        checkpoint.tickers_done.append(ticker)
        checkpoint.signals[ticker] = signal.to_dict()
        checkpoint.updated_at = datetime.now(timezone.utc).isoformat()
    
    # Save checkpoint
    path = CHECKPOINT_DIR / "exit_signals.json"
    with path.open("w") as f:
        json.dump({t: s.to_dict() for t, s in signals.items()}, f, indent=2, default=str)
    
    triggered = [t for t, s in signals.items() if s.exit_triggered]
    logger.info(f"Exit Engine: {len(triggered)}/{len(signals)} exit triggered")
    college_event("exit_engine_batch_complete", triggered=triggered, total=len(signals))
    
    return signals

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    # Synthetic test
    mock_dcf = {
        "iv_p50": 150.0,
        "iv_p25": 135.0,
        "iv_p10": 120.0,
    }
    
    # Test 1: Take profit
    signal = evaluate_exit_signal("TEST", 180.0, mock_dcf)
    print(f"Price 180 (1.2x IV_Fair): exit={signal.exit_triggered}, type={signal.exit_type}, reason={signal.exit_reason}")
    
    # Test 2: No exit
    signal = evaluate_exit_signal("TEST", 140.0, mock_dcf)
    print(f"Price 140 (0.93x IV_Fair): exit={signal.exit_triggered}, type={signal.exit_type}, reason={signal.exit_reason}")
    
    # Test 3: Structural break (simulated)
    signal = evaluate_exit_signal("TEST", 140.0, {
        **mock_dcf,
        "gross_profit": 40_000_000_000,
        "revenue": 100_000_000_000,
        "gross_profit_prior": 45_000_000_000,
        "revenue_prior": 100_000_000_000,
        "ebit": 25_000_000_000,
        "tax_rate": 0.21,
        "total_debt": 50_000_000_000,
        "cash": 20_000_000_000,
        "shares_outstanding": 1_000_000_000,
    })
    eva_str = f"{signal.eva:,.0f}" if signal.eva is not None else "N/A"
    margin_str = f"{signal.margin_decay_bps:.0f}" if signal.margin_decay_bps is not None else "N/A"
    print(f"Structural test: exit={signal.exit_triggered}, type={signal.exit_type}, margin_decay={margin_str}bps, eva={eva_str}")