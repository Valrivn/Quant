"""
Black Swan Engine — Fast-Slow Trigger, Moat Invariance, Sector Monitor
Pure functions, type hints, dataclasses. No global state.

FAST (5-day drawdown > 3.5σ): TIER 1 Provisional Emergency Trigger
  → Deploy 25% dry powder, Require Price <= IV_P25, No HMM confirmation

SLOW (HMM P(Crisis) > 0.40): TIER 2 Structural Deep Panic Trigger
  → Deploy 75-100% dry powder, Require Price <= IV_P10, HMM validates

DISENGAGE: If HMM not CRISIS after 10 trading days → cancel provisional

Moat Invariance: C_t >= μ_baseline while price crashes. If C_t collapses → DO NOT BUY.
Sector Monitor: 4 layers (Supply chain, Practitioner OMR, Peer ICR, Macro BAA spread)
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yaml

from config.logging_config import college_event

logger = logging.getLogger(__name__)

CHECKPOINT_DIR = Path("data/checkpoints/black_swan")
CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)


# ─── Enums & Data Classes ───────────────────────────────────────────────
class BlackSwanTier(Enum):
    NONE = "NONE"
    TIER1_PROVISIONAL = "TIER1_PROVISIONAL"
    TIER2_STRUCTURAL = "TIER2_STRUCTURAL"
    DISENGAGED = "DISENGAGED"


@dataclass
class FastTriggerState:
    """State for fast (5-day drawdown) trigger."""
    ticker: str
    current_drawdown_5d: float      # 5-day drawdown as decimal
    threshold_3_5_sigma: float      # 3.5σ threshold
    triggered: bool
    trigger_date: Optional[datetime] = None
    days_since_trigger: int = 0
    hmm_crisis_prob: Optional[float] = None
    disengage_pending: bool = False


@dataclass
class SlowTriggerState:
    """State for slow (HMM) trigger."""
    hmm_crisis_prob: float
    threshold: float = 0.40
    triggered: bool = False
    trigger_date: Optional[datetime] = None
    regime: str = "EXPANSION"


@dataclass
class MoatInvarianceCheck:
    """C_t moat invariance check during price crash."""
    ticker: str
    price_drawdown: float           # Current drawdown from peak
    c_t_current: float              # Current grassroots velocity
    c_t_baseline: float             # Baseline μ_baseline
    c_t_ratio: float                # c_t_current / c_t_baseline
    invariant: bool                 # C_t >= baseline while price crashes
    structural_impairment: bool     # C_t collapsed → DO NOT BUY


@dataclass
class SectorMonitorLayer:
    """Single layer of sector black swan monitor."""
    name: str
    value: float
    threshold: float
    triggered: bool
    details: str = ""


@dataclass
class SectorBlackSwanSignal:
    """Aggregate sector black swan signal."""
    sector: str
    layers: List[SectorMonitorLayer]
    composite_score: float          # 0-4 layers triggered
    sector_crisis: bool             # ≥3 layers triggered
    timestamp: datetime


@dataclass
class BlackSwanDecision:
    """Final black swan deployment decision."""
    tier: BlackSwanTier
    ticker: Optional[str] = None
    deploy_pct: float = 0.0         # % of dry powder to deploy
    entry_price_limit: Optional[float] = None  # IV_P25 or IV_P10
    hmm_crisis_prob: Optional[float] = None
    moat_invariant: Optional[bool] = None
    sector_crisis: Optional[bool] = None
    disengage_reason: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tier": self.tier.value,
            "ticker": self.ticker,
            "deploy_pct": self.deploy_pct,
            "entry_price_limit": self.entry_price_limit,
            "hmm_crisis_prob": self.hmm_crisis_prob,
            "moat_invariant": self.moat_invariant,
            "sector_crisis": self.sector_crisis,
            "disengage_reason": self.disengage_reason,
            "timestamp": self.timestamp.isoformat(),
            "warnings": self.warnings,
        }


# ─── Core Functions ─────────────────────────────────────────────────────

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


def load_hmm_crisis_prob() -> Optional[float]:
    """Load current HMM crisis probability from regime detector.
    
    Returns None if unavailable (degraded mode).
    """
    # Try to load from HMM regime detector output
    path = Path("data/checkpoints/hmm_regime/latest_report.json")
    if not path.exists():
        return None
    try:
        with path.open() as f:
            data = json.load(f)
        return data.get("regimes", {}).get("CRISIS", 0.0)
    except Exception:
        return None


def compute_5d_drawdown(prices: pd.Series) -> float:
    """Compute 5-day drawdown from 5-day high."""
    if len(prices) < 5:
        return 0.0
    recent = prices.iloc[-5:]
    high_5d = recent.max()
    current = recent.iloc[-1]
    if high_5d <= 0:
        return 0.0
    return (current - high_5d) / high_5d  # Negative = drawdown


def compute_drawdown_threshold(returns: pd.Series, sigma_mult: float = 3.5) -> float:
    """Compute 3.5σ drawdown threshold from historical 5-day returns."""
    if len(returns) < 60:
        return -0.10  # Default -10% if insufficient history
    
    # 5-day rolling returns
    ret_5d = (1 + returns).rolling(5).apply(lambda x: x.prod() - 1, raw=True).dropna()
    if len(ret_5d) < 30:
        return -0.10
    
    sigma = ret_5d.std()
    return -sigma_mult * sigma  # Negative threshold


def evaluate_fast_trigger(ticker: str, prices: pd.Series) -> FastTriggerState:
    """Evaluate FAST trigger: 5-day drawdown > 3.5σ."""
    drawdown_5d = compute_5d_drawdown(prices)
    
    # Historical 5-day returns for threshold
    returns = prices.pct_change().dropna()
    threshold = compute_drawdown_threshold(returns)
    
    triggered = drawdown_5d <= threshold  # More negative = worse
    
    return FastTriggerState(
        ticker=ticker,
        current_drawdown_5d=drawdown_5d,
        threshold_3_5_sigma=threshold,
        triggered=triggered,
        trigger_date=datetime.now(timezone.utc) if triggered else None,
    )


def evaluate_slow_trigger(hmm_crisis_prob: float) -> SlowTriggerState:
    """Evaluate SLOW trigger: HMM P(Crisis) > 0.40."""
    triggered = hmm_crisis_prob > 0.40
    return SlowTriggerState(
        hmm_crisis_prob=hmm_crisis_prob,
        triggered=triggered,
        trigger_date=datetime.now(timezone.utc) if triggered else None,
        regime="CRISIS" if triggered else "NON_CRISIS",
    )


def check_moat_invariance(
    ticker: str,
    price_drawdown: float,
    c_t_current: Optional[float],
    c_t_baseline: Optional[float],
) -> MoatInvarianceCheck:
    """Moat Invariance: C_t >= μ_baseline while price crashes.
    
    If C_t collapses (ratio < 0.8) → structural impairment → DO NOT BUY.
    """
    if c_t_current is None or c_t_baseline is None or c_t_baseline <= 0:
        return MoatInvarianceCheck(
            ticker=ticker,
            price_drawdown=price_drawdown,
            c_t_current=c_t_current or 0,
            c_t_baseline=c_t_baseline or 0,
            c_t_ratio=0,
            invariant=False,
            structural_impairment=True,
        )
    
    ratio = c_t_current / c_t_baseline
    # Invariant if C_t holding up (ratio >= 0.9) despite price crash
    invariant = ratio >= 0.9
    # Structural impairment if C_t collapsed (ratio < 0.8)
    structural_impairment = ratio < 0.8
    
    return MoatInvarianceCheck(
        ticker=ticker,
        price_drawdown=price_drawdown,
        c_t_current=c_t_current,
        c_t_baseline=c_t_baseline,
        c_t_ratio=ratio,
        invariant=invariant,
        structural_impairment=structural_impairment,
    )


def load_sector_monitor_data(sector: str) -> Dict[str, Any]:
    """Load data for 4-layer sector black swan monitor.
    
    Layers:
    1. Supply Chain: K_c >= 0.70 (concentration)
    2. Practitioner OMR < 0.20 (operating margin resilience)
    3. Peer ICR Z-score < -1.0 (interest coverage distress)
    4. Macro BAA spread > 250bps
    """
    # Placeholder - would load from actual data sources
    # For now return synthetic/mock data structure
    return {
        "supply_chain_kc": 0.65,      # Would come from supply chain mapping
        "practitioner_omr": 0.25,     # Would come from practitioner surveys
        "peer_icr_zscore": -0.5,      # Would come from peer financials
        "macro_baa_spread": 180,      # Would come from FRED BAA10Y
    }


def evaluate_sector_monitor(sector: str, data: Dict[str, Any]) -> SectorBlackSwanSignal:
    """Evaluate 4-layer sector black swan monitor."""
    layers = [
        SectorMonitorLayer(
            name="Supply Chain Concentration (K_c)",
            value=data.get("supply_chain_kc", 0),
            threshold=0.70,
            triggered=data.get("supply_chain_kc", 0) >= 0.70,
            details=f"K_c = {data.get('supply_chain_kc', 0):.2f} ({'≥' if data.get('supply_chain_kc', 0) >= 0.70 else '<'} 0.70)",
        ),
        SectorMonitorLayer(
            name="Practitioner OMR",
            value=data.get("practitioner_omr", 1),
            threshold=0.20,
            triggered=data.get("practitioner_omr", 1) < 0.20,
            details=f"OMR = {data.get('practitioner_omr', 1):.2f} ({'<' if data.get('practitioner_omr', 1) < 0.20 else '≥'} 0.20)",
        ),
        SectorMonitorLayer(
            name="Peer ICR Z-score",
            value=data.get("peer_icr_zscore", 0),
            threshold=-1.0,
            triggered=data.get("peer_icr_zscore", 0) < -1.0,
            details=f"Z-score = {data.get('peer_icr_zscore', 0):.2f} ({'<' if data.get('peer_icr_zscore', 0) < -1.0 else '≥'} -1.0)",
        ),
        SectorMonitorLayer(
            name="Macro BAA Spread",
            value=data.get("macro_baa_spread", 0),
            threshold=250,
            triggered=data.get("macro_baa_spread", 0) > 250,
            details=f"BAA Spread = {data.get('macro_baa_spread', 0):.0f} bps ({'>' if data.get('macro_baa_spread', 0) > 250 else '≤'} 250 bps)",
        ),
    ]
    
    composite = sum(1 for L in layers if L.triggered)
    sector_crisis = composite >= 3
    
    return SectorBlackSwanSignal(
        sector=sector,
        layers=layers,
        composite_score=composite,
        sector_crisis=sector_crisis,
        timestamp=datetime.now(timezone.utc),
    )


def make_black_swan_decision(
    ticker: str,
    fast_state: FastTriggerState,
    slow_state: SlowTriggerState,
    moat_check: MoatInvarianceCheck,
    sector_signal: SectorBlackSwanSignal,
    dcf_result: Dict[str, Any],
    current_price: float,
    config: Dict[str, Any],
    tier1_active_since: Optional[datetime] = None,
) -> BlackSwanDecision:
    """Make final black swan deployment decision."""
    warnings = []
    hmm_crisis_prob = slow_state.hmm_crisis_prob
    
    # DISENGAGE check: If Tier 1 active but HMM not CRISIS after 10 trading days
    if tier1_active_since:
        days_active = (datetime.now(timezone.utc) - tier1_active_since).days
        # Approximate trading days
        trading_days = int(days_active * 5 / 7)
        if trading_days >= 10 and not slow_state.triggered:
            return BlackSwanDecision(
                tier=BlackSwanTier.DISENGAGED,
                ticker=ticker,
                disengage_reason=f"Tier 1 active {trading_days} trading days without HMM CRISIS confirmation",
                hmm_crisis_prob=hmm_crisis_prob,
                warnings=["DISENGAGED: Provisional trigger expired"],
            )
    
    # Structural impairment veto
    if moat_check.structural_impairment:
        return BlackSwanDecision(
            tier=BlackSwanTier.NONE,
            ticker=ticker,
            moat_invariant=False,
            warnings=["STRUCTURAL IMPAIRMENT: C_t collapsed, DO NOT BUY"],
        )
    
    # TIER 2: Structural Deep Panic (SLOW trigger confirmed)
    if slow_state.triggered:
        iv_p10 = dcf_result.get("iv_p10", 0)
        if iv_p10 <= 0:
            return BlackSwanDecision(
                tier=BlackSwanTier.NONE,
                ticker=ticker,
                hmm_crisis_prob=hmm_crisis_prob,
                warnings=["HMM CRISIS but IV_P10 unavailable"],
            )
        
        if current_price <= iv_p10 and moat_check.invariant:
            return BlackSwanDecision(
                tier=BlackSwanTier.TIER2_STRUCTURAL,
                ticker=ticker,
                deploy_pct=0.75,  # 75-100% dry powder
                entry_price_limit=iv_p10,
                hmm_crisis_prob=hmm_crisis_prob,
                moat_invariant=True,
                sector_crisis=sector_signal.sector_crisis,
            )
        else:
            return BlackSwanDecision(
                tier=BlackSwanTier.NONE,
                ticker=ticker,
                hmm_crisis_prob=hmm_crisis_prob,
                moat_invariant=moat_check.invariant,
                sector_crisis=sector_signal.sector_crisis,
                warnings=["Tier 2 conditions not met: price > IV_P10 or moat not invariant"],
            )
    
    # TIER 1: Provisional Emergency (FAST trigger only)
    if fast_state.triggered:
        iv_p25 = dcf_result.get("iv_p25", 0)
        if iv_p25 <= 0:
            return BlackSwanDecision(
                tier=BlackSwanTier.NONE,
                ticker=ticker,
                warnings=["Fast trigger but IV_P25 unavailable"],
            )
        
        if current_price <= iv_p25 and moat_check.invariant:
            return BlackSwanDecision(
                tier=BlackSwanTier.TIER1_PROVISIONAL,
                ticker=ticker,
                deploy_pct=0.25,  # 25% dry powder
                entry_price_limit=iv_p25,
                hmm_crisis_prob=hmm_crisis_prob,
                moat_invariant=True,
                sector_crisis=sector_signal.sector_crisis,
            )
        else:
            return BlackSwanDecision(
                tier=BlackSwanTier.NONE,
                ticker=ticker,
                hmm_crisis_prob=hmm_crisis_prob,
                moat_invariant=moat_check.invariant,
                warnings=["Fast trigger but price > IV_P25 or moat not invariant"],
            )
    
    # No trigger
    return BlackSwanDecision(
        tier=BlackSwanTier.NONE,
        ticker=ticker,
        hmm_crisis_prob=hmm_crisis_prob,
        moat_invariant=moat_check.invariant if moat_check.c_t_baseline > 0 else None,
    )


def run_black_swan_check(
    ticker: str,
    prices: pd.Series,
    current_price: float,
    tier1_active_since: Optional[datetime] = None,
) -> BlackSwanDecision:
    """Run complete black swan check for a ticker."""
    config = load_position_sizing_config()
    dcf_results = load_dcf_results()
    dcf_result = dcf_results.get(ticker, {})
    
    # Fast trigger
    fast_state = evaluate_fast_trigger(ticker, prices)
    
    # Slow trigger (HMM)
    hmm_crisis_prob = load_hmm_crisis_prob()
    if hmm_crisis_prob is None:
        hmm_crisis_prob = 0.0
        fast_state.hmm_crisis_prob = 0.0
        slow_state = SlowTriggerState(hmm_crisis_prob=0.0, triggered=False)
        warnings = ["HMM crisis probability unavailable (degraded)"]
    else:
        fast_state.hmm_crisis_prob = hmm_crisis_prob
        slow_state = evaluate_slow_trigger(hmm_crisis_prob)
        warnings = []
    
    # Moat invariance (load C_t from entry timing)
    entry_path = Path("data/checkpoints/entry_timing/entry_signals.json")
    c_t_current = None
    c_t_baseline = None
    if entry_path.exists():
        try:
            with entry_path.open() as f:
                entry_data = json.load(f)
            if ticker in entry_data:
                c_t_current = entry_data[ticker].get("c_t_composite")
                c_t_baseline = entry_data[ticker].get("c_t_baseline")
        except Exception:
            pass
    
    price_drawdown = fast_state.current_drawdown_5d
    moat_check = check_moat_invariance(ticker, price_drawdown, c_t_current, c_t_baseline)
    
    # Sector monitor
    sector = None
    # Get sector from DCF result or mapping
    if dcf_result:
        sector = dcf_result.get("sector")
    if not sector:
        sector_map = {
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
        sector = sector_map.get(ticker, "Technology")
    
    sector_data = load_sector_monitor_data(sector)
    sector_signal = evaluate_sector_monitor(sector, sector_data)
    
    # Final decision
    decision = make_black_swan_decision(
        ticker, fast_state, slow_state, moat_check, sector_signal,
        dcf_result, current_price, config, tier1_active_since
    )
    decision.warnings.extend(warnings)
    
    college_event(
        "black_swan_decision",
        ticker=ticker,
        tier=decision.tier.value,
        fast_triggered=fast_state.triggered,
        slow_triggered=slow_state.triggered,
        hmm_crisis_prob=hmm_crisis_prob,
        drawdown_5d=fast_state.current_drawdown_5d,
        moat_invariant=moat_check.invariant,
        moat_ratio=moat_check.c_t_ratio,
        sector_crisis=sector_signal.sector_crisis,
        deploy_pct=decision.deploy_pct,
    )
    
    return decision


def run_black_swan_batch(
    tickers: List[str],
    prices_dict: Dict[str, pd.Series],
    current_prices: Dict[str, float],
    tier1_tracker: Optional[Dict[str, datetime]] = None,
) -> Dict[str, BlackSwanDecision]:
    """Run black swan check on multiple tickers."""
    if tier1_tracker is None:
        tier1_tracker = {}
    
    decisions = {}
    new_tier1 = {}
    
    for ticker in tickers:
        if ticker not in prices_dict or ticker not in current_prices:
            continue
        
        tier1_since = tier1_tracker.get(ticker)
        decision = run_black_swan_check(
            ticker,
            prices_dict[ticker],
            current_prices[ticker],
            tier1_since,
        )
        decisions[ticker] = decision
        
        # Track Tier 1 activations
        if decision.tier == BlackSwanTier.TIER1_PROVISIONAL:
            new_tier1[ticker] = datetime.now(timezone.utc)
        elif decision.tier == BlackSwanTier.DISENGAGED:
            # Remove from tracker
            if ticker in tier1_tracker:
                del tier1_tracker[ticker]
    
    # Update tracker
    tier1_tracker.update(new_tier1)
    
    # Save checkpoint
    path = CHECKPOINT_DIR / "black_swan_decisions.json"
    with path.open("w") as f:
        json.dump({
            t: d.to_dict() for t, d in decisions.items()
        }, f, indent=2, default=str)
    
    # Save tracker
    tracker_path = CHECKPOINT_DIR / "tier1_tracker.json"
    with tracker_path.open("w") as f:
        json.dump({t: d.isoformat() for t, d in tier1_tracker.items()}, f, indent=2)
    
    tier1_count = sum(1 for d in decisions.values() if d.tier == BlackSwanTier.TIER1_PROVISIONAL)
    tier2_count = sum(1 for d in decisions.values() if d.tier == BlackSwanTier.TIER2_STRUCTURAL)
    logger.info(f"Black Swan: Tier1={tier1_count}, Tier2={tier2_count}, None={len(decisions)-tier1_count-tier2_count}")
    
    return decisions


def load_tier1_tracker() -> Dict[str, datetime]:
    """Load Tier 1 activation tracker."""
    path = CHECKPOINT_DIR / "tier1_tracker.json"
    if not path.exists():
        return {}
    try:
        with path.open() as f:
            data = json.load(f)
        return {t: datetime.fromisoformat(d) for t, d in data.items()}
    except Exception:
        return {}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    # Synthetic test
    dates = pd.bdate_range(end=datetime.now(), periods=100)
    np.random.seed(42)
    # Normal drift then crash
    returns = np.random.normal(0.0005, 0.01, 100)
    returns[-5:] = np.random.normal(-0.03, 0.02, 5)  # 5-day crash
    prices = pd.Series(100 * np.cumprod(1 + returns), index=dates)
    
    config = load_position_sizing_config()
    
    # Test fast trigger
    fast = evaluate_fast_trigger("TEST", prices)
    print(f"Fast Trigger: drawdown={fast.current_drawdown_5d:.2%}, threshold={fast.threshold_3_5_sigma:.2%}, triggered={fast.triggered}")
    
    # Test slow trigger
    slow = evaluate_slow_trigger(0.45)
    print(f"Slow Trigger: crisis_prob={slow.hmm_crisis_prob:.2f}, triggered={slow.triggered}")
    
    # Test moat invariance
    moat = check_moat_invariance("TEST", -0.15, 0.8, 1.0)  # C_t dropped 20%
    print(f"Moat: ratio={moat.c_t_ratio:.2f}, invariant={moat.invariant}, impairment={moat.structural_impairment}")
    
    moat2 = check_moat_invariance("TEST", -0.15, 1.1, 1.0)  # C_t held up
    print(f"Moat (held): ratio={moat2.c_t_ratio:.2f}, invariant={moat2.invariant}, impairment={moat2.structural_impairment}")
    
    # Test sector monitor
    sector_data = {
        "supply_chain_kc": 0.75,
        "practitioner_omr": 0.15,
        "peer_icr_zscore": -1.5,
        "macro_baa_spread": 300,
    }
    sector = evaluate_sector_monitor("Technology", sector_data)
    print(f"Sector Crisis: {sector.sector_crisis} (score={sector.composite_score}/4)")
    for L in sector.layers:
        print(f"  {L.name}: {L.details} → {'TRIGGERED' if L.triggered else 'ok'}")