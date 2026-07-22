import pytest
import numpy as np
from psychological.qualitative_scoring import (
    EMAFilter,
    SubSectorConfig,
    BranchComposite,
    CultureComposite,
    HypeComposite,
    DoubleStandardizer,
    PublicationLagMatrix,
    LaneAlphaPipeline,
    LaneAlphaResult,
    tanh_clamp,
    tanh_clamp_unit,
)


class TestEMAFilter:
    def test_alpha_computation(self):
        f = EMAFilter(halflife=21, min_observations=5)
        expected = 1 - np.exp(-np.log(2) / 21)
        assert f.alpha() == pytest.approx(expected, rel=1e-6)

    def test_alpha_zero_halflife(self):
        f = EMAFilter(halflife=0, min_observations=5)
        assert f.alpha() == 1.0

    def test_cold_start_expanding_mean(self):
        f = EMAFilter(halflife=21, min_observations=5)
        vals = [10.0, 20.0, 30.0]
        emas = []
        for v in vals:
            emas.append(f.update("TEST", v))
        assert emas[0] == 10.0
        assert emas[1] == 15.0
        assert emas[2] == 20.0

    def test_warm_ema_after_min_observations(self):
        f = EMAFilter(halflife=21, min_observations=3)
        for v in [10.0, 20.0, 30.0]:
            f.update("TEST", v)
        a = f.alpha()
        cold_mean = (10.0 + 20.0) / 2.0
        third_ema = a * 30.0 + (1 - a) * cold_mean
        fourth_ema = a * 40.0 + (1 - a) * third_ema
        result = f.update("TEST", 40.0)
        assert result == pytest.approx(fourth_ema, rel=1e-6)

    def test_get_returns_none_for_unknown_key(self):
        f = EMAFilter(halflife=21)
        assert f.get("UNKNOWN") is None

    def test_get_returns_ema(self):
        f = EMAFilter(halflife=21, min_observations=1)
        f.update("TEST", 42.0)
        assert f.get("TEST") == 42.0

    def test_reset_single_key(self):
        f = EMAFilter(halflife=21, min_observations=1)
        f.update("A", 1.0)
        f.update("B", 2.0)
        f.reset("A")
        assert f.get("A") is None
        assert f.get("B") == 2.0

    def test_reset_all(self):
        f = EMAFilter(halflife=21, min_observations=1)
        f.update("A", 1.0)
        f.update("B", 2.0)
        f.reset()
        assert f.get("A") is None
        assert f.get("B") is None


class TestSubSectorConfig:
    def test_default_tickers(self):
        cfg = SubSectorConfig()
        assert "NVDA" in cfg.semiconductors
        assert "MSFT" in cfg.platform_software
        assert "AAPL" in cfg.hardware_oem

    def test_from_config_loads_yaml(self):
        cfg = SubSectorConfig.from_config()
        assert "NVDA" in cfg.semiconductors
        assert len(cfg.semiconductors) >= 3

    def test_get_subsector_for_ticker(self):
        cfg = SubSectorConfig()
        assert cfg.get_subsector_for_ticker("NVDA") == "semiconductors"
        assert cfg.get_subsector_for_ticker("MSFT") == "platform_software"
        assert cfg.get_subsector_for_ticker("AAPL") == "hardware_oem"
        assert cfg.get_subsector_for_ticker("UNKNOWN") is None

    def test_as_dict(self):
        cfg = SubSectorConfig()
        d = cfg.as_dict()
        assert "semiconductors" in d
        assert "platform_software" in d
        assert "hardware_oem" in d

    def test_get_peers_excludes_self(self):
        cfg = SubSectorConfig(
            semiconductors=["NVDA", "AMD", "INTC"],
            platform_software=[],
            hardware_oem=[],
        )
        peers = cfg.get_peers("NVDA")
        assert "NVDA" not in peers
        assert "AMD" in peers
        assert "INTC" in peers

    def test_get_peers_unknown_sector(self):
        cfg = SubSectorConfig()
        assert cfg.get_peers("UNKNOWN") == []


class TestBranchComposite:
    def test_compute_returns_none_for_empty_signals(self):
        comp = BranchComposite("test", EMAFilter(halflife=21, min_observations=1))
        assert comp.compute("NVDA", {}) is None

    def test_compute_weighted_average(self):
        comp = BranchComposite(
            "test",
            EMAFilter(halflife=21, min_observations=1),
            weights={"a": 2.0, "b": 1.0},
        )
        result = comp.compute("NVDA", {"a": 1.0, "b": 0.0})
        raw_avg = (1.0 * 2.0 + 0.0 * 1.0) / (2.0 + 1.0)
        import math
        expected = (math.tanh(raw_avg / 2.0) + 1.0) / 2.0
        assert result == pytest.approx(expected)

    def test_compute_unweighted(self):
        comp = BranchComposite("test", EMAFilter(halflife=21, min_observations=1))
        result = comp.compute("NVDA", {"a": 0.5, "b": 0.7})
        import math
        raw = 0.6
        expected = (math.tanh(raw / 2.0) + 1.0) / 2.0
        assert result == pytest.approx(expected)


class TestCultureComposite:
    def test_default_halflife_and_weights(self):
        comp = CultureComposite()
        assert comp.ema.halflife == 90
        assert comp.ema.min_observations == 20
        assert comp.weights["employee_sentiment"] == 0.35

    def test_compute_with_mock_signals(self):
        comp = CultureComposite(halflife=90, min_observations=1)
        signals = {
            "employee_sentiment": 0.8,
            "hiring_velocity": 0.6,
            "dev_velocity": 0.7,
            "product_sentiment": 0.5,
        }
        score = comp.compute("NVDA", signals)
        assert score is not None
        assert 0.0 <= score <= 1.0

    def test_score_after_cold_start(self):
        comp = CultureComposite(halflife=90, min_observations=3)
        for i in range(3):
            comp.compute("NVDA", {
                "employee_sentiment": 0.5,
                "hiring_velocity": 0.5,
                "dev_velocity": 0.5,
                "product_sentiment": 0.5,
            })
        score = comp.score("NVDA")
        assert score is not None
        import math
        bounded_raw = (math.tanh(0.5 / 2.0) + 1.0) / 2.0
        assert score == pytest.approx(bounded_raw)


class TestHypeComposite:
    def test_default_halflife_and_weights(self):
        comp = HypeComposite()
        assert comp.ema.halflife == 21
        assert comp.ema.min_observations == 5
        assert comp.weights["reddit_velocity"] == 0.30

    def test_compute_with_mock_signals(self):
        comp = HypeComposite(halflife=21, min_observations=1)
        signals = {
            "reddit_velocity": 0.9,
            "bull_bear_ratio": 0.7,
            "mention_velocity": 0.6,
            "social_sentiment": 0.8,
        }
        score = comp.compute("NVDA", signals)
        assert score is not None
        assert 0.0 <= score <= 1.0

    def test_smoothed_ema_tracks_trend(self):
        comp = HypeComposite(halflife=5, min_observations=1)
        scores = []
        for v in [0.1, 0.2, 0.3, 0.4, 0.5]:
            s = comp.compute("NVDA", {
                "reddit_velocity": v,
                "bull_bear_ratio": v,
                "mention_velocity": v,
                "social_sentiment": v,
            })
            scores.append(s)
        for i in range(1, len(scores)):
            assert scores[i] >= scores[i - 1]


class TestDoubleStandardizer:
    @pytest.fixture
    def subsector_config(self):
        return SubSectorConfig(
            semiconductors=["NVDA", "AMD", "INTC"],
            platform_software=[],
            hardware_oem=[],
        )

    def test_stage1_returns_none_with_insufficient_history(self, subsector_config):
        ds = DoubleStandardizer(subsector_config=subsector_config, min_history=5)
        result = ds.stage1("NVDA", 100.0)
        assert result is None

    def test_stage1_zscore_after_sufficient_history(self, subsector_config):
        ds = DoubleStandardizer(subsector_config=subsector_config, min_history=5)
        for v in [90.0, 91.0, 89.0, 92.0, 88.0]:
            ds.stage1("NVDA", v)
        result = ds.stage1("NVDA", 100.0)
        assert result is not None
        assert result > 0.0

    def test_stage1_constant_values_return_zero(self, subsector_config):
        ds = DoubleStandardizer(subsector_config=subsector_config, min_history=3)
        for _ in range(3):
            ds.stage1("NVDA", 50.0)
        result = ds.stage1("NVDA", 50.0)
        assert result == 0.0

    def test_stage2_returns_none_for_unknown_ticker(self, subsector_config):
        ds = DoubleStandardizer(subsector_config=subsector_config)
        result = ds.stage2("UNKNOWN", {})
        assert result is None

    def test_stage2_cross_sectional_zscore(self, subsector_config):
        ds = DoubleStandardizer(subsector_config=subsector_config, min_history=1)
        stage1_vals = {"NVDA": 1.0, "AMD": -0.5, "INTC": -0.5}
        result = ds.stage2("NVDA", stage1_vals)
        assert result is not None
        assert result > 0.0

    def test_stage2_isolated_ticker_returns_stage1(self, subsector_config):
        ds = DoubleStandardizer(subsector_config=subsector_config)
        import math
        expected = math.tanh(2.0 / 2.0)
        result = ds.stage2("NVDA", {"NVDA": 2.0})
        assert result == pytest.approx(expected)

    def test_standardize_returns_both_stages(self, subsector_config):
        ds = DoubleStandardizer(subsector_config=subsector_config, min_history=3)
        for v in [90.0, 91.0, 89.0]:
            ds.stage1("NVDA", v)
        s1, s2 = ds.standardize("NVDA", 100.0, {"AMD": 0.0, "INTC": 0.0})
        assert s1 is not None
        assert s2 is not None

    def test_standardize_returns_none_for_insufficient_history(self, subsector_config):
        ds = DoubleStandardizer(subsector_config=subsector_config, min_history=10)
        s1, s2 = ds.standardize("NVDA", 100.0, {})
        assert s1 is None
        assert s2 is None

    def test_reset_single_ticker(self, subsector_config):
        ds = DoubleStandardizer(subsector_config=subsector_config, min_history=1)
        ds.stage1("NVDA", 100.0)
        ds.stage1("AMD", 200.0)
        ds.reset("NVDA")
        ds.stage1("NVDA", 90.0)
        assert ds.stage1("AMD", 210.0) is not None

    def test_reset_all_tickers(self, subsector_config):
        ds = DoubleStandardizer(subsector_config=subsector_config, min_history=1)
        ds.stage1("NVDA", 100.0)
        ds.reset()
        assert ds.stage1("NVDA", 90.0) is not None


class TestMockDataMatrices:
    @pytest.fixture
    def mock_culture_matrix(self):
        tickers = ["NVDA", "AMD", "INTC", "MSFT"]
        return {
            t: {
                "employee_sentiment": np.random.uniform(0.3, 0.9),
                "hiring_velocity": np.random.uniform(0.2, 0.8),
                "dev_velocity": np.random.uniform(0.4, 1.0),
                "product_sentiment": np.random.uniform(0.3, 0.9),
            }
            for t in tickers
        }

    @pytest.fixture
    def mock_hype_matrix(self):
        tickers = ["NVDA", "AMD", "INTC", "MSFT"]
        return {
            t: {
                "reddit_velocity": np.random.uniform(0.0, 1.0),
                "bull_bear_ratio": np.random.uniform(-1.0, 2.0),
                "mention_velocity": np.random.uniform(0.0, 1.0),
                "social_sentiment": np.random.uniform(0.0, 1.0),
            }
            for t in tickers
        }

    def test_culture_composite_over_mock_matrix(self, mock_culture_matrix):
        comp = CultureComposite(halflife=90, min_observations=1)
        for ticker, signals in mock_culture_matrix.items():
            score = comp.compute(ticker, signals)
            assert score is not None
            assert 0.0 <= score <= 1.0

    def test_hype_composite_over_mock_matrix(self, mock_hype_matrix):
        comp = HypeComposite(halflife=21, min_observations=1)
        for ticker, signals in mock_hype_matrix.items():
            score = comp.compute(ticker, signals)
            assert score is not None

    def test_double_standardizer_mock_matrix(self):
        cfg = SubSectorConfig(
            semiconductors=["NVDA", "AMD", "INTC"],
            platform_software=["MSFT"],
            hardware_oem=[],
        )
        ds = DoubleStandardizer(subsector_config=cfg, min_history=3)
        for _ in range(3):
            for t in ["NVDA", "AMD", "INTC"]:
                ds.stage1(t, np.random.uniform(80, 120))
        peervals = {"NVDA": ds.stage1("NVDA", 110.0),
                    "AMD": ds.stage1("AMD", 90.0),
                    "INTC": ds.stage1("INTC", 100.0)}
        peervals = {k: v for k, v in peervals.items() if v is not None}
        if len(peervals) >= 3:
            s1, s2 = ds.standardize("NVDA", 105.0, peervals)
            assert s1 is not None
            assert s2 is not None

    def test_full_pipeline_integration(self):
        cfg = SubSectorConfig(
            semiconductors=["NVDA", "AMD", "INTC"],
            platform_software=[],
            hardware_oem=[],
        )
        culture = CultureComposite(halflife=90, min_observations=1)
        hype = HypeComposite(halflife=21, min_observations=1)
        ds = DoubleStandardizer(subsector_config=cfg, min_history=2)

        for step in range(3):
            for ticker in ["NVDA", "AMD", "INTC"]:
                c_signals = {k: np.random.uniform(0.3, 0.9)
                             for k in ["employee_sentiment", "hiring_velocity",
                                       "dev_velocity", "product_sentiment"]}
                h_signals = {k: np.random.uniform(0.0, 1.0)
                             for k in ["reddit_velocity", "bull_bear_ratio",
                                       "mention_velocity", "social_sentiment"]}
                c_score = culture.compute(ticker, c_signals)
                h_score = hype.compute(ticker, h_signals)
                ds.stage1(ticker, (c_score or 0.5) + (h_score or 0.5))

        peervals = {}
        for t in ["NVDA", "AMD", "INTC"]:
            v = ds.stage1(t, 1.0)
            if v is not None:
                peervals[t] = v

        if len(peervals) >= 3:
            s1, s2 = ds.standardize("NVDA", 1.5, peervals)
            assert s1 is not None
            assert s2 is not None


class TestTanhClamp:
    def test_tanh_clamp_bounds(self):
        for z in [-1e6, -100, -10, -1, 0, 1, 10, 100, 1e6]:
            clamped = tanh_clamp(z)
            assert -1.0 <= clamped <= 1.0
            assert clamped == tanh_clamp(z)  # deterministic
        for z in [-1e6, -100, -10, -1, 0, 1, 10, 100, 1e6]:
            clamped = tanh_clamp_unit(z)
            assert 0.0 <= clamped <= 1.0

    def test_tanh_clamp_zero(self):
        from psychological.qualitative_scoring import tanh_clamp, tanh_clamp_unit
        assert tanh_clamp(0.0) == 0.0
        assert tanh_clamp_unit(0.0) == 0.5

    def test_tanh_clamp_monotonic(self):
        from psychological.qualitative_scoring import tanh_clamp
        inputs = [-5.0, -2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0, 5.0]
        outputs = [tanh_clamp(z) for z in inputs]
        for i in range(1, len(outputs)):
            assert outputs[i] > outputs[i - 1]


class TestPublicationLagMatrix:
    def test_default_lags(self):
        from psychological.qualitative_scoring import PublicationLagMatrix
        plm = PublicationLagMatrix()
        assert plm.lag_for("employee_sentiment") == 3
        assert plm.lag_for("reddit_velocity") == 0
        assert plm.lag_for("unknown_key") == 0

    def test_adjust_timestamp(self):
        from psychological.qualitative_scoring import PublicationLagMatrix
        from datetime import datetime, timezone, timedelta
        plm = PublicationLagMatrix()
        base = datetime(2026, 6, 1, tzinfo=timezone.utc)
        adj = plm.adjust_timestamp("employee_sentiment", observed_at=base)
        assert adj == base + timedelta(days=3)

    def test_zero_lag_returns_same(self):
        from psychological.qualitative_scoring import PublicationLagMatrix
        from datetime import datetime, timezone
        plm = PublicationLagMatrix()
        base = datetime(2026, 6, 1, tzinfo=timezone.utc)
        adj = plm.adjust_timestamp("reddit_velocity", observed_at=base)
        assert adj == base

    def test_from_config(self):
        from psychological.qualitative_scoring import PublicationLagMatrix
        plm = PublicationLagMatrix.from_config()
        assert plm.lag_for("employee_sentiment") == 3

    def test_to_dict(self):
        from psychological.qualitative_scoring import PublicationLagMatrix
        plm = PublicationLagMatrix()
        d = plm.to_dict()
        assert isinstance(d, dict)
        assert "employee_sentiment" in d


class TestLaneAlphaPipeline:
    @pytest.fixture
    def pip_config(self):
        from psychological.qualitative_scoring import (
            SubSectorConfig, CultureComposite, HypeComposite,
            DoubleStandardizer, LaneAlphaPipeline,
        )
        cfg = SubSectorConfig(
            semiconductors=["NVDA", "AMD", "INTC"],
            platform_software=["MSFT", "CRM", "ADBE"],
            cloud_internet=["GOOGL", "META", "AMZN"],
            consumer_electronics=["AAPL", "TSLA"],
            hardware_oem=["DELL", "HPQ"],
        )
        culture = CultureComposite(halflife=90, min_observations=1)
        hype = HypeComposite(halflife=21, min_observations=1)
        ds = DoubleStandardizer(subsector_config=cfg, min_history=2)
        pipe = LaneAlphaPipeline(
            culture=culture, hype=hype,
            standardizer=ds, subsector_cfg=cfg,
        )
        return pipe, cfg

    def test_ingest_culture(self, pip_config):
        pipe, _ = pip_config
        signals = {
            "employee_sentiment": 0.8,
            "hiring_velocity": 0.6,
            "dev_velocity": 0.7,
            "product_sentiment": 0.5,
        }
        score = pipe.ingest_culture("NVDA", signals)
        assert score is not None
        assert 0.0 < score < 1.0

    def test_ingest_hype(self, pip_config):
        pipe, _ = pip_config
        signals = {
            "reddit_velocity": 0.9,
            "bull_bear_ratio": 0.7,
            "mention_velocity": 0.6,
            "social_sentiment": 0.8,
        }
        score = pipe.ingest_hype("NVDA", signals)
        assert score is not None
        assert 0.0 < score < 1.0

    def test_blended_branch_score_initially_none(self, pip_config):
        pipe, _ = pip_config
        assert pipe.blended_branch_score("NVDA") is None

    def test_blended_branch_after_ingest(self, pip_config):
        pipe, _ = pip_config
        c_signals = {k: 0.5 for k in
                     ["employee_sentiment", "hiring_velocity",
                      "dev_velocity", "product_sentiment"]}
        h_signals = {k: 0.5 for k in
                     ["reddit_velocity", "bull_bear_ratio",
                      "mention_velocity", "social_sentiment"]}
        pipe.ingest_culture("NVDA", c_signals)
        pipe.ingest_hype("NVDA", h_signals)
        blended = pipe.blended_branch_score("NVDA")
        assert blended is not None
        assert 0.0 < blended < 1.0

    def test_run_returns_lane_alpha_result(self, pip_config):
        pipe, _ = pip_config
        c_signals = {k: 0.7 for k in
                     ["employee_sentiment", "hiring_velocity",
                      "dev_velocity", "product_sentiment"]}
        h_signals = {k: 0.3 for k in
                     ["reddit_velocity", "bull_bear_ratio",
                      "mention_velocity", "social_sentiment"]}
        c_signals2 = {k: 0.6 for k in
                      ["employee_sentiment", "hiring_velocity",
                       "dev_velocity", "product_sentiment"]}
        h_signals2 = {k: 0.4 for k in
                      ["reddit_velocity", "bull_bear_ratio",
                       "mention_velocity", "social_sentiment"]}
        pipe.ingest_culture("NVDA", c_signals)
        pipe.ingest_hype("NVDA", h_signals)
        pipe.ingest_culture("AMD", c_signals2)
        pipe.ingest_hype("AMD", h_signals2)

        result = pipe.run("NVDA", c_signals2, h_signals2)
        assert result.ticker == "NVDA"
        assert result.culture_score is not None
        assert result.hype_score is not None
        assert result.blended_branch is not None
        assert result.subsector == "semiconductors"
        assert result.n_culture_signals == 4
        assert result.n_hype_signals == 4

    def test_run_two_tickers_cross_sectional(self, pip_config):
        pipe, _ = pip_config
        signals = {k: 0.5 for k in
                   ["employee_sentiment", "hiring_velocity",
                    "dev_velocity", "product_sentiment"]}
        h_signals = {k: 0.5 for k in
                     ["reddit_velocity", "bull_bear_ratio",
                      "mention_velocity", "social_sentiment"]}
        pipe.ingest_culture("NVDA", signals)
        pipe.ingest_hype("NVDA", h_signals)
        pipe.ingest_culture("AMD", signals)
        pipe.ingest_hype("AMD", h_signals)
        pipe.ingest_culture("NVDA", signals)
        pipe.ingest_hype("NVDA", h_signals)
        pipe.ingest_culture("AMD", signals)
        pipe.ingest_hype("AMD", h_signals)

        r_ = pipe.run("NVDA", signals, h_signals)
        r1 = pipe.run("NVDA", signals, h_signals)
        pipe.run("AMD", signals, h_signals)
        r2 = pipe.run("AMD", signals, h_signals)
        assert r1.stage1_z is not None
        assert r1.stage2_z is not None
        assert r1.final_score is not None
        assert r2.stage1_z is not None

    def test_reset_clears_state(self, pip_config):
        pipe, _ = pip_config
        signals = {k: 0.5 for k in
                   ["employee_sentiment", "hiring_velocity",
                    "dev_velocity", "product_sentiment"]}
        h_signals = {k: 0.5 for k in
                     ["reddit_velocity", "bull_bear_ratio",
                      "mention_velocity", "social_sentiment"]}
        pipe.ingest_culture("NVDA", signals)
        pipe.ingest_hype("NVDA", h_signals)
        pipe.reset("NVDA")
        assert pipe.culture.score("NVDA") is None
        assert pipe.hype.score("NVDA") is None

    def test_full_multi_ticker_pipeline(self):
        from psychological.qualitative_scoring import (
            SubSectorConfig, LaneAlphaPipeline,
        )
        cfg = SubSectorConfig(
            semiconductors=["NVDA", "AMD", "INTC"],
            platform_software=["MSFT"],
            hardware_oem=["AAPL"],
        )
        pipe = LaneAlphaPipeline(subsector_cfg=cfg)
        tickers = ["NVDA", "AMD", "INTC", "MSFT", "AAPL"]
        c_base = {k: 0.6 for k in
                  ["employee_sentiment", "hiring_velocity",
                   "dev_velocity", "product_sentiment"]}
        h_base = {k: 0.4 for k in
                  ["reddit_velocity", "bull_bear_ratio",
                   "mention_velocity", "social_sentiment"]}
        for t in tickers:
            pipe.ingest_culture(t, c_base)
            pipe.ingest_hype(t, h_base)

        results = []
        for t in tickers:
            r = pipe.run(t, c_base, h_base)
            results.append(r)

        for r in results:
            assert r.ticker in tickers
            assert r.blended_branch is not None
            assert r.subsector is not None
            assert 0.0 <= r.final_score <= 1.0

    def test_final_score_falls_back_to_blended(self, pip_config):
        pipe, cfg = pip_config
        cfg_no_peers = type(cfg)(
            semiconductors=["NVDA"],
            platform_software=[],
            hardware_oem=[],
        )
        pipe.subsector_cfg = cfg_no_peers
        signals = {k: 0.5 for k in
                   ["employee_sentiment", "hiring_velocity",
                    "dev_velocity", "product_sentiment"]}
        h_signals = {k: 0.5 for k in
                     ["reddit_velocity", "bull_bear_ratio",
                      "mention_velocity", "social_sentiment"]}
        pipe.ingest_culture("NVDA", signals)
        pipe.ingest_hype("NVDA", h_signals)
        result = pipe.run("NVDA", signals, h_signals)
        assert result.final_score is not None
        assert result.stage2_z is None  # no peers -> no stage2
