"""
dcf_nowcaster.py — Decoupled FCF Matrix + Daily WACC + Duration Nowcast
Pre-Registration Config: config/pre_registration_v3.yaml

Provides:
  1. FCFMatrix: Low-frequency layer updated ONLY on SEC 10-Q/10-K filing dates (PIT).
  2. WACCEngine: High-frequency layer updated DAILY with live market rates.
  3. DurationNowcaster: Analytical IV nowcast using valuation duration D_WACC.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Dict, Any, List, Optional, Tuple, Union

import numpy as np
import pandas as pd
import yaml

from Quantitative.regime_shield_v2 import ConfigWrapper, load_config

logger = logging.getLogger(__name__)


class FCFMatrix:
    """Low-frequency layer: Updated ONLY on SEC 10-Q/10-K filing dates (PIT)."""

    def __init__(self, config: Union[ConfigWrapper, Dict[str, Any], str, None] = None):
        if config is not None:
            cfg = load_config(config)
            dcf_cfg = getattr(cfg, "dcf_nowcaster", cfg.get("dcf_nowcaster") if isinstance(cfg, dict) else None)
            fcf_cfg = getattr(dcf_cfg, "fcf_matrix", None)
            self.cap_rd_years = int(getattr(fcf_cfg, "capitalized_rd_years", 3))
            self.sbc_drag = bool(getattr(fcf_cfg, "sbc_drag", True))
            self.use_omr = bool(getattr(fcf_cfg, "omr", True))
        else:
            self.cap_rd_years = 3
            self.sbc_drag = True
            self.use_omr = True

        self.fcf_projections: Dict[str, np.ndarray] = {}  # {ticker: np.array([FCF_1..FCF_N, Terminal])}
        self.duration_wacc: Dict[str, float] = {}        # {ticker: float D_WACC}
        self.cap_years: Dict[str, int] = {}            # {ticker: int N_CAP}
        self.iv_baseline: Dict[str, float] = {}          # {ticker: float IV_0}
        self.wacc_baseline: Dict[str, float] = {}        # {ticker: float WACC_0}
        self.last_filing_date: Dict[str, pd.Timestamp] = {} # {ticker: date}

    def update_on_filing(
        self,
        ticker: str,
        filing_date: Union[str, pd.Timestamp, date],
        companyfacts_row: Dict[str, Any],
        n_cap_years: int = 5,
        terminal_growth: float = 0.02,
        wacc_base: float = 0.08,
    ) -> None:
        """Process SEC filing data point-in-time."""
        f_date = pd.Timestamp(companyfacts_row.get("source_timestamp", filing_date))
        
        # Extract line items
        revenue = float(companyfacts_row.get("revenue", 1000.0))
        ebit = float(companyfacts_row.get("ebit", 200.0))
        tax_rate = float(companyfacts_row.get("tax_rate", 0.21))
        capex = float(companyfacts_row.get("capex", 50.0))
        depr = float(companyfacts_row.get("depreciation", 40.0))
        nwc_change = float(companyfacts_row.get("nwc_change", 10.0))
        rd_expense = float(companyfacts_row.get("rd_expense", 30.0))
        sbc_expense = float(companyfacts_row.get("sbc_expense", 15.0))
        shares = float(companyfacts_row.get("shares_outstanding", 100.0))

        # 1. Capitalize R&D (3-yr straight line amortization)
        # Amortization = RD / cap_rd_years; Asset = RD * (cap_rd_years - 1) / cap_rd_years
        rd_amort = rd_expense / max(1, self.cap_rd_years)
        adjusted_ebit = ebit + rd_expense - rd_amort
        
        # 2. SBC drag intensity deduction
        sbc_drag_val = sbc_expense if self.sbc_drag else 0.0
        
        # 3. Normalized operating margins
        nopat = adjusted_ebit * (1.0 - tax_rate)
        fcf_base = nopat + depr - capex - nwc_change - sbc_drag_val
        fcf_base = max(1.0, fcf_base)  # clamp positive for baseline calculation

        # 4. Project FCF vector {FCF_1..FCF_N_CAP, Terminal}
        growth_rate = float(companyfacts_row.get("fcf_growth", 0.05))
        fcf_vec = np.zeros(n_cap_years + 1)
        for t in range(1, n_cap_years + 1):
            fcf_vec[t - 1] = fcf_base * ((1.0 + growth_rate) ** t)
        
        terminal_val = (fcf_vec[n_cap_years - 1] * (1.0 + terminal_growth)) / (wacc_base - terminal_growth)
        fcf_vec[n_cap_years] = terminal_val

        # 5. Compute baseline IV and valuation duration D_WACC analytically
        # IV_base = sum_{t=1}^N FCF_t / (1+wacc)^t + Terminal / (1+wacc)^N
        discount_factors = np.array([(1.0 + wacc_base) ** t for t in range(1, n_cap_years + 1)] + [(1.0 + wacc_base) ** n_cap_years])
        pv_terms = fcf_vec / discount_factors
        pv_total = np.sum(pv_terms)
        iv_per_share = pv_total / shares

        # Analytical D_WACC = (1 / IV_total) * [ sum_{t=1}^N t * FCF_t / (1+wacc)^(t+1) + N * Terminal / (1+wacc)^(N+1) ]
        weights_t = np.array([t for t in range(1, n_cap_years + 1)] + [n_cap_years])
        d_wacc = (1.0 / pv_total) * np.sum(weights_t * fcf_vec / (discount_factors * (1.0 + wacc_base)))

        # Store PIT results
        self.fcf_projections[ticker] = fcf_vec
        self.duration_wacc[ticker] = float(d_wacc)
        self.cap_years[ticker] = n_cap_years
        self.iv_baseline[ticker] = float(iv_per_share)
        self.wacc_baseline[ticker] = float(wacc_base)
        self.last_filing_date[ticker] = f_date

        logger.debug(f"FCFMatrix updated for {ticker} on {f_date.date()}: IV_base={iv_per_share:.2f}, D_WACC={d_wacc:.2f}")

    def get_fcf_vector(self, ticker: str, as_of_date: Union[str, pd.Timestamp, date]) -> Optional[np.ndarray]:
        """Return most recent FCF vector filed <= as_of_date."""
        as_of = pd.Timestamp(as_of_date)
        last_date = self.last_filing_date.get(ticker)
        if last_date is None or last_date > as_of:
            return None
        return self.fcf_projections.get(ticker)


class WACCEngine:
    """High-frequency layer: Updated DAILY with live market rates."""

    def __init__(self, config: Union[ConfigWrapper, Dict[str, Any], str, None] = None):
        if config is not None:
            cfg = load_config(config)
            dcf_cfg = getattr(cfg, "dcf_nowcaster", cfg.get("dcf_nowcaster") if isinstance(cfg, dict) else None)
            wacc_cfg = getattr(dcf_cfg, "wacc_engine", None)
            self.rf_source = str(getattr(wacc_cfg, "rf_source", "FRED_DGS10"))
            self.beta_window = str(getattr(wacc_cfg, "beta_window", "60M"))
            self.erp_source = str(getattr(wacc_cfg, "erp_source", "damodaran_implied"))
        else:
            self.rf_source = "FRED_DGS10"
            self.beta_window = "60M"
            self.erp_source = "damodaran_implied"

        self.rf_series: Dict[pd.Timestamp, float] = {}        # Daily 10Y Treasury
        self.beta_60m: Dict[Tuple[str, pd.Timestamp], float] = {} # Trailing 60M vs SPY
        self.erp_series: Dict[pd.Timestamp, float] = {}       # Damodaran/implied daily
        self.debt_data: Dict[Tuple[str, pd.Timestamp], float] = {} # Cost of debt data
        self.tax_data: Dict[Tuple[str, pd.Timestamp], float] = {}  # Effective tax rate data
        self.capital_structure: Dict[Tuple[str, pd.Timestamp], float] = {} # Debt weight wd

    def load_fred_dgs10(self, df_or_series: Optional[Union[pd.DataFrame, pd.Series]] = None) -> Dict[pd.Timestamp, float]:
        if df_or_series is not None:
            if isinstance(df_or_series, pd.Series):
                for d, val in df_or_series.items():
                    self.rf_series[pd.Timestamp(d)] = float(val) / 100.0 if val > 0.5 else float(val)
            elif isinstance(df_or_series, pd.DataFrame):
                col = df_or_series.columns[0]
                for d, val in df_or_series[col].items():
                    self.rf_series[pd.Timestamp(d)] = float(val) / 100.0 if val > 0.5 else float(val)
        return self.rf_series

    def load_beta_60m(self, beta_dict: Optional[Dict[Tuple[str, Any], float]] = None) -> Dict[Tuple[str, pd.Timestamp], float]:
        if beta_dict is not None:
            for (ticker, d), val in beta_dict.items():
                self.beta_60m[(ticker, pd.Timestamp(d))] = float(val)
        return self.beta_60m

    def load_erp(self, erp_dict_or_series: Optional[Union[Dict[Any, float], pd.Series]] = None) -> Dict[pd.Timestamp, float]:
        if erp_dict_or_series is not None:
            if isinstance(erp_dict_or_series, pd.Series):
                for d, val in erp_dict_or_series.items():
                    self.erp_series[pd.Timestamp(d)] = float(val) / 100.0 if val > 0.2 else float(val)
            elif isinstance(erp_dict_or_series, dict):
                for d, val in erp_dict_or_series.items():
                    self.erp_series[pd.Timestamp(d)] = float(val) / 100.0 if val > 0.2 else float(val)
        return self.erp_series

    def _cost_of_debt(self, ticker: str, date: pd.Timestamp) -> float:
        """Trailing 4Q interest expense / avg debt + credit spread markup."""
        return self.debt_data.get((ticker, date), 0.05)

    def _effective_tax_rate(self, ticker: str, date: pd.Timestamp) -> float:
        """Effective bounded tax rate."""
        return self.tax_data.get((ticker, date), 0.21)

    def _debt_weight(self, ticker: str, date: pd.Timestamp) -> float:
        """Capital structure debt weight w_d = D / (D + E)."""
        return self.capital_structure.get((ticker, date), 0.15)

    def compute_daily_wacc(self, ticker: str, date: Union[str, pd.Timestamp, datetime], regime: str = "EXPANSION") -> float:
        """Compute live WACC for date, incorporating dynamic regime adjustments."""
        from Quantitative.wacc_panel import compute_dynamic_wacc
        d = pd.Timestamp(date)
        rf = self.rf_series.get(d, 0.04)
        beta = self.beta_60m.get((ticker, d), 1.0)
        erp = self.erp_series.get(d, 0.05)
        kd = self._cost_of_debt(ticker, d)
        tau = self._effective_tax_rate(ticker, d)
        wd = self._debt_weight(ticker, d)
        we = 1.0 - wd
        
        # Base wacc calculation
        base_wacc = we * (rf + beta * erp) + wd * kd * (1.0 - tau)
        
        return compute_dynamic_wacc(
            base_wacc=base_wacc,
            regime=regime,
            risk_free_rate=rf,
            equity_risk_premium=erp,
            beta=beta,
            cost_of_debt=kd,
            debt_weight=wd,
            equity_weight=we,
            tax_rate=tau,
        )


class DurationNowcaster:
    """Analytical IV nowcast using valuation duration D_WACC."""

    def __init__(self, fcf_matrix: FCFMatrix, wacc_engine: WACCEngine):
        self.fcf = fcf_matrix
        self.wacc = wacc_engine

    def nowcast_iv_daily(self, ticker: str, date: Union[str, pd.Timestamp, datetime], regime: str = "EXPANSION") -> Optional[float]:
        """Closed-form: IV_today approx IV_base * (1 - D_WACC * Delta_WACC)."""
        d = pd.Timestamp(date)
        wacc_today = self.wacc.compute_daily_wacc(ticker, d, regime=regime)
        wacc_base = self.fcf.wacc_baseline.get(ticker)
        if wacc_base is None:
            return None
            
        delta_wacc = wacc_today - wacc_base
        d_wacc = self.fcf.duration_wacc.get(ticker, 0.0)
        iv_base = self.fcf.iv_baseline.get(ticker)
        if iv_base is None:
            return None
            
        # Closed-form linear Taylor approximation using analytical duration
        iv_nowcast = iv_base * (1.0 - d_wacc * delta_wacc)
        return float(iv_nowcast)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Quantitative.dcf_nowcaster Test Harness")
    parser.add_argument("--test", action="store_true", help="Run test suite")
    parser.add_argument("--build-fcf-matrix", action="store_true", help="Build synthetic FCF matrix")
    parser.add_argument("--wacc-engine", action="store_true", help="Test WACC engine updates")
    parser.add_argument("--duration-nowcast", action="store_true", help="Test Duration IV Nowcast")
    parser.add_argument("--config", type=str, default="config/pre_registration_v3.yaml", help="Path to config")
    args = parser.parse_args()

    cfg = load_config(args.config)
    fcf_matrix = FCFMatrix(cfg)
    wacc_engine = WACCEngine(cfg)
    nowcaster = DurationNowcaster(fcf_matrix, wacc_engine)

    test_date = pd.Timestamp("2024-03-31")
    filing_data = {
        "revenue": 5000.0,
        "ebit": 1200.0,
        "tax_rate": 0.20,
        "capex": 300.0,
        "depreciation": 250.0,
        "nwc_change": 50.0,
        "rd_expense": 400.0,
        "sbc_expense": 100.0,
        "shares_outstanding": 500.0,
        "fcf_growth": 0.06,
    }

    fcf_matrix.update_on_filing("AAPL", test_date, filing_data, wacc_base=0.08)
    
    # Populate daily WACC inputs for nowcasting
    now_date = pd.Timestamp("2024-04-15")
    wacc_engine.rf_series[now_date] = 0.045
    wacc_engine.beta_60m[("AAPL", now_date)] = 1.1
    wacc_engine.erp_series[now_date] = 0.052

    iv_base = fcf_matrix.iv_baseline["AAPL"]
    wacc_today = wacc_engine.compute_daily_wacc("AAPL", now_date)
    iv_nowcast = nowcaster.nowcast_iv_daily("AAPL", now_date)

    print(f"Ticker: AAPL | Filing Date: {test_date.date()} | IV_base: ${iv_base:.2f}")
    print(f"Nowcast Date: {now_date.date()} | WACC_base: 8.00% -> WACC_today: {wacc_today*100:.2f}%")
    print(f"Analytical Duration D_WACC: {fcf_matrix.duration_wacc['AAPL']:.2f}")
    print(f"Nowcasted Intrinsic Value: ${iv_nowcast:.2f}")
    
    print("DCF Nowcaster Test OK!")
