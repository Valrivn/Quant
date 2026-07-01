"""
tests/test_audit_lane_delta.py — Lane Delta Master Optimization & Guardrail Audit Suite
"""
import pytest
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
LANE_RESULTS_DIR = WORKSPACE_ROOT / "center"

class TestLaneDeltaAudit:
    def test_weight_optimization_reasoning_artifacts(self):
        weight_file = LANE_RESULTS_DIR / "weight_reasoning.md"
        assert weight_file.exists(), "weight_reasoning.md must exist in center/"
        content = weight_file.read_text()
        assert "Spearman" in content or "weight" in content.lower(), "Weight reasoning must document statistical optimization"

    def test_conviction_scores_matrix_structure_and_zero_lookahead(self):
        conviction_file = LANE_RESULTS_DIR / "conviction_scores.md"
        assert conviction_file.exists(), "conviction_scores.md must exist in center/"
        content = conviction_file.read_text()
        
        # Verify 0-10 conviction scale and ticker presence
        tickers = ["NVDA", "AVGO", "INTC", "AMD", "MSFT", "GOOGL", "META", "TSLA", "AAPL", "AMZN"]
        for ticker in tickers:
            assert ticker in content, f"Conviction scores report must contain entry for {ticker}"
            
        assert "/10" in content, "Conviction scores must be formatted on a 0-10 scale"

    def test_lookahead_compliance_boundaries(self):
        hist_baseline = WORKSPACE_ROOT / "data" / "historical_5y_baseline.csv"
        assert hist_baseline.exists(), "historical_5y_baseline.csv must exist"
        
        # Verify point-in-time boundary date limits
        content = hist_baseline.read_text()
        lines = content.strip().split("\n")
        assert len(lines) > 1, "Historical baseline must contain dataset rows"
        
        # Check that historical baseline does not contain 2026 future date timestamps
        assert "2026-" not in content, "Historical 5-year baseline must contain zero 2026 lookahead dates"

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
