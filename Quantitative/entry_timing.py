"""
Stage 2: Entry Timing — Asymmetric Corridor, C_t Signals, Liquidity Gates
Pure functions, type hints, dataclasses. No global state.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yaml

from config.logging_config import college_event

# Phase 3: Import real C_t computation
from Quantitative.consumer_signal import compute_c_t, CTVelocity

logger = logging.getLogger(__name__)

CHECKPOINT_DIR = Path("data/checkpoints/entry_timing")
CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)


# ─── Data Classes ───────────────────────────────────────────────────────
@dataclass
class EntrySignal:
    """Entry timing signal for a single ticker."""
    ticker: str
    timestamp: datetime
    price: float
    iv_p25: float
    iv_p10: float
    # Asymmetric corridor
    corridor_low: float          # IV_P25 * (1 - discount)
    corridor_high: float         # IV_P25
    in_corridor: bool
    # C_t components (grassroots cascade velocity)
    google_trends_velocity: Optional[float] = None
    app_reviews_velocity: Optional[float] = None
    gdelt_velocity: Optional[float] = None
    github_velocity: Optional[float] = None
    c_t_composite: Optional[float] = None
    c_t_accelerating: bool = False
    c_t_baseline: Optional[float] = None
    # T_t, W_t (top-down / wall street) - should be QUIET for entry
    t_t_quiet: bool = True
    w_t_quiet: bool = True
    # Liquidity gates
    adv_ok: bool = False
    spread_ok: bool = False
    # Final decision
    entry_triggered: bool = False
    trigger_reason: str = ""
    degraded: bool = False
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "timestamp": self.timestamp.isoformat(),
            "price": self.price,
            "iv_p25": self.iv_p25,
            "iv_p10": self.iv_p10,
            "corridor_low": self.corridor_low,
            "corridor_high": self.corridor_high,
            "in_corridor": self.in_corridor,
            "google_trends_velocity": self.google_trends_velocity,
            "app_reviews_velocity": self.app_reviews_velocity,
            "gdelt_velocity": self.gdelt_velocity,
            "github_velocity": self.github_velocity,
            "c_t_composite": self.c_t_composite,
            "c_t_accelerating": self.c_t_accelerating,
            "c_t_baseline": self.c_t_baseline,
            "t_t_quiet": self.t_t_quiet,
            "w_t_quiet": self.w_t_quiet,
            "adv_ok": self.adv_ok,
            "spread_ok": self.spread_ok,
            "entry_triggered": self.entry_triggered,
            "trigger_reason": self.trigger_reason,
            "degraded": self.degraded,
            "warnings": self.warnings,
        }


@dataclass
class EntryCheckpoint:
    tickers_done: List[str] = field(default_factory=list)
    signals: Dict[str, Dict] = field(default_factory=dict)
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ─── Core Functions ─────────────────────────────────────────────────────

def load_position_sizing_config() -> Dict[str, Any]:
    path = Path("config/position_sizing.yaml")
    with path.open() as f:
        return yaml.safe_load(f)


def load_dcf_results() -> Dict[str, Any]:
    """Load DCF screen results for IV_P25/P10."""
    path = Path("data/checkpoints/dcf_screen/dcf_screen_results.json")
    if not path.exists():
        return {}
    with path.open() as f:
        return json.load(f)


def compute_asymmetric_corridor(iv_p25: float, discount_sector: float) -> Tuple[float, float]:
    """Asymmetric corridor: [IV_P25 * (1 - discount), IV_P25].
    
    Only buy when price is in this corridor.
    """
    low = iv_p25 * (1 - discount_sector)
    high = iv_p25
    return low, high


def load_grassroots_data(ticker: str, lookback_days: int = 60) -> Dict[str, pd.Series]:
    """Load grassroots data sources for C_t computation (legacy - kept for backward compat).
    
    Returns dict of source -> time series (velocity = rate of change).
    DEPRECATED: Use Quantitative.consumer_signal.compute_c_t for real multi-source C_t.
    """
    out = {}
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    
    # Google Trends
    gt_path = Path(f"data/source/google_trends/{ticker}.parquet")
    if gt_path.exists():
        try:
            df = pd.read_parquet(gt_path)
            df.index = pd.to_datetime(df.index)
            df = df[df.index >= cutoff]
            if not df.empty:
                out["google_trends"] = df["interest"].pct_change(5).fillna(0)
        except Exception:
            pass
    
    # App Store Reviews (via CDXJ/Wayback)
    app_path = Path(f"data/source/cdxj/app_store/{ticker}.json")
    if app_path.exists():
        try:
            with app_path.open() as f:
                data = json.load(f)
            if isinstance(data, list) and data:
                dates = [pd.Timestamp(d.get("date", "")) for d in data if d.get("date")]
                if dates:
                    s = pd.Series(1, index=dates).resample("D").sum().fillna(0)
                    s = s[s.index >= cutoff]
                    out["app_reviews"] = s.pct_change(5).fillna(0)
        except Exception:
            pass
    
    # GDELT
    gdelt_path = Path(f"data/source/gdelt/{ticker}.parquet")
    if gdelt_path.exists():
        try:
            df = pd.read_parquet(gdelt_path)
            df.index = pd.to_datetime(df.index)
            df = df[df.index >= cutoff]
            if not df.empty and "tone" in df.columns:
                out["gdelt"] = df["tone"].pct_change(5).fillna(0)
        except Exception:
            pass
    
    # GitHub (GH Archive)
    gh_path = Path(f"data/source/gh_archive/{ticker}.parquet")
    if gh_path.exists():
        try:
            df = pd.read_parquet(gh_path)
            df.index = pd.to_datetime(df.index)
            df = df[df.index >= cutoff]
            if not df.empty and "events" in df.columns:
                out["github"] = df["events"].pct_change(5).fillna(0)
        except Exception:
            pass
    
    return out


def compute_c_t_velocity(grassroots: Dict[str, pd.Series]) -> Tuple[Optional[float], bool, Optional[float]]:
    """Compute C_t composite velocity and acceleration (legacy - kept for backward compat).
    
    DEPRECATED: Use Quantitative.consumer_signal.compute_c_t for real multi-source C_t.
    """
    if not grassroots:
        return None, False, None
    
    all_dates = set()
    for s in grassroots.values():
        all_dates.update(s.index)
    all_dates = sorted(all_dates)
    
    if len(all_dates) < 10:
        return None, False, None
    
    aligned = pd.DataFrame(index=all_dates)
    weights = {"google_trends": 0.3, "app_reviews": 0.2, "gdelt": 0.25, "github": 0.25}
    total_w = 0.0
    
    for source, series in grassroots.items():
        w = weights.get(source, 0.25)
        aligned[source] = series.reindex(all_dates).fillna(0)
        total_w += w
    
    for source in weights:
        if source in aligned.columns:
            weights[source] /= total_w
    
    c_t = sum(aligned[src] * weights.get(src, 0) for src in aligned.columns)
    baseline = float(c_t.mean())
    ema_5 = c_t.ewm(span=5, adjust=False).mean()
    accelerating = bool(c_t.iloc[-1] > ema_5.iloc[-1]) if len(c_t) > 0 else False
    current_c_t = float(c_t.iloc[-1]) if len(c_t) > 0 else None
    
    return current_c_t, accelerating, baseline


def get_real_c_t_signals(tickers: List[str], lookback_days: int = 60) -> Dict[str, CTVelocity]:
    """Get real multi-source C_t signals using Phase 3 consumer_signal engine."""
    try:
        return compute_c_t(tickers, lookback_days=lookback_days)
    except Exception as e:
        logger.warning(f"Real C_t computation failed, falling back to legacy: {e}")
        return {}


def load_top_down_signals(ticker: str) -> Tuple[bool, bool]:
    """Load T_t (top-down macro) and W_t (Wall Street) signals.
    
    Returns: (t_t_quiet, w_t_quiet) - True means QUIET (good for entry)
    """
    # Placeholder - would integrate with macro regime, analyst revisions, etc.
    # For now, assume quiet unless we have contrary data
    t_t_quiet = True
    w_t_quiet = True
    
    # Check for analyst downgrades (W_t noisy)
    # Check for macro regime SHOCK/CRISIS (T_t noisy)
    # These would come from HMM regime detector and analyst data
    
    return t_t_quiet, w_t_quiet


def check_liquidity_gates(ticker: str, config: Dict[str, Any]) -> Tuple[bool, bool]:
    """Check ADV and spread liquidity gates.
    
    Returns: (adv_ok, spread_ok)
    """
    adv_ok = False
    spread_ok = False
    
    # Load from yfinance cache
    yf_path = Path(f"data/source/yfinance/{ticker}/prices.parquet")
    if yf_path.exists():
        try:
            df = pd.read_parquet(yf_path)
            if len(df) >= 20:
                # ADV: 20-day average volume
                adv = df["Volume"].rolling(20).mean().iloc[-1]
                min_adv = config.get("min_adv_shares", 1_000_000)
                adv_ok = adv >= min_adv
                
                # Spread proxy: (High - Low) / Close rolling median
                spread_pct = ((df["High"] - df["Low"]) / df["Close"]).rolling(20).median().iloc[-1]
                max_spread = config.get("max_spread_bps", 2) / 10000  # bps to decimal
                spread_ok = spread_pct <= max_spread
        except Exception:
            pass
    
    return adv_ok, spread_ok


def evaluate_entry_signal(
    ticker: str,
    price: float,
    dcf_result: Dict[str, Any],
    config: Dict[str, Any],
    c_t_signals: Optional[Dict[str, CTVelocity]] = None,
) -> EntrySignal:
    """Evaluate complete entry signal for a ticker."""
    iv_p25 = dcf_result.get("iv_p25", 0)
    iv_p10 = dcf_result.get("iv_p10", 0)
    discount_sector = dcf_result.get("discount_sector", 0.10)
    
    if iv_p25 <= 0:
        return EntrySignal(
            ticker=ticker,
            timestamp=datetime.now(timezone.utc),
            price=price,
            iv_p25=iv_p25,
            iv_p10=iv_p10,
            corridor_low=0,
            corridor_high=0,
            in_corridor=False,
            degraded=True,
            warnings=["Invalid IV_P25 from DCF"],
        )
    
    # Asymmetric corridor
    corridor_low, corridor_high = compute_asymmetric_corridor(iv_p25, discount_sector)
    in_corridor = corridor_low <= price <= corridor_high
    
    # C_t grassroots velocity - use real multi-source if available
    if c_t_signals and ticker in c_t_signals:
        ct = c_t_signals[ticker]
        c_t_composite = ct.composite_velocity
        c_t_accelerating = ct.accelerating
        c_t_baseline = ct.baseline
        grassroots = {src: sig.velocity for src, sig in ct.source_signals.items() if sig.velocity is not None}
        degraded = ct.degraded
    else:
        # Fallback to legacy synthetic C_t
        grassroots = load_grassroots_data(ticker)
        c_t_composite, c_t_accelerating, c_t_baseline = compute_c_t_velocity(grassroots)
        degraded = c_t_composite is None
    
    # T_t, W_t quiet check
    t_t_quiet, w_t_quiet = load_top_down_signals(ticker)
    
    # Liquidity gates
    adv_ok, spread_ok = check_liquidity_gates(ticker, config.get("liquidity", {}))
    
    # Entry logic:
    # 1. Price in asymmetric corridor
    # 2. C_t accelerating (grassroots momentum)
    # 3. T_t quiet AND W_t quiet (no top-down noise)
    # 4. Liquidity gates pass
    
    entry_triggered = False
    trigger_reason = ""
    
    if not in_corridor:
        trigger_reason = "Price outside asymmetric corridor"
    elif c_t_composite is None:
        trigger_reason = "C_t data unavailable (degraded)"
    elif not c_t_accelerating:
        trigger_reason = "C_t not accelerating"
    elif not t_t_quiet or not w_t_quiet:
        trigger_reason = "T_t or W_t not quiet"
    elif not adv_ok or not spread_ok:
        trigger_reason = "Liquidity gates failed"
    else:
        entry_triggered = True
        trigger_reason = "All entry conditions met"
    
    # Extract individual source velocities for logging
    def get_src_vel(src: str) -> Optional[float]:
        if c_t_signals and ticker in c_t_signals:
            sig = c_t_signals[ticker].source_signals.get(src)
            return sig.velocity if sig else None
        return float(grassroots.get(src, pd.Series()).iloc[-1]) if src in grassroots and len(grassroots.get(src, pd.Series())) > 0 else None
    
    signal = EntrySignal(
        ticker=ticker,
        timestamp=datetime.now(timezone.utc),
        price=price,
        iv_p25=iv_p25,
        iv_p10=iv_p10,
        corridor_low=corridor_low,
        corridor_high=corridor_high,
        in_corridor=in_corridor,
        google_trends_velocity=get_src_vel("google_trends"),
        app_reviews_velocity=get_src_vel("app_reviews"),
        gdelt_velocity=get_src_vel("gdelt"),
        github_velocity=get_src_vel("github"),
        c_t_composite=c_t_composite,
        c_t_accelerating=c_t_accelerating,
        c_t_baseline=c_t_baseline,
        t_t_quiet=t_t_quiet,
        w_t_quiet=w_t_quiet,
        adv_ok=adv_ok,
        spread_ok=spread_ok,
        entry_triggered=entry_triggered,
        trigger_reason=trigger_reason,
        degraded=degraded,
    )
    
    college_event(
        "entry_signal_evaluated",
        ticker=ticker,
        price=price,
        iv_p25=iv_p25,
        in_corridor=in_corridor,
        c_t=c_t_composite,
        c_t_accel=c_t_accelerating,
        t_t_quiet=t_t_quiet,
        w_t_quiet=w_t_quiet,
        adv_ok=adv_ok,
        spread_ok=spread_ok,
        triggered=entry_triggered,
        reason=trigger_reason,
    )
    
    return signal


def run_entry_timing(
    tickers: Optional[List[str]] = None,
    prices: Optional[Dict[str, float]] = None,
) -> Dict[str, EntrySignal]:
    """Main entry point: run entry timing on DCF-passed tickers."""
    if tickers is None:
        tickers = [
            "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA",
            "JPM", "V", "JNJ", "WMT", "PG", "MA", "UNH", "HD",
            "DIS", "PYPL", "ADBE", "NFLX", "CRM", "INTC", "CSCO",
            "PFE", "TMO", "ABBV", "ACN", "COST"
        ]
    
    config = load_position_sizing_config()
    dcf_results = load_dcf_results()
    
    # Filter to DCF-passed tickers
    passed_tickers = [t for t in tickers if dcf_results.get(t, {}).get("passes_screen", False)]
    logger.info(f"Entry Timing: {len(passed_tickers)} DCF-passed tickers")
    
    # Load current prices if not provided
    if prices is None:
        prices = {}
        for t in passed_tickers:
            yf_path = Path(f"data/source/yfinance/{t}/prices.parquet")
            if yf_path.exists():
                try:
                    df = pd.read_parquet(yf_path)
                    if not df.empty:
                        prices[t] = float(df["Close"].iloc[-1])
                except Exception:
                    pass
    
    # Phase 3: Compute real multi-source C_t for all passed tickers
    logger.info("Computing multi-source C_t signals...")
    c_t_signals = get_real_c_t_signals(passed_tickers, lookback_days=60)
    
    checkpoint = EntryCheckpoint()
    signals = {}
    
    for ticker in passed_tickers:
        if ticker not in prices:
            logger.warning(f"No price data for {ticker}")
            continue
        
        if ticker not in dcf_results:
            logger.warning(f"No DCF result for {ticker}")
            continue
        
        signal = evaluate_entry_signal(ticker, prices[ticker], dcf_results[ticker], config, c_t_signals)
        signals[ticker] = signal
        checkpoint.tickers_done.append(ticker)
        checkpoint.signals[ticker] = signal.to_dict()
        checkpoint.updated_at = datetime.now(timezone.utc).isoformat()
    
    # Save checkpoint
    path = CHECKPOINT_DIR / "entry_timing_checkpoint.json"
    with path.open("w") as f:
        json.dump({
            "tickers_done": checkpoint.tickers_done,
            "signals": checkpoint.signals,
            "updated_at": checkpoint.updated_at,
        }, f, indent=2, default=str)
    
    # Save results
    out_path = CHECKPOINT_DIR / "entry_signals.json"
    with out_path.open("w") as f:
        json.dump({t: s.to_dict() for t, s in signals.items()}, f, indent=2, default=str)
    
    triggered = [t for t, s in signals.items() if s.entry_triggered]
    logger.info(f"Entry Timing complete: {len(triggered)}/{len(passed_tickers)} triggered")
    college_event("entry_timing_batch_complete", triggered=triggered, total=len(passed_tickers))
    
    return signals


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    # Synthetic test
    config = load_position_sizing_config()
    
    # Mock DCF result
    mock_dcf = {
        "iv_p25": 150.0,
        "iv_p10": 130.0,
        "discount_sector": 0.12,
        "passes_screen": True,
    }
    
    # Test corridor
    low, high = compute_asymmetric_corridor(150.0, 0.12)
    print(f"Asymmetric corridor: [{low:.2f}, {high:.2f}]")
    
    # Test with price in corridor
    signal = evaluate_entry_signal("TEST", 140.0, mock_dcf, config)
    print(f"Price 140: in_corridor={signal.in_corridor}, triggered={signal.entry_triggered}, reason={signal.trigger_reason}")
    
    # Test with price above corridor
    signal = evaluate_entry_signal("TEST", 160.0, mock_dcf, config)
    print(f"Price 160: in_corridor={signal.in_corridor}, triggered={signal.entry_triggered}, reason={signal.trigger_reason}")
    
    # Test with price below corridor
    signal = evaluate_entry_signal("TEST", 120.0, mock_dcf, config)
    print(f"Price 120: in_corridor={signal.in_corridor}, triggered={signal.entry_triggered}, reason={signal.trigger_reason}")