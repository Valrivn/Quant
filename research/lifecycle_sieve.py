import os
import json
import hashlib
import time
from datetime import datetime, date
import numpy as np
import pandas as pd

def compute_growth(df_ticker, asof):
    asof_ts = pd.to_datetime(asof)
    sub = df_ticker[df_ticker['filed_date'] <= asof_ts].sort_values('filed_date')
    if len(sub) < 8:
        return np.nan
    rev_t = sub.iloc[-1]['revenue']
    latest_date = sub.iloc[-1]['filed_date']
    target_date = pd.to_datetime(latest_date) - pd.DateOffset(years=3)
    sub_past = sub[sub['filed_date'] <= target_date]
    if len(sub_past) == 0:
        if len(sub) >= 12:
            rev_past = sub.iloc[-12]['revenue']
        else:
            return np.nan
    else:
        rev_past = sub_past.iloc[-1]['revenue']
    if pd.isna(rev_t) or pd.isna(rev_past) or rev_past <= 0 or rev_t <= 0:
        return np.nan
    return float((rev_t / rev_past) ** (1.0 / 3.0) - 1.0)

def compute_margin_vol(df_ticker, asof):
    asof_ts = pd.to_datetime(asof)
    sub = df_ticker[df_ticker['filed_date'] <= asof_ts].sort_values('filed_date')
    if len(sub) < 20:
        return np.nan
    margins = sub['operating_margin'].dropna().tail(40)
    if len(margins) < 20:
        return np.nan
    return float(margins.std(ddof=1) * np.sqrt(4.0))

def compute_payout(df_ticker, asof):
    asof_ts = pd.to_datetime(asof)
    sub = df_ticker[df_ticker['filed_date'] <= asof_ts].sort_values('filed_date')
    sub_3y = sub.tail(12)
    if len(sub_3y) == 0:
        return np.nan
    div_sum = sub_3y['dividends_paid'].sum(skipna=True)
    ni_sum = sub_3y['net_income'].sum(skipna=True)
    if pd.isna(ni_sum) or ni_sum <= 0 or pd.isna(div_sum):
        return np.nan
    return float(div_sum / ni_sum)

def compute_reinvestment(df_ticker, asof):
    asof_ts = pd.to_datetime(asof)
    sub = df_ticker[df_ticker['filed_date'] <= asof_ts].sort_values('filed_date')
    sub_3y = sub.tail(12) if len(sub) >= 12 else sub
    if len(sub_3y) == 0:
        return np.nan
    capex_sum = sub_3y['capex'].sum(skipna=True) if 'capex' in sub_3y.columns else 0.0
    rd_sum = sub_3y['rd_expense'].sum(skipna=True) if 'rd_expense' in sub_3y.columns else 0.0
    acq_sum = sub_3y['acquisitions'].sum(skipna=True) if 'acquisitions' in sub_3y.columns else 0.0
    ocf_sum = sub_3y['operating_cash_flow'].sum(skipna=True) if 'operating_cash_flow' in sub_3y.columns else 0.0
    if pd.isna(ocf_sum) or ocf_sum <= 0:
        return np.nan
    return float((abs(capex_sum) + abs(rd_sum) + abs(acq_sum)) / ocf_sum)

def classify_category(g, margin_vol, cap, payout, reinvest):
    if margin_vol is None or np.isnan(margin_vol):
        return "UNCLASSIFIED"
    if margin_vol > 0.08:
        return "CYCLICAL"
    if g is None or np.isnan(g):
        return "UNCLASSIFIED"
    if g >= 0.20:
        return "FAST_GROWER"
    if 0.10 <= g < 0.20 and cap is not None and not np.isnan(cap) and cap > 10e9:
        return "STALWART"
    if g <= 0.05 and payout is not None and not np.isnan(payout) and payout > 0.50:
        return "SLOW_GROWER"
    return "UNCLASSIFIED"

def compute_icr(row):
    ebit = row.get('ebit', np.nan)
    int_exp = row.get('interest_expense', np.nan)
    if pd.isna(int_exp) or int_exp == 0 or (isinstance(int_exp, (int, float)) and abs(int_exp) < 1e-6):
        return 20.0
    if pd.isna(ebit):
        return np.nan
    return float(np.clip(ebit / abs(int_exp), 0.0, 20.0))

def compute_tax_rate(row):
    tax = row.get('tax_expense', row.get('income_tax_expense', np.nan))
    ni = row.get('net_income', np.nan)
    if pd.isna(tax) or pd.isna(ni):
        return 0.21
    pretax = ni + tax
    if pretax <= 0:
        return 0.21
    return float(np.clip(tax / pretax, 0.0, 0.5))

def compute_de(row):
    debt = row.get('total_debt', np.nan)
    equity = row.get('equity', row.get('stockholders_equity', np.nan))
    if pd.isna(debt) or pd.isna(equity) or equity <= 0:
        return 10.0, True
    return float(debt / equity), False

def compute_phi(icr, beta_u, t, de):
    icr_c = np.clip(icr if not pd.isna(icr) else 10.0, 0.0, 20.0)
    beta_c = max(beta_u if not pd.isna(beta_u) else 1.0, 0.1)
    de_c = np.clip(de if not pd.isna(de) else 0.0, 0.0, 10.0)
    t_c = t if not pd.isna(t) else 0.21
    return float((icr_c / beta_c) * (1.0 + (1.0 - t_c) * de_c))
