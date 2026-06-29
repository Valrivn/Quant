"""
tests/test_audit_lane_alpha.py — Lane Alpha Math Engine Validation Audit Suite
"""
import pytest
import math
import numpy as np
from opencode_scripts.qualitative_scoring import (
    EMAFilter, DoubleStandardizer, SubSectorConfig, tanh_clamp
)

class TestLaneAlphaAudit:
    @pytest.fixture
    def subsector_cfg(self):
        return SubSectorConfig()

    def test_double_standardizer_stage2_non_null_across_peer_arrays(self, subsector_cfg):
        ds = DoubleStandardizer(subsector_config=subsector_cfg, min_history=5)
        tickers = ["NVDA", "AVGO", "INTC", "AMD", "MSFT", "GOOGL", "META", "TSLA", "AAPL", "AMZN"]
        
        # Seed history
        for day in range(10):
            stage1_map = {}
            for t in tickers:
                val = float(10 + hash(t + str(day)) % 20)
                s1 = ds.stage1(t, val)
                if s1 is not None:
                    stage1_map[t] = s1
            
            if day >= 5:
                for t in tickers:
                    s2 = ds.stage2(t, stage1_map)
                    assert s2 is not None, f"Stage 2 z-score returned None for {t} on day {day}"
                    assert not math.isnan(s2), f"Stage 2 returned NaN for {t}"
                    assert not math.isinf(s2), f"Stage 2 returned Inf for {t}"
                    assert -1.0 <= s2 <= 1.0, f"Stage 2 out of bounds for {t}: {s2}"

    def test_ema_filter_cold_start_and_1000_row_matrix_check(self):
        ema = EMAFilter(halflife=21, min_observations=5)
        
        # Test cold start initialization
        first_val = ema.update("NVDA", 15.5)
        assert first_val == 15.5, "Cold start first observation must seed without distortion"
        
        # 1,000-row simulated matrix check across multiple assets and historical gaps
        np.random.seed(42)
        simulated_matrix = np.random.normal(loc=0.5, scale=2.5, size=(1000, 10))
        simulated_matrix[100:105, :] = np.nan # Simulate historical missing data gaps
        
        tickers = ["NVDA", "AVGO", "INTC", "AMD", "MSFT", "GOOGL", "META", "TSLA", "AAPL", "AMZN"]
        
        for row_idx in range(1000):
            for col_idx, ticker in enumerate(tickers):
                raw_val = simulated_matrix[row_idx, col_idx]
                if np.isnan(raw_val):
                    continue # Gap handling
                
                res = ema.update(ticker, float(raw_val))
                assert res is not None
                assert not math.isnan(res), f"NaN propagated at row {row_idx} for {ticker}"
                assert not math.isinf(res), f"Infinity propagated at row {row_idx} for {ticker}"

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
