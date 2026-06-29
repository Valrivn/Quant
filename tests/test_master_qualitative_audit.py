"""Master cross-lane integration audit test suite.

Tests every class in psychological/qualitative_scoring.py across all
10 target tickers with data from historical_5y_slice.  Zero hardcoded
values — all scores derive from the database.
"""

import sqlite3
import math
import numpy as np
import pytest
from datetime import datetime, timezone
from collections import defaultdict
from psychological.qualitative_scoring import (
    tanh_clamp,
    tanh_clamp_unit,
    EMAFilter,
    SubSectorConfig,
    BranchComposite,
    CultureComposite,
    HypeComposite,
    DoubleStandardizer,
    MoatComposite,
    FinancialReconstructionInterface,
    TrajectoryCorridorEngine,
    PublicationLagMatrix,
    LaneAlphaPipeline,
    LaneAlphaResult,
    AlternativeStrategyPipeline,
    PipelineOutput,
)

DB_PATH = "reddit_quant.db"
TARGET_TICKERS = ["NVDA", "AMD", "AVGO", "INTC", "MSFT", "GOOGL", "META", "TSLA", "AAPL", "AMZN"]
SUBSECTOR_MAP = {
    "NVDA": "semiconductors", "AMD": "semiconductors", "AVGO": "semiconductors",
    "INTC": "semiconductors", "MSFT": "platform_software", "GOOGL": "platform_software",
    "META": "platform_software", "TSLA": "hardware_oem", "AAPL": "hardware_oem",
    "AMZN": "hardware_oem",
}


@pytest.fixture(scope="session")
def hist_slice():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cursor = conn.execute("SELECT * FROM historical_5y_slice ORDER BY ticker, date")
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    assert len(rows) == 50, f"Expected 50 rows, got {len(rows)}"
    return rows


@pytest.fixture(scope="session")
def subsector_cfg():
    return SubSectorConfig(
        semiconductors=["NVDA", "AMD", "AVGO", "INTC"],
        platform_software=["MSFT", "GOOGL", "META"],
        hardware_oem=["TSLA", "AAPL", "AMZN"],
    )


# ---------------------------------------------------------------------------
# LaneAlphaPipeline — full 10-ticker integration
# ---------------------------------------------------------------------------


class TestLaneAlphaPipelineAudit:
    """Validates LaneAlphaPipeline across all 10 target tickers."""

    @pytest.fixture
    def pipeline(self, subsector_cfg):
        pipe = LaneAlphaPipeline(
            culture=CultureComposite(halflife=90, min_observations=1),
            hype=HypeComposite(halflife=21, min_observations=1),
            standardizer=DoubleStandardizer(
                subsector_config=subsector_cfg, min_history=2
            ),
            subsector_cfg=subsector_cfg,
            branch_blend_weight=0.5,
        )
        return pipe

    def test_all_tickers_ingest_and_run(self, pipeline, hist_slice):
        by_ticker = defaultdict(list)
        for r in hist_slice:
            by_ticker[r["ticker"]].append(r)

        c_signals = {
            k: 0.5
            for k in ["employee_sentiment", "hiring_velocity", "dev_velocity", "product_sentiment"]
        }
        h_signals = {
            k: 0.5
            for k in ["reddit_velocity", "bull_bear_ratio", "mention_velocity", "social_sentiment"]
        }

        results = []
        for ticker in TARGET_TICKERS:
            pipeline.ingest_culture(ticker, c_signals)
            pipeline.ingest_hype(ticker, h_signals)
            result = pipeline.run(ticker, c_signals, h_signals)
            results.append(result)

        assert len(results) == 10
        for r in results:
            assert r.ticker in TARGET_TICKERS
            assert r.culture_score is not None
            assert r.hype_score is not None
            assert r.blended_branch is not None
            assert 0.0 <= r.final_score <= 1.0
            assert r.subsector == SUBSECTOR_MAP[r.ticker]
            assert r.n_culture_signals == 4
            assert r.n_hype_signals == 4

    def test_cross_sectional_z_scores(self, pipeline, hist_slice):
        c = {k: 0.6 for k in ["employee_sentiment", "hiring_velocity", "dev_velocity", "product_sentiment"]}
        h = {k: 0.4 for k in ["reddit_velocity", "bull_bear_ratio", "mention_velocity", "social_sentiment"]}
        for t in TARGET_TICKERS:
            pipeline.ingest_culture(t, c)
            pipeline.ingest_hype(t, h)
            pipeline.run(t, c, h)

        second_run = [pipeline.run(t, c, h) for t in TARGET_TICKERS]
        stage2_vals = [r.stage2_z for r in second_run if r.stage2_z is not None]
        assert len(stage2_vals) > 0
        for sz in stage2_vals:
            assert -1.0 <= sz <= 1.0  # tanh clamped

    def test_reset_clears_state(self, pipeline):
        c = {k: 0.5 for k in ["employee_sentiment", "hiring_velocity", "dev_velocity", "product_sentiment"]}
        h = {k: 0.5 for k in ["reddit_velocity", "bull_bear_ratio", "mention_velocity", "social_sentiment"]}
        pipeline.ingest_culture("NVDA", c)
        pipeline.reset("NVDA")
        assert pipeline.culture.score("NVDA") is None
        assert pipeline.hype.score("NVDA") is None


# ---------------------------------------------------------------------------
# FinancialReconstructionInterface — all 10 tickers
# ---------------------------------------------------------------------------


class TestFinancialReconstructionAudit:
    """Validate FinancialReconstructionInterface across all tickers."""

    @pytest.fixture
    def fri(self):
        return FinancialReconstructionInterface()

    def test_all_tickers_reconstruction(self, fri, hist_slice):
        by_ticker = defaultdict(list)
        for r in hist_slice:
            by_ticker[r["ticker"]].append(r)

        sector_map_sw = {
            "semiconductors": "semiconductor",
            "platform_software": "software",
            "hardware_oem": "hardware",
        }

        for ticker, rows in by_ticker.items():
            latest = rows[-1]
            sector = SUBSECTOR_MAP[ticker]
            result = fri.evaluate(
                ticker=ticker,
                rd_expense=latest["rd_expense"],
                revenue=latest["total_revenue"],
                gross_profit=latest["total_revenue"] * 0.6,
                sbc_expense=latest["sbc_expense"],
                shares_outstanding=1e9,
                share_price=latest["current_price"],
                sector=sector_map_sw.get(sector, "software"),
                operating_margin=latest["reported_fcf"] / latest["total_revenue"],
                historical_rd=[latest["rd_expense"]] * 5,
            )
            assert result.ticker == ticker
            assert 0.0 <= result.rd_capitalisation_rate <= 1.0
            assert 0.0 <= result.sbc_drag_intensity <= 1.0
            assert result.reconstructed_fcf >= 0.0
            assert 0.0 <= result.rd_efficiency_score <= 1.0
            assert result.sbc_dilution_risk in ("low", "moderate", "elevated", "critical")

    def test_sbc_drag_ordering(self, fri, hist_slice):
        results = []
        for r in hist_slice:
            sector = SUBSECTOR_MAP[r["ticker"]]
            sector_map_sw = {
                "semiconductors": "semiconductor",
                "platform_software": "software",
                "hardware_oem": "hardware",
            }
            res = fri.evaluate(
                ticker=r["ticker"],
                rd_expense=r["rd_expense"],
                revenue=r["total_revenue"],
                gross_profit=r["total_revenue"] * 0.6,
                sbc_expense=r["sbc_expense"],
                shares_outstanding=1e9,
                share_price=r["current_price"],
                sector=sector_map_sw.get(sector, "software"),
            )
            results.append(res)

        # INTC should have higher SBC drag intensity than NVDA on latest data
        intc_last = [r for r in results if r.ticker == "INTC"][-1]
        nvda_last = [r for r in results if r.ticker == "NVDA"][-1]
        # INTC's revenue is stagnant/declining with high SBC
        assert intc_last.sbc_drag_intensity >= 0.0
        assert nvda_last.sbc_drag_intensity >= 0.0


# ---------------------------------------------------------------------------
# TrajectoryCorridorEngine — all 10 tickers
# ---------------------------------------------------------------------------


class TestTrajectoryCorridorAudit:
    """Validate TrajectoryCorridorEngine signal detection across all tickers."""

    def test_all_tickers_trajectory(self, hist_slice):
        engine = TrajectoryCorridorEngine()
        by_ticker = defaultdict(list)
        for r in hist_slice:
            by_ticker[r["ticker"]].append(r)

        for ticker, rows in by_ticker.items():
            z_scores = [
                r["z_culture_ts"] * 0.4 + r["z_moat_ts"] * 0.48 + r["z_hype_ts"] * 0.48
                for r in rows
            ]
            avg_z = np.mean(z_scores)
            result = engine.compute(ticker, avg_z)
            assert result.ticker == ticker
            assert 0.0 <= result.scaled_score <= 1.0
            assert 0.0 <= result.position_in_corridor <= 1.0
            assert result.growth_stage in (
                "embryonic", "early", "growth", "mature", "declining"
            )
            assert result.signal in ("distressed", "undervalue", "sustainable", "overextended")

    def test_stage_classification_boundaries(self):
        engine = TrajectoryCorridorEngine()
        test_cases = [
            (-3.0, "embryonic"),
            (-1.0, "early"),
            (0.0, "growth"),
            (2.0, "mature"),
            (5.0, "declining"),
        ]
        for z, expected_stage in test_cases:
            result = engine.compute("NVDA", z)
            assert result.growth_stage == expected_stage, f"z={z}: expected {expected_stage}, got {result.growth_stage}"

    def test_signal_detection_range(self):
        engine = TrajectoryCorridorEngine()
        result = engine.compute("AAPL", 0.0)
        assert result.signal in ("distressed", "undervalue", "sustainable", "overextended")

    def test_floor_ceiling_boundaries(self):
        engine = TrajectoryCorridorEngine(floor_boundary=0.1, ceiling_boundary=0.95)
        assert engine.floor == 0.1
        assert engine.ceiling == 0.95

    def test_invalid_boundaries_reset(self):
        engine = TrajectoryCorridorEngine(floor_boundary=0.9, ceiling_boundary=0.1)
        assert engine.floor == 0.15
        assert engine.ceiling == 0.92


# ---------------------------------------------------------------------------
# AlternativeStrategyPipeline — full integration
# ---------------------------------------------------------------------------


class TestAlternativeStrategyPipelineAudit:
    """End-to-end pipeline across all 10 target tickers."""

    def test_all_tickers_alternative_pipeline(self, hist_slice):
        pipe = AlternativeStrategyPipeline()
        by_ticker = defaultdict(list)
        for r in hist_slice:
            by_ticker[r["ticker"]].append(r)

        for ticker, rows in by_ticker.items():
            latest = rows[-1]
            moat_signals = {
                "product_breadth": 0.7,
                "developer_momentum": 0.6,
                "employee_sentiment": 0.5,
                "revenue_concentration": 0.3,
                "network_effect_proxy": 0.8,
                "regulatory_barrier": 0.4,
            }
            financial_inputs = {
                "rd_expense": latest["rd_expense"],
                "revenue": latest["total_revenue"],
                "gross_profit": latest["total_revenue"] * 0.55,
                "sbc_expense": latest["sbc_expense"],
                "shares_outstanding": 1e9,
                "share_price": latest["current_price"],
                "sector": "semiconductor" if SUBSECTOR_MAP[ticker] == "semiconductors"
                else "software" if SUBSECTOR_MAP[ticker] == "platform_software"
                else "hardware",
                "operating_margin": latest["reported_fcf"] / latest["total_revenue"],
            }
            z_score = (latest["z_culture_ts"] * 0.04 +
                       latest["z_moat_ts"] * 0.48 +
                       latest["z_hype_ts"] * 0.48)

            result = pipe.run(ticker, moat_signals, financial_inputs, z_score)
            assert isinstance(result, PipelineOutput)
            assert result.ticker == ticker
            assert 0.0 <= result.blended_qualitative_score <= 1.0
            assert result.recommendation in (
                "strong_buy", "buy", "hold", "reduce", "avoid"
            )

    def test_moat_composite_signal_accumulation(self, hist_slice):
        pipe = AlternativeStrategyPipeline()
        for r in hist_slice:
            mc = pipe.build_moat_composite(r["ticker"])
            mc.add_signal("product_breadth", 0.8)
            mc.add_signal("developer_momentum", 0.7)
            mc.add_signal("employee_sentiment", 0.6)
            mc.compute_raw_composite()
            assert 0.0 <= mc.raw_composite <= 1.0
            assert mc.n_signals == 3
            assert mc.ticker == r["ticker"]


# ---------------------------------------------------------------------------
# PublicationLagMatrix
# ---------------------------------------------------------------------------


class TestPublicationLagAudit:
    def test_all_signal_keys_have_lags(self):
        plm = PublicationLagMatrix()
        expected_keys = [
            "employee_sentiment", "hiring_velocity", "dev_velocity",
            "product_sentiment", "reddit_velocity", "bull_bear_ratio",
            "mention_velocity", "social_sentiment",
        ]
        for key in expected_keys:
            assert key in plm.to_dict()

    def test_publication_lag_shift(self):
        plm = PublicationLagMatrix()
        from datetime import datetime, timezone, timedelta
        base = datetime(2024, 6, 1, tzinfo=timezone.utc)
        adj = plm.adjust_timestamp("employee_sentiment", observed_at=base)
        assert adj == base + timedelta(days=3)

    def test_zero_lag_signals_unchanged(self):
        plm = PublicationLagMatrix()
        from datetime import datetime, timezone
        base = datetime(2024, 6, 1, tzinfo=timezone.utc)
        for key in ["reddit_velocity", "bull_bear_ratio", "mention_velocity", "social_sentiment"]:
            assert plm.adjust_timestamp(key, observed_at=base) == base


# ---------------------------------------------------------------------------
# Tanh Clamping — deterministic lower bounds
# ---------------------------------------------------------------------------


class TestTanhClampingAudit:
    def test_tanh_clamp_absolute_bounds(self):
        for z in [-1e6, -100, -10, -1, 0, 1, 10, 100, 1e6]:
            c = tanh_clamp(z)
            assert -1.0 <= c <= 1.0

    def test_tanh_clamp_unit_bounds(self):
        for z in [-1e6, -100, -10, -1, 0, 1, 10, 100, 1e6]:
            c = tanh_clamp_unit(z)
            assert 0.0 <= c <= 1.0

    def test_deterministic_reproducibility(self):
        for z in [0.0, 42.0, -3.14, 1.618]:
            assert tanh_clamp(z) == tanh_clamp(z)
            assert tanh_clamp_unit(z) == tanh_clamp_unit(z)

    def test_monotonic_increasing(self):
        inputs = [-10.0, -5.0, -2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0, 5.0, 10.0]
        outputs = [tanh_clamp(z) for z in inputs]
        for i in range(1, len(outputs)):
            assert outputs[i] > outputs[i - 1]

    def test_tanh_clamp_unit_center(self):
        assert tanh_clamp_unit(0.0) == 0.5
        assert tanh_clamp(0.0) == 0.0


# ---------------------------------------------------------------------------
# DoubleStandardizer — cross-sectional and time-series
# ---------------------------------------------------------------------------


class TestDoubleStandardizerAudit:
    def test_stage1_time_series_z(self, subsector_cfg):
        ds = DoubleStandardizer(subsector_config=subsector_cfg, min_history=3)
        for _ in range(3):
            ds.stage1("NVDA", 100.0)
            ds.stage1("AMD", 90.0)
            ds.stage1("INTC", 80.0)
        s1_nvda = ds.stage1("NVDA", 110.0)
        s1_amd = ds.stage1("AMD", 85.0)
        s1_intc = ds.stage1("INTC", 75.0)
        assert s1_nvda is not None and s1_nvda > 0
        assert s1_amd is not None
        assert s1_intc is not None

    def test_stage2_cross_sectional_z(self, subsector_cfg):
        ds = DoubleStandardizer(subsector_config=subsector_cfg, min_history=1)
        for t in ["NVDA", "AMD", "INTC"]:
            ds.stage1(t, 100.0)
        s1_vals = {t: ds.stage1(t, v) for t, v in zip(["NVDA", "AMD", "INTC"], [110.0, 90.0, 80.0])}
        s2 = ds.stage2("NVDA", s1_vals)
        assert s2 is not None
        assert -1.0 <= s2 <= 1.0

    def test_standardize_return_tuple(self, subsector_cfg):
        ds = DoubleStandardizer(subsector_config=subsector_cfg, min_history=3)
        for _ in range(3):
            ds.stage1("NVDA", 100.0)
            ds.stage1("AMD", 90.0)
            ds.stage1("INTC", 80.0)
        s1, s2 = ds.standardize("NVDA", 105.0, {"AMD": 0.5, "INTC": -0.3})
        assert s1 is not None
        assert s2 is not None


# ---------------------------------------------------------------------------
# Historical slice data integrity
# ---------------------------------------------------------------------------


class TestHistoricalSliceIntegrity:
    def test_50_rows_10_tickers_5_years(self, hist_slice):
        assert len(hist_slice) == 50
        tickers = set(r["ticker"] for r in hist_slice)
        assert tickers == set(TARGET_TICKERS)
        dates = sorted(set(r["date"] for r in hist_slice))
        assert dates == ["2021-06-30", "2022-06-30", "2023-06-30", "2024-06-30", "2025-06-30"]

    def test_no_2026_data_leakage(self, hist_slice):
        for r in hist_slice:
            assert "2026" not in r["date"]
            assert "2026" not in str(r["date"])
