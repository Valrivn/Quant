"""
swap_barrier.py — Phase 8 Exponential Barrier Swap Logic (Regime-Conditional)
Pre-Registration Config: config/pre_registration_phase8.yaml
Regime Thresholds Config: config/regime_thresholds.yaml
Quality Thresholds Config: config/quality_thresholds.yaml

Provides:
  1. ExponentialSwapBarrier: Dynamic regime-conditional swap threshold solver with OMR moat protection.
  2. evaluate_swap_candidates: Evaluates portfolio holdings vs candidate pool and emits trade audit blocks.
"""

from __future__ import annotations

import argparse
import logging
import math
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Union

import numpy as np
import pandas as pd
import yaml

from ingestion.trade_audit import TradeAuditLedger

logger = logging.getLogger(__name__)


REGIME_BARRIER_MULTIPLIERS = {
    "EXPANSION": 1.00,   # Loose barrier
    "RECOVERY": 1.15,    # Medium-loose
    "SHOCK": 1.25,       # Tight barrier
    "CRISIS": 1.50,      # Reverted to 1.50x per CEO ruling B-20260920-001
}


class ExponentialSwapBarrier:
    """Exponential Barrier Swap solver with regime conditioning and OMR moat protection."""

    def __init__(self, config: Union[Dict[str, Any], str, Path, None] = None):
        self.c_bps = 150.0
        self.t_target_months = 24.0
        self.gamma = 0.5
        self.epsilon = 1e-6
        self.moat_omr_threshold = 0.50
        self.moat_penalty_step_bps = 10.0  # +10bps per 0.1 OMR above threshold
        self.moat_penalty_fallback_bps = 100.0

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
        qc = p8.get("quality_composite", cfg.get("quality_composite", {}))
        sb = p8.get("swap_barrier", cfg.get("swap_barrier", {}))

        self.c_bps = float(sb.get("C_bps", 150.0))
        self.t_target_months = float(sb.get("t_target_months", 24.0))
        self.gamma = float(sb.get("gamma", 0.5))
        self.epsilon = float(sb.get("epsilon", 1e-6))
        self.moat_omr_threshold = float(qc.get("moat_omr_threshold", 0.50))
        self.moat_penalty_fallback_bps = float(sb.get("moat_penalty_bps", 100.0))

    def calculate_moat_penalty_bps(self, omr: Optional[float] = None, is_moat_protected: bool = False) -> float:
        """Moat penalty: +10bps per 0.1 OMR above 0.5 threshold.
        
        If OMR is not provided, falls back to 100bps if is_moat_protected is True.
        """
        if omr is not None:
            if omr >= self.moat_omr_threshold:
                excess_omr = omr - self.moat_omr_threshold
                steps = excess_omr / 0.10
                return float(max(0.0, steps * self.moat_penalty_step_bps))
            return 0.0
        return float(self.moat_penalty_fallback_bps) if is_moat_protected else 0.0

    def calculate_barrier_bps(
        self,
        holding_months: float,
        omr: Optional[float] = None,
        is_moat_protected: bool = False,
        regime: str = "EXPANSION",
    ) -> float:
        """Calculate dynamic regime-conditioned swap barrier in basis points.
        
        B(t) = (C_bps * exp(-gamma * t / t_target) + moat_penalty_bps) * regime_multiplier
        """
        holding_clamped = max(0.0, float(holding_months))
        decay_factor = math.exp(-self.gamma * (holding_clamped / self.t_target_months))
        base_barrier = self.c_bps * decay_factor
        moat_penalty = self.calculate_moat_penalty_bps(omr=omr, is_moat_protected=is_moat_protected)
        
        regime_mult = REGIME_BARRIER_MULTIPLIERS.get(regime.upper(), 1.00)
        total_barrier = (base_barrier + moat_penalty) * regime_mult
        return float(total_barrier)

    def is_moat_eligible(self, roic: float, wacc: float, omr: float) -> bool:
        """Check moat condition: ROIC > WACC + 0.02 AND OMR >= 0.5."""
        return (roic > (wacc + 0.02)) and (omr >= self.moat_omr_threshold)

    def should_swap(
        self,
        upside_i: float,  # upside of current holding i (e.g. IV_i / P_i - 1)
        upside_j: float,  # upside of candidate j (e.g. IV_j / P_j - 1)
        holding_months: float,
        omr_i: Optional[float] = None,
        is_moat_protected: bool = False,
        regime: str = "EXPANSION",
        **kwargs,
    ) -> Tuple[bool, float, float]:
        """Determine if candidate j offers enough edge to breach swap barrier over holding i.
        
        Returns: (should_swap, edge_bps, barrier_bps)
        """
        edge_bps = (upside_j - upside_i) * 10000.0
        barrier_bps = self.calculate_barrier_bps(
            holding_months=holding_months,
            omr=omr_i,
            is_moat_protected=is_moat_protected,
            regime=regime,
        )
        
        should = edge_bps > (barrier_bps + self.epsilon)
        
        # KILL SWITCH (forward-looking)
        kill = (kwargs.get("hmm_probs", {}).get("CRISIS", 0) > 0.60 and 
                kwargs.get("price_iv_ratio", 1.0) < 0.80 and
                kwargs.get("spread_ratio", 1.0) > 2.0)
        if kill:
            return False, edge_bps, barrier_bps

        return should, float(edge_bps), float(barrier_bps)


def evaluate_swap_candidates(
    holdings: List[Dict[str, Any]],
    candidates: List[Dict[str, Any]],
    asof_date: Union[str, pd.Timestamp],
    regime_df: Optional[pd.DataFrame] = None,
    regime_path: Optional[Union[str, Path]] = "data/regime/regime_daily_nowcast.parquet",
    ledger: Optional[TradeAuditLedger] = None,
    config_path: str = "config/pre_registration_phase8.yaml",
) -> List[Dict[str, Any]]:
    """Evaluate swap opportunities between current portfolio holdings and candidate tickers.
    
    Reads daily regime nowcast if regime_df is not directly passed.
    Emits trade audit blocks with Appendix A trigger_reason.swap schema for executed swaps.
    """
    asof_ts = pd.Timestamp(asof_date)

    # 1. Determine regime state
    regime_state = "EXPANSION"
    asof_tz_naive = asof_ts.tz_localize(None) if asof_ts.tzinfo is not None else asof_ts

    if regime_df is not None and not regime_df.empty:
        reg_ts = pd.to_datetime(regime_df["timestamp_utc"]).dt.tz_localize(None)
        reg_row = regime_df[reg_ts <= asof_tz_naive]
        if not reg_row.empty:
            regime_state = str(reg_row.iloc[-1].get("regime_state", "EXPANSION"))
    elif regime_path and Path(regime_path).exists():
        try:
            df_reg = pd.read_parquet(regime_path)
            reg_ts = pd.to_datetime(df_reg["timestamp_utc"]).dt.tz_localize(None)
            reg_row = df_reg[reg_ts <= asof_tz_naive]
            if not reg_row.empty:
                regime_state = str(reg_row.iloc[-1].get("regime_state", "EXPANSION"))
        except Exception as e:
            logger.warning(f"Could not load regime daily nowcast from {regime_path}: {e}")

    solver = ExponentialSwapBarrier(config=config_path)
    swaps_executed = []
    audit_blocks = []

    used_candidates = set()
    for h in holdings:
        ticker_i = h["ticker"]
        upside_i = float(h.get("upside", (h.get("iv_p25", 100.0) / max(0.01, h.get("price", 100.0))) - 1.0))
        t_held = float(h.get("t_held_months", 1.0))
        omr_i = h.get("omr")
        is_moat_i = bool(h.get("is_moat", False if omr_i is None else (omr_i >= 0.5)))
        fractal_vol_i = float(h.get("fractal_vol", 0.02))

        # Find best candidate j
        best_cand = None
        best_edge = -999999.0
        best_barrier = 0.0

        for c in candidates:
            if c["ticker"] == ticker_i or c["ticker"] in used_candidates:
                continue
            upside_j = float(c.get("upside", (c.get("iv_p25", 100.0) / max(0.01, c.get("price", 100.0))) - 1.0))
            should, edge_bps, barrier_bps = solver.should_swap(
                upside_i=upside_i,
                upside_j=upside_j,
                holding_months=t_held,
                omr_i=omr_i,
                is_moat_protected=is_moat_i,
                regime=regime_state,
            )
            if should and edge_bps > best_edge:
                best_edge = edge_bps
                best_barrier = barrier_bps
                best_cand = c

        if best_cand is not None:
            used_candidates.add(best_cand["ticker"])
            moat_pen_bps = solver.calculate_moat_penalty_bps(omr=omr_i, is_moat_protected=is_moat_i)
            swap_record = {
                "asof_date": asof_ts,
                "regime": regime_state,
                "holding_sold": ticker_i,
                "candidate_bought": best_cand["ticker"],
                "edge_bps": best_edge,
                "barrier_bps": best_barrier,
                "t_held_months": t_held,
                "omr": omr_i,
            }
            swaps_executed.append(swap_record)

            if ledger is not None:
                asof_str = str(asof_ts.date())
                now_ts = pd.Timestamp.now(tz="UTC").tz_localize(None)

                # Sold holding block
                tr_sold = {
                    "valuation": {
                        "iv_p25": float(h.get("iv_p25", 100.0)),
                        "price": float(h.get("price", 100.0)),
                        "discount_pct": float(h.get("discount_pct", 0.0)),
                        "iv_method": str(h.get("iv_method", "dcf_v2_nowcast")),
                        "iv_basis_filed_date": str(h.get("filed_date", asof_str)),
                        "iv_nowcast_ts": asof_ts.isoformat(),
                    },
                    "swap": {
                        "barrier_level": float(best_barrier),
                        "moat_penalty": float(moat_pen_bps),
                        "fractal_vol": float(fractal_vol_i),
                        "t_held_months": float(t_held),
                    },
                }
                b_sold = ledger.append(
                    ticker=ticker_i,
                    asof=asof_ts,
                    wacc_v2=float(h.get("wacc_v2", 0.10)),
                    ke_v2=float(h.get("ke_v2", 0.11)),
                    kd_pre_tax_v2=float(h.get("kd_pre_tax_v2", 0.04)),
                    erp_blend=float(h.get("erp_blend", 0.045)),
                    rf_10y=float(h.get("rf_10y", 0.042)),
                    beta_source=str(h.get("beta_source", "sector_bottom_up")),
                    retrieval_ts=now_ts,
                    action="SWAP_OUT",
                    gate_name="EXPONENTIAL_SWAP_BARRIER",
                    stage_gate=1,
                    trigger_reason=tr_sold,
                )

                # Bought candidate block
                tr_bought = {
                    "valuation": {
                        "iv_p25": float(best_cand.get("iv_p25", 120.0)),
                        "price": float(best_cand.get("price", 100.0)),
                        "discount_pct": float(best_cand.get("discount_pct", 0.15)),
                        "iv_method": str(best_cand.get("iv_method", "dcf_v2_nowcast")),
                        "iv_basis_filed_date": str(best_cand.get("filed_date", asof_str)),
                        "iv_nowcast_ts": asof_ts.isoformat(),
                    },
                    "swap": {
                        "barrier_level": float(best_barrier),
                        "moat_penalty": 0.0,
                        "fractal_vol": float(best_cand.get("fractal_vol", 0.02)),
                        "t_held_months": 0.0,
                    },
                }
                b_bought = ledger.append(
                    ticker=best_cand["ticker"],
                    asof=asof_ts,
                    wacc_v2=float(best_cand.get("wacc_v2", 0.10)),
                    ke_v2=float(best_cand.get("ke_v2", 0.11)),
                    kd_pre_tax_v2=float(best_cand.get("kd_pre_tax_v2", 0.04)),
                    erp_blend=float(best_cand.get("erp_blend", 0.045)),
                    rf_10y=float(best_cand.get("rf_10y", 0.042)),
                    beta_source=str(best_cand.get("beta_source", "sector_bottom_up")),
                    retrieval_ts=now_ts,
                    action="SWAP_IN",
                    gate_name="EXPONENTIAL_SWAP_BARRIER",
                    stage_gate=1,
                    trigger_reason=tr_bought,
                )
                audit_blocks.extend([b_sold, b_bought])

    return swaps_executed


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Test Quantitative.swap_barrier")
    parser.add_argument("--test", action="store_true", help="Run test suite")
    args = parser.parse_args()

    sb = ExponentialSwapBarrier()
    # Test 1: EXPANSION vs CRISIS
    should_exp, edge, bar_exp = sb.should_swap(upside_i=0.10, upside_j=0.25, holding_months=6.0, omr_i=0.7, regime="EXPANSION")
    should_cri, _, bar_cri = sb.should_swap(upside_i=0.10, upside_j=0.25, holding_months=6.0, omr_i=0.7, regime="CRISIS")

    print(f"EXPANSION: Edge={edge:.0f}bps, Barrier={bar_exp:.0f}bps => Swap={should_exp}")
    print(f"CRISIS:    Edge={edge:.0f}bps, Barrier={bar_cri:.0f}bps => Swap={should_cri}")
    assert bar_cri > bar_exp, "CRISIS barrier must be tighter than EXPANSION barrier"

    print("ExponentialSwapBarrier Phase 8 OK!")

