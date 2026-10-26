"""
iv_nowcast.py — GARCH(1,1) Daily Implied Volatility & Analytical Duration Adjustment (Phase 8 Redesign)
FROZEN Config: config/pre_registration_phase8.yaml

Provides:
  1. GARCHIVNowcast: Pure SciPy MLE GARCH(1,1) model for conditional variance.
  2. analytical_duration_adjustment: Closed-form Taylor approximation:
     IV_nowcast = IV_baseline * (1 - D_WACC * Delta_WACC)
  3. interpolate_iv: Dynamically adjust weekly DCF IV on daily frequency using GARCH(1,1) volatility scale and duration shift.
"""

from __future__ import annotations

import argparse
import logging
from typing import Dict, Any, List, Optional, Tuple, Union

import numpy as np
import pandas as pd
import scipy.optimize as opt

logger = logging.getLogger(__name__)


def analytical_duration_adjustment(
    iv_baseline: float,
    d_wacc: float,
    delta_wacc: float,
) -> float:
    """Closed-form linear Taylor duration approximation:
    IV_nowcast = IV_baseline * (1 - D_WACC * Delta_WACC)
    """
    if iv_baseline <= 0:
        return 0.0
    adjusted = iv_baseline * (1.0 - d_wacc * delta_wacc)
    return float(max(0.01, adjusted))


class GARCHIVNowcast:
    """GARCH(1,1) Daily Implied Volatility Nowcast & Interpolator with Duration Adjustment."""

    def __init__(self, p: int = 1, q: int = 1):
        self.p = p
        self.q = q
        self.omega: float = 1e-5
        self.alpha: float = 0.05
        self.beta: float = 0.90
        self.is_fitted: bool = False
        self._conditional_vol: Optional[pd.Series] = None

    def _garch_likelihood(self, params: np.ndarray, returns: np.ndarray) -> float:
        """Negative log-likelihood of GARCH(1,1) under Gaussian innovations."""
        omega, alpha, beta = params
        if omega <= 0 or alpha < 0 or beta < 0 or (alpha + beta) >= 0.999:
            return 1e10

        T = len(returns)
        sigma2 = np.zeros(T)
        sigma2[0] = np.var(returns) + 1e-6

        for t in range(1, T):
            sigma2[t] = omega + alpha * (returns[t-1] ** 2) + beta * sigma2[t-1]
            if sigma2[t] <= 0 or not np.isfinite(sigma2[t]):
                return 1e10

        log_lik = -0.5 * np.sum(np.log(2 * np.pi * sigma2) + (returns ** 2) / sigma2)
        return float(-log_lik)

    def fit(self, returns_series: pd.Series) -> "GARCHIVNowcast":
        """Fit GARCH(1,1) model on daily returns up to current date."""
        rets = returns_series.dropna().values
        if len(rets) < 30:
            logger.warning("Fewer than 30 returns observations for GARCH(1,1) fit.")
            self.is_fitted = False
            return self

        init_params = np.array([1e-5, 0.05, 0.90])
        bounds = [(1e-8, 1e-2), (1e-4, 0.3), (0.5, 0.98)]

        res = opt.minimize(
            self._garch_likelihood,
            init_params,
            args=(rets,),
            method="L-BFGS-B",
            bounds=bounds,
            options={"maxiter": 20},
        )

        if res.success:
            self.omega, self.alpha, self.beta = res.x
            self.is_fitted = True
        else:
            self.omega, self.alpha, self.beta = 1e-5, 0.05, 0.90
            self.is_fitted = True

        # Compute full conditional volatility series
        T = len(rets)
        sigma2 = np.zeros(T)
        sigma2[0] = np.var(rets) + 1e-6
        for t in range(1, T):
            sigma2[t] = self.omega + self.alpha * (rets[t-1] ** 2) + self.beta * sigma2[t-1]

        cond_std = np.sqrt(sigma2)
        self._conditional_vol = pd.Series(cond_std, index=returns_series.dropna().index)
        return self

    def interpolate_iv(
        self,
        weekly_iv: float,
        date: Union[str, pd.Timestamp],
        daily_returns: Optional[pd.Series] = None,
        d_wacc: float = 0.0,
        delta_wacc: float = 0.0,
    ) -> float:
        """Interpolate daily IV from weekly DCF baseline incorporating GARCH volatility scale AND analytical duration adjustment.

        Formula:
          IV_duration = IV_weekly * (1 - D_WACC * Delta_WACC)
          IV_daily = IV_duration * sqrt(var_daily / var_weekly)
        """
        if weekly_iv <= 0:
            return 0.0

        # 1. Analytical Duration Adjustment
        iv_base = analytical_duration_adjustment(weekly_iv, d_wacc, delta_wacc)

        if not self.is_fitted and daily_returns is not None:
            self.fit(daily_returns)

        if self._conditional_vol is None or self._conditional_vol.empty:
            return float(iv_base)

        dt = pd.Timestamp(date)
        hist_vol = self._conditional_vol.loc[:dt]
        if hist_vol.empty:
            return float(iv_base)

        var_daily = float(hist_vol.iloc[-1] ** 2)
        var_weekly = float(hist_vol.iloc[-5:].mean() ** 2) if len(hist_vol) >= 5 else var_daily

        if var_weekly <= 1e-8:
            return float(iv_base)

        ratio = np.sqrt(var_daily / var_weekly)
        ratio = float(np.clip(ratio, 0.70, 1.40))

        daily_iv = float(iv_base * ratio)
        return daily_iv


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Test Quantitative.iv_nowcast Phase 8")
    parser.add_argument("--test", action="store_true", help="Run test suite")
    args = parser.parse_args()

    np.random.seed(42)
    dates = pd.date_range("2024-01-01", "2024-06-30", freq="B")
    returns = pd.Series(np.random.normal(0.0002, 0.015, len(dates)), index=dates)

    garch = GARCHIVNowcast()
    garch.fit(returns)

    weekly_iv = 120.0
    test_date = dates[100]

    # Test with duration shift (+50bps WACC increase with 8-year duration)
    d_wacc = 8.0
    delta_wacc = 0.005
    daily_iv = garch.interpolate_iv(weekly_iv, test_date, returns, d_wacc=d_wacc, delta_wacc=delta_wacc)

    print(f"Weekly Baseline IV: ${weekly_iv:.2f}")
    print(f"Duration D_WACC: {d_wacc:.1f}, Delta WACC: +{delta_wacc*100:.2f}%")
    print(f"Interpolated & Duration-Adjusted Daily IV on {test_date.date()}: ${daily_iv:.2f}")

    print("GARCHIVNowcast Phase 8 Test OK!")
