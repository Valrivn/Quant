"""Unit tests — Quantitative/survival_adjustment.py (Phase 8 WS-6, bigpickle-worker)."""

import numpy as np
import pandas as pd
import pytest

from Quantitative.survival_adjustment import (
    SURVIVAL_DEFAULTS,
    compute_survival_columns,
    liquidation_value_ps,
    p_survival_priority_cascade,
    survival_adjusted_value,
    survival_position_cap,
    tangible_bv_ex_cash,
)


class TestPSurvivalCascade:
    """Frozen priority order: distress > young ramp > moat > mature, clamp."""

    def test_distress_wins_over_everything(self):
        for years, roic, wacc in [(1.0, 0.40, 0.05), (10.0, 0.40, 0.05), (0.0, 0.0, 0.0)]:
            p, branch, _ = p_survival_priority_cascade(1.2, years, roic, wacc)
            assert branch == "distressed"
            assert p == SURVIVAL_DEFAULTS["survival_distressed"] == 0.40

    def test_young_ramp_linear(self):
        p0, branch, prov = p_survival_priority_cascade(8.0, 0.0, 0.05, 0.10)
        p2, _, _ = p_survival_priority_cascade(8.0, 2.0, 0.05, 0.10)
        p4, _, _ = p_survival_priority_cascade(8.0, 4.0, 0.05, 0.10)
        assert branch == "young_ramp"
        assert p0 == pytest.approx(0.60)
        assert p2 == pytest.approx(0.70)
        assert p4 == pytest.approx(0.80)

    def test_young_ramp_beats_moat(self):
        # 2yr public with huge ROIC spread still ramps (priority order)
        p, branch, _ = p_survival_priority_cascade(8.0, 2.0, 0.40, 0.05)
        assert branch == "young_ramp" and p == pytest.approx(0.70)

    def test_moat_threshold_exact_gap(self):
        # gap == exactly 5% does NOT qualify (strictly greater)
        _, branch, _ = p_survival_priority_cascade(8.0, 12.0, 0.15, 0.10)
        assert branch == "mature"
        p, branch, _ = p_survival_priority_cascade(8.0, 12.0, 0.1501, 0.10)
        assert branch == "moat" and p == 0.95

    def test_mature_base(self):
        p, branch, _ = p_survival_priority_cascade(8.0, 12.0, 0.08, 0.10)
        assert branch == "mature" and p == 0.90

    def test_clamp_bounds(self):
        lo, hi = SURVIVAL_DEFAULTS["survival_clamp"]
        for icr, yrs, roic, wacc in [(0.5, 50, 0.0, 0.1), (20, 0.001, 0.5, 0.05),
                                     (20, 100, 0.0, 0.1)]:
            p, _, _ = p_survival_priority_cascade(icr, yrs, roic, wacc)
            assert lo <= p <= hi

    def test_missing_inputs_are_neutral(self):
        # NaN icr -> healthy; NaN years -> mature (no young ramp); NaN roic -> no moat
        p, branch, _ = p_survival_priority_cascade(np.nan, 12.0, 0.30, 0.10)
        assert branch == "moat"
        p, branch, _ = p_survival_priority_cascade(8.0, np.nan, 0.0, 0.10)
        assert branch == "mature" and p == 0.90
        p, branch, _ = p_survival_priority_cascade(8.0, 12.0, np.nan, 0.10)
        assert branch == "mature"
        p, branch, _ = p_survival_priority_cascade(8.0, 12.0, 0.30, np.nan)
        assert branch == "mature"  # wacc NaN -> gap cannot be computed


class TestLiquidation:
    """C6.3: equity basis, no cash double-count; negative LV clamped; schema >= 0."""

    def test_formula(self):
        eq, intg, cash, debt, shares = 1000.0, 100.0, 200.0, 300.0, 100.0
        lv, prov = liquidation_value_ps(eq, intg, cash, debt, shares, 0.70)
        expected = (0.70 * (1000 - 100 - 200) + (200 - 300)) / 100  # 3.9
        assert lv == pytest.approx(expected)
        assert not prov["clamped_negative"]

    def test_no_cash_double_count(self):
        # C6.3: cash enters LV only via net_cash at PAR — never again at the
        # 0.70 haircut.  All-equity-cash company: LV_eq = net_cash exactly.
        lv, _ = liquidation_value_ps(100.0, 0.0, 100.0, 0.0, 10.0, 0.70)
        assert lv == pytest.approx(10.0)          # 100 net_cash / 10 shares
        assert lv != pytest.approx(7.0)           # 0.70 on cash would be double-count
        # tangible eq: 0.7 x (500 - 0 - 100) + (100 - 50) = 330 eq -> 33.0 / sh
        lv3, _ = liquidation_value_ps(500.0, 0.0, 100.0, 50.0, 10.0, 0.70)
        assert lv3 == pytest.approx(33.0)

    def test_negative_lv_clamped_with_flag(self):
        lv, prov = liquidation_value_ps(50.0, 100.0, 0.0, 500.0, 10.0, 0.70)
        assert lv == 0.0 and prov["clamped_negative"] is True

    def test_missing_equity_returns_none(self):
        lv, prov = liquidation_value_ps(np.nan, 0.0, 100.0, 0.0, 10.0, 0.70)
        assert lv is None

    def test_missing_shares_returns_none_per_share(self):
        lv, prov = liquidation_value_ps(1000.0, 0.0, 100.0, 0.0, None, 0.70)
        assert lv is None and prov.get("per_share") is False

    def test_tangible_bv_ex_cash(self):
        assert tangible_bv_ex_cash(1000.0, 200.0, 100.0) == pytest.approx(700.0)
        assert tangible_bv_ex_cash(1000.0, np.nan, 100.0) == pytest.approx(900.0)  # no intangibles
        assert tangible_bv_ex_cash(np.nan, 0.0, 100.0) is None


class TestSurvivalAdjustedValue:
    """SAV = p x GC + (1-p) x LV; schema invariant SAV <= GC x 1.0001."""

    def test_blend(self):
        sav, _ = survival_adjusted_value(10.0, 4.0, 0.8)
        assert sav == pytest.approx(0.8 * 10.0 + 0.2 * 4.0)

    def test_invariant_when_lv_exceeds_gc(self):
        sav, prov = survival_adjusted_value(10.0, 30.0, 0.5)
        assert sav <= 10.0 * 1.0001
        assert prov.get("liquidation_value_capped") is True
        assert prov.get("liquidation_raw_ps") == pytest.approx(30.0)

    def test_lv_cap_disabled_keeps_raw(self):
        # without the leg cap the blend (20.0) violates the schema invariant ->
        # the numeric-noise invariant clamp still holds SAV at GC x 1.0001
        sav, prov = survival_adjusted_value(10.0, 30.0, 0.5, lv_cap=False)
        assert sav == pytest.approx(10.0 * 1.0001)
        assert "liquidation_value_capped" not in prov
        assert prov.get("invariant_clamped") is True

    def test_missing_lv_contributes_nothing(self):
        # missing LV -> liquidation leg adds nothing: blend = GC exactly
        sav, prov = survival_adjusted_value(10.0, None, 0.8)
        assert sav == pytest.approx(10.0)
        assert prov.get("liquidation_missing") is True

    def test_missing_gc_returns_none(self):
        sav, _ = survival_adjusted_value(None, 5.0, 0.5)
        assert sav is None


class TestSurvivalPositionCap:
    """Plan §4: p < 0.70 -> max weight 0.5%."""

    def test_low_p_caps(self):
        assert survival_position_cap(0.60, 0.010) == pytest.approx(0.005)
        assert survival_position_cap(0.60, 0.005) == pytest.approx(0.005)
        assert survival_position_cap(0.60, None) == pytest.approx(0.005)

    def test_high_p_keeps_base(self):
        assert survival_position_cap(0.90, 0.010) == pytest.approx(0.010)
        assert survival_position_cap(0.90, None) == pytest.approx(1.0)

    def test_boundary_0_70(self):
        assert survival_position_cap(0.70, 0.010) == pytest.approx(0.010)  # not strictly below


class TestFrameHelper:
    """compute_survival_columns row-anchored pass (PIT inputs already joined)."""

    def test_frame_columns(self):
        df = pd.DataFrame({
            "ticker": ["X", "X", "X"],
            "fiscal_end_date": pd.to_datetime(["2022-12-31", "2023-03-31", "2023-06-30"]),
            "filed_date": pd.to_datetime(["2023-02-15", "2023-05-10", "2023-08-10"]),
            "stockholders_equity": [500.0, 510.0, 520.0],
            "cash": [80.0, 85.0, 90.0],
            "total_debt": [120.0, 125.0, 130.0],
            "interest_coverage": [3.0, 3.2, 9.0],
            "roic": [0.06, 0.07, 0.11],
            "wacc_used": [0.09, 0.09, 0.09],
            "years_public": [2.0, 2.5, 3.0],
            "going_concern_iv_ps": [8.0, 8.5, 9.0],
            "shares_outstanding": [10.0, 10.0, 10.0],
        })
        out = compute_survival_columns(df)
        for c in ("p_survival", "liquidation_value_ps", "survival_adjusted_iv",
                  "liquidation_value_capped"):
            assert c in out.columns
        # young ramp -> 0.60 + 0.05 x years
        assert out.loc[0, "p_survival"] == pytest.approx(0.70)
        # survival-adjusted never exceeds GC x 1.0001
        assert (out["survival_adjusted_iv"] <= out["going_concern_iv_ps"] * 1.0001).all()
        # liquidation per share = (0.7 x (eq - cash) + cash - debt)/shares
        lv0_expect = (0.7 * (500 - 80) + (80 - 120)) / 10.0
        assert out.loc[0, "liquidation_value_ps"] == pytest.approx(lv0_expect)
        # third row: ROIC 0.11 - WACC 0.09 = 0.02 -> no moat; young ramp continues
        assert out.loc[2, "p_survival"] == pytest.approx(0.75)

    def test_frame_handles_missing_equity(self):
        df = pd.DataFrame({
            "stockholders_equity": [np.nan], "cash": [10.0], "total_debt": [5.0],
            "interest_coverage": [8.0], "roic": [0.08], "wacc_used": [0.09],
            "years_public": [12.0], "going_concern_iv_ps": [5.0],
            "shares_outstanding": [1.0],
        })
        out = compute_survival_columns(df)
        assert pd.isna(out.loc[0, "liquidation_value_ps"])
        assert out.loc[0, "survival_adjusted_iv"] == pytest.approx(5.0)