"""Unit tests — Quantitative/narrative_valuation.py (Phase 8 WS-6/C6.1, bigpickle-worker)."""

import numpy as np
import pytest

from Quantitative.narrative_valuation import (
    NARRATIVE_DEFAULTS,
    compute_narrative_valuation,
    credit_capacity,
    margin_path,
    project_fcf_path,
    reinvestment_rate_v2,
    revenue_growth_path,
    sc_ratio_used,
    terminal_value,
    unfunded_growth_check,
)
from Quantitative.survival_adjustment import SURVIVAL_DEFAULTS


class TestReinvestmentIdentity:
    """C6.1 corrected basis: RR = (dRev / S_C) / EBIT(1-tau), clamped >= 0."""

    def test_identity(self):
        rr = reinvestment_rate_v2(100.0, 2.0, 100.0, 0.21)
        assert rr == pytest.approx((100.0 / 2.0) / (100.0 * 0.79))

    def test_schema_lower_bound(self):
        assert reinvestment_rate_v2(-50.0, 2.0, 100.0, 0.21) == 0.0  # shrinking
        assert reinvestment_rate_v2(100.0, 2.0, -10.0, 0.21) == 0.0  # loss-making
        assert reinvestment_rate_v2(100.0, 0.0, 100.0, 0.21) == 0.0  # no S/C

    def test_scaling(self):
        # doubling S/C halves the reinvestment required for the same growth
        a = reinvestment_rate_v2(100.0, 1.0, 100.0, 0.21)
        b = reinvestment_rate_v2(100.0, 2.0, 100.0, 0.21)
        assert a == pytest.approx(2.0 * b)


class TestGrowthAndMarginPaths:
    """Linear convergence; year-7 industry anchor; flat after (years 8-10)."""

    def test_young_decline(self):
        g = revenue_growth_path(0.20, 0.02, 7, 10)
        assert g[0] == pytest.approx(0.20 + (0.02 - 0.20) * (1.0 / 7.0))
        assert g[5] == pytest.approx(0.20 + (0.02 - 0.20) * (6.0 / 7.0))
        assert g[6] == pytest.approx(0.02)
        assert g[9] == pytest.approx(0.02)

    def test_margin_convergence(self):
        m = margin_path(0.05, 0.12, 7, 10)
        assert m[0] == pytest.approx(0.05 + (0.12 - 0.05) * (1.0 / 7.0))
        assert m[6] == pytest.approx(0.12)
        assert m[9] == pytest.approx(0.12)

    def test_no_convergence_when_same(self):
        m = margin_path(0.10, 0.10, 7, 10)
        assert all(x == pytest.approx(0.10) for x in m)


class TestTerminalValue:
    """TV = FCF_10 (1+g) / (WACC - g); 50bps freeze guard."""

    def test_formula(self):
        tv, g_used, prov = terminal_value(100.0, 0.01, 0.08)
        assert g_used == pytest.approx(0.01)
        assert tv == pytest.approx(100.0 * 1.01 / 0.07)
        assert not prov.get("g_terminal_frozen")

    def test_guard_freeze(self):
        tv, g_used, prov = terminal_value(100.0, 0.055, 0.06)
        assert prov.get("g_terminal_frozen") is True
        assert g_used == pytest.approx(0.055)  # WACC - 50bps
        # re-derive TV with the frozen g
        assert tv == pytest.approx(100.0 * 1.055 / 0.005)

    def test_boundary_exactly_50bps_not_frozen(self):
        tv, g_used, prov = terminal_value(100.0, 0.075, 0.08)  # spread == 50bps
        assert not prov.get("g_terminal_frozen")
        assert g_used == pytest.approx(0.075)


class TestProjectFcfPath:
    """young RR = dRev/S_C/NOPAT (0 when NOPAT<=0); mature RR = g/ROIC capped 0.95."""

    def test_mature_g_over_roic(self):
        fcf = project_fcf_path(100.0, 0.10, 0.21, [0.02] * 10, [0.10] * 10,
                               2.0, 0.10, False)
        nopat0 = 100.0 * 1.02 * 0.10 * 0.79
        assert fcf[0] == pytest.approx(nopat0 * (1 - 0.02 / 0.10))

    def test_mature_growth_cap(self):
        # g=0.12, ROIC=0.05 -> RR would be 2.4 -> capped 0.95
        fcf = project_fcf_path(100.0, 0.10, 0.21, [0.12] * 10, [0.10] * 10,
                               2.0, 0.05, False)
        nopat0 = 100.0 * 1.12 * 0.10 * 0.79
        assert fcf[0] == pytest.approx(nopat0 * (1 - 0.95))

    def test_young_sc_driven(self):
        # NOPAT year0 = 100 x (1+g) x margin x (1-tau); RR = dRev/S_C/NOPAT
        g = [0.10] * 10
        fcf = project_fcf_path(100.0, 0.10, 0.21, g, [0.10] * 10, 2.0, 0.12, True)
        rev1 = 110.0
        nopat1 = rev1 * 0.10 * 0.79
        rr = (10.0 / 2.0) / nopat1
        assert fcf[0] == pytest.approx(nopat1 * (1 - rr))
        assert 0 <= rr <= 0.95

    def test_young_loss_burns(self):
        # negative-NOPAT year reinvests nothing (FCF = NOPAT, burn)
        fcf = project_fcf_path(100.0, -0.10, 0.21, [0.0] * 10, [-0.10] * 10,
                               2.0, 0.12, True)
        nopat1 = 100.0 * (-0.10) * 0.79
        assert fcf[0] == pytest.approx(nopat1)   # RR = 0


class TestCreditCapacity:
    """C7.2 frozen: ratio x Total_Assets - Total_Debt."""

    def test_formula_and_provenance(self):
        cc, ratio = credit_capacity(0.20, 1000.0, 100.0)
        assert cc == pytest.approx(100.0)
        assert ratio == 0.20

    def test_missing_assets_or_debt(self):
        cc, ratio = credit_capacity(0.20, None, 100.0)
        assert cc is None and ratio == 0.20
        cc, _ = credit_capacity(0.20, 1000.0, None)
        assert cc is None

    def test_negative_or_zero_ratio_falls_back(self):
        cc, ratio = credit_capacity(-0.1, 1000.0, 100.0)
        assert ratio == NARRATIVE_DEFAULTS["default_target_debt_ratio"] == 0.25
        assert cc == pytest.approx(0.25 * 1000.0 - 100.0)

    def test_nonpositive_assets(self):
        cc, _ = credit_capacity(0.20, 0.0, 100.0)
        assert cc is None


class TestUnfundedGrowth:
    """C7.3: Required = dRev/S_C; Available = OCF + Cash + CC; >1.2x -> flag."""

    def test_flag_with_gap(self):
        flag, gap, hc, pen, avail = unfunded_growth_check(200.0, 1.0, 50.0, 0.0, 0.0)
        assert flag is True and avail == 50.0
        assert gap == pytest.approx((200.0 - 50.0) / 50.0)   # 3.0
        assert hc == 0.5 and pen == 200.0

    def test_funded_within_threshold(self):
        flag, gap, hc, pen, _ = unfunded_growth_check(50.0, 1.0, 50.0, 0.0, 0.0)
        assert not flag and hc == 0.0 and pen == 0.0

    def test_threshold_boundary(self):
        # required == 1.2 x available -> NOT unfunded (strictly greater required)
        flag, _, _, _, _ = unfunded_growth_check(60.0, 1.0, 50.0, 0.0, 0.0)
        assert not flag

    def test_negative_delta_never_flags(self):
        flag, gap, hc, pen, _ = unfunded_growth_check(-10.0, 1.0, 0.0, 0.0, 0.0)
        assert not flag and hc == 0.0 and pen == 0.0
        flag, _, _, _, _ = unfunded_growth_check(None, 1.0, 0.0, 0.0, 0.0)
        assert not flag

    def test_negative_available_any_required_is_unfunded(self):
        # cc deeply negative -> available < 0; positive Required is unfunded
        flag, gap, hc, pen, avail = unfunded_growth_check(10.0, 1.0, 0.0, 0.0, -100.0)
        assert flag is True and avail == -100.0
        assert gap is None  # NaN-gap documented, not a numeric value
        assert hc == 0.5 and pen == 200.0


class TestScRatioResolution:
    """Young -> industry; mature -> company else industry; P95 clamp; floor."""

    def test_young_uses_industry(self):
        assert sc_ratio_used(None, 1.5, True, 1.9) == pytest.approx(1.5)
        assert sc_ratio_used(9.9, 1.5, True, 1.9) == pytest.approx(1.5)

    def test_mature_uses_company_then_industry(self):
        assert sc_ratio_used(1.2, 1.5, False, 1.9) == pytest.approx(1.2)
        assert sc_ratio_used(None, 1.5, False, 1.9) == pytest.approx(1.5)

    def test_nonpositive_clamps_to_p95(self):
        assert sc_ratio_used(None, 0.0, False, 1.9) == pytest.approx(1.9)
        assert sc_ratio_used(-1.0, 0.0, False, 1.9) == pytest.approx(1.9)

    def test_all_missing_floor(self):
        assert sc_ratio_used(None, None, False, None) == pytest.approx(
            NARRATIVE_DEFAULTS["sc_ratio_min"])


class TestOrchestrator:
    """compute_narrative_valuation: golden mature row + young + unfunded + guards."""

    def test_mature_golden_row(self):
        res = compute_narrative_valuation(
            revenue=1000.0, ebit=100.0, tax_rate=0.21, operating_margin=0.10,
            roic=0.12, revenue_yoy=0.03, delta_rev=30.0, wacc=0.08,
            rf_10y=0.0424, inflation=0.02, years_public=15.0, is_young=False,
            company_sc=2.0, industry_sc=1.8, cross_sectional_p95=2.5,
            ocf=80.0, cash=120.0, total_debt=200.0, total_assets=1400.0,
            sector_target_debt_ratio=0.20, net_debt=80.0, shares_outstanding=100.0,
            sector_median_margin=0.10)
        # g_T = R_f - Inflation = 2.24% (spread 5.76% > 50bps -> not frozen)
        assert res.g_terminal_used == pytest.approx(0.0224)
        assert res.wacc_used == 0.08
        # S/C = company (mature)
        assert res.sc_ratio_used == 2.0
        assert res.reinvestment_rate_v2 == pytest.approx(15.0 / 79.0)
        # funded: required 15 <= 1.2 x (80 + 120 + 80) = 336
        assert res.unfunded_growth_flag is False
        assert res.wacc_penalty_bps_applied == 0.0
        # survival: mature branch (no moat at 4pt spread, ICR missing)
        assert res.p_survival == SURVIVAL_DEFAULTS["survival_mature"]
        # invariant
        assert res.survival_adjusted_iv <= res.going_concern_iv_ps * 1.0001
        assert res.going_concern_iv_ps > 0.0
        # FCF re-derivation on path base
        g = 0.0224
        fcf0 = 1000.0 * (1 + g) * 0.10 * 0.79 * (1 - g / 0.12)
        assert res.provenance["ev_fcff"] > fcf0     # PV of 10y + TV
        # equity basis: IV_total = EV - net_debt (ev_fcff in provenance is
        # rounded to cents -> comparison within the rounding band)
        assert res.going_concern_iv == pytest.approx(
            res.provenance["ev_fcff"] - 80.0, abs=0.011)

    def test_young_row_uses_industry_sc_and_ramp(self):
        res = compute_narrative_valuation(
            revenue=100.0, ebit=15.0, tax_rate=0.21, operating_margin=0.15,
            roic=0.20, revenue_yoy=0.35, delta_rev=35.0, wacc=0.09,
            rf_10y=0.0424, inflation=0.02, years_public=2.0, is_young=True,
            company_sc=9.0,  # ignored for young
            industry_sc=1.5, cross_sectional_p95=2.5,
            ocf=5.0, cash=30.0, total_debt=40.0, total_assets=200.0,
            sector_target_debt_ratio=0.22, net_debt=10.0, shares_outstanding=10.0,
            sector_median_margin=0.15, icr=8.0)
        assert res.sc_ratio_used == 1.5                 # young -> industry
        assert res.years_public_used == 2.0
        # young_ramp: 0.60 + 0.05 x 2
        assert res.p_survival == pytest.approx(0.70)
        assert res.wacc_penalty_bps_applied == 0.0

    def test_unfunded_haircut_and_penalty_logged(self):
        res = compute_narrative_valuation(
            revenue=500.0, ebit=80.0, tax_rate=0.21, operating_margin=0.16,
            roic=0.15, revenue_yoy=0.18, delta_rev=90.0, wacc=0.08,
            rf_10y=0.0424, inflation=0.02, years_public=12.0, is_young=False,
            company_sc=0.5, industry_sc=0.8, cross_sectional_p95=1.5,
            ocf=20.0, cash=5.0, total_debt=400.0, total_assets=800.0,
            sector_target_debt_ratio=0.20, net_debt=395.0, shares_outstanding=10.0,
            sector_median_margin=0.16)
        assert res.unfunded_growth_flag is True
        assert res.terminal_growth_haircut == 0.5
        assert res.wacc_penalty_bps_applied == 200.0
        assert res.wacc_used == pytest.approx(0.10)
        # g_T haircut: 0.0224 x (1 - 0.5)
        assert res.g_terminal_used == pytest.approx(0.0112)
        # required_reinvestment = delta / sc
        assert res.required_reinvestment == pytest.approx(180.0)

    def test_terminal_guard_freezes_in_orchestrator(self):
        # rf 0.10 - inflation 0.02 = 0.08 vs wacc 0.06 -> freeze to 0.055
        res = compute_narrative_valuation(
            revenue=1000.0, ebit=100.0, tax_rate=0.21, operating_margin=0.10,
            roic=0.12, revenue_yoy=0.03, delta_rev=10.0, wacc=0.06,
            rf_10y=0.10, inflation=0.02, years_public=15.0, is_young=False,
            company_sc=2.0, industry_sc=2.0, cross_sectional_p95=2.5,
            ocf=100.0, cash=50.0, total_debt=100.0, total_assets=1000.0,
            sector_target_debt_ratio=0.20, net_debt=50.0, shares_outstanding=10.0,
            sector_median_margin=0.10)
        assert res.g_terminal_used == pytest.approx(0.055)
        assert res.provenance["g_terminal_frozen"] is True

    def test_empty_input_guard(self):
        res = compute_narrative_valuation(
            revenue=None, ebit=100.0, tax_rate=0.21, operating_margin=0.10,
            roic=0.12, revenue_yoy=0.03, delta_rev=30.0, wacc=0.08,
            rf_10y=0.0424, inflation=0.02, years_public=15.0, is_young=False,
            company_sc=2.0, industry_sc=1.8, cross_sectional_p95=2.5,
            ocf=80.0, cash=120.0, total_debt=200.0, total_assets=1400.0,
            sector_target_debt_ratio=0.20, net_debt=80.0, shares_outstanding=100.0,
            sector_median_margin=0.10)
        assert res.provenance.get("empty_input") is True
        assert res.going_concern_iv is None and res.survival_adjusted_iv is None
        assert res.sc_ratio_used == NARRATIVE_DEFAULTS["sc_ratio_min"]