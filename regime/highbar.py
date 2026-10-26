"""
High-bar override gate for breaking re-entry embargoes.
"""

from typing import Dict, Any


def can_break_embargo(asset: Dict[str, Any]) -> bool:
    """
    Requires ALL of the following:
    - (z_val <= -1.0 OR dcf_p10 == True)
    - (roic - wacc) >= 0.02 AND eva > 0 modal path passes
    - spread_velocity_21d <= 0.0
    - icr >= 5.0
    """
    z_val = float(asset.get("z_val", 0.0))
    dcf_p10 = bool(asset.get("dcf_p10", False))
    val_gate = (z_val <= -1.0) or dcf_p10

    roic = float(asset.get("roic", 0.0))
    wacc = float(asset.get("wacc", 0.0))
    eva_modal_pass = bool(asset.get("eva_modal_pass", False))
    spread_gate = (roic - wacc >= 0.02) and eva_modal_pass

    spread_vel = float(asset.get("spread_velocity_21d", 0.0))
    vel_gate = spread_vel <= 0.0

    icr = float(asset.get("icr", 0.0))
    icr_gate = icr >= 5.0

    return val_gate and spread_gate and vel_gate and icr_gate
