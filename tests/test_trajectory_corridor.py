import pytest
from psychological.qualitative_scoring import (
    TrajectoryCorridorEngine,
    TrajectoryCorridorResult,
    MoatComposite,
    AlternativeStrategyPipeline,
    FinancialReconstructionInterface,
    create_trajectory_corridor_engine,
    create_alternative_strategy_pipeline,
)


class TestMoatComposite:
    @pytest.fixture
    def moat(self):
        return MoatComposite(ticker="NVDA")

    def test_init(self, moat):
        assert moat.ticker == "NVDA"
        assert moat.scores == {}
        assert moat.ema_60d is None

    def test_add_signal(self, moat):
        moat.add_signal("product_breadth", 0.85)
        assert moat.scores["product_breadth"] == 0.85

    def test_add_signal_clamps(self, moat):
        moat.add_signal("product_breadth", 1.5)
        assert moat.scores["product_breadth"] == 1.0
        moat.add_signal("developer_momentum", -0.5)
        assert moat.scores["developer_momentum"] == 0.0

    def test_add_signal_unknown_key(self, moat):
        moat.add_signal("nonexistent", 0.5)
        assert "nonexistent" not in moat.scores

    def test_compute_raw_composite_empty(self, moat):
        assert moat.compute_raw_composite() == 0.0

    def test_compute_raw_composite_single(self, moat):
        moat.add_signal("product_breadth", 1.0)
        assert moat.compute_raw_composite() == 1.0

    def test_compute_raw_composite_multi(self, moat):
        moat.add_signal("product_breadth", 1.0)
        moat.add_signal("developer_momentum", 0.5)
        composite = moat.compute_raw_composite()
        assert 0.0 < composite < 1.0

    def test_update_ema(self, moat):
        moat.add_signal("product_breadth", 0.8)
        ema = moat.update_ema()
        assert ema == pytest.approx(0.8)
        assert moat.ema_60d == pytest.approx(0.8)

    def test_update_ema_with_previous(self, moat):
        moat.add_signal("product_breadth", 0.8)
        ema = moat.update_ema(previous_ema=0.5)
        alpha = 2.0 / 61.0
        expected = alpha * 0.8 + (1 - alpha) * 0.5
        assert ema == pytest.approx(expected)

    def test_ema_static(self):
        result = MoatComposite.ema(0.5, 0.8, period=60)
        alpha = 2.0 / 61.0
        expected = alpha * 0.8 + (1 - alpha) * 0.5
        assert result == pytest.approx(expected)

    def test_ema_no_previous(self):
        result = MoatComposite.ema(None, 0.8, period=60)
        assert result == 0.8

    def test_moat_default_weights(self):
        mc = MoatComposite(ticker="NVDA")
        assert "product_breadth" in mc.MOAT_DEFAULT_WEIGHTS
        assert abs(sum(mc.MOAT_DEFAULT_WEIGHTS.values()) - 1.0) < 0.01


class TestTrajectoryCorridorEngine:
    @pytest.fixture
    def engine(self):
        return TrajectoryCorridorEngine()

    @pytest.fixture
    def custom_engine(self):
        return TrajectoryCorridorEngine(floor_boundary=0.1, ceiling_boundary=0.95)

    def test_tanh_scale_zero(self, engine):
        assert engine.tanh_scale(0.0) == 0.0

    def test_tanh_scale_positive(self, engine):
        val = engine.tanh_scale(2.0)
        assert 0.0 < val < 1.0

    def test_tanh_scale_negative(self, engine):
        val = engine.tanh_scale(-2.0)
        assert -1.0 < val < 0.0

    def test_compute_negative_z(self, engine):
        result = engine.compute("NVDA", -3.0)
        assert result.ticker == "NVDA"
        assert result.raw_z == -3.0
        assert result.growth_stage == "embryonic"
        assert 0.0 <= result.scaled_score <= 1.0
        assert result.signal == "distressed"

    def test_compute_positive_z(self, engine):
        result = engine.compute("NVDA", 2.0)
        assert result.growth_stage == "mature"
        assert result.signal in ("overextended", "sustainable", "undervalue")

    def test_compute_zero_z(self, engine):
        result = engine.compute("NVDA", 0.0)
        assert result.growth_stage == "growth"
        assert 0.0 <= result.position_in_corridor <= 1.0

    def test_compute_high_z(self, engine):
        result = engine.compute("NVDA", 5.0)
        assert result.growth_stage == "declining"

    def test_compute_low_z(self, engine):
        result = engine.compute("NVDA", -5.0)
        assert result.growth_stage == "embryonic"

    def test_low_negative_z_is_distressed(self, engine):
        result = engine.compute("NVDA", -5.0)
        assert result.signal == "distressed"

    def test_negative_z_within_same_stage_is_monotonic(self, engine):
        r1 = engine.compute("NVDA", -1.5)
        r2 = engine.compute("NVDA", -1.0)
        assert r1.position_in_corridor <= r2.position_in_corridor

    def test_positive_z_within_same_stage_is_monotonic(self, engine):
        r1 = engine.compute("NVDA", 0.0)
        r2 = engine.compute("NVDA", 0.5)
        assert r1.position_in_corridor <= r2.position_in_corridor

    def test_high_z_drops_to_declining(self, engine):
        result = engine.compute("NVDA", 5.0)
        assert result.growth_stage == "declining"
        assert result.decay_factor == 0.20

    def test_signal_maps_to_correct_range(self, engine):
        for z in [-5.0, -2.0, 0.0, 2.0, 5.0]:
            result = engine.compute("NVDA", z)
            assert 0.0 <= result.scaled_score <= 1.0
            assert 0.0 <= result.position_in_corridor <= 1.0
            assert result.signal in ("distressed", "undervalue", "sustainable", "overextended")

    def test_custom_boundaries(self, custom_engine):
        result = custom_engine.compute("AMD", 0.0)
        assert result.floor_boundary == 0.1
        assert result.ceiling_boundary == 0.95

    def test_invalid_boundaries_corrected(self):
        engine = TrajectoryCorridorEngine(floor_boundary=0.9, ceiling_boundary=0.1)
        assert engine.floor == 0.15
        assert engine.ceiling == 0.92

    def test_corridor_width(self, engine):
        result = engine.compute("NVDA", 0.0)
        assert result.corridor_width == pytest.approx(0.77)

    def test_create_factory(self):
        engine = create_trajectory_corridor_engine(floor_boundary=0.05, ceiling_boundary=0.95)
        assert isinstance(engine, TrajectoryCorridorEngine)

    def test_all_stages_covered(self, engine):
        test_zs = [-3.5, -1.0, 0.0, 2.0, 4.0]
        stages = set()
        for z in test_zs:
            result = engine.compute("TEST", z)
            stages.add(result.growth_stage)
        assert stages == {"embryonic", "early", "growth", "mature", "declining"}


class TestAlternativeStrategyPipeline:
    @pytest.fixture
    def pipeline(self):
        return AlternativeStrategyPipeline()

    def test_init(self, pipeline):
        assert pipeline.financial is not None
        assert pipeline.trajectory is not None

    def test_run_minimal(self, pipeline):
        output = pipeline.run(
            ticker="NVDA",
            moat_signals={"product_breadth": 0.8, "developer_momentum": 0.7},
        )
        assert output.ticker == "NVDA"
        assert output.moat.ema_60d is not None
        assert output.financial_reconstruction is None
        assert output.trajectory is None
        assert 0.0 <= output.blended_qualitative_score <= 1.0
        assert output.recommendation in ("strong_buy", "buy", "hold", "reduce", "avoid")

    def test_run_full(self, pipeline):
        output = pipeline.run(
            ticker="NVDA",
            moat_signals={
                "product_breadth": 0.9,
                "developer_momentum": 0.85,
                "employee_sentiment": 0.75,
            },
            financial_inputs={
                "rd_expense": 8_000_000_000,
                "revenue": 60_000_000_000,
                "gross_profit": 40_000_000_000,
                "sbc_expense": 1_200_000_000,
                "shares_outstanding": 2_500_000_000,
                "share_price": 800.0,
                "sector": "semiconductor",
                "operating_margin": 0.35,
            },
            z_score=0.8,
        )
        assert output.financial_reconstruction is not None
        assert output.trajectory is not None
        assert output.trajectory.growth_stage == "growth"

    def test_run_poor_signals(self, pipeline):
        output = pipeline.run(
            ticker="INTC",
            moat_signals={"product_breadth": 0.2, "developer_momentum": 0.15},
            financial_inputs={
                "rd_expense": 5_000_000_000,
                "revenue": 15_000_000_000,
                "gross_profit": 5_000_000_000,
                "sbc_expense": 800_000_000,
                "shares_outstanding": 4_000_000_000,
                "share_price": 30.0,
                "sector": "semiconductor",
                "operating_margin": 0.05,
            },
            z_score=-2.0,
        )
        assert output.blended_qualitative_score <= 0.5
        assert output.recommendation in ("hold", "reduce", "avoid")

    def test_run_strong_signals(self, pipeline):
        output = pipeline.run(
            ticker="NVDA",
            moat_signals={
                "product_breadth": 0.95,
                "developer_momentum": 0.95,
                "employee_sentiment": 0.90,
                "revenue_concentration": 0.30,
                "network_effect_proxy": 0.85,
                "regulatory_barrier": 0.70,
            },
            financial_inputs={
                "rd_expense": 8_000_000_000,
                "revenue": 60_000_000_000,
                "gross_profit": 42_000_000_000,
                "sbc_expense": 500_000_000,
                "shares_outstanding": 2_500_000_000,
                "share_price": 800.0,
                "sector": "semiconductor",
                "operating_margin": 0.48,
            },
            z_score=0.5,
        )
        assert output.blended_qualitative_score >= 0.55

    def test_create_factory(self):
        pipeline = create_alternative_strategy_pipeline()
        assert isinstance(pipeline, AlternativeStrategyPipeline)


class TestMoatCompositeIntegration:
    def test_add_signal_custom_weight(self):
        mc = MoatComposite(ticker="NVDA")
        mc.add_signal("product_breadth", 0.8, weight=0.5)
        assert mc.weights["product_breadth"] == 0.5

    def test_compute_raw_composite_custom_weights(self):
        mc = MoatComposite(ticker="NVDA")
        mc.add_signal("product_breadth", 1.0, weight=1.0)
        mc.add_signal("developer_momentum", 0.0, weight=1.0)
        composite = mc.compute_raw_composite()
        assert composite == 0.5

    def test_n_signals_tracked(self):
        mc = MoatComposite(ticker="NVDA")
        mc.add_signal("product_breadth", 0.8)
        mc.add_signal("developer_momentum", 0.7)
        mc.compute_raw_composite()
        assert mc.n_signals == 2
