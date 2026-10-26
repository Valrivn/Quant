"""
tests/test_moat_dollar_valuation.py — Unit Tests for Moat Dollar Valuation Engine.

Tests:
1. Baseline justified moat cap: MAX_JUSTIFIED_PREMIUM == 0.80 (+80%).
2. Dynamic volatility scaler V_i in [0.70, 1.25]:
   - Pristine low variance/covariance scales up to 1.25 (expanding moat premium).
   - High variance/covariance dampens down to 0.70.
3. Fragility F_i and customer concentration > 30%:
   - <= 30% has no concentration penalty (flag=False).
   - 35% triggers flagged modest penalty of exactly 0.075 (flag=True).
   - Graceful handling (no crash, position preserved).
4. Durability D_i in [0.0, 1.0] and monotonicity with fragility.
5. Dollar fair value IV_moat and Alpha Spread Delta_alpha.
6. Output dataclass MoatValuationOutput and batch evaluation.
"""

import numpy as np
import pandas as pd
import pytest

from valuation.moat_dollar_valuation import (
    MoatDollarValuationEngine,
    MoatValuationOutput,
    compute_moat_valuation,
    MAX_JUSTIFIED_PREMIUM,
)


class TestMoatDollarValuationEngine:

    def setup_method(self):
        self.engine = MoatDollarValuationEngine(
            max_justified_premium=0.80,
            median_cf_variance=0.04,
            mad_cf_variance=0.02,
            median_market_covariance=0.02,
            mad_market_covariance=0.01,
        )

    def test_baseline_cap_constant(self):
        """Assert MAX_JUSTIFIED_PREMIUM baseline ceiling is +80%."""
        assert MAX_JUSTIFIED_PREMIUM == 0.80
        assert self.engine.MAX_JUSTIFIED_PREMIUM == 0.80
        assert self.engine.max_justified_premium == 0.80

    def test_volatility_scaler_bounds_and_dynamics(self):
        """Test V_i scaling up to 1.25 under low vol and down to 0.70 under high vol."""
        # Median variance & covariance -> V_i == 1.00
        v_neutral = self.engine.compute_volatility_scaler(
            cash_flow_variance=0.04,
            market_covariance=0.02,
        )
        assert pytest.approx(v_neutral, abs=1e-4) == 1.00

        # Pristine stability (near zero variance and negative/zero covariance)
        # tanh -> -1, V_i -> 1.0 - 0.25*(-1) - 0.15*(-1) = 1.40 -> clamped to 1.25
        v_low = self.engine.compute_volatility_scaler(
            cash_flow_variance=0.0001,
            market_covariance=-0.05,
        )
        assert pytest.approx(v_low, abs=1e-4) == 1.25

        # Extreme distress/volatility (very high variance and market covariance)
        # tanh -> +1, V_i -> 1.0 - 0.25*(1) - 0.15*(1) = 0.60 -> clamped to 0.70
        v_high = self.engine.compute_volatility_scaler(
            cash_flow_variance=0.50,
            market_covariance=0.20,
        )
        assert pytest.approx(v_high, abs=1e-4) == 0.70

    def test_customer_concentration_graceful_penalty(self):
        """Assert >30% concentration flags gracefully without killing position."""
        # Case 1: 20% concentration (<= 30%) -> zero concentration penalty, flag is False
        f_clean, flag_clean = self.engine.compute_fragility(
            customer_conc_share=0.20,
            antitrust_litigation=0.0,
            supplier_choke_share=0.0,
        )
        assert flag_clean is False
        assert f_clean == 0.0

        # Case 2: 35% concentration -> flagged penalty = 0.30 * (0.35 - 0.30) / 0.20 = 0.075
        f_35, flag_35 = self.engine.compute_fragility(
            customer_conc_share=0.35,
            antitrust_litigation=0.0,
            supplier_choke_share=0.0,
        )
        assert flag_35 is True
        assert pytest.approx(f_35, abs=1e-6) == 0.075

        # Case 3: 50% concentration -> penalty = 0.30 * (0.50 - 0.30) / 0.20 = 0.30
        f_50, flag_50 = self.engine.compute_fragility(
            customer_conc_share=0.50,
            antitrust_litigation=0.0,
            supplier_choke_share=0.0,
        )
        assert flag_50 is True
        assert pytest.approx(f_50, abs=1e-6) == 0.30

        # Percentage format (e.g. 35.0 instead of 0.35)
        f_pct, flag_pct = self.engine.compute_fragility(
            customer_conc_share=35.0,
            antitrust_litigation=False,
            supplier_choke_share=0.0,
        )
        assert flag_pct is True
        assert pytest.approx(f_pct, abs=1e-6) == 0.075

    def test_durability_bounds_and_monotonicity(self):
        """Assert D_i in [0, 1] and decreases with increasing fragility."""
        ms = 0.80
        d_no_fragility = self.engine.compute_durability(moat_strength=ms, fragility=0.0)
        assert 0.999 < d_no_fragility <= 1.0

        d_moderate = self.engine.compute_durability(moat_strength=ms, fragility=0.20)
        d_severe = self.engine.compute_durability(moat_strength=ms, fragility=0.80)

        assert d_no_fragility > d_moderate > d_severe > 0.0
        assert 0.0 <= d_severe <= 1.0

    def test_moat_strength_clamping(self):
        """Assert MS_i in [0, 1] across extreme negative and positive z-scores."""
        # Extreme negative z-scores
        ms_neg = self.engine.compute_moat_strength(-5.0, -5.0, -5.0, -5.0)
        assert 0.0 <= ms_neg < 0.10

        # Neutral z-scores (0.0) -> exactly 0.50
        ms_neutral = self.engine.compute_moat_strength(0.0, 0.0, 0.0, 0.0)
        assert pytest.approx(ms_neutral, abs=1e-4) == 0.50

        # Extreme positive z-scores
        ms_pos = self.engine.compute_moat_strength(5.0, 5.0, 5.0, 5.0)
        assert 0.90 < ms_pos <= 1.0

    def test_complete_valuation_flow_nvda_case(self):
        """Verify full evaluation pipeline, dollar fair value, and alpha spread."""
        output = self.engine.evaluate(
            ticker="NVDA",
            current_price=120.0,
            iv_baseline=100.0,
            dev_lockin_z=2.5,
            ai_dominance_z=2.8,
            patent_utility_z=1.5,
            rpo_backlog_z=2.0,
            customer_conc_share=0.35,  # Flagged customer concentration
            antitrust_litigation=False,
            supplier_choke_share=0.10,
            cash_flow_variance=0.01,   # Low CF variance
            market_covariance=0.005,   # Low market covariance
        )

        assert isinstance(output, MoatValuationOutput)
        assert output.ticker == "NVDA"
        assert output.customer_conc_flag is True
        assert output.fragility > 0.0
        assert output.durability < 1.0
        assert output.volatility_scaler > 1.0  # Scales up due to low variance/covariance
        assert output.justified_premium > 0.0
        # Dollar fair value IV_moat = IV_baseline * (1 + Pi_justified)
        assert pytest.approx(output.iv_moat, abs=1e-4) == output.iv_baseline * (1.0 + output.justified_premium)
        # Market premium = (120 - 100) / 100 = 0.20
        assert pytest.approx(output.market_premium, abs=1e-4) == 0.20
        # Alpha spread = Pi_justified - Market_Premium
        assert pytest.approx(output.alpha_spread, abs=1e-4) == output.justified_premium - output.market_premium

    def test_convenience_function(self):
        """Assert compute_moat_valuation convenience function works identically."""
        res = compute_moat_valuation(
            ticker="MSFT",
            current_price=400.0,
            iv_baseline=350.0,
            dev_lockin_z=1.8,
            ai_dominance_z=1.5,
            patent_utility_z=1.2,
            rpo_backlog_z=1.0,
            customer_conc_share=0.15,
            antitrust_litigation=0.2,
            supplier_choke_share=0.05,
            cash_flow_variance=0.03,
            market_covariance=0.015,
        )
        assert res.ticker == "MSFT"
        assert res.customer_conc_flag is False
        assert 0.0 < res.justified_premium < 0.80 * 1.25
        assert res.iv_moat > res.iv_baseline

    def test_batch_evaluation(self):
        """Test evaluate_batch with pandas DataFrame."""
        df = pd.DataFrame([
            {
                "ticker": "NVDA",
                "current_price": 120.0,
                "iv_baseline": 100.0,
                "dev_lockin_z": 2.5,
                "ai_dominance_z": 2.8,
                "patent_utility_z": 1.5,
                "rpo_backlog_z": 2.0,
                "customer_conc_share": 0.35,
                "antitrust_litigation": False,
                "supplier_choke_share": 0.10,
                "cash_flow_variance": 0.02,
                "market_covariance": 0.01,
            },
            {
                "ticker": "INTC",
                "current_price": 25.0,
                "iv_baseline": 30.0,
                "dev_lockin_z": -0.5,
                "ai_dominance_z": -1.2,
                "patent_utility_z": 0.8,
                "rpo_backlog_z": -0.8,
                "customer_conc_share": 0.10,
                "antitrust_litigation": True,
                "supplier_choke_share": 0.25,
                "cash_flow_variance": 0.08,
                "market_covariance": 0.04,
            },
        ])

        df_out = self.engine.evaluate_batch(df)
        assert len(df_out) == 2
        assert "justified_premium" in df_out.columns
        assert "iv_moat" in df_out.columns
        assert "alpha_spread" in df_out.columns
        assert df_out.loc[0, "justified_premium"] > df_out.loc[1, "justified_premium"]
