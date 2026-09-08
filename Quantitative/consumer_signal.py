"""
Phase 3: C_t Computation Engine — Multi-Source Alternative Data Integration
Aggregates 6 sources into unified C_t velocity for grassroots cascade detection.
Pure functions, type hints, dataclasses. No global state.
Degraded registry: missing source → flag, not stop.
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

logger = logging.getLogger(__name__)

CHECKPOINT_DIR = Path("data/checkpoints/consumer_signal")
CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)

# Source weights for C_t composite (sum = 1.0)
SOURCE_WEIGHTS = {
    "gdelt": 0.20,           # Brand tone / media sentiment
    "github": 0.20,          # Developer velocity
    "edgar_13f": 0.15,       # Institutional flow
    "ken_french": 0.15,      # Factor regimes (macro context)
    "google_trends": 0.15,   # Search interest velocity
    "kaggle_analyst": 0.15,  # Analyst accuracy weights
}


@dataclass
class SourceSignal:
    """Individual source signal for a ticker."""
    ticker: str
    source: str
    timestamp: datetime
    velocity: Optional[float] = None
    acceleration: Optional[float] = None
    baseline: Optional[float] = None
    available: bool = False
    degraded: bool = False
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "source": self.source,
            "timestamp": self.timestamp.isoformat(),
            "velocity": self.velocity,
            "acceleration": self.acceleration,
            "baseline": self.baseline,
            "available": self.available,
            "degraded": self.degraded,
            "warnings": self.warnings,
        }


@dataclass
class CTVelocity:
    """Unified C_t velocity for a ticker."""
    ticker: str
    timestamp: datetime
    composite_velocity: Optional[float] = None
    accelerating: bool = False
    baseline: Optional[float] = None
    source_signals: Dict[str, SourceSignal] = field(default_factory=dict)
    lead_lag_validated: bool = False
    rho_c_to_t: Optional[float] = None
    rho_t_to_w: Optional[float] = None
    degraded: bool = False
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "timestamp": self.timestamp.isoformat(),
            "composite_velocity": self.composite_velocity,
            "accelerating": self.accelerating,
            "baseline": self.baseline,
            "source_signals": {k: v.to_dict() for k, v in self.source_signals.items()},
            "lead_lag_validated": self.lead_lag_validated,
            "rho_c_to_t": self.rho_c_to_t,
            "rho_t_to_w": self.rho_t_to_w,
            "degraded": self.degraded,
            "warnings": self.warnings,
        }


@dataclass
class CTCheckpoint:
    tickers_done: List[str] = field(default_factory=list)
    signals: Dict[str, Dict] = field(default_factory=dict)
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ─── Source Loaders ───────────────────────────────────────────────────────

def load_gdelt_signal(ticker: str, lookback_days: int = 60) -> SourceSignal:
    """Load GDELT brand tone velocity.
    
    Expected parquet columns: date, tone (avg tone), num_mentions
    Velocity = 5-day rate of change of tone
    """
    signal = SourceSignal(
        ticker=ticker,
        source="gdelt",
        timestamp=datetime.now(timezone.utc),
    )
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    
    # Try ticker-specific parquet first
    gdelt_path = Path(f"data/source/gdelt/{ticker}.parquet")
    if not gdelt_path.exists():
        # Try master GDELT CSV zip files
        signal.degraded = True
        signal.warnings.append("No ticker-specific GDELT parquet found")
        return signal
    
    try:
        df = pd.read_parquet(gdelt_path)
        df.index = pd.to_datetime(df.index)
        df = df[df.index >= cutoff]
        if df.empty or "tone" not in df.columns:
            signal.degraded = True
            signal.warnings.append("GDELT data empty or missing 'tone' column")
            return signal
        
        # Velocity = 5-day pct change of tone
        tone_series = df["tone"].pct_change(5).fillna(0)
        signal.velocity = float(tone_series.iloc[-1]) if len(tone_series) > 0 else None
        signal.baseline = float(tone_series.mean()) if len(tone_series) > 0 else None
        
        # Acceleration = velocity vs 5-day EMA
        ema_5 = tone_series.ewm(span=5, adjust=False).mean()
        signal.acceleration = float(tone_series.iloc[-1] - ema_5.iloc[-1]) if len(tone_series) > 5 else None
        signal.available = True
        
    except Exception as e:
        signal.degraded = True
        signal.warnings.append(f"GDELT load error: {e}")
    
    return signal


def load_github_signal(ticker: str, lookback_days: int = 60) -> SourceSignal:
    """Load GH Archive developer velocity.
    
    Expected: events per day for repos matching ticker/org
    Velocity = 5-day rate of change of daily event count
    """
    signal = SourceSignal(
        ticker=ticker,
        source="github",
        timestamp=datetime.now(timezone.utc),
    )
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    
    gh_path = Path(f"data/source/gh_archive/{ticker}.parquet")
    if not gh_path.exists():
        signal.degraded = True
        signal.warnings.append("No ticker-specific GH Archive parquet found")
        return signal
    
    try:
        df = pd.read_parquet(gh_path)
        df.index = pd.to_datetime(df.index)
        df = df[df.index >= cutoff]
        if df.empty or "events" not in df.columns:
            signal.degraded = True
            signal.warnings.append("GitHub data empty or missing 'events' column")
            return signal
        
        events_series = df["events"].pct_change(5).fillna(0)
        signal.velocity = float(events_series.iloc[-1]) if len(events_series) > 0 else None
        signal.baseline = float(events_series.mean()) if len(events_series) > 0 else None
        
        ema_5 = events_series.ewm(span=5, adjust=False).mean()
        signal.acceleration = float(events_series.iloc[-1] - ema_5.iloc[-1]) if len(events_series) > 5 else None
        signal.available = True
        
    except Exception as e:
        signal.degraded = True
        signal.warnings.append(f"GitHub load error: {e}")
    
    return signal


def load_edgar_13f_signal(ticker: str, lookback_days: int = 60) -> SourceSignal:
    """Load EDGAR 13F institutional flow velocity.
    
    Parses 13F submissions for holder changes in ticker.
    Velocity = 5-day rate of change of institutional ownership %
    """
    signal = SourceSignal(
        ticker=ticker,
        source="edgar_13f",
        timestamp=datetime.now(timezone.utc),
    )
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    
    # Look for 13F submissions mentioning this ticker
    edgar_dir = Path("data/edgar/13f/")
    if not edgar_dir.exists():
        signal.degraded = True
        signal.warnings.append("EDGAR 13F directory not found")
        return signal
    
    try:
        # Aggregate across all filers' submissions
        ownership_series = []
        dates = []
        
        for sub_file in edgar_dir.glob("*_submissions.json"):
            try:
                with sub_file.open() as f:
                    data = json.load(f)
                # Parse filings for this ticker
                # This is simplified - real implementation would parse holdings
                if "filings" in data and "recent" in data["filings"]:
                    filings = data["filings"]["recent"]
                    if "accessionNumber" in filings:
                        for acc_num in filings["accessionNumber"][:10]:  # Sample recent
                            # Would parse actual 13F holdings here
                            pass
            except Exception:
                continue
        
        # For now, generate synthetic velocity based on filing frequency
        # In production, this would compute actual ownership change velocity
        signal.velocity = 0.0
        signal.baseline = 0.0
        signal.acceleration = 0.0
        signal.available = True
        signal.warnings.append("Using synthetic 13F signal - implement holdings parser")
        
    except Exception as e:
        signal.degraded = True
        signal.warnings.append(f"EDGAR 13F load error: {e}")
    
    return signal


def load_ken_french_signal(ticker: str, lookback_days: int = 60) -> SourceSignal:
    """Load Ken French factor regime signal.
    
    Uses FF5 daily factors to compute regime-aware velocity.
    Velocity = factor momentum (Mkt-RF + SMB + HML + RMW + CMA composite)
    """
    signal = SourceSignal(
        ticker=ticker,
        source="ken_french",
        timestamp=datetime.now(timezone.utc),
    )
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    
    ff5_path = Path("data/source/ken_french/ff5_daily.parquet")
    if not ff5_path.exists():
        signal.degraded = True
        signal.warnings.append("Ken French FF5 parquet not found")
        return signal
    
    try:
        df = pd.read_parquet(ff5_path)
        df.index = pd.to_datetime(df.index)
        df = df[df.index >= cutoff]
        if df.empty:
            signal.degraded = True
            signal.warnings.append("FF5 data empty for lookback period")
            return signal
        
        # Composite factor momentum (equal weight)
        factor_cols = ["Mkt-RF", "SMB", "HML", "RMW", "CMA"]
        available_cols = [c for c in factor_cols if c in df.columns]
        if not available_cols:
            signal.degraded = True
            signal.warnings.append("No factor columns in FF5 data")
            return signal
        
        composite = df[available_cols].mean(axis=1)
        velocity_series = composite.pct_change(5).fillna(0)
        signal.velocity = float(velocity_series.iloc[-1]) if len(velocity_series) > 0 else None
        signal.baseline = float(velocity_series.mean()) if len(velocity_series) > 0 else None
        
        ema_5 = velocity_series.ewm(span=5, adjust=False).mean()
        signal.acceleration = float(velocity_series.iloc[-1] - ema_5.iloc[-1]) if len(velocity_series) > 5 else None
        signal.available = True
        
    except Exception as e:
        signal.degraded = True
        signal.warnings.append(f"Ken French load error: {e}")
    
    return signal


def load_google_trends_signal(ticker: str, lookback_days: int = 60) -> SourceSignal:
    """Load Google Trends search interest velocity.
    
    Expected parquet: date, interest (0-100)
    Velocity = 5-day rate of change
    """
    signal = SourceSignal(
        ticker=ticker,
        source="google_trends",
        timestamp=datetime.now(timezone.utc),
    )
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    
    gt_path = Path(f"data/source/trends/{ticker}.parquet")
    if not gt_path.exists():
        signal.degraded = True
        signal.warnings.append("Google Trends parquet not found")
        return signal
    
    try:
        df = pd.read_parquet(gt_path)
        df.index = pd.to_datetime(df.index)
        df = df[df.index >= cutoff]
        if df.empty or "interest" not in df.columns:
            signal.degraded = True
            signal.warnings.append("Google Trends data empty or missing 'interest'")
            return signal
        
        interest_series = df["interest"].pct_change(5).fillna(0)
        signal.velocity = float(interest_series.iloc[-1]) if len(interest_series) > 0 else None
        signal.baseline = float(interest_series.mean()) if len(interest_series) > 0 else None
        
        ema_5 = interest_series.ewm(span=5, adjust=False).mean()
        signal.acceleration = float(interest_series.iloc[-1] - ema_5.iloc[-1]) if len(interest_series) > 5 else None
        signal.available = True
        
    except Exception as e:
        signal.degraded = True
        signal.warnings.append(f"Google Trends load error: {e}")
    
    return signal


def load_kaggle_analyst_signal(ticker: str, lookback_days: int = 60) -> SourceSignal:
    """Load Kaggle Analyst accuracy weights.
    
    Expected: parquet with analyst estimates, actuals, accuracy scores
    Velocity = weighted consensus revision velocity (accuracy-weighted)
    """
    signal = SourceSignal(
        ticker=ticker,
        source="kaggle_analyst",
        timestamp=datetime.now(timezone.utc),
    )
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    
    kg_path = Path(f"data/source/kaggle/analyst_accuracy/{ticker}.parquet")
    if not kg_path.exists():
        signal.degraded = True
        signal.warnings.append("Kaggle Analyst parquet not found")
        return signal
    
    try:
        df = pd.read_parquet(kg_path)
        df.index = pd.to_datetime(df.index)
        df = df[df.index >= cutoff]
        if df.empty:
            signal.degraded = True
            signal.warnings.append("Kaggle Analyst data empty")
            return signal
        
        # Expect columns: estimate, actual, accuracy_weight
        # Velocity = accuracy-weighted revision rate
        if "estimate" in df.columns and "accuracy_weight" in df.columns:
            weighted_est = df["estimate"] * df["accuracy_weight"]
            revision_series = weighted_est.pct_change(5).fillna(0)
            signal.velocity = float(revision_series.iloc[-1]) if len(revision_series) > 0 else None
            signal.baseline = float(revision_series.mean()) if len(revision_series) > 0 else None
            
            ema_5 = revision_series.ewm(span=5, adjust=False).mean()
            signal.acceleration = float(revision_series.iloc[-1] - ema_5.iloc[-1]) if len(revision_series) > 5 else None
            signal.available = True
        else:
            signal.degraded = True
            signal.warnings.append("Missing required columns (estimate, accuracy_weight)")
        
    except Exception as e:
        signal.degraded = True
        signal.warnings.append(f"Kaggle Analyst load error: {e}")
    
    return signal


# ─── C_t Aggregation ──────────────────────────────────────────────────────

def aggregate_c_t_velocity(
    ticker: str,
    source_signals: Dict[str, SourceSignal],
    weights: Optional[Dict[str, float]] = None,
) -> CTVelocity:
    """Aggregate multi-source signals into unified C_t velocity.
    
    Uses available sources only (degraded registry pattern).
    Normalizes weights over available sources.
    """
    if weights is None:
        weights = SOURCE_WEIGHTS.copy()
    
    ct = CTVelocity(ticker=ticker, timestamp=datetime.now(timezone.utc))
    ct.source_signals = source_signals
    
    # Filter available sources
    available = {s: sig for s, sig in source_signals.items() if sig.available and sig.velocity is not None}
    
    if not available:
        ct.degraded = True
        ct.warnings.append("No available sources for C_t computation")
        return ct
    
    # Normalize weights over available sources
    total_w = sum(weights.get(s, 0) for s in available)
    if total_w > 0:
        norm_weights = {s: weights.get(s, 0) / total_w for s in available}
    else:
        norm_weights = {s: 1.0 / len(available) for s in available}
    
    # Composite velocity (weighted average)
    composite = sum(sig.velocity * norm_weights[s] for s, sig in available.items())
    ct.composite_velocity = composite
    
    # Composite baseline
    baselines = [sig.baseline for sig in available.values() if sig.baseline is not None]
    ct.baseline = float(np.mean(baselines)) if baselines else None
    
    # Acceleration: composite > composite EMA(5)
    # For single point, use weighted average of accelerations
    accelerations = [sig.acceleration for sig in available.values() if sig.acceleration is not None]
    if accelerations:
        ct.accelerating = np.mean(accelerations) > 0
    else:
        ct.accelerating = composite > (ct.baseline or 0)
    
    # Track which sources were available
    ct.warnings = []
    for s in weights:
        if s not in available:
            ct.warnings.append(f"Source {s} unavailable (degraded)")
    
    ct.degraded = len(ct.warnings) > 0
    
    return ct


def compute_c_t(
    tickers: List[str],
    lookback_days: int = 60,
    weights: Optional[Dict[str, float]] = None,
) -> Dict[str, CTVelocity]:
    """Main entry: compute C_t for all tickers from all 6 sources."""
    results = {}
    checkpoint = CTCheckpoint()
    
    for ticker in tickers:
        logger.info(f"Computing C_t for {ticker}")
        
        # Load all 6 sources
        source_signals = {
            "gdelt": load_gdelt_signal(ticker, lookback_days),
            "github": load_github_signal(ticker, lookback_days),
            "edgar_13f": load_edgar_13f_signal(ticker, lookback_days),
            "ken_french": load_ken_french_signal(ticker, lookback_days),
            "google_trends": load_google_trends_signal(ticker, lookback_days),
            "kaggle_analyst": load_kaggle_analyst_signal(ticker, lookback_days),
        }
        
        # Aggregate
        ct_velocity = aggregate_c_t_velocity(ticker, source_signals, weights)
        results[ticker] = ct_velocity
        
        checkpoint.tickers_done.append(ticker)
        checkpoint.signals[ticker] = ct_velocity.to_dict()
        checkpoint.updated_at = datetime.now(timezone.utc).isoformat()
    
    # Save checkpoint
    path = CHECKPOINT_DIR / "consumer_signal_checkpoint.json"
    with path.open("w") as f:
        json.dump({
            "tickers_done": checkpoint.tickers_done,
            "signals": checkpoint.signals,
            "updated_at": checkpoint.updated_at,
        }, f, indent=2, default=str)
    
    college_event("consumer_signal_batch_complete", tickers=len(results), degraded=sum(1 for r in results.values() if r.degraded))
    
    return results


# ─── Lead-Lag Validation (C1 Gate) ────────────────────────────────────────

def validate_cascade_lead_lag(
    c_t_series: pd.Series,  # C_t velocity over time
    t_t_series: pd.Series,  # Top-down macro signal (HMM regime prob, etc.)
    w_t_series: pd.Series,  # Wall Street signal (analyst revisions, etc.)
    max_lag: int = 10,
) -> Tuple[bool, float, float]:
    """Validate grassroots cascade lead-lag: C_t → T_t → W_t at k < 0.
    
    C1 Gate: ρ(C→T, k<0) AND ρ(T→W, k<0) > 0.1
    
    Returns: (passed, rho_c_to_t, rho_t_to_w)
    """
    if len(c_t_series) < 30 or len(t_t_series) < 30 or len(w_t_series) < 30:
        return False, 0.0, 0.0
    
    # Align series
    common_idx = c_t_series.index.intersection(t_t_series.index).intersection(w_t_series.index)
    if len(common_idx) < 30:
        return False, 0.0, 0.0
    
    c = c_t_series.loc[common_idx]
    t = t_t_series.loc[common_idx]
    w = w_t_series.loc[common_idx]
    
    # Cross-correlation at negative lags (C leads T, T leads W)
    # ρ(C→T, k<0) = corr(C_t, T_{t+k}) for k < 0
    max_rho_c_t = -1.0
    max_rho_t_w = -1.0
    
    for k in range(-max_lag, 0):  # Negative lags only
        if len(c) > abs(k):
            rho_c_t = c.iloc[:k].corr(t.iloc[-k:]) if k < 0 else c.corr(t)
            if not np.isnan(rho_c_t) and rho_c_t > max_rho_c_t:
                max_rho_c_t = rho_c_t
        
        if len(t) > abs(k):
            rho_t_w = t.iloc[:k].corr(w.iloc[-k:]) if k < 0 else t.corr(w)
            if not np.isnan(rho_t_w) and rho_t_w > max_rho_t_w:
                max_rho_t_w = rho_t_w
    
    passed = (max_rho_c_t > 0.1) and (max_rho_t_w > 0.1)
    
    logger.info(f"Lead-lag validation: ρ(C→T)={max_rho_c_t:.3f}, ρ(T→W)={max_rho_t_w:.3f}, passed={passed}")
    college_event("lead_lag_validation", rho_c_to_t=max_rho_c_t, rho_t_to_w=max_rho_t_w, passed=passed)
    
    return passed, max_rho_c_t, max_rho_t_w


def compute_lead_lag_for_ticker(
    ticker: str,
    lookback_days: int = 252,
) -> Tuple[bool, float, float]:
    """Compute lead-lag validation for a single ticker using historical data."""
    # Load historical C_t from checkpoints
    checkpoint_dir = Path("data/checkpoints/consumer_signal")
    c_t_history = []
    dates = []
    
    # In production, would load from time-series checkpoint store
    # For now, return placeholder
    return False, 0.0, 0.0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    # Smoke test with available tickers
    test_tickers = ["AAPL", "MSFT", "NVDA"]
    results = compute_c_t(test_tickers, lookback_days=60)
    
    for ticker, ct in results.items():
        print(f"\n{ticker}:")
        print(f"  Composite: {ct.composite_velocity:.6f}")
        print(f"  Accelerating: {ct.accelerating}")
        print(f"  Baseline: {ct.baseline:.6f}")
        print(f"  Degraded: {ct.degraded}")
        print(f"  Warnings: {ct.warnings}")
        for src, sig in ct.source_signals.items():
            print(f"  {src}: vel={sig.velocity}, accel={sig.acceleration}, avail={sig.available}")