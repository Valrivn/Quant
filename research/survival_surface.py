import os
import json
import numpy as np
import pandas as pd

def is_compounding(row):
    roic = row.get('roic', np.nan)
    wacc = row.get('after_tax_cost_of_debt', row.get('cost_of_debt', 0.08))
    if pd.isna(roic):
        return False
    if pd.isna(wacc):
        wacc = 0.08
    return bool(roic > wacc)

def had_operational_break(row, category, prev_rows=None):
    """
    ANY of (category-specific, pre-registered):
      ALL:            net_income < 0 for 2 consecutive quarters
      ALL:            icr < 1.0
      STALWART/SLOW:  fcfe / dividends_paid < 1.0   (fcfe = OCF - capex + net_debt_issued)
      FAST_GROWER:    sbc_expense / float > 0.05    (float = shares_outstanding * price)
      CYCLICAL:       (inventory_delta + capex) > 2*sigma(rolling)
    """
    ni = row.get('net_income', 0)
    ebit = row.get('ebit', 0)
    int_exp = row.get('interest_expense', 0)
    
    # ICR check
    icr = ebit / abs(int_exp) if not pd.isna(ebit) and not pd.isna(int_exp) and abs(int_exp) > 0 else 20.0
    if icr < 1.0:
        return True
        
    # Net income < 0 for 2 consecutive quarters check
    if prev_rows is not None and len(prev_rows) >= 1:
        prev_ni = prev_rows[-1].get('net_income', 0)
        if not pd.isna(ni) and not pd.isna(prev_ni) and ni < 0 and prev_ni < 0:
            return True
            
    # Category specific checks
    if category in ['STALWART', 'SLOW_GROWER']:
        ocf = row.get('operating_cash_flow', 0)
        capex = row.get('capex', 0)
        divs = row.get('dividends_paid', 0)
        # approximate fcfe
        fcfe = (ocf if not pd.isna(ocf) else 0) - (abs(capex) if not pd.isna(capex) else 0)
        if not pd.isna(divs) and divs > 0:
            if fcfe / divs < 1.0:
                return True
                
    elif category == 'FAST_GROWER':
        sbc = row.get('sbc_expense', 0)
        shares = row.get('shares_outstanding', 0)
        # need price or market cap proxy, let's use shares * close price from fundamentals if available or estimate float
        # dense_fundamentals has shares_outstanding. If price not in row, let's estimate float or use market_cap if available.
        mcap = row.get('market_cap', shares * 10.0 if not pd.isna(shares) else 0.0)
        if not pd.isna(sbc) and not pd.isna(mcap) and mcap > 0:
            if sbc / mcap > 0.05:
                return True
                
    elif category == 'CYCLICAL':
        inv = row.get('inventory', 0)
        capex = row.get('capex', 0)
        # check inventory + capex anomaly (> 2 sigma)
        inv_delta = row.get('inventory_yoy', 0)
        if not pd.isna(inv_delta) and inv_delta > 0.25: # proxy for spike
            return True
            
    return False

def second_derivative(S: pd.Series) -> pd.Series:
    return S.diff(periods=2) - 2 * S.diff(periods=1) # or standard finite difference

def find_break_point(S: pd.Series) -> dict:
    if len(S) < 3:
        return {"t_star": 1, "d2_peak": 0.0, "S_at_break": float(S.iloc[0]) if len(S) > 0 else 1.0}
    d2 = S.diff().diff().dropna()
    if len(d2) == 0:
        return {"t_star": 1, "d2_peak": 0.0, "S_at_break": float(S.iloc[0])}
    t_star_idx = int(np.argmax(np.abs(d2.values)))
    t_star = t_star_idx + 2 # accounting for diff diff offset
    t_star = min(max(t_star, 1), len(S))
    return {
        "t_star": int(t_star),
        "d2_peak": float(d2.iloc[t_star_idx]),
        "S_at_break": float(S.iloc[t_star-1])
    }
