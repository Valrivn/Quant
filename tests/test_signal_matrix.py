import pytest
from unittest.mock import patch
from psychological.signal_matrix import SignalMatrix, create_signal_matrix, SignalMatrixOutput, ExecutionDirective, PsychologicalRegime


class TestSignalMatrix:
    @pytest.fixture
    def mock_config(self):
        return {
            "signal_matrix": {
                "thresholds": {
                    "strong_buy_confidence": 0.75,
                    "weak_buy_confidence": 0.55,
                    "lockdown_confidence": 0.70,
                    "reduce_risk_confidence": 0.60
                },
                "dcf_weight": 0.15,
                "min_dcf_for_buy": 0.3
            }
        }

    @pytest.fixture
    def matrix(self, mock_config):
        with patch('psychological.signal_matrix.load_hybrid_config') as mock_load:
            mock_load.return_value = {"signal_matrix": mock_config["signal_matrix"]}
            matrix = SignalMatrix(config_dict=mock_config["signal_matrix"])
            yield matrix

    def test_init(self, matrix):
        assert matrix is not None
        assert matrix.thresholds["strong_buy_confidence"] == 0.75
        assert matrix.thresholds["weak_buy_confidence"] == 0.55
        assert matrix.min_dcf_for_buy == 0.3

    def test_compute_dcf_signal_no_data(self, matrix):
        signal = matrix.compute_dcf_signal(None)
        assert signal == 0.5
        
        signal = matrix.compute_dcf_signal({})
        assert signal == 0.5

    def test_compute_dcf_signal_missing_fields_missing(self, matrix):
        signal = matrix.compute_dcf_signal({"intrinsic_floor": 100})
        assert signal == 0.5

    def test_compute_dcf_signal_valid(self, matrix):
        dcf_data = {
            "intrinsic_floor": 150.0,
            "intrinsic_ceiling": 200.0,
            "current_price": 160.0,
            "margin_of_safety": 0.25
        }
        
        signal = matrix.compute_dcf_signal(dcf_data)
        
        # upside = (200-160)/(200-150) = 40/50 = 0.8
        # margin_adjusted = 0.8 * 1.25 = 1.0
        assert signal == 1.0

    def test_compute_dcf_signal_above_ceiling(self, matrix):
        dcf_data = {
            "intrinsic_floor": 150.0,
            "intrinsic_ceiling": 200.0,
            "current_price": 210.0,
            "margin_of_safety": 0.25
        }
        
        signal = matrix.compute_dcf_signal(dcf_data)
        assert signal == 0.0

    def test_compute_dcf_signal_below_floor(self, matrix):
        dcf_data = {
            "intrinsic_floor": 150.0,
            "intrinsic_ceiling": 200.0,
            "current_price": 100.0,
            "margin_of_safety": 0.25
        }
        
        signal = matrix.compute_dcf_signal(dcf_data)
        assert signal == 1.0

    def test_map_regime_to_directive_panic_strong_buy(self, matrix):
        directive = matrix.map_regime_to_directive(
            "PANIC_CAPITULATION", 0.85, 0.5, True
        )
        assert directive == ExecutionDirective.STRONG_CONTRARIAN_BUY

    def test_map_regime_to_directive_panic_weak_buy(self, matrix):
        directive = matrix.map_regime_to_directive(
            "PANIC_CAPITULATION", 0.60, 0.5, True
        )
        assert directive == ExecutionDirective.WEAK_CONTRARIAN_BUY

    def test_map_regime_to_directive_panic_hold(self, matrix):
        directive = matrix.map_regime_to_directive(
            "PANIC_CAPITULATION", 0.50, 0.5, True
        )
        assert directive == ExecutionDirective.HOLD

    def test_map_regime_to_directive_panic_no_dcf(self, matrix):
        directive = matrix.map_regime_to_directive(
            "PANIC_CAPITULATION", 0.85, 0.1, True
        )
        assert directive == ExecutionDirective.HOLD

    def test_map_regime_to_directive_euphoria_lockdown(self, matrix):
        directive = matrix.map_regime_to_directive(
            "CROWD_EUPHORIA", 0.80, 0.5, True
        )
        assert directive == ExecutionDirective.LOCKDOWN

    def test_map_regime_to_directive_euphoria_reduce_risk(self, matrix):
        directive = matrix.map_regime_to_directive(
            "CROWD_EUPHORIA", 0.50, 0.5, True
        )
        assert directive == ExecutionDirective.REDUCE_RISK

    def test_map_regime_to_directive_asymmetric_divergence(self, matrix):
        directive = matrix.map_regime_to_directive(
            "ASYMMETRIC_DIVERGENCE", 0.70, 0.5, True
        )
        assert directive == ExecutionDirective.ASYMMETRIC_OVERRIDE_BUY

    def test_map_regime_to_directive_glassdoor_github(self, matrix):
        directive = matrix.map_regime_to_directive(
            "ASYMMETRIC_GLASSDOOR_GITHUB_DIVERGENCE", 0.70, 0.5, True
        )
        assert directive == ExecutionDirective.NO_TRADE

    def test_map_regime_to_directive_apathy(self, matrix):
        directive = matrix.map_regime_to_directive(
            "APATHY", 0.30, 0.5, True
        )
        assert directive == ExecutionDirective.HOLD

    def test_map_regime_to_directive_neutral(self, matrix):
        directive = matrix.map_regime_to_directive(
            "NEUTRAL", 0.20, 0.5, True
        )
        assert directive == ExecutionDirective.HOLD

    def test_map_regime_to_directive_validation_override(self, matrix):
        directive = matrix.map_regime_to_directive(
            "VALIDATION_OVERRIDE_NEUTRAL", 0.50, 0.5, False
        )
        assert directive == ExecutionDirective.NO_TRADE

    def test_map_regime_to_directive_validation_failed(self, matrix):
        directive = matrix.map_regime_to_directive(
            "PANIC_CAPITULATION", 0.85, 0.5, False
        )
        assert directive == ExecutionDirective.NO_TRADE

    def test_evaluate_panic_capitulation(self, matrix):
        dcf_data = {
            "intrinsic_floor": 150.0,
            "intrinsic_ceiling": 200.0,
            "current_price": 160.0,
            "margin_of_safety": 0.25
        }
        
        result = matrix.evaluate(
            regime="PANIC_CAPITULATION",
            fused_confidence=0.85,
            dcf_floor_data=dcf_data,
            validation_passed=True,
            validation_details={"combined_penalty": 1.0},
            contrarian_buy_authorized=True
        )
        
        assert result.regime == "PANIC_CAPITULATION"
        assert result.execution_directive == "STRONG_CONTRARIAN_BUY"
        assert result.contrarian_buy_authorized is True
        assert result.fused_confidence == 0.85
        assert result.dcf_floor_signal == 1.0
        assert result.validation_passed is True
        assert "Panic capitulation" in result.rationale

    def test_evaluate_crowd_euphoria(self, matrix):
        result = matrix.evaluate(
            regime="CROWD_EUPHORIA",
            fused_confidence=0.80,
            dcf_floor_data=None,
            validation_passed=True,
            validation_details={},
            contrarian_buy_authorized=False
        )
        
        assert result.regime == "CROWD_EUPHORIA"
        assert result.execution_directive == "LOCKDOWN"
        assert result.contrarian_buy_authorized is False
        assert result.dcf_floor_signal == 0.5
        assert "Crowd euphoria" in result.rationale

    def test_evaluate_asymmetric_divergence(self, matrix):
        result = matrix.evaluate(
            regime="ASYMMETRIC_DIVERGENCE",
            fused_confidence=0.70,
            dcf_floor_data=None,
            validation_passed=True,
            validation_details={},
            contrarian_buy_authorized=True
        )
        
        assert result.regime == "ASYMMETRIC_DIVERGENCE"
        assert result.execution_directive == "ASYMMETRIC_OVERRIDE_BUY"
        assert result.contrarian_buy_authorized is True
        assert "Asymmetric divergence" in result.rationale

    def test_evaluate_validation_override(self, matrix):
        result = matrix.evaluate(
            regime="VALIDATION_OVERRIDE_NEUTRAL",
            fused_confidence=0.50,
            dcf_floor_data=None,
            validation_passed=False,
            validation_details={},
            contrarian_buy_authorized=False
        )
        
        assert result.regime == "VALIDATION_OVERRIDE_NEUTRAL"
        assert result.execution_directive == "NO_TRADE"
        assert result.contrarian_buy_authorized is False
        assert result.validation_passed is False
        assert "Cross-validation gate triggered" in result.rationale

    def test_evaluate_apathy(self, matrix):
        result = matrix.evaluate(
            regime="APATHY",
            fused_confidence=0.30,
            dcf_floor_data=None,
            validation_passed=True,
            validation_details={},
            contrarian_buy_authorized=False
        )
        
        assert result.regime == "APATHY"
        assert result.execution_directive == "HOLD"
        assert result.contrarian_buy_authorized is False

    def test_evaluate_neutral(self, matrix):
        result = matrix.evaluate(
            regime="NEUTRAL",
            fused_confidence=0.20,
            dcf_floor_data=None,
            validation_passed=True,
            validation_details={},
            contrarian_buy_authorized=False
        )
        
        assert result.regime == "NEUTRAL"
        assert result.execution_directive == "HOLD"
        assert result.contrarian_buy_authorized is False


class TestEnums:
    def test_execution_directive_values(self):
        assert ExecutionDirective.STRONG_CONTRARIAN_BUY.value == "STRONG_CONTRARIAN_BUY"
        assert ExecutionDirective.WEAK_CONTRARIAN_BUY.value == "WEAK_CONTRARIAN_BUY"
        assert ExecutionDirective.HOLD.value == "HOLD"
        assert ExecutionDirective.LOCKDOWN.value == "LOCKDOWN"
        assert ExecutionDirective.NO_TRADE.value == "NO_TRADE"

    def test_psychological_regime_values(self):
        assert PsychologicalRegime.PANIC_CAPITULATION.value == "PANIC_CAPITULATION"
        assert PsychologicalRegime.CROWD_EUPHORIA.value == "CROWD_EUPHORIA"
        assert PsychologicalRegime.ASYMMETRIC_DIVERGENCE.value == "ASYMMETRIC_DIVERGENCE"
        assert PsychologicalRegime.NEUTRAL.value == "NEUTRAL"


class TestCreateSignalMatrix:
    def test_create_signal_matrix(self):
        with patch('psychological.signal_matrix.load_hybrid_config') as mock_load:
            mock_load.return_value = {"signal_matrix": {}}
            matrix = create_signal_matrix()
            assert isinstance(matrix, SignalMatrix)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])