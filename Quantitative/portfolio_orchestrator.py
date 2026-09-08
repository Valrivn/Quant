"""
Portfolio Orchestrator — Main Loop Coordinating Stages 1-4 + HMM State
Pure functions, type hints, dataclasses. No global state.
Phase 1: Stages 1-2 + Fallback + HMM integration stub
Phase 2 (Nemotron): Stages 3-4 + Black Swan + Backtest + Full Integration
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

# Import pipeline stages
from Quantitative.dcf_screen import run_dcf_screen, DCFOutputs
from Quantitative.entry_timing import run_entry_timing, EntrySignal, get_real_c_t_signals
from Quantitative.exit_engine import run_exit_engine, ExitSignal
from Quantitative.sizing_rebalancing import run_sizing_rebalancing, SizingPlan, generate_orders_from_plan
from Quantitative.fallback_engine import run_fallback_engine, get_fallback_weights_for_regime
from Quantitative.black_swan import (
    run_black_swan_batch,
    BlackSwanDecision,
    BlackSwanTier,
    load_tier1_tracker,
    load_position_sizing_config,
)
from Quantitative.hmm_regime import RegimeDetector, RegimeState, build_macro_features
from Quantitative.consumer_signal import compute_c_t, validate_cascade_lead_lag

logger = logging.getLogger(__name__)

CHECKPOINT_DIR = Path("data/checkpoints/portfolio_orchestrator")
CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)


# ─── Data Classes ───────────────────────────────────────────────────────
@dataclass
class PortfolioState:
    """Current portfolio state."""
    cash: float
    positions: Dict[str, float]       # ticker -> shares
    position_values: Dict[str, float] # ticker -> market value
    total_value: float
    dry_powder: float                 # Cash reserved for black swan
    regime: str                       # Current HMM regime
    timestamp: datetime


@dataclass
class OrchestrationDecision:
    """Complete orchestration decision for one cycle."""
    timestamp: datetime
    regime: RegimeState
    regime_confident: bool
    hmm_crisis_prob: float
    
    # Stage 1: DCF Screen
    dcf_results: Dict[str, DCFOutputs]
    dcf_passed: List[str]
    
    # Stage 2: Entry Timing
    entry_signals: Dict[str, EntrySignal]
    entry_triggered: List[str]
    
    # Stage 3: Exit (Phase 2)
    exit_signals: Dict[str, ExitSignal] = field(default_factory=dict)
    exit_triggered: List[str] = field(default_factory=list)
    
    # Stage 4: Sizing/Rebalancing (Phase 2)
    sizing_plan: Optional[SizingPlan] = None
    target_weights: Dict[str, float] = field(default_factory=dict)
    
    # Black Swan
    black_swan_decisions: Dict[str, BlackSwanDecision] = field(default_factory=dict)
    fallback_weights: Dict[str, float] = field(default_factory=dict)
    
    # Portfolio actions
    buy_orders: List[Dict[str, Any]] = field(default_factory=list)
    sell_orders: List[Dict[str, Any]] = field(default_factory=list)
    fallback_orders: List[Dict[str, Any]] = field(default_factory=list)
    
    degraded: bool = False
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        sizing_dict = self.sizing_plan.to_dict() if self.sizing_plan else {}
        return {
            "timestamp": self.timestamp.isoformat(),
            "regime": self.regime.value,
            "regime_confident": self.regime_confident,
            "hmm_crisis_prob": self.hmm_crisis_prob,
            "dcf_passed": self.dcf_passed,
            "entry_triggered": self.entry_triggered,
            "exit_triggered": self.exit_triggered,
            "black_swan_tiers": {t: d.tier.value for t, d in self.black_swan_decisions.items()},
            "fallback_weights": self.fallback_weights,
            "buy_orders": self.buy_orders,
            "sell_orders": self.sell_orders,
            "fallback_orders": self.fallback_orders,
            "sizing_plan": sizing_dict,
            "degraded": self.degraded,
            "warnings": self.warnings,
        }


# ─── Core Functions ─────────────────────────────────────────────────────

def load_universe() -> List[str]:
    """Load master ticker universe."""
    return [
        "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA",
        "JPM", "V", "JNJ", "WMT", "PG", "MA", "UNH", "HD",
        "DIS", "PYPL", "ADBE", "NFLX", "CRM", "INTC", "CSCO",
        "PFE", "TMO", "ABBV", "ACN", "COST"
    ]


def load_current_prices(tickers: List[str]) -> Dict[str, float]:
    """Load current prices from yfinance cache."""
    prices = {}
    for t in tickers:
        path = Path(f"data/source/yfinance/{t}/prices.parquet")
        if path.exists():
            try:
                df = pd.read_parquet(path)
                if not df.empty:
                    prices[t] = float(df["Close"].iloc[-1])
            except Exception as e:
                logger.warning(f"Price load failed for {t}: {e}")
    return prices


def load_price_series(tickers: List[str], lookback_days: int = 252) -> Dict[str, pd.Series]:
    """Load price series for drawdown calculations."""
    series = {}
    cutoff = datetime.now(timezone.utc) - pd.Timedelta(days=lookback_days * 1.5)  # Calendar days
    for t in tickers:
        path = Path(f"data/source/yfinance/{t}/prices.parquet")
        if path.exists():
            try:
                df = pd.read_parquet(path)
                df.index = pd.to_datetime(df.index)
                df = df[df.index >= cutoff]
                if not df.empty:
                    series[t] = df["Close"]
            except Exception as e:
                logger.warning(f"Price series load failed for {t}: {e}")
    return series


def get_hmm_regime() -> Tuple[RegimeState, bool, float]:
    """Get current HMM regime state (stub for Phase 1, full in Phase 2).
    
    Returns: (regime, confident, crisis_prob)
    """
    # Try to load latest HMM report
    path = Path("data/checkpoints/hmm_regime/latest_report.json")
    if path.exists():
        try:
            with path.open() as f:
                report = json.load(f)
            regime = RegimeState(report.get("detected", "EXPANSION"))
            confident = report.get("confident", False)
            crisis_prob = report.get("regimes", {}).get("CRISIS", 0.0)
            return regime, confident, crisis_prob
        except Exception:
            pass
    
    # Fallback: run detector on synthetic/macro data
    try:
        # Build macro features
        features = build_macro_features()
        if len(features) > 250:
            detector = RegimeDetector(n_states=3, seed=42)
            detector.retrain(features, until=features.index[-1])
            report = detector.decode(features, until=features.index[-1])
            
            # Save for next time
            Path("data/checkpoints/hmm_regime").mkdir(parents=True, exist_ok=True)
            with Path("data/checkpoints/hmm_regime/latest_report.json").open("w") as f:
                json.dump({
                    "detected": report.detected.value,
                    "regimes": report.regimes,
                    "confident": report.confident,
                    "date": report.date.isoformat(),
                }, f, indent=2)
            
            return report.detected, report.confident, report.crisis_prob
    except Exception as e:
        logger.warning(f"HMM regime detection failed: {e}")
    
    # Ultimate fallback
    return RegimeState.EXPANSION, False, 0.0


def determine_fallback_regime(
    hmm_regime: RegimeState,
    hmm_crisis_prob: float,
    black_swan_decisions: Dict[str, BlackSwanDecision],
) -> str:
    """Determine fallback engine regime based on HMM + Black Swan state."""
    # Check for any Tier 2 activation
    has_tier2 = any(d.tier == BlackSwanTier.TIER2_STRUCTURAL for d in black_swan_decisions.values())
    if has_tier2:
        return "TIER2_STRUCTURAL"
    
    # Check for any Tier 1 activation
    has_tier1 = any(d.tier == BlackSwanTier.TIER1_PROVISIONAL for d in black_swan_decisions.values())
    if has_tier1:
        return "TIER1_PROVISIONAL"
    
    # HMM-based regime
    if hmm_regime == RegimeState.CRISIS or hmm_crisis_prob > 0.40:
        return "TIER2_STRUCTURAL"
    elif hmm_regime == RegimeState.SHOCK or hmm_crisis_prob > 0.25:
        return "TIER1_PROVISIONAL"
    
    return "NORMAL"


def generate_buy_orders(
    entry_triggered: List[str],
    entry_signals: Dict[str, EntrySignal],
    current_prices: Dict[str, float],
    portfolio_value: float,
    dry_powder: float,
    config: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Generate buy orders for entry-triggered tickers."""
    orders = []
    sizing_config = config["sizing_rules"]
    
    for ticker in entry_triggered:
        signal = entry_signals[ticker]
        price = current_prices.get(ticker)
        if not price:
            continue
        
        # Position sizing based on conviction
        base_pct = sizing_config["base_position_pct"]
        max_pct = sizing_config["max_single_position"]
        
        # Determine conviction
        if signal.c_t_accelerating and signal.c_t_composite and signal.c_t_baseline:
            if signal.c_t_composite / signal.c_t_baseline > 1.2:
                conviction = "high"
            else:
                conviction = "medium"
        else:
            conviction = "low"
        
        mult = sizing_config["conviction_multiplier"][conviction]
        target_pct = min(base_pct * mult, max_pct)
        target_value = portfolio_value * target_pct
        
        # Check dry powder
        if target_value > dry_powder:
            target_value = dry_powder * 0.5  # Don't use all dry powder on one
            target_pct = target_value / portfolio_value
        
        shares = int(target_value / price)
        if shares > 0:
            orders.append({
                "ticker": ticker,
                "action": "BUY",
                "shares": shares,
                "limit_price": price * 1.005,  # 0.5% limit buffer
                "target_pct": target_pct,
                "conviction": conviction,
                "reason": signal.trigger_reason,
            })
            dry_powder -= shares * price
    
    return orders


def generate_fallback_orders(
    fallback_weights: Dict[str, float],
    current_prices: Dict[str, float],
    portfolio_value: float,
    cash_available: float,
) -> List[Dict[str, Any]]:
    """Generate fallback asset orders (treasuries/gold)."""
    orders = []
    
    for ticker, weight in fallback_weights.items():
        price = current_prices.get(ticker)
        if not price:
            continue
        
        target_value = portfolio_value * weight
        if target_value > cash_available:
            target_value = cash_available
        
        shares = int(target_value / price)
        if shares > 0:
            orders.append({
                "ticker": ticker,
                "action": "BUY",
                "shares": shares,
                "limit_price": price * 1.002,  # Tighter for liquid ETFs
                "target_pct": weight,
                "reason": "Fallback allocation",
            })
            cash_available -= shares * price
    
    return orders


def generate_black_swan_orders(
    black_swan_decisions: Dict[str, BlackSwanDecision],
    current_prices: Dict[str, float],
    portfolio_value: float,
    dry_powder: float,
) -> List[Dict[str, Any]]:
    """Generate orders from black swan decisions."""
    orders = []
    remaining_powder = dry_powder
    
    # Sort by tier priority: Tier 2 first, then Tier 1
    sorted_decisions = sorted(
        black_swan_decisions.items(),
        key=lambda x: (x[1].tier == BlackSwanTier.TIER2_STRUCTURAL, x[1].tier == BlackSwanTier.TIER1_PROVISIONAL),
        reverse=True,
    )
    
    for ticker, decision in sorted_decisions:
        if decision.tier == BlackSwanTier.NONE or decision.tier == BlackSwanTier.DISENGAGED:
            continue
        
        price = current_prices.get(ticker)
        if not price:
            continue
        
        # Check price limit
        if decision.entry_price_limit and price > decision.entry_price_limit:
            logger.info(f"Black Swan {decision.tier.value} {ticker}: price {price:.2f} > limit {decision.entry_price_limit:.2f}")
            continue
        
        deploy_value = portfolio_value * decision.deploy_pct
        deploy_value = min(deploy_value, remaining_powder)
        
        shares = int(deploy_value / price)
        if shares > 0:
            orders.append({
                "ticker": ticker,
                "action": "BUY",
                "shares": shares,
                "limit_price": price * 1.01,  # 1% buffer for crisis entries
                "target_pct": deploy_value / portfolio_value,
                "tier": decision.tier.value,
                "reason": f"Black Swan {decision.tier.value}",
            })
            remaining_powder -= shares * price
    
    return orders


def run_orchestration_cycle(
    portfolio_state: Optional[PortfolioState] = None,
) -> OrchestrationDecision:
    """Run one complete orchestration cycle (Stages 1-4 + HMM + Black Swan + Fallback)."""
    timestamp = datetime.now(timezone.utc)
    universe = load_universe()
    current_prices = load_current_prices(universe)
    price_series = load_price_series(universe)
    
    # ─── HMM Regime ───
    hmm_regime, hmm_confident, hmm_crisis_prob = get_hmm_regime()
    
    # ─── Stage 1: DCF Screen ───
    logger.info("Running Stage 1: DCF Screen")
    dcf_results = run_dcf_screen(universe)
    dcf_passed = [t for t, r in dcf_results.items() if r.passes_screen]
    logger.info(f"DCF passed: {len(dcf_passed)}/{len(universe)}")
    
    # ─── Phase 3: Consumer Signal (C_t) ───
    logger.info("Running Consumer Signal (C_t computation)")
    c_t_signals = compute_c_t(dcf_passed, lookback_days=60)
    logger.info(f"C_t computed for {len(c_t_signals)} tickers")
    
    # ─── Stage 2: Entry Timing (with real C_t) ───
    logger.info("Running Stage 2: Entry Timing")
    entry_signals = run_entry_timing(dcf_passed, current_prices)
    entry_triggered = [t for t, s in entry_signals.items() if s.entry_triggered]
    logger.info(f"Entry triggered: {len(entry_triggered)}")
    
    # ─── Stage 3: Exit Engine ───
    logger.info("Running Stage 3: Exit Engine")
    exit_signals = run_exit_engine(list(portfolio_state.positions.keys()) if portfolio_state else [], current_prices)
    exit_triggered = [t for t, s in exit_signals.items() if s.exit_triggered]
    logger.info(f"Exit triggered: {len(exit_triggered)}")
    
    # ─── Black Swan Check ───
    logger.info("Running Black Swan Check")
    tier1_tracker = load_tier1_tracker()
    black_swan_decisions = run_black_swan_batch(
        dcf_passed,  # Only check DCF-passed tickers
        {t: price_series[t] for t in dcf_passed if t in price_series},
        {t: current_prices[t] for t in dcf_passed if t in current_prices},
        tier1_tracker,
    )
    
    # ─── Fallback Allocation ───
    fallback_regime = determine_fallback_regime(hmm_regime, hmm_crisis_prob, black_swan_decisions)
    logger.info(f"Fallback regime: {fallback_regime}")
    fallback_weights = get_fallback_weights_for_regime(fallback_regime)
    
    # ─── Portfolio State ───
    if portfolio_state is None:
        portfolio_state = PortfolioState(
            cash=1_000_000,  # Default $1M
            positions={},
            position_values={},
            total_value=1_000_000,
            dry_powder=100_000,  # 10% dry powder
            regime=hmm_regime.value,
            timestamp=timestamp,
        )
    
    # ─── Stage 4: Sizing & Rebalancing ───
    logger.info("Running Stage 4: Sizing & Rebalancing")
    sizing_plan = run_sizing_rebalancing(
        portfolio_state={
            "total_value": portfolio_state.total_value,
            "cash": portfolio_state.cash,
            "positions": portfolio_state.positions,
            "position_values": portfolio_state.position_values,
        },
        dcf_results={t: r.to_dict() for t, r in dcf_results.items()},
        entry_signals={t: s.to_dict() for t, s in entry_signals.items()},
        exit_signals={t: e.to_dict() for t, e in exit_signals.items()},
        black_swan_decisions={t: d.to_dict() for t, d in black_swan_decisions.items()},
        fallback_weights=fallback_weights,
        regime=hmm_regime.value,
        hmm_crisis_prob=hmm_crisis_prob,
        current_prices=current_prices,
    )
    
    # Generate orders from sizing plan
    buy_orders, sell_orders = generate_orders_from_plan(sizing_plan, current_prices)
    
    # Add fallback orders
    fb_orders = []
    for pos in sizing_plan.fallback_positions:
        if pos.action == "BUY" and pos.trade_shares > 0:
            price = current_prices.get(pos.ticker, 0)
            fb_orders.append({
                "ticker": pos.ticker,
                "action": "BUY",
                "shares": pos.trade_shares,
                "limit_price": price * 1.002,
                "target_weight": pos.target_weight,
                "conviction": "high",
                "reason": "Fallback allocation",
            })
    
    # ─── Compile Decision ───
    decision = OrchestrationDecision(
        timestamp=timestamp,
        regime=hmm_regime,
        regime_confident=hmm_confident,
        hmm_crisis_prob=hmm_crisis_prob,
        dcf_results=dcf_results,
        dcf_passed=dcf_passed,
        entry_signals=entry_signals,
        entry_triggered=entry_triggered,
        exit_signals=exit_signals,
        exit_triggered=exit_triggered,
        sizing_plan=sizing_plan,
        target_weights={t: p.target_weight for t, p in sizing_plan.positions},
        black_swan_decisions=black_swan_decisions,
        fallback_weights=fallback_weights,
        buy_orders=buy_orders,
        sell_orders=sell_orders,
        fallback_orders=fb_orders,
        degraded=hmm_crisis_prob == 0.0 and not hmm_confident,
        warnings=[],
    )
    
    # Save checkpoint
    path = CHECKPOINT_DIR / f"orchestration_{timestamp.strftime('%Y%m%d_%H%M%S')}.json"
    with path.open("w") as f:
        json.dump(decision.to_dict(), f, indent=2, default=str)
    
    # Also save latest
    latest_path = CHECKPOINT_DIR / "latest_orchestration.json"
    with latest_path.open("w") as f:
        json.dump(decision.to_dict(), f, indent=2, default=str)
    
    college_event(
        "orchestration_cycle_complete",
        timestamp=timestamp.isoformat(),
        regime=hmm_regime.value,
        crisis_prob=hmm_crisis_prob,
        dcf_passed=len(dcf_passed),
        entry_triggered=len(entry_triggered),
        exit_triggered=len(exit_triggered),
        tier1_count=sum(1 for d in black_swan_decisions.values() if d.tier == BlackSwanTier.TIER1_PROVISIONAL),
        tier2_count=sum(1 for d in black_swan_decisions.values() if d.tier == BlackSwanTier.TIER2_STRUCTURAL),
        fallback_regime=fallback_regime,
        buy_orders=len(buy_orders),
        sell_orders=len(sell_orders),
        fallback_orders=len(fb_orders),
        total_equity_weight=sizing_plan.total_equity_weight,
        total_fallback_weight=sizing_plan.total_fallback_weight,
        within_limits=sizing_plan.within_limits,
    )
    
    logger.info(
        f"Orchestration complete: "
        f"Regime={hmm_regime.value}, CrisisProb={hmm_crisis_prob:.2%}, "
        f"DCF={len(dcf_passed)}, Entry={len(entry_triggered)}, Exit={len(exit_triggered)}, "
        f"BS_T1={sum(1 for d in black_swan_decisions.values() if d.tier==BlackSwanTier.TIER1_PROVISIONAL)}, "
        f"BS_T2={sum(1 for d in black_swan_decisions.values() if d.tier==BlackSwanTier.TIER2_STRUCTURAL)}, "
        f"Orders: Buy={len(buy_orders)}, Sell={len(sell_orders)}, Fallback={len(fb_orders)}, "
        f"Equity={sizing_plan.total_equity_weight:.1%}, Fallback={sizing_plan.total_fallback_weight:.1%}, "
        f"WithinLimits={sizing_plan.within_limits}"
    )
    
    return decision


def run_continuous_orchestrator(interval_seconds: int = 3600) -> None:
    """Run orchestrator continuously (for production deployment)."""
    import time
    import signal
    
    shutdown = False
    
    def signal_handler(signum, frame):
        nonlocal shutdown
        logger.info("Shutdown signal received")
        shutdown = True
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    logger.info(f"Starting continuous orchestrator (interval: {interval_seconds}s)")
    
    while not shutdown:
        try:
            run_orchestration_cycle()
        except Exception as e:
            logger.error(f"Orchestration cycle failed: {e}")
            college_event("orchestration_cycle_error", error=str(e))
        
        if shutdown:
            break
        
        # Sleep with periodic shutdown check
        for _ in range(interval_seconds):
            if shutdown:
                break
            time.sleep(1)
    
    logger.info("Continuous orchestrator stopped")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    # Single cycle test
    decision = run_orchestration_cycle()
    
    print(f"\n=== Orchestration Decision ===")
    print(f"Timestamp: {decision.timestamp}")
    print(f"Regime: {decision.regime.value} (confident: {decision.regime_confident})")
    print(f"HMM Crisis Prob: {decision.hmm_crisis_prob:.2%}")
    print(f"DCF Passed: {decision.dcf_passed}")
    print(f"Entry Triggered: {decision.entry_triggered}")
    print(f"Black Swan Tiers: { {t: d.tier.value for t, d in decision.black_swan_decisions.items()} }")
    print(f"Fallback Weights: {decision.fallback_weights}")
    print(f"Buy Orders: {len(decision.buy_orders)}")
    for o in decision.buy_orders:
        print(f"  BUY {o['ticker']}: {o['shares']} shares @ ${o['limit_price']:.2f} ({o.get('reason', '')})")
    print(f"Fallback Orders: {len(decision.fallback_orders)}")
    for o in decision.fallback_orders:
        print(f"  BUY {o['ticker']}: {o['shares']} shares @ ${o['limit_price']:.2f}")