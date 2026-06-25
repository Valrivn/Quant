import pytest
from psychological.state_machine import PsychologicalStateMachine, create_state_machine
from psychological.interfaces import RegimeOutput


class TestPsychologicalStateMachine:
    @pytest.fixture
    def state_machine(self):
        return create_state_machine()

    def test_panic_capitulation(self, state_machine):
        result = state_machine.evaluate(
            bull_bear_ratio=0.2,
            velocity_sigma=2.5
        )
        
        assert result["regime"] == "PANIC_CAPITULATION"
        assert result["contrarian_buy_authorized"] is True
        assert result["confidence"] > 0

    def test_crowd_euphoria(self, state_machine):
        result = state_machine.evaluate(
            bull_bear_ratio=5.0,
            velocity_sigma=3.0
        )
        
        assert result["regime"] == "CROWD_EUPHORIA"
        assert result["contrarian_buy_authorized"] is False
        assert result["confidence"] > 0

    def test_asymmetric_divergence(self, state_machine):
        result = state_machine.evaluate(
            bull_bear_ratio=1.0,
            velocity_sigma=0.2,
            employee_sentiment_proxy=-2.0,
            dev_velocity_sigma=1.5
        )
        
        assert result["regime"] == "ASYMMETRIC_DIVERGENCE"
        assert result["contrarian_buy_authorized"] is False
        assert result["confidence"] == 0.7

    def test_apathy(self, state_machine):
        result = state_machine.evaluate(
            bull_bear_ratio=1.2,
            velocity_sigma=0.3
        )
        
        assert result["regime"] == "APATHY"
        assert result["contrarian_buy_authorized"] is False
        assert result["confidence"] == 0.3

    def test_neutral(self, state_machine):
        result = state_machine.evaluate(
            bull_bear_ratio=2.0,
            velocity_sigma=1.0
        )
        
        assert result["regime"] == "NEUTRAL"
        assert result["contrarian_buy_authorized"] is False
        assert result["confidence"] == 0.2

    def test_validation_override(self, state_machine):
        result = state_machine.evaluate(
            bull_bear_ratio=0.2,
            velocity_sigma=2.5,
            glassdoor_raw=4.5,
            comparably_badge=30
        )
        
        assert result["regime"] == "VALIDATION_OVERRIDE_NEUTRAL"
        assert result["contrarian_buy_authorized"] is False
        assert result["confidence"] == 0.0

    def test_glassdoor_github_divergence(self, state_machine):
        result = state_machine.evaluate(
            bull_bear_ratio=1.0,
            velocity_sigma=0.0,
            glassdoor_raw=4.5,
            comparably_badge=30
        )
        
        assert result["regime"] == "VALIDATION_OVERRIDE_NEUTRAL"
        assert result["contrarian_buy_authorized"] is False
        assert result["confidence"] == 0.0

    def test_panic_boundary_conditions(self, state_machine):
        result = state_machine.evaluate(
            bull_bear_ratio=0.25,
            velocity_sigma=2.0
        )
        
        assert result["regime"] == "PANIC_CAPITULATION"

    def test_euphoria_boundary_conditions(self, state_machine):
        result = state_machine.evaluate(
            bull_bear_ratio=4.0,
            velocity_sigma=2.5
        )
        
        assert result["regime"] == "CROWD_EUPHORIA"

    def test_apathy_boundary_conditions(self, state_machine):
        result = state_machine.evaluate(
            bull_bear_ratio=0.8,
            velocity_sigma=0.5
        )
        
        assert result["regime"] == "APATHY"

    def test_validation_penalty_applied(self, state_machine):
        result = state_machine.evaluate(
            bull_bear_ratio=0.2,
            velocity_sigma=2.5,
            glassdoor_raw=4.0,
            comparably_badge=60
        )
        
        assert result["confidence"] < 1.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])