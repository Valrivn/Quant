import pytest
from unittest.mock import patch
from psychological.scrapers.validation_gate import CrossValidationGate, create_validation_gate, ValidationGateResult


class TestCrossValidationGate:
    @pytest.fixture
    def mock_config(self):
        return {
            "validation_gate": {
                "kappa": 5.0,
                "divergence_threshold": 0.20,
                "confidence_floor": 0.40,
                "max_penalty": 0.1
            }
        }

    @pytest.fixture
    def gate(self, mock_config):
        with patch('psychological.scrapers.validation_gate.load_hybrid_config') as mock_load:
            mock_load.return_value = {"validation_gate": mock_config["validation_gate"]}
            gate = CrossValidationGate(config_dict=mock_config["validation_gate"])
            yield gate

    def test_init(self, gate):
        assert gate is not None
        assert gate.kappa == 5.0
        assert gate.divergence_threshold == 0.20
        assert gate.confidence_floor == 0.40
        assert gate.max_penalty == 0.1

    def test_normalize_glassdoor(self, gate):
        assert gate.normalize_glassdoor(4.5) == 0.9
        assert gate.normalize_glassdoor(5.0) == 1.0
        assert gate.normalize_glassdoor(2.5) == 0.5
        assert gate.normalize_glassdoor(0.0) == 0.0
        assert gate.normalize_glassdoor(-1.0) == 0.0
        assert gate.normalize_glassdoor(None) == 0.0

    def test_normalize_comparably(self, gate):
        assert gate.normalize_comparably(80) == 0.8
        assert gate.normalize_comparably(100) == 1.0
        assert gate.normalize_comparably(50) == 0.5
        assert gate.normalize_comparably(0) == 0.0
        assert gate.normalize_comparably(-10) == 0.0
        assert gate.normalize_comparably(None) == 0.0

    def test_compute_divergence(self, gate):
        assert gate.compute_divergence(0.5, 0.5) == 0.0
        assert gate.compute_divergence(1.0, 0.0) == 1.0
        assert gate.compute_divergence(0.8, 0.2) == pytest.approx(0.75)
        # denominator = max(0.1, 0.1) = 0.1, divergence = 0.8/0.1 = 8.0
        assert gate.compute_divergence(0.1, 0.9) == 8.0

    def test_compute_penalty_below_threshold(self, gate):
        assert gate.compute_penalty(0.1) == 1.0
        assert gate.compute_penalty(0.2) == 1.0

    def test_compute_penalty_above_threshold(self, gate):
        penalty = gate.compute_penalty(0.5)
        assert 0.1 <= penalty < 1.0
        
        penalty = gate.compute_penalty(1.0)
        assert penalty == 0.1

    def test_check_hard_gate(self, gate):
        assert gate.check_hard_gate(1.0) is False
        assert gate.check_hard_gate(0.5) is False
        assert gate.check_hard_gate(0.3) is True

    def test_evaluate_both_present(self, gate):
        result = gate.evaluate(4.0, 80)
        
        assert result.normalized_glassdoor == 0.8
        assert result.normalized_comparably == 0.8
        assert result.divergence == 0.0
        assert result.penalty_multiplier == 1.0
        assert result.override_triggered is False

    def test_evaluate_divergence(self, gate):
        result = gate.evaluate(4.5, 30)
        
        assert result.normalized_glassdoor == 0.9
        assert result.normalized_comparably == 0.3
        assert result.divergence > 0.5
        assert result.penalty_multiplier < 1.0

    def test_evaluate_missing_glassdoor(self, gate):
        result = gate.evaluate(None, 80)
        
        assert result.normalized_glassdoor is None
        assert result.normalized_comparably == 0.8
        assert result.divergence is None
        assert result.penalty_multiplier == 1.0
        assert result.override_triggered is False

    def test_evaluate_missing_comparably(self, gate):
        result = gate.evaluate(4.0, None)
        
        assert result.normalized_glassdoor == 0.8
        assert result.normalized_comparably is None
        assert result.divergence is None
        assert result.penalty_multiplier == 1.0
        assert result.override_triggered is False

    def test_evaluate_both_missing(self, gate):
        result = gate.evaluate(None, None)
        
        assert result.normalized_glassdoor is None
        assert result.normalized_comparably is None
        assert result.divergence is None
        assert result.penalty_multiplier == 1.0
        assert result.override_triggered is False

    def test_evaluate_override_triggered(self, gate):
        result = gate.evaluate(1.0, 95)
        
        assert result.override_triggered is True
        assert result.penalty_multiplier < 0.4

    def test_apply_to_confidence(self, gate):
        assert gate.apply_to_confidence(0.8, 1.0) == 0.8
        assert gate.apply_to_confidence(0.8, 0.5) == 0.4
        assert gate.apply_to_confidence(1.0, 0.1) == 0.1
        assert gate.apply_to_confidence(0.5, 0.0) == 0.0
        
        # Clamping
        assert gate.apply_to_confidence(1.5, 1.0) == 1.0
        assert gate.apply_to_confidence(-0.5, 1.0) == 0.0


class TestCreateValidationGate:
    def test_create_validation_gate(self):
        with patch('psychological.scrapers.validation_gate.load_hybrid_config') as mock_load:
            mock_load.return_value = {"validation_gate": {}}
            gate = create_validation_gate()
            assert isinstance(gate, CrossValidationGate)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])