"""StockTwits & Reddit Investor Mindshare and Volatility Regulator.

Repurposes social sentiment streams (StockTwits + Reddit) as an investor
mindshare volume gauge and crowding/volatility suppressor Omega_i in [0.50, 1.00].

Mathematical Specification:
1. Investor Mindshare Volume:
   M_i(t) = Mentions_StockTwits(i, t) + Mentions_Reddit(i, t)
   Mindshare percentile P_i in [0, 1] relative to cross-section or historical baseline.
2. Bullish Ratio:
   BullishRatio_i = BullishCount_i / max(BearishCount_i, 1)
3. Volatility / Crowding Suppressor (Omega_i):
   - Normal condition (un-crowded): Omega_i = 1.00
   - Retail hype crowding penalty:
     If retail hype hits P85-P95 with high bullish ratio (>3.0),
     scale down Omega_i towards 0.50.
     Omega_i in [0.50, 1.00] strictly.
4. Historical Backfilling:
   Integrates with local `reddit_quant.db` daily aggregations.
5. Strict PIT:
   Every record carries an unalterable timestamp ('date', 'event_date', or 'retrieval_timestamp').
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

logger = logging.getLogger("Qualitative.psychological.stocktwits_regulator")


def utc_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


class StockTwitsRegulator:
    """Regulates qualitative alpha signals using StockTwits + Reddit crowding metrics."""

    def __init__(
        self,
        db_path: str = "reddit_quant.db",
        p_hype_low: float = 0.85,
        p_hype_high: float = 0.95,
        bullish_ratio_threshold: float = 3.0,
        min_omega: float = 0.50,
        max_omega: float = 1.00,
    ):
        """Initialize the regulator with crowding thresholds.

        Parameters:
            db_path: Path to sqlite database containing daily_aggregations.
            p_hype_low: Lower percentile boundary for retail hype (default: 0.85).
            p_hype_high: Upper percentile boundary for maximum retail hype (default: 0.95).
            bullish_ratio_threshold: Bullish/bearish ratio threshold (default: 3.0).
            min_omega: Minimum suppressor value (floor = 0.50).
            max_omega: Maximum suppressor value (ceiling = 1.00).
        """
        self.db_path = str(db_path)
        self.p_hype_low = float(p_hype_low)
        self.p_hype_high = float(p_hype_high)
        self.bullish_ratio_threshold = float(bullish_ratio_threshold)
        self.min_omega = float(min_omega)
        self.max_omega = float(max_omega)

    @staticmethod
    def compute_bullish_ratio(
        bullish_count: Union[int, float],
        bearish_count: Union[int, float],
    ) -> float:
        """Compute the ratio of bullish messages to bearish messages.

        Returns:
            float: Bullish ratio >= 0.0 (uses denominator max(bearish_count, 1)).
        """
        b_count = max(0.0, float(bullish_count))
        bear_count = max(0.0, float(bearish_count))
        denominator = max(1.0, bear_count)
        return float(b_count / denominator)

    def compute_omega(
        self,
        mindshare_percentile: float,
        bullish_ratio: float,
    ) -> float:
        """Compute the crowding/volatility suppressor Omega_i in [0.50, 1.00].

        Logic:
        - If mindshare_percentile < p_hype_low or bullish_ratio <= bullish_ratio_threshold:
          Omega_i = 1.00 (no crowding penalty).
        - If mindshare_percentile in [p_hype_low, p_hype_high] with bullish_ratio > 3.0:
          Scales down towards 0.50 in proportion to hype percentile and bullish excess.
        - If mindshare_percentile >= p_hype_high with bullish_ratio > 3.0:
          Omega_i hits the 0.50 floor.

        Returns:
            float bounded strictly in [min_omega, max_omega] = [0.50, 1.00].
        """
        p = float(mindshare_percentile)
        br = float(bullish_ratio)

        # Baseline: no crowding penalty
        if p < self.p_hype_low or br <= self.bullish_ratio_threshold:
            return float(self.max_omega)

        # Hype intensity h in [0.0, 1.0]
        denom = max(1e-5, self.p_hype_high - self.p_hype_low)
        h = min(1.0, max(0.0, (p - self.p_hype_low) / denom))

        # Bullish ratio excess factor (scales smoothly from 1.0 at threshold up to full weight)
        excess_ratio = min(1.0, max(0.0, (br - self.bullish_ratio_threshold) / 2.0))
        # Even right at threshold > 3.0, crowding risk is already triggered:
        ratio_weight = 0.5 + 0.5 * excess_ratio

        # Crowding penalty fraction in [0.0, 1.0]
        penalty_fraction = h * ratio_weight

        # Scale omega between max_omega (1.00) and min_omega (0.50)
        omega = self.max_omega - penalty_fraction * (self.max_omega - self.min_omega)

        # Enforce strict bounds
        omega = max(self.min_omega, min(self.max_omega, omega))
        return float(omega)

    def regulate_snapshot(
        self,
        ticker: str,
        stocktwits_mentions: int = 0,
        stocktwits_bullish: int = 0,
        stocktwits_bearish: int = 0,
        reddit_mentions: int = 0,
        reddit_sentiment: float = 0.0,
        mindshare_percentile: Optional[float] = None,
        as_of_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Compute the regulator outputs for a single stock snapshot."""
        tk = ticker.upper().strip()
        st_mentions = max(0, int(stocktwits_mentions))
        rd_mentions = max(0, int(reddit_mentions))
        total_mindshare = st_mentions + rd_mentions

        # Infer additional bullish/bearish counts from reddit if available
        if rd_mentions > 0 and stocktwits_bullish == 0 and stocktwits_bearish == 0:
            if reddit_sentiment > 0.15:
                inferred_bullish = int(rd_mentions * 0.75)
                inferred_bearish = int(rd_mentions * 0.25)
            elif reddit_sentiment < -0.15:
                inferred_bullish = int(rd_mentions * 0.25)
                inferred_bearish = int(rd_mentions * 0.75)
            else:
                inferred_bullish = int(rd_mentions * 0.50)
                inferred_bearish = int(rd_mentions * 0.50)
            total_bullish = inferred_bullish
            total_bearish = inferred_bearish
        else:
            total_bullish = stocktwits_bullish
            total_bearish = stocktwits_bearish

        bullish_ratio = self.compute_bullish_ratio(total_bullish, total_bearish)

        p = mindshare_percentile if mindshare_percentile is not None else 0.50
        omega = self.compute_omega(p, bullish_ratio)

        is_crowded = bool(p >= self.p_hype_low and bullish_ratio > self.bullish_ratio_threshold)
        ts = utc_now_iso()

        return {
            "ticker": tk,
            "mindshare_volume": total_mindshare,
            "stocktwits_mentions": st_mentions,
            "reddit_mentions": rd_mentions,
            "bullish_count": total_bullish,
            "bearish_count": total_bearish,
            "bullish_ratio": bullish_ratio,
            "mindshare_percentile": float(p),
            "omega_i": float(omega),
            "is_crowded": is_crowded,
            "event_date": as_of_date or ts[:10],
            "retrieval_timestamp": ts,
            "pit_timestamp_column": "retrieval_timestamp" if as_of_date is None else "event_date",
        }

    def regulate_panel(
        self,
        panel_df: pd.DataFrame,
        date_col: str = "date",
    ) -> pd.DataFrame:
        """Compute cross-sectional mindshare percentiles and Omega_i across tickers.

        Accepts DataFrame with columns:
        ['ticker', 'stocktwits_mentions' (or 'mention_count'), 'bullish_count', 'bearish_count']
        Optional: ['reddit_mentions', 'date']
        """
        if panel_df.empty:
            return pd.DataFrame()

        df = panel_df.copy()
        if "ticker" not in df.columns:
            raise ValueError("Input DataFrame must contain 'ticker' column.")

        # Resolve mention columns
        st_col = "stocktwits_mentions" if "stocktwits_mentions" in df.columns else (
            "mention_count" if "mention_count" in df.columns else None
        )
        st_mentions = df[st_col].fillna(0).astype(float) if st_col else pd.Series(0.0, index=df.index)
        rd_mentions = df["reddit_mentions"].fillna(0).astype(float) if "reddit_mentions" in df.columns else pd.Series(0.0, index=df.index)
        total_mindshare = st_mentions + rd_mentions
        df["mindshare_volume"] = total_mindshare

        # Compute cross-sectional percentile within date if date present, else across whole df
        if date_col in df.columns:
            df["mindshare_percentile"] = df.groupby(date_col)["mindshare_volume"].rank(pct=True)
        else:
            df["mindshare_percentile"] = df["mindshare_volume"].rank(pct=True)

        # Bullish ratio
        bull_col = "bullish_count" if "bullish_count" in df.columns else None
        bear_col = "bearish_count" if "bearish_count" in df.columns else None

        bull_counts = df[bull_col].fillna(0).astype(float) if bull_col else pd.Series(0.0, index=df.index)
        bear_counts = df[bear_col].fillna(0).astype(float) if bear_col else pd.Series(0.0, index=df.index)

        df["bullish_ratio"] = [
            self.compute_bullish_ratio(b, be) for b, be in zip(bull_counts, bear_counts)
        ]

        # Compute Omega_i
        df["omega_i"] = [
            self.compute_omega(p, br)
            for p, br in zip(df["mindshare_percentile"], df["bullish_ratio"])
        ]
        df["is_crowded"] = (df["mindshare_percentile"] >= self.p_hype_low) & (df["bullish_ratio"] > self.bullish_ratio_threshold)

        ts = utc_now_iso()
        if "retrieval_timestamp" not in df.columns:
            df["retrieval_timestamp"] = ts
        if "pit_timestamp_column" not in df.columns:
            df["pit_timestamp_column"] = date_col if date_col in df.columns else "retrieval_timestamp"

        return df

    def query_reddit_daily_aggregations(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        tickers: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        """Query local reddit_quant.db daily aggregations."""
        if not Path(self.db_path).exists():
            logger.warning("Database path %s does not exist.", self.db_path)
            return pd.DataFrame()

        with closing(sqlite3.connect(self.db_path)) as conn:
            query = """
                SELECT 
                    ticker,
                    date,
                    SUM(mention_count) as reddit_mentions,
                    SUM(weighted_sum) / MAX(SUM(total_weight), 1.0) as reddit_avg_sentiment
                FROM daily_aggregations
                WHERE 1=1
            """
            params: List[Any] = []
            if start_date:
                query += " AND date >= ?"
                params.append(start_date)
            if end_date:
                query += " AND date <= ?"
                params.append(end_date)
            if tickers:
                placeholders = ",".join(["?"] * len(tickers))
                query += f" AND ticker IN ({placeholders})"
                params.extend([t.upper() for t in tickers])

            query += " GROUP BY ticker, date ORDER BY date, ticker"
            df = pd.read_sql_query(query, conn, params=params)

        return df

    def merge_with_reddit_daily(
        self,
        stocktwits_df: pd.DataFrame,
        as_of_date: Optional[str] = None,
    ) -> pd.DataFrame:
        """Merge external StockTwits dataframe with historical Reddit aggregations."""
        if stocktwits_df.empty:
            return pd.DataFrame()

        st_df = stocktwits_df.copy()
        if "ticker" not in st_df.columns:
            raise ValueError("stocktwits_df must contain 'ticker' column.")

        # Format date column
        date_val = as_of_date
        if date_val is None and "date" in st_df.columns:
            date_val = str(st_df["date"].iloc[0])

        tickers = list(st_df["ticker"].unique())
        reddit_df = self.query_reddit_daily_aggregations(
            start_date=date_val,
            end_date=date_val,
            tickers=tickers,
        )

        if not reddit_df.empty:
            merged = pd.merge(st_df, reddit_df, on=["ticker", "date"] if "date" in st_df.columns and "date" in reddit_df.columns else "ticker", how="left")
        else:
            merged = st_df.copy()
            merged["reddit_mentions"] = 0
            merged["reddit_avg_sentiment"] = 0.0

        merged["reddit_mentions"] = merged["reddit_mentions"].fillna(0)
        merged["reddit_avg_sentiment"] = merged["reddit_avg_sentiment"].fillna(0.0)

        # Regulate panel
        result_df = self.regulate_panel(merged)
        return result_df

    def backfill_historical_regulator(
        self,
        start_date: str,
        end_date: str,
        tickers: Optional[List[str]] = None,
        persist_table: Optional[str] = None,
    ) -> pd.DataFrame:
        """Backfill daily regulator metrics from reddit_quant.db historical aggregations."""
        reddit_df = self.query_reddit_daily_aggregations(
            start_date=start_date,
            end_date=end_date,
            tickers=tickers,
        )

        if reddit_df.empty:
            return pd.DataFrame()

        df = reddit_df.copy()
        # Derive proxy bullish/bearish counts from sentiment
        # In a backfill where only sentiment exists, positive sentiment provides bullish count
        df["bullish_count"] = (df["reddit_mentions"] * np.clip(0.5 + 0.5 * df["reddit_avg_sentiment"], 0.0, 1.0)).round().astype(int)
        df["bearish_count"] = (df["reddit_mentions"] - df["bullish_count"]).clip(lower=0).astype(int)
        df["stocktwits_mentions"] = 0

        regulated_df = self.regulate_panel(df, date_col="date")

        if persist_table and Path(self.db_path).exists():
            with closing(sqlite3.connect(self.db_path)) as conn:
                regulated_df.to_sql(persist_table, conn, if_exists="append", index=False)
                conn.commit()
            logger.info("Persisted %d rows to table '%s'", len(regulated_df), persist_table)

        return regulated_df
