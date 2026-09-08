"""
Fallback Engine — Gold/Treasury Allocation with NAV/Liquidity Gates
Pure functions, type hints, dataclasses. No global state.
Physical gold trusts ONLY (GLDM, IAU). No miners (GDX).
NAV clamping gate: freeze if discount > -50 bps
Liquidity gate: ADV > 1M shares, spread ≤ 2 bps
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

CHECKPOINT_DIR = Path("data/checkpoints/fallback_engine")
CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)


# ─── Data Classes ───────────────────────────────────────────────────────
@dataclass
class FallbackAsset:
    """Single fallback asset (ETF) with gate status."""
    ticker: str
    asset_class: str           # "treasuries" or "gold"
    priority: int              # 1 = primary, 2 = secondary
    nav_discount_bps: Optional[float] = None
    spread_bps: Optional[float] = None
    adv_shares: Optional[float] = None
    nav_gate_pass: bool = False
    liquidity_gate_pass: bool = False
    eligible: bool = False
    weight: float = 0.0
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "asset_class": self.asset_class,
            "priority": self.priority,
            "nav_discount_bps": self.nav_discount_bps,
            "spread_bps": self.spread_bps,
            "adv_shares": self.adv_shares,
            "nav_gate_pass": self.nav_gate_pass,
            "liquidity_gate_pass": self.liquidity_gate_pass,
            "eligible": self.eligible,
            "weight": self.weight,
            "warnings": self.warnings,
        }


@dataclass
class FallbackAllocation:
    """Complete fallback allocation for current regime."""
    timestamp: datetime
    regime: str                # "NORMAL", "TIER1_PROVISIONAL", "TIER2_STRUCTURAL"
    treasury_weight: float
    gold_weight: float
    assets: List[FallbackAsset]
    total_eligible_weight: float
    cash_residual: float       # Weight not allocated due to gate failures
    degraded: bool = False
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "regime": self.regime,
            "treasury_weight": self.treasury_weight,
            "gold_weight": self.gold_weight,
            "assets": [a.to_dict() for a in self.assets],
            "total_eligible_weight": self.total_eligible_weight,
            "cash_residual": self.cash_residual,
            "degraded": self.degraded,
            "warnings": self.warnings,
        }


# ─── Configuration ──────────────────────────────────────────────────────

def load_etf_config() -> Dict[str, Any]:
    path = Path("config/etf_config.yaml")
    with path.open() as f:
        return yaml.safe_load(f)


# ─── Gate Functions ─────────────────────────────────────────────────────

def compute_nav_discount(ticker: str) -> Optional[float]:
    """Compute NAV discount in basis points.
    
    discount_bps = (NAV - Price) / NAV * 10000
    Negative = trading at discount to NAV
    """
    # Try to load NAV data from cache
    nav_path = Path(f"data/source/etf_nav/{ticker}.parquet")
    price_path = Path(f"data/source/yfinance/{ticker}/prices.parquet")
    
    if not nav_path.exists() or not price_path.exists():
        return None
    
    try:
        nav_df = pd.read_parquet(nav_path)
        price_df = pd.read_parquet(price_path)
        
        if nav_df.empty or price_df.empty:
            return None
        
        # Align on date
        latest_nav = nav_df["nav"].iloc[-1]
        latest_price = price_df["Close"].iloc[-1]
        
        if latest_nav <= 0:
            return None
        
        discount_bps = (latest_nav - latest_price) / latest_nav * 10000
        return float(discount_bps)
    except Exception as e:
        logger.warning(f"NAV discount calc failed for {ticker}: {e}")
        return None


def check_nav_gate(ticker: str, asset_class: str, config: Dict[str, Any]) -> Tuple[bool, Optional[float], List[str]]:
    """NAV clamping gate: freeze if discount > -50 bps (i.e., discount worse than -0.50%).
    
    For treasuries/gold ETFs, we expect small premiums/discounts.
    Gate fails if discount < -50 bps (trading too far below NAV).
    """
    warnings = []
    discount_bps = compute_nav_discount(ticker)
    
    if discount_bps is None:
        warnings.append("NAV data unavailable")
        return False, None, warnings
    
    # NAV tolerance from config
    tolerance_bps = config["fallback"][asset_class]["nav_tolerance_bps"]
    critical_trigger = -50  # Hard freeze at -50 bps discount
    
    # Pass if discount > critical_trigger (i.e., not too negative)
    # Also warn if outside tolerance
    if discount_bps < critical_trigger:
        warnings.append(f"NAV discount {discount_bps:.1f} bps exceeds critical -50 bps — GATE FAIL")
        return False, discount_bps, warnings
    
    if abs(discount_bps) > tolerance_bps:
        warnings.append(f"NAV discount {discount_bps:.1f} bps outside tolerance ±{tolerance_bps} bps")
    
    return True, discount_bps, warnings


def check_liquidity_gate(ticker: str, asset_class: str, config: Dict[str, Any]) -> Tuple[bool, Optional[float], Optional[float], List[str]]:
    """Liquidity gate: ADV > 1M shares, spread ≤ 2 bps."""
    warnings = []
    
    price_path = Path(f"data/source/yfinance/{ticker}/prices.parquet")
    if not price_path.exists():
        warnings.append("Price data unavailable for liquidity check")
        return False, None, None, warnings
    
    try:
        df = pd.read_parquet(price_path)
        if len(df) < 20:
            warnings.append("Insufficient price history for liquidity check")
            return False, None, None, warnings
        
        # ADV: 20-day average daily volume
        adv = float(df["Volume"].rolling(20).mean().iloc[-1])
        min_adv = config["fallback"][asset_class]["min_adv_shares"]
        
        # Spread proxy: median (High-Low)/Close over 20 days in bps
        spread_pct = ((df["High"] - df["Low"]) / df["Close"]).rolling(20).median().iloc[-1]
        spread_bps = float(spread_pct * 10000)
        max_spread_bps = config["fallback"][asset_class]["max_spread_bps"]
        
        adv_ok = adv >= min_adv
        spread_ok = spread_bps <= max_spread_bps
        
        if not adv_ok:
            warnings.append(f"ADV {adv:,.0f} < min {min_adv:,.0f}")
        if not spread_ok:
            warnings.append(f"Spread {spread_bps:.1f} bps > max {max_spread_bps} bps")
        
        return adv_ok and spread_ok, adv, spread_bps, warnings
        
    except Exception as e:
        warnings.append(f"Liquidity check failed: {e}")
        return False, None, None, warnings


def evaluate_asset(ticker: str, asset_class: str, priority: int, config: Dict[str, Any]) -> FallbackAsset:
    """Evaluate a single fallback asset through all gates."""
    nav_ok, nav_discount, nav_warnings = check_nav_gate(ticker, asset_class, config)
    liq_ok, adv, spread_bps, liq_warnings = check_liquidity_gate(ticker, asset_class, config)
    
    all_warnings = nav_warnings + liq_warnings
    eligible = nav_ok and liq_ok
    
    return FallbackAsset(
        ticker=ticker,
        asset_class=asset_class,
        priority=priority,
        nav_discount_bps=nav_discount,
        spread_bps=spread_bps,
        adv_shares=adv,
        nav_gate_pass=nav_ok,
        liquidity_gate_pass=liq_ok,
        eligible=eligible,
        warnings=all_warnings,
    )


def build_fallback_allocation(regime: str, config: Dict[str, Any]) -> FallbackAllocation:
    """Build complete fallback allocation for given regime."""
    fb_config = config["fallback"]
    assets = []
    
    # Treasuries: primary (BIL), secondary (SHV)
    assets.append(evaluate_asset(fb_config["treasuries"]["primary"], "treasuries", 1, config))
    assets.append(evaluate_asset(fb_config["treasuries"]["secondary"], "treasuries", 2, config))
    
    # Gold: primary (GLDM), secondary (IAU)
    assets.append(evaluate_asset(fb_config["gold"]["primary"], "gold", 1, config))
    assets.append(evaluate_asset(fb_config["gold"]["secondary"], "gold", 2, config))
    
    # Target weights from config
    target_treasury = fb_config["allocation"]["treasuries"]
    target_gold = fb_config["allocation"]["gold"]
    
    # Allocate weights to eligible assets (priority order within class)
    treasury_weight = 0.0
    gold_weight = 0.0
    
    for asset in assets:
        if not asset.eligible:
            asset.weight = 0.0
            continue
        
        if asset.asset_class == "treasuries":
            # Primary gets full treasury allocation if eligible
            if asset.priority == 1:
                asset.weight = target_treasury
                treasury_weight = target_treasury
            # Secondary only used if primary fails
            elif asset.priority == 2 and treasury_weight == 0:
                asset.weight = target_treasury
                treasury_weight = target_treasury
        elif asset.asset_class == "gold":
            if asset.priority == 1:
                asset.weight = target_gold
                gold_weight = target_gold
            elif asset.priority == 2 and gold_weight == 0:
                asset.weight = target_gold
                gold_weight = target_gold
    
    total_eligible = treasury_weight + gold_weight
    cash_residual = 1.0 - total_eligible
    
    # Degraded if any primary asset failed
    degraded = any(not a.eligible and a.priority == 1 for a in assets)
    
    allocation = FallbackAllocation(
        timestamp=datetime.now(timezone.utc),
        regime=regime,
        treasury_weight=treasury_weight,
        gold_weight=gold_weight,
        assets=assets,
        total_eligible_weight=total_eligible,
        cash_residual=cash_residual,
        degraded=degraded,
        warnings=[w for a in assets for w in a.warnings],
    )
    
    college_event(
        "fallback_allocation_built",
        regime=regime,
        treasury_weight=treasury_weight,
        gold_weight=gold_weight,
        total_eligible=total_eligible,
        cash_residual=cash_residual,
        degraded=degraded,
        assets=[{"ticker": a.ticker, "eligible": a.eligible, "weight": a.weight} for a in assets],
    )
    
    return allocation


def run_fallback_engine(regime: str = "NORMAL") -> FallbackAllocation:
    """Main entry point: build fallback allocation for current regime."""
    config = load_etf_config()
    allocation = build_fallback_allocation(regime, config)
    
    # Save checkpoint
    path = CHECKPOINT_DIR / f"fallback_allocation_{regime.lower()}.json"
    with path.open("w") as f:
        json.dump(allocation.to_dict(), f, indent=2, default=str)
    
    logger.info(
        f"Fallback Engine ({regime}): "
        f"Treasuries={allocation.treasury_weight:.1%}, "
        f"Gold={allocation.gold_weight:.1%}, "
        f"Cash={allocation.cash_residual:.1%}, "
        f"Degraded={allocation.degraded}"
    )
    
    return allocation


def get_fallback_weights_for_regime(regime: str) -> Dict[str, float]:
    """Convenience: get simple weight dict for portfolio orchestrator."""
    allocation = run_fallback_engine(regime)
    weights = {}
    for asset in allocation.assets:
        if asset.eligible and asset.weight > 0:
            weights[asset.ticker] = asset.weight
    return weights


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    # Test with different regimes
    for regime in ["NORMAL", "TIER1_PROVISIONAL", "TIER2_STRUCTURAL"]:
        print(f"\n=== {regime} ===")
        alloc = run_fallback_engine(regime)
        for asset in alloc.assets:
            status = "✓" if asset.eligible else "✗"
            print(f"  {status} {asset.ticker} ({asset.asset_class}): "
                  f"NAV={asset.nav_discount_bps:.1f}bps, "
                  f"Spread={asset.spread_bps:.1f}bps, "
                  f"ADV={asset.adv_shares:,.0f}, "
                  f"Weight={asset.weight:.1%}")
        print(f"  Total Eligible: {alloc.total_eligible_weight:.1%}, Cash: {alloc.cash_residual:.1%}")