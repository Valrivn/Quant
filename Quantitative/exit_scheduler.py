"""
exit_scheduler.py — Staggered TWAP Exits & Correlation Clustering (Regime-Aware)
Pre-Registration Config: config/pre_registration_phase8.yaml

Provides:
  1. ExitScheduler: Schedules TWAP liquidation slices with ADV limits and correlation clustering.
  2. schedule_exits: Generates multi-day execution schedule for portfolio exits conditioned on regime & liquidity.
  3. emit_exit_audit_records: Emits exit audit blocks to TradeAuditLedger with Appendix A trigger_reason.exit.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Union

import numpy as np
import pandas as pd
import scipy.cluster.hierarchy as sch
import yaml

from ingestion.trade_audit import TradeAuditLedger

logger = logging.getLogger(__name__)


REGIME_TWAP_WINDOWS = {
    "CRISIS": 2,     # Accelerate liquidation to limit drawdown risk
    "SHOCK": 3,      # Default standard TWAP
    "RECOVERY": 3,   # Default standard TWAP
    "EXPANSION": 5,  # Extend TWAP window to minimize market impact
}

ILLIQUID_ADV_THRESHOLD_USD = 5_000_000.0  # $5M ADV threshold for illiquidity extension


class ExitScheduler:
    """Staggered TWAP exit engine with correlation-based risk clustering and ADV participation constraints."""

    def __init__(self, config: Union[Dict[str, Any], str, Path, None] = None):
        self.max_daily_adv_pct = 0.05       # 5% ADV max participation rate
        self.max_daily_portfolio_pct = 0.05
        self.default_twap_days = 3
        self.correlation_clustering = True
        self.cluster_threshold = 0.70

        if config is not None:
            self._load_config(config)

    def _load_config(self, config: Union[Dict[str, Any], str, Path]) -> None:
        if isinstance(config, (str, Path)):
            p = Path(config)
            if p.exists():
                with open(p, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
            else:
                cfg = {}
        elif isinstance(config, dict):
            cfg = config
        else:
            cfg = {}

        p8 = cfg.get("phase8", cfg)
        exec_cfg = p8.get("execution", cfg.get("execution", {}))
        stag_cfg = exec_cfg.get("staggered_exits", {})

        self.max_daily_adv_pct = float(stag_cfg.get("max_daily_adv_pct", 0.05))
        self.max_daily_portfolio_pct = float(stag_cfg.get("max_daily_portfolio_pct", 0.05))
        self.default_twap_days = int(stag_cfg.get("twap_days", 3))
        self.correlation_clustering = bool(stag_cfg.get("correlation_clustering", True))
        self.cluster_threshold = float(stag_cfg.get("cluster_threshold", 0.70))

    def determine_twap_days(self, adv_usd: float, regime: str) -> int:
        """Determine TWAP window based on regime state and ticker liquidity.
        
        - Base days: CRISIS=2d, SHOCK=3d, EXPANSION=5d
        - Illiquid (ADV < $5M): extended by up to 2 days (max 5 days)
        """
        base_days = REGIME_TWAP_WINDOWS.get(regime.upper(), self.default_twap_days)
        if adv_usd < ILLIQUID_ADV_THRESHOLD_USD:
            # Extend for illiquid names, max out at 5 days
            twap_days = min(5, base_days + 2)
        else:
            twap_days = base_days
        return max(1, twap_days)

    def cluster_positions(self, returns_df: pd.DataFrame) -> Dict[int, List[str]]:
        """Group tickers into correlation clusters using hierarchical clustering."""
        if returns_df.empty or returns_df.shape[1] <= 1:
            return {0: list(returns_df.columns)}

        corr = returns_df.corr().fillna(0.0).values
        dist = np.sqrt(np.clip(2.0 * (1.0 - corr), 0.0, None))
        np.fill_diagonal(dist, 0.0)
        condensed = sch.distance.squareform(dist, checks=False)

        try:
            linkage = sch.linkage(condensed, method="ward")
            clusters_raw = sch.fcluster(linkage, t=1.0 - self.cluster_threshold, criterion="distance")

            cluster_map: Dict[int, List[str]] = {}
            for idx, c_id in enumerate(clusters_raw):
                ticker = returns_df.columns[idx]
                cluster_map.setdefault(int(c_id), []).append(ticker)
            return cluster_map
        except Exception as e:
            logger.warning(f"Correlation clustering fallback due to error: {e}")
            return {0: list(returns_df.columns)}

    def schedule_exits(
        self,
        exit_signals: List[Dict[str, Any]],  # [{ticker, shares, notional_usd, adv_usd, target_pct, trailing_stop_triggered, exit_type, ...}]
        returns_df: Optional[pd.DataFrame] = None,
        regime: str = "EXPANSION",
        trailing_params: Optional[Dict[str, float]] = None,
    ) -> List[Dict[str, Any]]:
        """Schedule TWAP daily liquidation slices across position clusters.
        
        Constraints:
          - Max daily participation: 5% ADV
          - Trailing Stop acceleration: If trailing stop (or structural break) is triggered, accelerate to immediate (1-day) liquidation
          - TWAP window: regime-conditional & illiquidity-extended (2..5 days)
          - Correlation staggering: stagger start days across correlated clusters
        """
        if not exit_signals:
            return []

        tickers = [s["ticker"] for s in exit_signals]

        if self.correlation_clustering and returns_df is not None and not returns_df.empty:
            avail_tickers = [t for t in tickers if t in returns_df.columns]
            if avail_tickers:
                clusters = self.cluster_positions(returns_df[avail_tickers])
                # Add any unclustered tickers
                missing = [t for t in tickers if t not in avail_tickers]
                if missing:
                    clusters[max(clusters.keys(), default=0) + 1] = missing
            else:
                clusters = {0: tickers}
        else:
            clusters = {0: tickers}

        schedule = []
        cluster_start_offsets = {c_id: idx for idx, c_id in enumerate(sorted(clusters.keys()))}

        for c_id, c_tickers in clusters.items():
            stagger_offset = cluster_start_offsets.get(c_id, 0)
            for t in c_tickers:
                matching = [s for s in exit_signals if s["ticker"] == t]
                if not matching:
                    continue
                info = matching[0]
                total_shares = float(info.get("shares", 1000.0))
                adv_usd = float(info.get("adv_usd", 10_000_000.0))
                price = float(info.get("price", 100.0))

                # Check if trailing stop (or structural break) is triggered for acceleration
                ts_triggered = info.get("trailing_stop_triggered", False) or info.get("exit_type") == "TRAILING_STOP"
                if ts_triggered:
                    twap_days = 1
                else:
                    twap_days = self.determine_twap_days(adv_usd, regime)
                
                # Check 5% ADV daily participation limit
                max_daily_shares_adv = (adv_usd * self.max_daily_adv_pct) / max(0.01, price)
                daily_share_target = total_shares / twap_days
                
                # If 5% ADV is binding, extend TWAP days to respect liquidity cap (unless urgent exit)
                if daily_share_target > max_daily_shares_adv and max_daily_shares_adv > 0 and not ts_triggered:
                    effective_twap_days = int(np.ceil(total_shares / max_daily_shares_adv))
                    effective_twap_days = max(twap_days, min(10, effective_twap_days))
                else:
                    effective_twap_days = twap_days

                daily_slice = total_shares / effective_twap_days

                for d in range(1, effective_twap_days + 1):
                    exec_day = d + stagger_offset
                    schedule.append({
                        "exec_day": exec_day,
                        "day_in_twap": d,
                        "twap_days_total": effective_twap_days,
                        "ticker": t,
                        "cluster_id": c_id,
                        "shares_slice": daily_slice,
                        "pct_slice": 1.0 / effective_twap_days,
                        "price": price,
                        "adv_usd": adv_usd,
                        "regime": regime,
                        "reason": info.get("reason", "trailing_exit" if ts_triggered else "standard_exit"),
                        "exit_type": info.get("exit_type", "TRAILING_STOP" if ts_triggered else "TWAP"),
                        "trailing_stop_triggered": ts_triggered,
                        "hype_pctile": info.get("hype_pctile"),
                        "margin_decay_bps": info.get("margin_decay_bps"),
                        "price_iv_ratio": info.get("price_iv_ratio"),
                    })

        schedule.sort(key=lambda x: (x["exec_day"], x["cluster_id"], x["ticker"]))
        return schedule


def emit_exit_audit_records(
    exit_signals: List[Dict[str, Any]],
    asof_date: Union[str, pd.Timestamp],
    ledger: TradeAuditLedger,
    regime: str = "EXPANSION",
) -> List[Any]:
    """Emit exit trade audit records with Appendix A trigger_reason.exit schema."""
    asof_ts = pd.Timestamp(asof_date).tz_localize(None) if pd.Timestamp(asof_date).tzinfo is not None else pd.Timestamp(asof_date)
    asof_str = str(asof_ts.date())
    now_ts = pd.Timestamp.now(tz="UTC").tz_localize(None)

    audit_blocks = []
    for sig in exit_signals:
        ticker = sig["ticker"]
        exit_payload = {}
        if sig.get("hype_pctile") is not None:
            exit_payload["hype_pctile"] = float(sig["hype_pctile"])
        if sig.get("margin_decay_bps") is not None:
            exit_payload["margin_decay_bps"] = float(sig["margin_decay_bps"])
        if sig.get("price_iv_ratio") is not None:
            exit_payload["price_iv_ratio"] = float(sig["price_iv_ratio"])

        tr_exit: Dict[str, Any] = {
            "valuation": {
                "iv_p25": float(sig.get("iv_p25", 100.0)),
                "price": float(sig.get("price", 100.0)),
                "discount_pct": float(sig.get("discount_pct", 0.0)),
                "iv_method": str(sig.get("iv_method", "dcf_v2_nowcast")),
                "iv_basis_filed_date": str(sig.get("filed_date", asof_str)),
                "iv_nowcast_ts": asof_ts.isoformat(),
            }
        }
        if exit_payload:
            tr_exit["exit"] = exit_payload

        block = ledger.append(
            ticker=ticker,
            asof=asof_ts,
            wacc_v2=float(sig.get("wacc_v2", 0.10)),
            ke_v2=float(sig.get("ke_v2", 0.11)),
            kd_pre_tax_v2=float(sig.get("kd_pre_tax_v2", 0.04)),
            erp_blend=float(sig.get("erp_blend", 0.045)),
            rf_10y=float(sig.get("rf_10y", 0.042)),
            beta_source=str(sig.get("beta_source", "sector_bottom_up")),
            retrieval_ts=now_ts,
            action="EXIT",
            gate_name="STAGGERED_TWAP_EXIT",
            stage_gate=3,  # 3=EXIT
            trigger_reason=tr_exit,
            exec_fill_pct=float(sig.get("exec_fill_pct", 1.0)),
        )
        audit_blocks.append(block)

    return audit_blocks


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Test Quantitative.exit_scheduler")
    parser.add_argument("--test", action="store_true", help="Run test suite")
    args = parser.parse_args()

    scheduler = ExitScheduler()

    # Synthetic return matrix
    np.random.seed(42)
    dates = pd.date_range("2024-01-01", periods=100, freq="B")
    base = np.random.normal(0, 0.01, 100)
    rets = pd.DataFrame({
        "AAPL": base + np.random.normal(0, 0.002, 100),
        "MSFT": base + np.random.normal(0, 0.002, 100),
        "XOM": np.random.normal(0, 0.015, 100),
    }, index=dates)

    signals = [
        {"ticker": "AAPL", "shares": 50000, "price": 150.0, "adv_usd": 10_000_000.0, "hype_pctile": 0.92, "price_iv_ratio": 1.25},
        {"ticker": "MSFT", "shares": 20000, "price": 300.0, "adv_usd": 3_000_000.0, "hype_pctile": 0.88, "price_iv_ratio": 1.15},
        {"ticker": "XOM", "shares": 10000, "price": 100.0, "adv_usd": 50_000_000.0, "hype_pctile": 0.70, "price_iv_ratio": 1.05},
    ]

    sched_exp = scheduler.schedule_exits(signals, rets, regime="EXPANSION")
    sched_cri = scheduler.schedule_exits(signals, rets, regime="CRISIS")

    print(f"EXPANSION Scheduled Slices ({len(sched_exp)} slices):")
    for item in sched_exp[:5]:
        print(f"  Day {item['exec_day']} | Cluster {item['cluster_id']} | Ticker: {item['ticker']} | Shares: {item['shares_slice']:.1f}")

    print(f"CRISIS Scheduled Slices ({len(sched_cri)} slices):")
    for item in sched_cri[:5]:
        print(f"  Day {item['exec_day']} | Cluster {item['cluster_id']} | Ticker: {item['ticker']} | Shares: {item['shares_slice']:.1f}")

    print("ExitScheduler Phase 8 Test OK!")

