import pytest
from unittest.mock import patch
from psychological.scrapers.cross_validation import CrossValidationEngine, create_cross_validation_engine, CrossValidationResult


class TestCrossValidationEngine:
    @pytest.fixture
    def mock_config(self):
        return {
            "cross_validation": {
                "divergence_threshold": 0.3,
                "kappa": 5.0
            }
        }

    @pytest.fixture
    def engine(self, mock_config):
        with patch('psychological.scrapers.cross_validation.load_hybrid_config') as mock_load:
            mock_load.return_value = {"cross_validation": mock_config["cross_validation"]}
            engine = CrossValidationEngine(config_dict=mock_config["cross_validation"])
            yield engine

    def test_init(self, engine):
        assert engine is not None
        assert engine.divergence_threshold == 0.3
        assert engine.kappa == 5.0

    def test_normalize_score(self, engine):
        assert engine._normalize_score(2.5, 0, 5) == 0.5
        assert engine._normalize_score(5.0, 0, 5) == 1.0
        assert engine._normalize_score(0.0, 0, 5) == 0.0
        assert engine._normalize_score(6.0, 0, 5) == 1.0
        assert engine._normalize_score(-1.0, 0, 5) == 0.0
        assert engine._normalize_score(None, 0, 5) == 0.5

    def test_compute_divergence(self, engine):
        assert engine._compute_divergence(0.5, 0.5) == 0.0
        assert engine._compute_divergence(1.0, 0.0) == 1.0
        assert engine._compute_divergence(0.8, 0.2) == pytest.approx(0.75)

    def test_exponential_penalty(self, engine):
        assert engine._exponential_penalty(0.2) == 1.0
        penalty = engine._exponential_penalty(0.5)
        assert 0.1 <= penalty < 1.0
        
        penalty = engine._exponential_penalty(1.0)
        assert penalty == 0.1

    def test_validate_layer1_glassdoor_comparably(self, engine):
        result = engine.validate_layer1_glassdoor_comparably(4.0, 80)
        
        assert result.layer_name == "Layer1_Glassdoor_Comparably"
        assert 0.0 <= result.convergence_score <= 1.0
        assert 0.1 <= result.penalty_multiplier <= 1.0
        assert "glassdoor_normalized" in result.details
        assert "comparably_normalized" in result.details
        assert "divergence" in result.details

    def test_validate_layer1_no_data(self, engine):
        result = engine.validate_layer1_glassdoor_comparably(None, None)
        
        # Both normalized to 0.5, so divergence is 0, convergence is 1.0
        assert result.convergence_score == 1.0
        assert result.penalty_multiplier == 1.0

    def test_validate_layer2_jobspy_github(self, engine):
        result = engine.validate_layer2_jobspy_github(1.5, 0.8)
        
        assert result.layer_name == "Layer2_JobSpy_GitHub"
        assert 0.0 <= result.convergence_score <= 1.0
        assert 0.1 <= result.penalty_multiplier <= 1.0

    def test_validate_layer3_product_reddit(self, engine):
        result = engine.validate_layer3_product_reddit(0.5, 2.0)
        
        assert result.layer_name == "Layer3_Product_Reddit"
        assert 0.0 <= result.convergence_score <= 1.0
        assert 0.1 <= result.penalty_multiplier <= 1.0

    def test_validate_layer4_dcf_regime(self, engine):
        result = engine.validate_layer4_dcf_regime(0.7, 0.8)
        
        assert result.layer_name == "Layer4_DCF_Regime"
        assert 0.0 <= result.convergence_score <= 1.0
        assert 0.1 <= result.penalty_multiplier <= 1.0

    def test_validate_apewisdom_reddit_github(self, engine):
        result = engine.validate_apewisdom_reddit_github(0.6, 2.0, 0.8)
        
        assert result.layer_name == "ApeWisdom_Reddit_GitHub_Convergence"
        assert 0.0 <= result.convergence_score <= 1.0
        assert 0.1 <= result.penalty_multiplier <= 1.0
        assert "triangular_convergence" in result.details

    def test_run_all_validations(self, engine):
        results = engine.run_all_validations(
            glassdoor_raw=4.0,
            comparably_badge=85,
            jobspy_zscore=1.5,
            github_velocity=0.8,
            product_sentiment=0.2,
            reddit_ratio=2.5,
            dcf_signal=0.7,
            regime_confidence=0.8,
            apewisdom_sentiment=0.6
        )
        
        assert "layer1" in results
        assert "layer2" in results
        assert "layer3" in results
        assert "layer4" in results
        assert "apewisdom_reddit_github" in results
        assert len(results) == 5

    def test_compute_aggregate_penalty(self, engine):
        results = {
            "layer1": CrossValidationResult("L1", 0.8, 0.9, {}),
            "layer2": CrossValidationResult("L2", 0.7, 0.8, {}),
            "layer3": CrossValidationResult("L3", 0.6, 0.7, {})
        }
        
        penalty = engine.compute_aggregate_penalty(results)
        assert 0.1 <= penalty <= 1.0

    def test_compute_aggregate_penalty_empty(self, engine):
        penalty = engine.compute_aggregate_penalty({})
        assert penalty == 1.0

    def test_evaluate_all_layers(self, engine):
        regime_data = {
            "employee_sentiment_proxy": 4.0,
            "comparably_badge_score": 85,
            "jobspy_zscore_1y": 1.5,
            "product_sentiment_proxy": 0.2,
            "confidence_score": 0.8
        }
        
        result = engine.evaluate_all_layers(
            ticker="AAPL",
            regime_data=regime_data,
            fintech_sentiment=0.6,
            reddit_bull_bear_ratio=2.5,
            dev_velocity=0.8,
            quant_value=0.7
        )
        
        assert "layers" in result
        assert "combined_penalty" in result
        assert "final_override" in result
        assert "validation_passed" in result
        assert len(result["layers"]) == 5
        assert 0.1 <= result["combined_penalty"] <= 1.0

    def test_evaluate_all_layers_override(self, engine):
        regime_data = {
            "employee_sentiment_proxy": 4.0,
            "comparably_badge_score": 10,
            "jobspy_zscore_1y": -3.0,
            "product_sentiment_proxy": -0.5,
            "confidence_score": 0.1
        }
        
        result = engine.evaluate_all_layers(
            ticker="AAPL",
            regime_data=regime_data,
            fintech_sentiment=0.1,
            reddit_bull_bear_ratio=0.5,
            dev_velocity=-1.0,
            quant_value=0.1
        )
        
        assert result["final_override"] is True
        assert result["validation_passed"] is False


class TestCreateCrossValidationEngine:
    def test_create_cross_validation_engine(self):
        with patch('psychological.scrapers.cross_validation.load_hybrid_config') as mock_load:
            mock_load.return_value = {"cross_validation": {}}
            engine = create_cross_validation_engine()
            assert isinstance(engine, CrossValidationEngine)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])