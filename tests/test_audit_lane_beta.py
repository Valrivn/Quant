"""
tests/test_audit_lane_beta.py — Lane Beta Extended Fundamental Model Audit Suite
"""
import pytest
from psychological.qualitative_scoring import (
    TrajectoryCorridorEngine, FinancialReconstructionInterface
)

class TestLaneBetaAudit:
    def test_trajectory_corridor_extreme_input_variance_and_separation(self):
        engine = TrajectoryCorridorEngine(floor_boundary=0.15, ceiling_boundary=0.92)
        
        # Standard anomaly vs extreme anomaly
        res_std = engine.compute("NVDA", z_score=2.0)
        res_extreme = engine.compute("NVDA", z_score=3.5)
        
        # Verify dynamic range separation
        assert res_extreme.scaled_score != res_std.scaled_score, "Extreme anomaly must have range separation from standard anomaly"
        assert res_extreme.decay_factor < res_std.decay_factor, "Piecewise decay factor must decrease for higher growth stages"
        assert res_extreme.growth_stage in ["mature", "declining"]
        assert res_std.growth_stage == "mature"

    def test_trajectory_corridor_asymmetric_piecewise_curve_behavior(self):
        engine = TrajectoryCorridorEngine(floor_boundary=0.15, ceiling_boundary=0.92)
        
        z_range = [-4.0, -2.0, -0.5, 0.0, 1.0, 2.0, 3.5, 5.0]
        scores = [engine.compute("AAPL", z).scaled_score for z in z_range]
        
        # Verify non-linear curve behavior (not flat linear projection)
        diffs = [scores[i+1] - scores[i] for i in range(len(scores)-1)]
        assert len(set(round(d, 4) for d in diffs)) > 1, "Trajectory curve must exhibit piecewise non-linear rate of change"

    def test_financial_reconstruction_rd_capitalization_and_sbc_drag(self):
        fri = FinancialReconstructionInterface()
        
        # Test R&D amortization vs simple period expense
        res = fri.evaluate(
            ticker="AVGO",
            rd_expense=5000000000.0,
            revenue=30000000000.0,
            gross_profit=20000000000.0,
            sbc_expense=1000000000.0,
            shares_outstanding=460000000.0,
            share_price=175.0,
            sector="semiconductor"
        )
        
        assert res.rd_capitalisation_rate > 0.0, "Current R&D expenditure must be capitalized rather than simple period expense"
        assert res.rd_asset_years == 5.0, "Semiconductor R&D asset life must be multi-year timeline (5 years)"
        assert res.sbc_drag_intensity > 0.0, "SBC expense must trigger dilution and revenue intensity growth drag"
        assert res.sbc_dilution_risk in ["low", "moderate", "elevated", "critical"]

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
