"""
wacc_panel.py — Dynamic WACC Panel & Calculation by Regime
Pre-Registration Config: config/pre_registration_v5.yaml

The function anchors on base_wacc and applies regime-specific proportional adjustments.
EXPANSION is the neutral regime (no change). Other regimes scale the equity cost proportionally.
"""

from __future__ import annotations

from typing import Dict, Any, Optional
import numpy as np
import pandas as pd

REGIME_WACC_ADJUSTMENT = {
    "EXPANSION": {"erp_mult": 1.00, "rf_adj": 0.00, "beta_adj": 1.00},
    "SHOCK":     {"erp_mult": 1.25, "rf_adj": 0.005, "beta_adj": 1.10},
    "CRISIS":    {"erp_mult": 1.50, "rf_adj": 0.015, "beta_adj": 1.20},
    "RECOVERY":  {"erp_mult": 1.10, "rf_adj": 0.002, "beta_adj": 1.05},
}


class WACCPanel:
    """Daily WACC Panel with 100% coverage across universe and regime awareness."""

    def __init__(self, ke_var_threshold: float = 0.0085, config: Optional[Dict[str, Any]] = None):
        if config is not None:
            ke_var_threshold = float(config.get("ke_var_threshold", ke_var_threshold))
        self.ke_var_threshold = ke_var_threshold

    def build_panel(
        self,
        tickers: list[str],
        dates: pd.DatetimeIndex,
        base_wacc_map: Optional[Dict[str, float]] = None,
        regime_series: Optional[pd.Series] = None,
    ) -> pd.DataFrame:
        """Builds daily 100% coverage panel of dynamic WACC values."""
        if base_wacc_map is None:
            base_wacc_map = {t: 0.09 for t in tickers}
        if regime_series is None:
            regime_series = pd.Series("EXPANSION", index=dates)

        records = []
        for dt in dates:
            reg = str(regime_series.get(dt, "EXPANSION"))
            for t in tickers:
                bw = base_wacc_map.get(t, 0.09)
                dw = compute_dynamic_wacc(
                    base_wacc=bw,
                    regime=reg,
                    risk_free_rate=0.04,
                    equity_risk_premium=0.05,
                    beta=1.0,
                )
                records.append({
                    "date": dt,
                    "ticker": t,
                    "base_wacc": bw,
                    "regime": reg,
                    "dynamic_wacc": dw,
                    "ke_var_threshold": self.ke_var_threshold,
                })
        return pd.DataFrame(records)


def compute_dynamic_wacc(
    base_wacc: float,
    regime: str,
    risk_free_rate: float,
    equity_risk_premium: float,
    beta: float,
    cost_of_debt: float = 0.05,
    debt_weight: float = 0.20,
    equity_weight: float = 0.80,
    tax_rate: float = 0.21,
) -> float:
    """Compute regime-adjusted dynamic WACC anchored on base_wacc.
    
    The function anchors on base_wacc and applies regime-specific adjustments
    as proportional scaling of the equity cost component.
    
    EXPANSION is the neutral regime (returns base_wacc unchanged).
    Other regimes scale the equity cost of capital proportionally.
    
    Args:
        base_wacc: The base WACC to anchor on (e.g., 0.09)
        regime: Current regime state (EXPANSION, SHOCK, CRISIS, RECOVERY)
        risk_free_rate: Base risk-free rate
        equity_risk_premium: Base equity risk premium
        beta: Base beta
        cost_of_debt: Cost of debt
        debt_weight: Debt weight in capital structure
        equity_weight: Equity weight in capital structure
        tax_rate: Corporate tax rate
    
    Returns:
        Regime-adjusted WACC anchored on base_wacc (EXPANSION = base_wacc)
    """
    regime_str = regime.upper() if isinstance(regime, str) else "EXPANSION"
    adj = REGIME_WACC_ADJUSTMENT.get(regime_str, {"erp_mult": 1.0, "rf_adj": 0.0, "beta_adj": 1.0})
    
    # EXPANSION is neutral - return base_wacc unchanged
    if regime_str == "EXPANSION":
        return float(base_wacc)
    
    # Compute the proportional change in equity cost of capital
    ke_base = risk_free_rate + beta * equity_risk_premium
    
    # Regime-adjusted equity cost
    erp = equity_risk_premium * adj["erp_mult"]
    rf = risk_free_rate + adj["rf_adj"]
    beta_adj = beta * adj["beta_adj"]
    ke_regime = rf + beta_adj * erp
    
    # Proportional change in equity cost
    if ke_base > 0:
        ke_ratio = ke_regime / ke_base
    else:
        ke_ratio = 1.0
    
    # Decompose base_wacc into equity and debt portions
    kd_after_tax = cost_of_debt * (1.0 - tax_rate)
    debt_portion = debt_weight * kd_after_tax
    equity_portion_base = base_wacc - debt_weight * kd_after_tax
    
    # Scale equity portion by ke_ratio
    equity_portion_adj = equity_portion_base * ke_ratio
    
    # Reconstruct WACC
    wacc = equity_portion_adj + debt_weight * cost_of_debt * (1.0 - tax_rate)
    return float(wacc)


def compute_dynamic_wacc_components(
    base_wacc: float,
    regime: str,
    risk_free_rate: float,
    equity_risk_premium: float,
    beta: float,
    cost_of_debt: float = 0.05,
    debt_weight: float = 0.20,
    equity_weight: float = 0.80,
    tax_rate: float = 0.21,
) -> Dict[str, float]:
    """Compute dynamic WACC with component breakdown for audit."""
    regime_str = regime.upper() if isinstance(regime, str) else "EXPANSION"
    adj = REGIME_WACC_ADJUSTMENT.get(regime_str, {"erp_mult": 1.0, "rf_adj": 0.0, "beta_adj": 1.0})
    
    # Base components
    ke_base = risk_free_rate + beta * equity_risk_premium
    kd_after_tax = cost_of_debt * (1.0 - tax_rate)
    
    # Regime-adjusted components
    erp = equity_risk_premium * adj["erp_mult"]
    rf = risk_free_rate + adj["rf_adj"]
    beta_adj = beta * adj["beta_adj"]
    ke_regime = rf + beta_adj * erp
    
    # Proportional change
    ke_ratio = ke_regime / ke_base if ke_base > 0 else 1.0
    
    kd_after_tax = cost_of_debt * (1.0 - tax_rate)
    debt_portion = debt_weight * kd_after_tax
    equity_portion_base = base_wacc - debt_weight * kd_after_tax
    equity_portion_adj = equity_portion_base * ke_ratio
    
    wacc = equity_portion_adj + debt_portion
    
    return {
        "wacc": float(wacc),
        "base_wacc": float(base_wacc),
        "ke_base": float(risk_free_rate + beta * equity_risk_premium),
        "ke_regime": float(rf + beta * adj["beta_adj"] * equity_risk_premium * adj["erp_mult"]),
        "delta_ke": float((rf + beta * adj["beta_adj"] * equity_risk_premium * adj["erp_mult"]) - (risk_free_rate + beta * equity_risk_premium)),
        "rf_base": float(risk_free_rate),
        "rf_regime": float(risk_free_rate + adj["rf_adj"]),
        "erp_base": float(equity_risk_premium),
        "erp_regime": float(equity_risk_premium * adj["erp_mult"]),
        "beta_base": float(beta),
        "beta_regime": float(beta * adj["beta_adj"]),
        "regime": regime_str,
        "adj_erp_mult": float(adj["erp_mult"]),
        "adj_rf_adj": float(adj["rf_adj"]),
        "adj_beta_adj": float(adj["beta_adj"]),
    }