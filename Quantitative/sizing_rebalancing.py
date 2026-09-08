"""
Stage 4: Sizing & Rebalancing Engine — Portfolio Sizing & Macro Shield
Pure functions, type hints, dataclasses. No global state.
College-audited logging via college_event().
Checkpointed: save intermediate state for resume.

Dynamic Position Sizing:
- Conviction multipliers (High: 1.5x, Medium: 1.0x, Low: 0.5x) on base 5%
- Hard Caps: Max single position 15%, max sector 30%
- Dry Powder: Baseline 10%, Crisis Tier1 25%, Crisis Tier2 75-100%

Rebalancing:
- 2% drift bands, min $1000 trade, 15 bps cost budget

Regime Allocation:
- EXPANSION: 80-100% equity
- SHOCK: 40-60% equity
- CRISIS: fallback to 60% Treasuries/40% Gold
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

CHECKPOINT_DIR = Path("data/checkpoints/sizing_rebalancing")
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
class PositionTarget:
    """Target position for a ticker."""
    ticker: str
    sector: str
    current_shares: float = 0.0
    current_value: float = 0.0
    current_weight: float = 0.0
    target_weight: float = 0.0
    target_shares: int = 0
    target_value: float = 0.0
    trade_shares: int = 0
    trade_value: float = 0.0
    action: str = "HOLD"  # BUY, SELL, HOLD
    conviction: str = "low"
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "sector": self.sector,
            "current_shares": self.current_shares,
            "current_value": self.current_value,
            "current_weight": self.current_weight,
            "target_weight": self.target_weight,
            "target_shares": self.target_shares,
            "target_value": self.target_value,
            "trade_shares": self.trade_shares,
            "trade_value": self.trade_value,
            "action": self.action,
            "conviction": self.conviction,
            "reason": self.reason,
        }

@dataclass
class SizingPlan:
    """Complete sizing and rebalancing plan."""
    timestamp: datetime
    regime: str
    equity_target_pct: float
    fallback_target_pct: float
    cash_target_pct: float
    
    positions: List[PositionTarget]
    fallback_positions: List[PositionTarget]  # Gold/Treasury targets
    
    total_equity_weight: float
    total_fallback_weight: float
    total_cash_weight: float
    
    # Drift metrics
    max_drift_pct: float
    trades_needed: int
    estimated_cost_bps: float
    
    # Constraints
    sector_exposures: Dict[str, float]
    max_single_position_pct: float
    max_sector_exposure_pct: float
    within_limits: bool
    
    degraded: bool = False
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "regime": self.regime,
            "equity_target_pct": self.equity_target_pct,
            "fallback_target_pct": self.fallback_target_pct,
            "cash_target_pct": self.cash_target_pct,
            "positions": [p.to_dict() for p in self.positions],
            "fallback_positions": [p.to_dict() for p in self.fallback_positions],
            "total_equity_weight": self.total_equity_weight,
            "total_fallback_weight": self.total_fallback_weight,
            "total_cash_weight": self.total_cash_weight,
            "max_drift_pct": self.max_drift_pct,
            "trades_needed": self.trades_needed,
            "estimated_cost_bps": self.estimated_cost_bps,
            "sector_exposures": self.sector_exposures,
            "max_single_position_pct": self.max_single_position_pct,
            "max_sector_exposure_pct": self.max_sector_exposure_pct,
            "within_limits": self.within_limits,
            "degraded": self.degraded,
            "warnings": self.warnings,
        }

@dataclass
class RebalanceCheckpoint:
    plans: List[Dict] = field(default_factory=list)
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

def load_position_sizing_config() -> Dict[str, Any]:
    path = Path("config/position_sizing.yaml")
    with path.open() as f:
        return yaml.safe_load(f)

def load_etf_config() -> Dict[str, Any]:
    path = Path("config/etf_config.yaml")
    with path.open() as f:
        return yaml.safe_load(f)

def get_regime_equity_target(regime: str, hmm_crisis_prob: float) -> Tuple[float, float, float]:
    """Get equity/fallback/cash targets based on regime.
    
    Returns: (equity_pct, fallback_pct, cash_pct)
    """
    if regime == "CRISIS" or hmm_crisis_prob > 0.40:
        # CRISIS: fallback to 60% Treasuries / 40% Gold, 0% equity
        return 0.0, 1.0, 0.0
    elif regime == "SHOCK" or hmm_crisis_prob > 0.25:
        # SHOCK: 40-60% equity, rest fallback
        equity = 0.50  # Middle of 40-60%
        fallback = 0.40
        cash = 0.10
        return equity, fallback, cash
    else:
        # EXPANSION: 80-100% equity
        equity = 0.90  # Middle of 80-100%
        fallback = 0.0
        cash = 0.10
        return equity, fallback, cash

def determine_conviction(
    entry_signal: Optional[Dict[str, Any]],
    exit_signal: Optional[Dict[str, Any]],
    black_swan_tier: str = "NONE",
) -> str:
    """Determine conviction level for position sizing."""
    # Black Swan Tier 2 = high conviction
    if black_swan_tier == "TIER2_STRUCTURAL":
        return "high"
    
    # Black Swan Tier 1 = medium-high
    if black_swan_tier == "TIER1_PROVISIONAL":
        return "medium"
    
    # Normal entry: check C_t acceleration
    if entry_signal:
        c_t_accel = entry_signal.get("c_t_accelerating", False)
        c_t_composite = entry_signal.get("c_t_composite")
        c_t_baseline = entry_signal.get("c_t_baseline")
        
        if c_t_accel and c_t_composite and c_t_baseline:
            if c_t_composite / c_t_baseline > 1.2:
                return "high"
            return "medium"
    
    # Exit triggered reduces conviction
    if exit_signal and exit_signal.get("exit_triggered"):
        return "low"
    
    return "low"

def compute_base_target_weight(
    conviction: str,
    config: Dict[str, Any],
    regime: str,
    hmm_crisis_prob: float,
) -> float:
    """Compute base target weight for a position."""
    sizing = config["sizing_rules"]
    base_pct = sizing["base_position_pct"]  # 0.05
    max_pct = sizing["max_single_position"]  # 0.15
    
    mult = sizing["conviction_multiplier"][conviction]
    target = base_pct * mult
    
    # Regime adjustment
    if regime == "SHOCK" or hmm_crisis_prob > 0.25:
        target *= 0.7  # Reduce in shock
    elif regime == "CRISIS" or hmm_crisis_prob > 0.40:
        target *= 0.3  # Drastically reduce in crisis
    
    return min(target, max_pct)

def enforce_sector_caps(
    targets: List[PositionTarget],
    max_sector_pct: float = 0.30,
) -> List[PositionTarget]:
    """Enforce max sector exposure by scaling down proportionally."""
    # Compute current sector weights
    sector_weights = {}
    for t in targets:
        sector_weights[t.sector] = sector_weights.get(t.sector, 0) + t.target_weight
    
    # Scale down over-exposed sectors
    for sector, weight in sector_weights.items():
        if weight > max_sector_pct:
            scale = max_sector_pct / weight
            for t in targets:
                if t.sector == sector:
                    t.target_weight *= scale
                    t.target_value = t.target_weight  # Will be rescaled to portfolio value later
                    t.target_shares = int(t.target_value / t.current_value * t.current_shares) if t.current_shares > 0 else 0
    
    return targets

def enforce_single_position_cap(
    targets: List[PositionTarget],
    max_single_pct: float = 0.15,
) -> List[PositionTarget]:
    """Enforce max single position cap."""
    for t in targets:
        if t.target_weight > max_single_pct:
            t.target_weight = max_single_pct
    return targets

def compute_drift(
    current_weight: float,
    target_weight: float,
) -> float:
    """Compute absolute drift from target."""
    return abs(current_weight - target_weight)

def check_rebalance_needed(
    targets: List[PositionTarget],
    band_width: float = 0.02,
    min_trade_size: float = 1000.0,
) -> Tuple[List[PositionTarget], int, float]:
    """Check which positions need rebalancing.
    
    Returns: (updated_targets, trades_needed, estimated_cost_bps)
    """
    trades = 0
    total_cost_bps = 0.0
    
    for t in targets:
        drift = compute_drift(t.current_weight, t.target_weight)
        
        if drift > band_width:
            t.trade_shares = int(t.target_shares - t.current_shares)
            t.trade_value = abs(t.trade_shares) * (t.current_value / t.current_shares) if t.current_shares > 0 else 0
            
            if t.trade_value >= min_trade_size:
                t.action = "BUY" if t.trade_shares > 0 else "SELL"
                trades += 1
                # Estimate cost: 15 bps per trade
                total_cost_bps += 15 * (t.trade_value / 1_000_000)  # Scale by trade size
            else:
                t.action = "HOLD"
                t.trade_shares = 0
                t.trade_value = 0
        else:
            t.action = "HOLD"
            t.trade_shares = 0
            t.trade_value = 0
    
    return targets, trades, total_cost_bps

def build_fallback_targets(
    fallback_weights: Dict[str, float],
    current_prices: Dict[str, float],
    portfolio_value: float,
    fallback_target_pct: float,
) -> List[PositionTarget]:
    """Build fallback (gold/treasury) position targets."""
    targets = []
    
    for ticker, weight in fallback_weights.items():
        price = current_prices.get(ticker)
        if not price:
            continue
        
        # Scale weight by fallback allocation percentage
        scaled_weight = weight * fallback_target_pct
        target_value = portfolio_value * scaled_weight
        target_shares = int(target_value / price)
        
        # Current position
        current_shares = 0.0
        current_value = 0.0
        
        targets.append(PositionTarget(
            ticker=ticker,
            sector="FALLBACK",
            current_shares=current_shares,
            current_value=current_value,
            current_weight=0.0,
            target_weight=scaled_weight,
            target_shares=target_shares,
            target_value=target_value,
            action="BUY" if target_shares > 0 else "HOLD",
            conviction="high",
            reason="Fallback allocation",
        ))
    
    return targets

def run_sizing_rebalancing(
    portfolio_state: Dict[str, Any],
    dcf_results: Dict[str, Any],
    entry_signals: Dict[str, Any],
    exit_signals: Dict[str, Any],
    black_swan_decisions: Dict[str, Any],
    fallback_weights: Dict[str, float],
    regime: str,
    hmm_crisis_prob: float,
    current_prices: Dict[str, float],
) -> SizingPlan:
    """Main entry point: compute complete sizing and rebalancing plan."""
    config = load_position_sizing_config()
    etf_config = load_etf_config()
    sizing_config = config["sizing_rules"]
    rebal_config = config["rebalancing"]
    
    # Portfolio state
    total_value = portfolio_state.get("total_value", 1_000_000)
    cash = portfolio_state.get("cash", 0)
    positions = portfolio_state.get("positions", {})  # ticker -> shares
    position_values = portfolio_state.get("position_values", {})  # ticker -> market value
    
    # Regime-based allocation targets
    equity_target, fallback_target, cash_target = get_regime_equity_target(regime, hmm_crisis_prob)
    
    # Dry powder allocation
    dry_powder_config = sizing_config["dry_powder"]
    if regime == "CRISIS" or hmm_crisis_prob > 0.40:
        dry_powder_pct = dry_powder_config["crisis_tier2"]  # 0.75
    elif regime == "SHOCK" or hmm_crisis_prob > 0.25:
        dry_powder_pct = dry_powder_config["crisis_tier1"]  # 0.25
    else:
        dry_powder_pct = dry_powder_config["baseline"]  # 0.10
    
    available_equity = total_value * equity_target * (1 - dry_powder_pct)
    
    # Build equity position targets
    position_targets = []
    sector_exposures = {}
    
    for ticker in positions.keys():
        if ticker not in current_prices:
            continue
        
        price = current_prices[ticker]
        shares = positions.get(ticker, 0)
        current_value = position_values.get(ticker, shares * price)
        current_weight = current_value / total_value if total_value > 0 else 0
        sector = SECTOR_MAP.get(ticker, "Technology")
        
        # Get signals
        entry_sig = entry_signals.get(ticker, {})
        exit_sig = exit_signals.get(ticker, {})
        bs_decision = black_swan_decisions.get(ticker, {})
        bs_tier = bs_decision.get("tier", "NONE") if isinstance(bs_decision, dict) else "NONE"
        if hasattr(bs_decision, 'tier'):
            bs_tier = bs_decision.tier.value
        
        # Determine conviction
        conviction = determine_conviction(entry_sig, exit_sig, bs_tier)
        
        # Compute target weight
        base_weight = compute_base_target_weight(conviction, config, regime, hmm_crisis_prob)
        
        # Adjust for exit signal
        if exit_sig and exit_sig.get("exit_triggered"):
            base_weight *= 0.0  # Full exit
        
        target_value = available_equity * (base_weight / equity_target) if equity_target > 0 else 0
        target_shares = int(target_value / price) if price > 0 else 0
        target_weight = (target_shares * price) / total_value if total_value > 0 else 0
        
        # Track sector exposure
        sector_exposures[sector] = sector_exposures.get(sector, 0) + target_weight
        
        position_targets.append(PositionTarget(
            ticker=ticker,
            sector=sector,
            current_shares=shares,
            current_value=current_value,
            current_weight=current_weight,
            target_weight=target_weight,
            target_shares=target_shares,
            target_value=target_value,
            conviction=conviction,
            reason=f"Conviction: {conviction}, Regime: {regime}",
        ))
    
    # Enforce constraints
    position_targets = enforce_single_position_cap(
        position_targets, sizing_config["max_single_position"]
    )
    position_targets = enforce_sector_caps(
        position_targets, sizing_config["max_sector_exposure"]
    )
    
    # Check rebalancing
    position_targets, trades_needed, est_cost_bps = check_rebalance_needed(
        position_targets,
        band_width=rebal_config["band_width"],
        min_trade_size=rebal_config["min_trade_size"],
    )
    
    # Build fallback targets
    fallback_targets = build_fallback_targets(
        fallback_weights, current_prices, total_value, fallback_target
    )
    
    # Compute totals
    total_equity_weight = sum(t.target_weight for t in position_targets)
    total_fallback_weight = sum(t.target_weight for t in fallback_targets)
    total_cash_weight = 1.0 - total_equity_weight - total_fallback_weight
    
    max_drift = max(
        (compute_drift(t.current_weight, t.target_weight) for t in position_targets),
        default=0.0
    )
    
    max_single = max((t.target_weight for t in position_targets), default=0.0)
    max_sector = max(sector_exposures.values()) if sector_exposures else 0.0
    
    within_limits = (
        max_single <= sizing_config["max_single_position"] and
        max_sector <= sizing_config["max_sector_exposure"]
    )
    
    plan = SizingPlan(
        timestamp=datetime.now(timezone.utc),
        regime=regime,
        equity_target_pct=equity_target,
        fallback_target_pct=fallback_target,
        cash_target_pct=cash_target,
        positions=position_targets,
        fallback_positions=fallback_targets,
        total_equity_weight=total_equity_weight,
        total_fallback_weight=total_fallback_weight,
        total_cash_weight=total_cash_weight,
        max_drift_pct=max_drift,
        trades_needed=trades_needed,
        estimated_cost_bps=est_cost_bps,
        sector_exposures=sector_exposures,
        max_single_position_pct=max_single,
        max_sector_exposure_pct=max_sector,
        within_limits=within_limits,
    )
    
    college_event(
        "sizing_rebalancing_complete",
        regime=regime,
        equity_target=equity_target,
        fallback_target=fallback_target,
        cash_target=cash_target,
        total_equity_weight=total_equity_weight,
        trades_needed=trades_needed,
        est_cost_bps=est_cost_bps,
        within_limits=within_limits,
        max_single=max_single,
        max_sector=max_sector,
    )
    
    # Save checkpoint
    path = CHECKPOINT_DIR / f"sizing_plan_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    with path.open("w") as f:
        json.dump(plan.to_dict(), f, indent=2, default=str)
    
    return plan

def generate_orders_from_plan(
    plan: SizingPlan,
    current_prices: Dict[str, float],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Generate buy/sell orders from sizing plan."""
    buy_orders = []
    sell_orders = []
    
    for pos in plan.positions:
        if pos.action == "BUY" and pos.trade_shares > 0:
            price = current_prices.get(pos.ticker, 0)
            buy_orders.append({
                "ticker": pos.ticker,
                "action": "BUY",
                "shares": pos.trade_shares,
                "limit_price": price * 1.005,
                "target_weight": pos.target_weight,
                "conviction": pos.conviction,
                "reason": pos.reason,
            })
        elif pos.action == "SELL" and pos.trade_shares < 0:
            price = current_prices.get(pos.ticker, 0)
            sell_orders.append({
                "ticker": pos.ticker,
                "action": "SELL",
                "shares": abs(pos.trade_shares),
                "limit_price": price * 0.995,
                "target_weight": pos.target_weight,
                "conviction": pos.conviction,
                "reason": pos.reason,
            })
    
    for pos in plan.fallback_positions:
        if pos.action == "BUY" and pos.trade_shares > 0:
            price = current_prices.get(pos.ticker, 0)
            buy_orders.append({
                "ticker": pos.ticker,
                "action": "BUY",
                "shares": pos.trade_shares,
                "limit_price": price * 1.002,
                "target_weight": pos.target_weight,
                "conviction": pos.conviction,
                "reason": pos.reason,
            })
    
    return buy_orders, sell_orders

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    # Synthetic test
    mock_portfolio = {
        "total_value": 1_000_000,
        "cash": 100_000,
        "positions": {"AAPL": 100, "MSFT": 50},
        "position_values": {"AAPL": 15_000, "MSFT": 17_500},
    }
    
    mock_dcf = {
        "AAPL": {"iv_p50": 150, "iv_p25": 135},
        "MSFT": {"iv_p50": 350, "iv_p25": 315},
    }
    
    mock_entry = {
        "AAPL": {"c_t_accelerating": True, "c_t_composite": 1.2, "c_t_baseline": 1.0},
        "MSFT": {"c_t_accelerating": False, "c_t_composite": 0.8, "c_t_baseline": 1.0},
    }
    
    mock_exit = {}
    mock_bs = {}
    mock_fallback = {"BIL": 0.6, "GLDM": 0.4}
    mock_prices = {"AAPL": 150, "MSFT": 350, "BIL": 100, "GLDM": 50}
    
    plan = run_sizing_rebalancing(
        mock_portfolio, mock_dcf, mock_entry, mock_exit, mock_bs,
        mock_fallback, "EXPANSION", 0.05, mock_prices
    )
    
    print(f"Regime: {plan.regime}")
    print(f"Equity Target: {plan.equity_target_pct:.1%}")
    print(f"Fallback Target: {plan.fallback_target_pct:.1%}")
    print(f"Cash Target: {plan.cash_target_pct:.1%}")
    print(f"Total Equity: {plan.total_equity_weight:.1%}")
    print(f"Max Single: {plan.max_single_position_pct:.1%}")
    print(f"Max Sector: {plan.max_sector_exposure_pct:.1%}")
    print(f"Within Limits: {plan.within_limits}")
    print(f"Trades Needed: {plan.trades_needed}")
    print(f"Est Cost: {plan.estimated_cost_bps:.1f} bps")
    
    for pos in plan.positions:
        print(f"  {pos.ticker}: curr_w={pos.current_weight:.2%}, tgt_w={pos.target_weight:.2%}, action={pos.action}, shares={pos.trade_shares}, conv={pos.conviction}")
    
    for pos in plan.fallback_positions:
        print(f"  {pos.ticker} (fallback): tgt_w={pos.target_weight:.2%}, shares={pos.target_shares}")
    
    buy_orders, sell_orders = generate_orders_from_plan(plan, mock_prices)
    print(f"\nBuy Orders: {len(buy_orders)}")
    for o in buy_orders:
        print(f"  BUY {o['ticker']}: {o['shares']} @ ${o['limit_price']:.2f}")
    print(f"Sell Orders: {len(sell_orders)}")
    for o in sell_orders:
        print(f"  SELL {o['ticker']}: {o['shares']} @ ${o['limit_price']:.2f}")