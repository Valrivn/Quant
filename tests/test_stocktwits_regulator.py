"""Unit and integration tests for Qualitative/psychological/stocktwits_regulator.py."""

import sqlite3
import pytest
import pandas as pd
import numpy as np

from Qualitative.psychological.stocktwits_regulator import StockTwitsRegulator


class TestStockTwitsRegulator:
    @pytest.fixture
    def regulator(self):
        return StockTwitsRegulator()

    @pytest.fixture
    def mock_db(self, tmp_path):
        """Create a temporary sqlite db mimicking reddit_quant.db daily_aggregations."""
        db_file = tmp_path / "mock_reddit_quant.db"
        conn = sqlite3.connect(str(db_file))
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE daily_aggregations (
                ticker TEXT,
                date TEXT,
                category TEXT,
                subreddit TEXT,
                mention_count INTEGER,
                avg_sentiment REAL,
                weighted_sum REAL,
                total_weight REAL,
                model_version TEXT,
                source TEXT,
                PRIMARY KEY (ticker, date, category, subreddit)
            )
        """)
        # Insert test data
        cursor.execute("""
            INSERT INTO daily_aggregations 
            (ticker, date, category, subreddit, mention_count, avg_sentiment, weighted_sum, total_weight, model_version, source)
            VALUES 
            ('AAPL', '2026-09-01', 'retail', 'wallstreetbets', 100, 0.6, 60.0, 100.0, 'v1', 'reddit'),
            ('AAPL', '2026-09-01', 'retail', 'stocks', 50, 0.4, 20.0, 50.0, 'v1', 'reddit'),
            ('MSFT', '2026-09-01', 'retail', 'wallstreetbets', 30, 0.1, 3.0, 30.0, 'v1', 'reddit'),
            ('NVDA', '2026-09-01', 'retail', 'wallstreetbets', 500, 0.9, 450.0, 500.0, 'v1', 'reddit')
        """)
        conn.commit()
        conn.close()
        return str(db_file)

    def test_bullish_ratio_calculation(self, regulator):
        """Verify bullish ratio calculation and zero denominator safety."""
        # Standard ratio
        assert regulator.compute_bullish_ratio(90, 10) == 9.0
        # Zero bearish count (protected by max(bearish, 1))
        assert regulator.compute_bullish_ratio(15, 0) == 15.0
        # Zero bullish count
        assert regulator.compute_bullish_ratio(0, 20) == 0.0
        # Both zero
        assert regulator.compute_bullish_ratio(0, 0) == 0.0

    def test_omega_uncrowded_baseline(self, regulator):
        """Verify Omega_i is 1.00 when retail hype or bullish ratio is below threshold."""
        # Low hype, low ratio
        assert regulator.compute_omega(mindshare_percentile=0.50, bullish_ratio=1.5) == 1.00
        # Low hype, high ratio (>3.0) -> No crowding penalty since volume/hype is low
        assert regulator.compute_omega(mindshare_percentile=0.70, bullish_ratio=5.0) == 1.00
        # High hype (P90), but bearish/balanced ratio (<=3.0) -> No crowding penalty
        assert regulator.compute_omega(mindshare_percentile=0.92, bullish_ratio=2.5) == 1.00
        # Edge of threshold: P84 with high ratio -> No crowding penalty
        assert regulator.compute_omega(mindshare_percentile=0.84, bullish_ratio=6.0) == 1.00

    def test_omega_crowding_penalty_scaling(self, regulator):
        """Verify Omega_i scales down towards 0.50 when hype is P85-P95 and bullish ratio > 3.0."""
        # At P85 boundary with bullish ratio > 3.0, penalty starts
        omega_p85 = regulator.compute_omega(mindshare_percentile=0.85, bullish_ratio=4.0)
        assert omega_p85 == 1.00

        # At P90 (midpoint of P85-P95) with bullish ratio 4.0: Omega_i should be between 0.50 and 1.00
        omega_p90 = regulator.compute_omega(mindshare_percentile=0.90, bullish_ratio=4.0)
        assert 0.50 < omega_p90 < 1.00

        # At P95+ with extreme bullish ratio (>3.0), Omega_i scales down to floor 0.50
        omega_p95 = regulator.compute_omega(mindshare_percentile=0.95, bullish_ratio=5.0)
        assert np.isclose(omega_p95, 0.50, atol=1e-3)

        # At P99 with extreme bullish ratio
        omega_p99 = regulator.compute_omega(mindshare_percentile=0.99, bullish_ratio=10.0)
        assert omega_p99 == 0.50

    def test_omega_strict_bounds(self, regulator):
        """Ensure Omega_i is strictly bounded in [0.50, 1.00] across extreme inputs."""
        for p in [-0.5, 0.0, 0.5, 0.85, 0.90, 0.95, 1.0, 2.0]:
            for br in [0.0, 1.0, 3.0, 3.1, 5.0, 100.0]:
                omega = regulator.compute_omega(mindshare_percentile=p, bullish_ratio=br)
                assert 0.50 <= omega <= 1.00, f"Failed bound for p={p}, br={br}: {omega}"

    def test_regulate_snapshot(self, regulator):
        """Test regulating a single stock snapshot with full metrics output."""
        snap = regulator.regulate_snapshot(
            ticker="NVDA",
            stocktwits_mentions=800,
            stocktwits_bullish=700,
            stocktwits_bearish=50,
            reddit_mentions=200,
            mindshare_percentile=0.96,
            as_of_date="2026-09-01",
        )
        assert snap["ticker"] == "NVDA"
        assert snap["mindshare_volume"] == 1000
        assert snap["bullish_ratio"] == 700 / 50.0  # 14.0
        assert snap["omega_i"] == 0.50
        assert snap["is_crowded"] is True
        assert "retrieval_timestamp" in snap
        assert snap["event_date"] == "2026-09-01"

    def test_regulate_panel(self, regulator):
        """Test cross-sectional panel ranking and Omega_i calculation."""
        panel = pd.DataFrame([
            {"ticker": "AAPL", "stocktwits_mentions": 100, "bullish_count": 50, "bearish_count": 50},
            {"ticker": "MSFT", "stocktwits_mentions": 200, "bullish_count": 120, "bearish_count": 80},
            {"ticker": "GOOGL", "stocktwits_mentions": 300, "bullish_count": 180, "bearish_count": 120},
            {"ticker": "NVDA", "stocktwits_mentions": 1000, "bullish_count": 900, "bearish_count": 50},
        ])
        regulated = regulator.regulate_panel(panel)
        assert len(regulated) == 4
        assert "mindshare_percentile" in regulated.columns
        assert "omega_i" in regulated.columns

        # NVDA is the top volume (P100) with bullish ratio 18.0 -> must be crowded (Omega = 0.50)
        nvda_row = regulated[regulated["ticker"] == "NVDA"].iloc[0]
        assert nvda_row["mindshare_percentile"] == 1.0
        assert nvda_row["omega_i"] == 0.50
        assert bool(nvda_row["is_crowded"]) is True

        # AAPL is the lowest volume (P25) -> uncrowded (Omega = 1.00)
        aapl_row = regulated[regulated["ticker"] == "AAPL"].iloc[0]
        assert aapl_row["omega_i"] == 1.00
        assert bool(aapl_row["is_crowded"]) is False

    def test_merge_with_reddit_daily(self, mock_db):
        """Test merging external StockTwits feed with local reddit_quant.db daily aggregations."""
        reg = StockTwitsRegulator(db_path=mock_db)
        stocktwits_df = pd.DataFrame([
            {"ticker": "AAPL", "date": "2026-09-01", "stocktwits_mentions": 50, "bullish_count": 40, "bearish_count": 10},
            {"ticker": "MSFT", "date": "2026-09-01", "stocktwits_mentions": 10, "bullish_count": 6, "bearish_count": 4},
        ])
        merged = reg.merge_with_reddit_daily(stocktwits_df, as_of_date="2026-09-01")
        assert len(merged) == 2
        # AAPL reddit mentions from mock DB was 100 + 50 = 150
        aapl_row = merged[merged["ticker"] == "AAPL"].iloc[0]
        assert aapl_row["reddit_mentions"] == 150
        assert aapl_row["mindshare_volume"] == 50 + 150  # 200

    def test_backfill_historical_regulator(self, mock_db):
        """Test backfilling regulator factors from historical reddit_quant.db."""
        reg = StockTwitsRegulator(db_path=mock_db)
        backfill_df = reg.backfill_historical_regulator(
            start_date="2026-09-01",
            end_date="2026-09-01",
            persist_table="test_regulator_daily",
        )
        assert len(backfill_df) == 3  # AAPL, MSFT, NVDA
        assert "omega_i" in backfill_df.columns
        assert "mindshare_volume" in backfill_df.columns
        assert "retrieval_timestamp" in backfill_df.columns
        # Verify persistence into mock db
        conn = sqlite3.connect(mock_db)
        count = conn.execute("SELECT COUNT(*) FROM test_regulator_daily").fetchone()[0]
        conn.close()
        assert count == 3
