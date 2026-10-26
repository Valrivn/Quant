"""Unit tests — Quantitative/total_beta.py (Phase 8 WS-5.5/C5.6, bigpickle-worker)."""

import numpy as np
import pandas as pd
import pytest

from Quantitative.total_beta import (
    CUSTOM_SECTOR_BETA_U,
    UNIVERSE_MEDIAN_BETA_U,
    TOTAL_BETA_DEFAULTS,
    compute_beta_wacc_columns,
    compute_total_beta,
    d_e_market_net,
    hamada_lever,
    sector_beta_u_unlevered,
    tax_rate_applied,
    total_beta_adjusted,
    total_beta_applies,
    total_beta_raw,
)


class TestRhoFloor:
    """DEFECT 3: beta_L / max(rho, 0.25); negative rho clamps, never inverts."""

    def test_floor_low_positive_rho(self):
        raw, rho_used, _ = total_beta_raw(1.2, 0.05)
        assert raw == pytest.approx(1.2 / 0.25)
        assert rho_used == 0.25

    def test_negative_rho_clamped_not_amplified(self):
        raw, rho_used, prov = total_beta_raw(1.2, -0.10)
        assert rho_used == 0.25
        assert raw == pytest.approx(1.2 / 0.25)   # same as floor path
        assert prov.get("rho_negative_clamped") is True

    def test_high_rho_passes_through(self):
        raw, rho_used, _ = total_beta_raw(1.2, 0.80)
        assert raw == pytest.approx(1.2 / 0.80)
        assert rho_used == 0.80

    def test_boundary_rho_equals_floor(self):
        raw, rho_used, _ = total_beta_raw(1.2, 0.25)
        assert raw == pytest.approx(4.8)

    def test_missing_rho_neutral_and_degraded(self):
        raw, rho_used, prov = total_beta_raw(1.2, None)
        assert raw is None and rho_used == 0.5
        assert prov.get("rho_degraded") is True
        raw, rho_used, _ = total_beta_raw(1.2, np.nan)
        assert raw is None and rho_used == 0.5


class TestSigmoidCeiling:
    """total_beta_adj = 5.0 / (1 + e^(-raw/3.0)); agrees with wacc_engine_v2."""

    def test_matches_wacc_engine_regression_pin(self):
        # regressed beta 7.2 at rho 1.0 -> 4.5841 (pinned by test_wacc_engine_v2)
        adj, _, _ = total_beta_adjusted(7.2, 1.0)
        assert adj == pytest.approx(4.5841, abs=1e-3)

    def test_analytic_value(self):
        adj, _, _ = total_beta_adjusted(0.5, 0.8)  # raw 0.625
        assert adj == pytest.approx(5.0 / (1.0 + np.exp(-0.625 / 3.0)))

    def test_monotone_ceiling(self):
        lo, _, _ = total_beta_adjusted(0.5, 1.0)
        hi, _, _ = total_beta_adjusted(3.0, 1.0)
        sat, _, _ = total_beta_adjusted(200.0, 0.25)
        assert lo < hi < sat
        assert 0.0 < lo and sat <= 5.0

    def test_missing_rho_none(self):
        adj, rho_used, prov = total_beta_adjusted(1.2, None)
        assert adj is None and prov.get("rho_degraded")


class TestApplyCriteria:
    """Total Beta only for small_cap/private/concentrated/adv_low; NOT large liquid."""

    CFG = TOTAL_BETA_DEFAULTS

    def test_small_cap(self):
        applies, reason = total_beta_applies(1.5e9, 1e8, 0.01, False, self.CFG)
        assert applies and reason == "small_cap"

    def test_small_cap_boundary(self):
        # strictly below $2B threshold
        applies, _ = total_beta_applies(2.0e9, 1e8, 0.01, False, self.CFG)
        assert not applies

    def test_private(self):
        applies, reason = total_beta_applies(5e9, 1e8, 0.01, True, self.CFG)
        assert applies and reason == "private"

    def test_concentrated(self):
        applies, reason = total_beta_applies(5e9, 1e8, 0.031, False, self.CFG)
        assert applies and reason == "concentrated"

    def test_adv_low(self):
        applies, reason = total_beta_applies(5e9, 4.9e6, 0.01, False, self.CFG)
        assert applies and reason == "adv_low"

    def test_public_large_liquid_exempt(self):
        applies, reason = total_beta_applies(5e9, 1e8, 0.01, False, self.CFG)
        assert not applies and reason == ""

    def test_priority_order(self):
        reason = total_beta_applies(1e9, 1e6, 0.05, True, self.CFG)[1]
        assert reason == "private"   # private checked before other criteria

    def test_missing_market_cap_no_small_cap(self):
        applies, reason = total_beta_applies(np.nan, 1e6, 0.01, False, self.CFG)
        assert applies and reason == "adv_low"   # ADV still qualifies


class TestHamadaAndSector:
    """Net-debt Hamada + Damodaran custom-sector medians (Appendix B layer 2)."""

    def test_hamada_net_debt(self):
        bl = hamada_lever(0.9, 0.5, 0.21, False)
        assert bl == pytest.approx(0.9 * (1 + 0.79 * 0.5))

    def test_hamada_missing_debt_is_unlevered(self):
        assert hamada_lever(0.9, None, 0.21) == pytest.approx(0.9)
        assert hamada_lever(0.9, 0.0, 0.21) == pytest.approx(0.9)

    def test_financials_carve_out_cap(self):
        # Financials D/E capped at 0.60 before Hamada levering
        bl = hamada_lever(0.4373, 5.0, 0.21, True)
        assert bl == pytest.approx(0.4373 * (1 + 0.79 * 0.60))

    def test_non_financials_unbounded(self):
        # non-financial D/E 5.0 used raw
        bl = hamada_lever(0.4373, 5.0, 0.21, False)
        assert bl == pytest.approx(0.4373 * (1 + 0.79 * 5.0))

    def test_sector_median_fallback(self):
        for sector, b in CUSTOM_SECTOR_BETA_U.items():
            beta_u, src = sector_beta_u_unlevered(sector)
            assert beta_u == pytest.approx(b)
            assert src == "sector_median_fallback"

    def test_universe_median_fallback(self):
        beta_u, src = sector_beta_u_unlevered("UnknownSector")
        assert beta_u == pytest.approx(UNIVERSE_MEDIAN_BETA_U)
        assert src == "universe_median_fallback"
        beta_u2, _ = sector_beta_u_unlevered(None)
        assert beta_u2 == pytest.approx(UNIVERSE_MEDIAN_BETA_U)

    def test_d_e_market_net(self):
        assert d_e_market_net(50.0, 1000.0) == pytest.approx(0.05)
        assert d_e_market_net(10.0, 1000.0) == pytest.approx(0.01)
        assert d_e_market_net(500.0, 10.0) == pytest.approx(20.0)   # capped at schema max
        assert d_e_market_net(-50.0, 1000.0) is None       # net cash -> no levering
        assert d_e_market_net(0.0, 1000.0) is None          # zero debt -> no levering
        assert d_e_market_net(None, 1000.0) is None
        assert d_e_market_net(50.0, None) is None
        assert d_e_market_net(50.0, 0.0) is None            # missing/zero market cap

    def test_tax_fallback(self):
        assert tax_rate_applied(0.21) == pytest.approx(0.21)
        assert tax_rate_applied(0.12) == pytest.approx(0.12)
        assert tax_rate_applied(0.60) == pytest.approx(0.21)  # out of [0, 0.35]
        assert tax_rate_applied(-0.1) == pytest.approx(0.21)
        assert tax_rate_applied(None) == pytest.approx(0.21)


class TestRowPipeline:
    """compute_beta_wacc_columns: canonical schema column set + total beta wiring."""

    def test_columns_and_coverage(self):
        df = pd.DataFrame({
            "ticker": ["A", "B", "C", "D"],
            "sector": ["Software", "Financial", "Energy", None],
            "market_cap": [1e9, 5e9, 8e9, 6e9],
            "net_debt": [100e6, -50e6, 300e6, 200e6],
            "tax_rate_applied": [0.21, 0.20, 0.05, 0.30],
            "rho": [0.6, 0.9, 0.3, 0.0],
        })
        out = compute_beta_wacc_columns(df, rho_col="rho")
        for c in ("beta_unlevered_sector", "beta_levered_v2", "beta_source",
                  "d_e_market_net", "rho_market_60m", "total_beta",
                  "tax_rate_applied", "debt_mv_source",
                  "total_beta_applied", "total_beta_reason"):
            assert c in out.columns
        # schema invariant: total_beta non-null -> rho_market_60m non-null
        assert (out["total_beta"].notna() & out["rho_market_60m"].isna()).sum() == 0
        # beta_levered -> beta_source non-null
        assert (out["beta_levered_v2"].notna() & out["beta_source"].isna()).sum() == 0
        # only the small-cap row is total-beta adjusted
        assert bool(out.loc[0, "total_beta_applied"]) is True
        assert bool(out.loc[1, "total_beta_applied"]) is False
        assert out.loc[0, "total_beta_reason"] == "small_cap"

    def test_compute_total_beta_orchestrator(self):
        res = compute_total_beta(beta_unlevered=0.9553, beta_levered=1.5, beta_source="sector_median_fallback",
                                 d_e_net=0.3, rho_market_60m=0.8, market_cap=1e9,
                                 adv_usd=1e8, position_pct_nav=0.01, is_private=False)
        assert res.total_beta_applied is True
        assert res.total_beta_if_applied is not None
        raw = 1.5 / 0.8
        assert res.total_beta_if_applied == pytest.approx(5.0 / (1 + np.exp(-raw / 3.0)))
        assert res.provenance["apply_criteria"] == (True, "small_cap")

    def test_exempt_keeps_plain_beta(self):
        res = compute_total_beta(0.79, 0.95, "sector_median_fallback", 0.1,
                                 rho_market_60m=0.9, market_cap=5e9, adv_usd=1e8,
                                 position_pct_nav=0.01, is_private=False)
        assert res.total_beta_applied is False
        assert res.total_beta_if_applied is None
        assert res.provenance.get("exempt_public_liquid") is True