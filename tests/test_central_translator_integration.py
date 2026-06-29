import pytest
import math
from unittest.mock import patch, AsyncMock, MagicMock
from psychological.scrapers.moat_discovery import (
    MoatNode, MoatTree, MoatWeightingLayer, MoatScoringEngine,
    create_moat_weighting_layer, create_moat_scoring_engine
)
from psychological.scrapers.cross_validation import CrossValidationEngine, CrossValidationResult
from psychological.orchestrator import (
    PsychologicalOrchestrator, create_psychological_orchestrator
)


def _make_node(name: str, ticker: str = "NVDA", stars: int = 500, node_type: str = "platform",
               source: str = "wikipedia", description: str = None) -> MoatNode:
    return MoatNode(
        name=name, source=source, ticker=ticker, node_type=node_type,
        stars=stars, description=description
    )


class TestMoatWeightingLayer:
    @pytest.fixture
    def mock_config(self):
        return {
            "star_decay_factor": 0.15,
            "max_nodes_per_ticker": 8,
            "sec_revenue_segments": {
                "NVDA": ["AI", "DataCenter", "Gaming", "Automotive"]
            },
            "moat_overrides": {
                "NVDA:NVIDIA Apex": 0.95
            }
        }

    @pytest.fixture
    def layer(self, mock_config):
        with patch("psychological.scrapers.moat_discovery.load_hybrid_config") as mock_load:
            mock_load.return_value = {"moat_discovery": mock_config}
            return MoatWeightingLayer(config_dict=mock_config)

    def test_compute_star_velocity(self, layer):
        node = _make_node("Test", stars=5000)
        v = layer.compute_star_velocity(node)
        assert 0.0 <= v <= 1.0
        assert v == 0.5

    def test_compute_star_velocity_none(self, layer):
        node = _make_node("Test", stars=None)
        assert layer.compute_star_velocity(node) == 0.0

    def test_apply_star_velocity_decay(self, layer):
        node = _make_node("Test", stars=10000)
        decayed = layer.apply_star_velocity_decay(node)
        assert 0.0 <= decayed <= 1.0
        assert decayed < 1.0  # decay factor < 1

    def test_cross_reference_revenue_segments(self, layer):
        node = _make_node("Core", description="AI training platform for DataCenter workloads")
        score = layer.cross_reference_revenue_segments(node, "NVDA")
        assert score > 0
        assert score <= 1.0

    def test_cross_reference_no_match(self, layer):
        node = _make_node("Core", description="Unknown unrelated product")
        score = layer.cross_reference_revenue_segments(node, "NVDA")
        assert score == 0.0

    def test_node_override(self, layer):
        node = _make_node("NVIDIA Apex")
        assert layer.node_override(node) == 0.95

    def test_node_override_missing(self, layer):
        node = _make_node("Unknown")
        assert layer.node_override(node) is None

    def test_compute_node_weight_with_override(self, layer):
        node = _make_node("NVIDIA Apex")
        weight = layer.compute_node_weight(node, "NVDA")
        assert weight == 0.95

    def test_compute_node_weight_no_override(self, layer):
        node = _make_node("Core", stars=5000, description="AI platform for DataCenter")
        weight = layer.compute_node_weight(node, "NVDA")
        assert 0.0 <= weight <= 1.0

    def test_rank_nodes_respects_max(self, layer):
        nodes = [_make_node(f"Node{i}", stars=1000 - i * 50) for i in range(12)]
        tree = MoatTree(ticker="NVDA", company_name="NVIDIA", nodes=nodes)
        ranked = layer.rank_nodes(tree)
        assert len(ranked.nodes) <= 8


class TestMoatScoringEngine:
    @pytest.fixture
    def engine(self):
        return MoatScoringEngine()

    def test_budget_initial(self, engine):
        assert engine.budget_remaining("NVDA") == 20

    def test_consume_budget(self, engine):
        assert engine._consume_budget("NVDA", 5)
        assert engine.budget_remaining("NVDA") == 15

    def test_consume_budget_exceed(self, engine):
        assert engine._consume_budget("NVDA", 20)
        assert not engine._consume_budget("NVDA", 1)
        assert engine.budget_remaining("NVDA") == 0

    @pytest.mark.asyncio
    async def test_score_node_no_budget(self, engine):
        engine._consume_budget("NVDA", 20)
        node = _make_node("Core")
        result = await engine.score_node(node, "NVDA")
        assert result is node

    @pytest.mark.asyncio
    async def test_score_node_no_scrapers(self, engine):
        node = _make_node("Core")
        result = await engine.score_node(node, "NVDA")
        assert result.stars == 500  # unchanged

    @pytest.mark.asyncio
    async def test_score_node_g2_exhausted(self, engine):
        engine._consume_budget("NVDA", 20)
        node = _make_node("Core")
        rating = await engine.score_node_g2(node, "NVDA")
        assert rating is None

    @pytest.mark.asyncio
    async def test_score_node_reddit_exhausted(self, engine):
        engine._consume_budget("NVDA", 20)
        node = _make_node("Core")
        mentions = await engine.score_node_reddit(node, "NVDA")
        assert mentions is None

    @pytest.mark.asyncio
    async def test_score_node_capterra_exhausted(self, engine):
        engine._consume_budget("NVDA", 20)
        node = _make_node("Core")
        rating = await engine.score_node_capterra(node, "NVDA")
        assert rating is None

    @pytest.mark.asyncio
    async def test_score_node_app_store_exhausted(self, engine):
        engine._consume_budget("NVDA", 20)
        node = _make_node("Core")
        rating = await engine.score_node_app_store(node, "NVDA")
        assert rating is None

    @pytest.mark.asyncio
    async def test_score_tree_sorts_by_stars(self, engine):
        nodes = [
            _make_node("Low", stars=300),
            _make_node("High", stars=900),
        ]
        tree = MoatTree(ticker="NVDA", company_name="NVIDIA", nodes=nodes)
        scored = await engine.score_tree(tree)
        assert scored.nodes[0].name == "High"

    @pytest.mark.asyncio
    async def test_budget_reset_per_tree(self, engine):
        tree = MoatTree(ticker="NVDA", company_name="NVIDIA", nodes=[_make_node("A")])
        await engine.score_tree(tree)
        assert engine.budget_remaining("NVDA") == 16


class TestCrossValidationMoat:
    @pytest.fixture
    def engine(self):
        with patch("psychological.scrapers.cross_validation.load_hybrid_config") as mock_load:
            mock_load.return_value = {"cross_validation": {"divergence_threshold": 0.3, "kappa": 5.0}}
            return CrossValidationEngine(config_dict={"divergence_threshold": 0.3, "kappa": 5.0})

    def test_validate_moat_convergence_aligned(self, engine):
        tree = MoatTree(ticker="NVDA", company_name="NVIDIA", nodes=[
            _make_node("CUDA", stars=900),
            _make_node("TensorRT", stars=700),
        ])
        result = engine.validate_moat_convergence(moat_tree=tree, github_metrics=0.5, product_sentiment=0.6)
        assert result["convergence_score"] >= 0.0
        assert result["details"]["top_moat_nodes"] == ["CUDA", "TensorRT"]

    def test_validate_moat_convergence_divergent(self, engine):
        result = engine.validate_moat_convergence(moat_tree=None, github_metrics=-2.0, product_sentiment=1.0)
        assert 0.0 <= result["penalty_multiplier"] <= 1.0

    def test_validate_moat_convergence_empty_tree(self, engine):
        tree = MoatTree(ticker="NVDA", company_name="NVIDIA", nodes=[])
        result = engine.validate_moat_convergence(moat_tree=tree, github_metrics=0.0, product_sentiment=0.0)
        assert result["details"]["top_moat_nodes"] == []


class TestOrchestratorQualitativePipeline:
    @pytest.fixture
    def mock_config(self):
        return {
            "fusion_weights": {"psychological_regime": 0.6, "fintech_confirmation": 0.25, "quantitative_value": 0.15},
            "cross_validation": {"divergence_threshold": 0.3, "kappa": 5.0},
        }

    @pytest.mark.asyncio
    async def test_run_qualitative_pipeline_structure(self, mock_config):
        with patch("psychological.orchestrator.load_hybrid_config") as mock_hybrid:
            mock_hybrid.return_value = {"psychological": mock_config}

            with patch("psychological.orchestrator.create_nlp_engine") as mock_nlp, \
                 patch("psychological.orchestrator.create_velocity_tracker") as mock_vt, \
                 patch("psychological.orchestrator.create_state_machine") as mock_sm, \
                 patch("psychological.orchestrator.create_behavioral_feature_store") as mock_bfs, \
                 patch("psychological.orchestrator.create_cross_validation_engine") as mock_cv, \
                 patch("psychological.orchestrator.create_signal_matrix") as mock_sig:

                orch = PsychologicalOrchestrator(config_dict=mock_config)
                orch.initialize_scrapers = AsyncMock()
                orch.run_primary_pipeline = AsyncMock(return_value=MagicMock(
                    source="reddit_custom", tickers_processed=["NVDA"],
                    vectors_committed=0, regimes_committed=0, errors=[]
                ))
                orch.run_secondary_pipeline = AsyncMock(return_value={
                    "NVDA": {"employee_sentiment_proxy": None, "dev_fork_acceleration": 0.3, "product_sentiment_proxy": 0.4}
                })
                orch.cross_validation_engine = MagicMock()
                orch.cross_validation_engine.validate_moat_convergence.return_value = {
                    "convergence_score": 0.85, "penalty_multiplier": 1.0, "details": {"aligned": True}
                }
                orch.product_intel_engine = None
                orch.reddit_scraper = None

                result = await orch.run_qualitative_pipeline("NVDA")
                assert result["ticker"] == "NVDA"
                assert "branch1_employer_sentiment" in result
                assert "branch2_moat_discovery" in result
                assert "moat_convergence" in result
                assert "primary_pipeline" in result
