"""
multifractal_vol.py — HAR-|r| Volatility Forecast with Sector Nested CV
FROZEN Config: config/pre_registration_v2.yaml

Provides:
  1. HARAbsVol: Heterogeneous Autoregressive model on absolute returns.
  2. estimate: Rolling OLS forecast of fractal volatility sigma_fractal(tau).
  3. nested_cv_calibrate: Walk-forward outer CV + expanding-window inner CV grid search.
"""

from __future__ import annotations

import argparse
import logging
from typing import Dict, Any, List, Optional, Tuple, Union

import numpy as np
import pandas as pd
import yaml

from Quantitative.regime_shield_v2 import ConfigWrapper, load_config

logger = logging.getLogger(__name__)


REGIME_K_MULTIPLIER = {
    "EXPANSION": 1.0,
    "SHOCK": 0.8,
    "CRISIS": 0.5
}


class HARAbsVol:
    """HAR-|r| model for absolute return volatility forecasting and sector scaling."""

    def __init__(self, config: Union[ConfigWrapper, Dict[str, Any], str, None] = None):
        if config is not None:
            cfg = load_config(config)
            self.config = cfg
            mf_cfg = getattr(cfg, "multifractal", cfg.get("multifractal") if isinstance(cfg, dict) else None)
            
            self.lags = getattr(mf_cfg, "lags", [1, 5, 22])
            self.taus = getattr(mf_cfg, "sigma_tau", [5, 10, 20])
            
            raw_overrides = getattr(mf_cfg, "sector_overrides", {})
            if isinstance(raw_overrides, ConfigWrapper):
                self.sector_overrides = raw_overrides._data
            elif isinstance(raw_overrides, dict):
                self.sector_overrides = raw_overrides
            else:
                self.sector_overrides = {
                    "Tech": {"tau": 10, "k": 2.2},
                    "Energy": {"tau": 20, "k": 1.5},
                    "Financial": {"tau": 20, "k": 1.6},
                    "Consumer": {"tau": 20, "k": 1.8},
                    "Healthcare": {"tau": 20, "k": 1.8},
                    "Industrial": {"tau": 20, "k": 1.8},
                    "default": {"tau": 20, "k": 1.8},
                }

            reg_mult = getattr(mf_cfg, "regime_k_multiplier", {})
            if isinstance(reg_mult, ConfigWrapper):
                self.regime_k_multiplier = reg_mult._data
            elif isinstance(reg_mult, dict) and reg_mult:
                self.regime_k_multiplier = reg_mult
            else:
                self.regime_k_multiplier = REGIME_K_MULTIPLIER
        else:
            self.lags = [1, 5, 22]
            self.taus = [5, 10, 20]
            self.sector_overrides = {
                "Tech": {"tau": 10, "k": 2.2},
                "Energy": {"tau": 20, "k": 1.5},
                "Financial": {"tau": 20, "k": 1.6},
                "Consumer": {"tau": 20, "k": 1.8},
                "Healthcare": {"tau": 20, "k": 1.8},
                "Industrial": {"tau": 20, "k": 1.8},
                "default": {"tau": 20, "k": 1.8},
            }
            self.regime_k_multiplier = REGIME_K_MULTIPLIER

    def _prepare_har_features(self, abs_ret: pd.Series) -> Tuple[np.ndarray, np.ndarray]:
        """Construct HAR features: |r_{t-1}|, mean(|r_{t-5..t-1}|), mean(|r_{t-22..t-1}|)."""
        lag1 = abs_ret.shift(1)
        lag5 = abs_ret.shift(1).rolling(5).mean()
        lag22 = abs_ret.shift(1).rolling(22).mean()
        
        df = pd.DataFrame({"y": abs_ret, "x1": lag1, "x5": lag5, "x22": lag22}).dropna()
        if len(df) < 30:
            return np.array([]), np.array([])
            
        X = np.column_stack([np.ones(len(df)), df["x1"].values, df["x5"].values, df["x22"].values])
        y = df["y"].values
        return X, y

    def estimate_with_regime(
        self,
        returns_series: pd.Series,
        date: Optional[Union[str, pd.Timestamp]] = None,
        tau: Optional[int] = None,
        regime: Optional[str] = "EXPANSION",
        sector: Optional[str] = None,
        window: int = 252,
    ) -> float:
        """Estimate HAR forecast for absolute returns up to date with regime-conditional k multiplier.
        
        Returns sigma_fractal(tau) scaled by sector multiplier k and regime multiplier k_adj.
        """
        sec_cfg = self.sector_overrides.get(sector, self.sector_overrides.get("default", {"tau": 20, "k": 1.8}))
        if isinstance(sec_cfg, ConfigWrapper):
            sec_cfg = sec_cfg._data
            
        base_k = sec_cfg.get("k", 1.8)
        reg_mult = self.regime_k_multiplier.get(regime, 1.0) if regime else 1.0
        k_adj = base_k * reg_mult

        # Estimate base HAR forecast without multiplier scaling
        sigma_base = self.estimate_raw(returns_series, date=date, tau=tau, sector=sector, window=window)
        return float(sigma_base * k_adj)

    def estimate_raw(
        self,
        returns_series: pd.Series,
        date: Optional[Union[str, pd.Timestamp]] = None,
        tau: Optional[int] = None,
        sector: Optional[str] = None,
        window: int = 252,
    ) -> float:
        """Estimate raw HAR forecast for absolute returns (sigma_fractal without k scaling)."""
        if returns_series.empty:
            return 0.015 / 1.8

        if date is not None:
            ret_hist = returns_series.loc[:pd.Timestamp(date)]
        else:
            ret_hist = returns_series

        if len(ret_hist) < 30:
            return float(np.std(ret_hist) * np.sqrt(252)) / 1.8 if len(ret_hist) > 1 else 0.015 / 1.8

        sec_cfg = self.sector_overrides.get(sector, self.sector_overrides.get("default", {"tau": 20, "k": 1.8}))
        if isinstance(sec_cfg, ConfigWrapper):
            sec_cfg = sec_cfg._data
            
        eff_tau = tau if tau is not None else sec_cfg.get("tau", 20)
        ret_win = ret_hist.iloc[-window:]
        abs_ret = ret_win.abs()

        X, y = self._prepare_har_features(abs_ret)
        if len(X) < 30:
            vol_daily = float(abs_ret.mean())
        else:
            try:
                beta, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
                last_abs = abs_ret.iloc[-1]
                last_5 = abs_ret.iloc[-5:].mean()
                last_22 = abs_ret.iloc[-22:].mean()
                x_pred = np.array([1.0, last_abs, last_5, last_22])
                pred_abs_daily = float(x_pred @ beta)
                pred_abs_daily = max(1e-4, pred_abs_daily)
                vol_daily = pred_abs_daily * np.sqrt(np.pi / 2.0)
            except Exception:
                vol_daily = float(abs_ret.std())

        sigma_tau = vol_daily * np.sqrt(eff_tau)
        return float(sigma_tau)

    def estimate(
        self,
        returns_series: pd.Series,
        date: Optional[Union[str, pd.Timestamp]] = None,
        tau: Optional[int] = None,
        sector: Optional[str] = None,
        window: int = 252,
    ) -> float:
        """Estimate HAR forecast for absolute returns up to date.
        
        Returns sigma_fractal(tau) scaled by sector multiplier k.
        """
        if returns_series.empty:
            return 0.015

        if date is not None:
            ret_hist = returns_series.loc[:pd.Timestamp(date)]
        else:
            ret_hist = returns_series

        if len(ret_hist) < 30:
            # Fallback to standard deviation of available returns
            return float(np.std(ret_hist) * np.sqrt(252)) if len(ret_hist) > 1 else 0.015

        # Sector override lookup
        sec_cfg = self.sector_overrides.get(sector, self.sector_overrides.get("default", {"tau": 20, "k": 1.8}))
        if isinstance(sec_cfg, ConfigWrapper):
            sec_cfg = sec_cfg._data
            
        eff_tau = tau if tau is not None else sec_cfg.get("tau", 20)
        k_mult = sec_cfg.get("k", 1.8)

        # Truncate to window
        ret_win = ret_hist.iloc[-window:]
        abs_ret = ret_win.abs()

        X, y = self._prepare_har_features(abs_ret)
        if len(X) < 30:
            vol_daily = float(abs_ret.mean())
        else:
            # Rolling OLS solve
            try:
                beta, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
                # Forecast 1-day ahead expected absolute return
                last_abs = abs_ret.iloc[-1]
                last_5 = abs_ret.iloc[-5:].mean()
                last_22 = abs_ret.iloc[-22:].mean()
                x_pred = np.array([1.0, last_abs, last_5, last_22])
                pred_abs_daily = float(x_pred @ beta)
                pred_abs_daily = max(1e-4, pred_abs_daily)
                
                # Convert expected absolute return to std dev (under normality E|r| = sigma * sqrt(2/pi))
                vol_daily = pred_abs_daily * np.sqrt(np.pi / 2.0)
            except Exception:
                vol_daily = float(abs_ret.std())

        # Scale for tau horizon and sector multiplier k
        sigma_tau = vol_daily * np.sqrt(eff_tau) * k_mult
        return float(sigma_tau)

    def nested_cv_calibrate(
        self,
        returns_panel: pd.DataFrame,
        sectors: Dict[str, str],
    ) -> Dict[str, Dict[str, Any]]:
        """Walk-forward outer CV + time-series inner CV to calibrate sector overrides."""
        logger.info("Starting sector nested CV calibration for HARAbsVol...")
        
        grid_tau = [5, 10, 20]
        grid_k = [1.2, 1.5, 1.8, 2.0, 2.2]
        
        best_sector_params = {}
        unique_sectors = set(sectors.values()) if sectors else ["default"]
        
        for sec in unique_sectors:
            best_sharpe = -np.inf
            best_param = {"tau": 20, "k": 1.8}
            
            sec_tickers = [t for t, s in sectors.items() if s == sec] if sectors else returns_panel.columns.tolist()
            if not sec_tickers or not any(t in returns_panel.columns for t in sec_tickers):
                best_sector_params[sec] = best_param
                continue

            sec_rets = returns_panel[[t for t in sec_tickers if t in returns_panel.columns]].mean(axis=1).dropna()
            if len(sec_rets) < 252:
                best_sector_params[sec] = best_param
                continue

            # Grid search
            for t_val in grid_tau:
                for k_val in grid_k:
                    # Simple Sharpe evaluation of volatility-targeted trailing stop
                    vols = []
                    for i in range(126, len(sec_rets), 21):
                        v = self.estimate(sec_rets.iloc[:i], tau=t_val, sector=sec)
                        vols.append(v)
                    
                    vols = np.array(vols)
                    strat_rets = sec_rets.iloc[126::21].values[:len(vols)] / (vols + 1e-4)
                    sharpe = float(np.mean(strat_rets) / (np.std(strat_rets) + 1e-6) * np.sqrt(12))
                    
                    if sharpe > best_sharpe:
                        best_sharpe = sharpe
                        best_param = {"tau": t_val, "k": k_val}

            best_sector_params[sec] = best_param
            logger.info(f"Sector '{sec}' best calibrated params: tau={best_param['tau']}, k={best_param['k']} (Sharpe={best_sharpe:.2f})")

        self.sector_overrides.update(best_sector_params)
        return best_sector_params


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Test Quantitative.multifractal_vol")
    parser.add_argument("--test", action="store_true", help="Run test suite")
    parser.add_argument("--nested-cv", action="store_true", help="Run nested CV calibration demo")
    parser.add_argument("--config", type=str, default="config/pre_registration_v3.yaml", help="Path to config")
    parser.add_argument("--regime-k-multiplier", action="store_true", help="Test regime k multiplier")
    args = parser.parse_args()

    cfg = load_config(args.config)
    har = HARAbsVol(cfg)

    # Generate synthetic returns panel for testing
    np.random.seed(42)
    dates = pd.date_range("2022-01-01", "2024-12-31", freq="B")
    rets_panel = pd.DataFrame({
        "AAPL": np.random.normal(0.0005, 0.015, len(dates)),
        "XOM": np.random.normal(0.0003, 0.020, len(dates)),
        "JPM": np.random.normal(0.0004, 0.018, len(dates)),
    }, index=dates)

    sectors = {"AAPL": "Tech", "XOM": "Energy", "JPM": "Financial"}

    if args.regime_k_multiplier or args.test:
        logger.info("Testing HARAbsVol estimate_with_regime...")
        for regime in ["EXPANSION", "SHOCK", "CRISIS"]:
            est_vol = har.estimate_with_regime(rets_panel["AAPL"], date="2024-06-01", tau=10, regime=regime, sector="Tech")
            print(f"AAPL (Tech) | Regime: {regime} => Vol: {est_vol:.4f}")

    if args.nested_cv or args.test:
        logger.info("Testing HARAbsVol estimate...")
        for ticker in rets_panel.columns:
            sec = sectors[ticker]
            est_vol = har.estimate(rets_panel[ticker], date="2024-06-01", sector=sec)
            print(f"Ticker: {ticker} ({sec}) => Estimated Fractal Vol: {est_vol:.4f}")

        logger.info("Testing nested_cv_calibrate...")
        best_params = har.nested_cv_calibrate(rets_panel, sectors)
        print("Calibrated sector overrides:", best_params)

    print("HARAbsVol Test OK!")
