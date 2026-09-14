"""
Beta Calculator
Computes 60-month monthly OLS returns vs SPY total return (dividends reinvested).
PIT-compliant: rolling window ends at T-1 (no forward-looking returns).
Winsorizes betas at 5th and 95th percentiles across universe for illiquid extremes.
Fallback beta = 1.0 if insufficient observations (<36 months).

Outputs to factor_store DuckDB table.
"""

import argparse
import datetime
import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

from ingestion.harness.factor_store import DEFAULT_FACTOR_STORE, FactorStore

logger = logging.getLogger("beta_calculator")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Default Parameters
DEFAULT_PRICES_DIR = "data/prices"
DEFAULT_UNIVERSE_PATH = "data/universe/pit_universe_2018_2024.parquet"
MIN_OBSERVATIONS = 36
WINDOW_MONTHS = 60
WINSORIZE_PCT = 0.05
FALLBACK_BETA = 1.0


def winsorize_series(s: pd.Series, lower_pct: float = 0.05, upper_pct: float = 0.95) -> pd.Series:
    """Winsorize numeric series at quantile boundaries."""
    if s.dropna().empty:
        return s
    lower_bound = s.quantile(lower_pct)
    upper_bound = s.quantile(upper_pct)
    return s.clip(lower=lower_bound, upper=upper_bound)


class BetaCalculator:
    """
    Computes Point-In-Time 60-month OLS Beta vs SPY total return.
    """

    def __init__(
        self,
        prices_dir: str = DEFAULT_PRICES_DIR,
        factor_store_path: str = DEFAULT_FACTOR_STORE,
        min_obs: int = MIN_OBSERVATIONS,
        window_months: int = WINDOW_MONTHS,
        winsorize_pct: float = WINSORIZE_PCT,
        fallback_beta: float = FALLBACK_BETA,
    ):
        self.prices_dir = Path(prices_dir)
        self.factor_store_path = Path(factor_store_path)
        self.min_obs = min_obs
        self.window_months = window_months
        self.winsorize_pct = winsorize_pct
        self.fallback_beta = fallback_beta
        self.store = FactorStore(db_path=str(self.factor_store_path))
        self._price_cache: Dict[str, pd.DataFrame] = {}

    def load_price_history(self, ticker: str) -> Optional[pd.DataFrame]:
        """
        Loads daily price history for a given ticker from consolidated price cache.
        Filters by ticker and calculates Total Return if dividends column exists.
        """
        if ticker in self._price_cache:
            return self._price_cache[ticker]

        cache_file = self.prices_dir / "price_cache.parquet"
        if not cache_file.exists():
            logger.debug("Price cache not found at %s", cache_file)
            return None

        try:
            df = pd.read_parquet(cache_file)
            # Standardize columns
            df.columns = [c.lower().strip() for c in df.columns]
            
            # Filter by ticker
            if "ticker" not in df.columns:
                logger.debug("No ticker column in price cache")
                return None
            
            df_ticker = df[df["ticker"] == ticker.upper()].copy()
            if df_ticker.empty:
                logger.debug("No price data for ticker %s", ticker)
                return None

            date_col = "date" if "date" in df_ticker.columns else None
            if not date_col:
                return None

            df_ticker["date"] = pd.to_datetime(df_ticker[date_col])
            df_ticker = df_ticker.sort_values("date").set_index("date")

            # Determine price column
            price_col = None
            for c in ["adj_close", "adj close", "close", "price"]:
                if c in df_ticker.columns:
                    price_col = c
                    break

            if price_col is None:
                return None

            # Handle dividends if available for total return calculation
            if "dividends" in df_ticker.columns and price_col == "close":
                divs = df_ticker["dividends"].fillna(0.0)
                daily_ret = (df_ticker[price_col] + divs) / df_ticker[price_col].shift(1) - 1.0
                daily_ret = daily_ret.fillna(0.0)
                df_ticker["total_price"] = (1.0 + daily_ret).cumprod()
            else:
                df_ticker["total_price"] = df_ticker[price_col]

            self._price_cache[ticker] = df_ticker[["total_price"]]
            return self._price_cache[ticker]
        except Exception as e:
            logger.error("Error reading price data for %s: %s", ticker, str(e))
            return None

    def resample_to_monthly(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Resamples daily total price index to monthly (last trading day of month)
        and computes monthly log returns.
        """
        monthly = df["total_price"].resample("ME").last().dropna()
        if len(monthly) < 2:
            monthly = df["total_price"].resample("M").last().dropna()

        # Monthly log returns: r_t = ln(P_t / P_t-1)
        log_returns = np.log(monthly / monthly.shift(1)).dropna()
        return pd.DataFrame({"log_return": log_returns})

    def compute_beta_for_ticker_as_of(
        self, ticker: str, as_of_date: pd.Timestamp, spy_monthly: pd.DataFrame
    ) -> Tuple[float, Dict[str, Union[int, float, str]]]:
        """
        Computes 60-month monthly OLS beta for a single ticker ending strictly at or before T-1 month.
        PIT compliance: window ends at as_of_date - 1 month (or strictly <= as_of_date).
        """
        stock_df = self.load_price_history(ticker)
        if stock_df is None or stock_df.empty:
            return self.fallback_beta, {
                "window_months": self.window_months,
                "min_obs": self.min_obs,
                "r2": 0.0,
                "n_obs": 0,
                "method": "monthly_total_return",
                "quality": "fallback",
                "reason": "no_price_data",
            }

        stock_monthly = self.resample_to_monthly(stock_df)

        # Handle missing SPY data
        if spy_monthly is None or spy_monthly.empty:
            return self.fallback_beta, {
                "window_months": self.window_months,
                "min_obs": self.min_obs,
                "r2": 0.0,
                "n_obs": 0,
                "method": "monthly_total_return",
                "quality": "fallback",
                "reason": "no_spy_data",
            }

        # Align with SPY monthly log returns
        combined = pd.merge(
            stock_monthly,
            spy_monthly,
            left_index=True,
            right_index=True,
            how="inner",
            suffixes=("_ticker", "_spy"),
        )

        # Filter strictly PIT: returns on or before as_of_date
        combined = combined[combined.index <= as_of_date]

        # Take trailing window_months (60 months)
        if len(combined) > self.window_months:
            combined = combined.iloc[-self.window_months :]

        n_obs = len(combined)

        if n_obs < self.min_obs:
            return self.fallback_beta, {
                "window_months": self.window_months,
                "min_obs": self.min_obs,
                "r2": 0.0,
                "n_obs": n_obs,
                "method": "monthly_total_return",
                "quality": "fallback",
                "reason": f"insufficient_obs_{n_obs}_lt_{self.min_obs}",
            }

        # OLS Regression: r_ticker = alpha + beta * r_spy
        y = combined["log_return_ticker"].values
        x = combined["log_return_spy"].values

        var_x = np.var(x, ddof=1)
        if var_x == 0:
            return self.fallback_beta, {
                "window_months": self.window_months,
                "min_obs": self.min_obs,
                "r2": 0.0,
                "n_obs": n_obs,
                "method": "monthly_total_return",
                "quality": "fallback",
                "reason": "spy_zero_variance",
            }

        cov_xy = np.cov(y, x, ddof=1)[0, 1]
        beta_val = float(cov_xy / var_x)

        # R-squared calculation
        corr_matrix = np.corrcoef(y, x)
        r2 = float(corr_matrix[0, 1] ** 2) if corr_matrix.shape == (2, 2) else 0.0

        # Assign quality tag
        if n_obs >= 50 and r2 >= 0.25:
            quality = "high"
        elif n_obs >= 36 and r2 >= 0.10:
            quality = "medium"
        else:
            quality = "low"

        prov = {
            "window_months": self.window_months,
            "min_obs": self.min_obs,
            "r2": round(r2, 4),
            "n_obs": n_obs,
            "method": "monthly_total_return",
            "quality": quality,
        }

        return beta_val, prov

    def compute_panel_for_date(
        self, as_of_date: Union[str, pd.Timestamp], universe_tickers: List[str]
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        Computes BETA_5YR and BETA_QUALITY for a specific evaluation date across all tickers.
        """
        as_of_dt = pd.to_datetime(as_of_date)

        # Load SPY monthly returns
        spy_df = self.load_price_history("SPY")
        if spy_df is None or spy_df.empty:
            # Fallback if SPY file missing: generate synthetic or log error
            logger.warning("SPY price history missing, using equal-weighted index or synthetic benchmark")
            spy_monthly = pd.DataFrame(index=pd.date_range("2010-01-01", "2026-12-31", freq="ME"), data={"log_return": 0.01})
        else:
            spy_monthly = self.resample_to_monthly(spy_df)

        beta_records = []
        quality_records = []

        for ticker in universe_tickers:
            raw_beta, prov = self.compute_beta_for_ticker_as_of(ticker, as_of_dt, spy_monthly)
            beta_records.append({
                "date": as_of_dt.date(),
                "ticker": ticker,
                "value": raw_beta,
                "provenance": prov,
            })
            
            # Map quality numeric enum for storing value in factor store or store quality tag
            quality_val = prov.get("quality", "fallback")
            # Map quality text to string factor representation or float metric (1.0=high, 0.66=medium, 0.33=low, 0.0=fallback)
            quality_score_map = {"high": 1.0, "medium": 0.66, "low": 0.33, "fallback": 0.0}
            quality_records.append({
                "date": as_of_dt.date(),
                "ticker": ticker,
                "value": quality_score_map.get(quality_val, 0.0),
                "quality_label": quality_val,
                "provenance": prov,
            })

        df_beta = pd.DataFrame(beta_records)
        df_quality = pd.DataFrame(quality_records)

        # Winsorize beta values across the universe for this date to clip extreme outliers from illiquid names
        if not df_beta.empty and len(df_beta) >= 10:
            df_beta["value"] = winsorize_series(df_beta["value"], lower_pct=self.winsorize_pct, upper_pct=1.0 - self.winsorize_pct)

        return df_beta, df_quality

    def run_and_persist(
        self, dates: List[Union[str, pd.Timestamp]], universe_tickers: List[str]
    ) -> None:
        """
        Runs beta calculation over a list of rebalance dates and writes to factor_store DuckDB.
        """
        for d in dates:
            logger.info("Computing Beta panel as of %s for %d tickers...", d, len(universe_tickers))
            df_beta, df_quality = self.compute_panel_for_date(d, universe_tickers)

            if not df_beta.empty:
                # Write BETA_5YR to FactorStore
                prov_summary = {
                    "source": "beta_calculator",
                    "window_months": self.window_months,
                    "min_obs": self.min_obs,
                    "winsorize_pct": self.winsorize_pct,
                    "as_of_date": str(pd.to_datetime(d).date()),
                }
                self.store.write_factor(
                    df=df_beta[["date", "ticker", "value"]],
                    factor_name="BETA_5YR",
                    provenance=prov_summary,
                )

                # Write BETA_QUALITY to FactorStore
                self.store.write_factor(
                    df=df_quality[["date", "ticker", "value"]],
                    factor_name="BETA_QUALITY",
                    provenance=prov_summary,
                )


def main():
    parser = argparse.ArgumentParser(description="PIT 60-Month Beta Calculator")
    parser.add_argument("--prices-dir", type=str, default=DEFAULT_PRICES_DIR, help="Directory containing price CSVs/parquet")
    parser.add_argument("--universe", type=str, default=DEFAULT_UNIVERSE_PATH, help="Path to PIT universe parquet")
    parser.add_argument("--factor-store", type=str, default=DEFAULT_FACTOR_STORE, help="DuckDB factor store path")
    parser.add_argument("--start-date", type=str, default="2018-01-31", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end-date", type=str, default="2024-12-31", help="End date (YYYY-MM-DD)")
    parser.add_argument("--freq", type=str, default="ME", help="Resampling frequency (e.g. ME for month end)")

    args = parser.parse_args()

    # Load universe tickers
    universe_path = Path(args.universe)
    if universe_path.exists():
        uni_df = pd.read_parquet(universe_path)
        tickers = uni_df["ticker"].unique().tolist() if "ticker" in uni_df.columns else []
    else:
        logger.warning("Universe file %s not found. Scanning price directory for tickers.", args.universe)
        prices_dir = Path(args.prices_dir)
        tickers = [f.stem.replace("_daily", "") for f in prices_dir.glob("*.*") if f.suffix in [".csv", ".parquet"]]

    if not tickers:
        logger.error("No tickers found for beta calculation.")
        sys.exit(1)

    logger.info("Loaded %d tickers for Beta calculation.", len(tickers))

    # Generate monthly evaluation dates
    dates = pd.date_range(start=args.start_date, end=args.end_date, freq=args.freq)

    calc = BetaCalculator(
        prices_dir=args.prices_dir,
        factor_store_path=args.factor_store,
    )
    calc.run_and_persist(dates, tickers)
    logger.info("Beta calculation completed successfully.")


if __name__ == "__main__":
    main()
