"""Unit tests — validation/spike_guards.py (Phase 8 WS-7, bigpickle-worker)."""

import numpy as np
import pytest

from validation.spike_guards import (
    DATA_GATE_DEFAULTS,
    MAD_DEFAULTS,
    SC_DEFAULTS,
    apply_weight_caps,
    consecutive_flagged_cycles,
    data_gate_state,
    graduation_check,
    mad_bounds,
    mad_filter_series,
    mad_gate_check,
    mad_trigger_rate,
    merged_config,
    sc_constraint_guard,
)

CFG = {"mad_filter": MAD_DEFAULTS, "data_gate": DATA_GATE_DEFAULTS,
       "sc_constraint": SC_DEFAULTS}


class TestMadFilter:
    """C7.1: 90d median + 3x scaled-MAD band; min_obs + MAD=0 guards."""

    def test_single_outlier_replaced_with_median(self):
        s = [10.0, 10.1, 9.9, 10.0, 10.05, 10.0, 55.0, 10.0, 10.1, 9.95]
        replaced, mask, n_out = mad_filter_series(s, window=5, k=3.0, min_observations=4)
        assert n_out == 1
        assert mask[6] is True
        assert replaced[6] == pytest.approx(10.0)   # rolling median
        # irrelevant/clean windows untouched
        assert replaced[0] == pytest.approx(10.0)

    def test_min_obs_guard_no_flags(self):
        _, mask, n = mad_filter_series([10.0, 11.0, 500.0], window=90,
                                       k=3.0, min_observations=30)
        assert n == 0 and not any(mask)

    def test_mad_zero_guard_constant_series(self):
        _, mask, n = mad_filter_series([5.0] * 50, window=10, k=3.0, min_observations=5)
        assert n == 0 and not any(mask)

    def test_mad_zero_guard_single_blip(self):
        # a lone outlier gives MAD=0 in a constant window -> no filter (guard)
        _, mask, n = mad_filter_series([5.0] * 9 + [500.0], window=10,
                                       k=3.0, min_observations=5)
        assert n == 0 and not any(mask)

    def test_mad_bounds_shape_and_symmetry(self):
        rng = np.random.default_rng(0)
        s = (100.0 + rng.normal(0, 1.0, 40)).tolist()
        bounds, ok = mad_bounds(s, window=10, k=3.0, min_observations=5)
        assert len(bounds) == len(s) == len(ok)
        # windows of 10 obs (>= min_observations 5) are populated from i=9 on
        assert all(o is True for o in ok[9:])
        for i in range(10, 40):
            assert bounds[i][0] < 100.0 and bounds[i][1] > 100.0  # median near 100
            assert bounds[i][0] < bounds[i][1]

    def test_downside_offset_spike_near_zero_floor(self):
        # an offset below the band median still flags (band, not level, decides)
        s = [20.0, 20.1, 19.9, 20.0, 20.05, 20.0, 1.0, 20.0, 20.1, 19.95]
        _, mask, n = mad_filter_series(s, window=5, k=3.0, min_observations=4)
        assert n == 1 and mask[6] is True


class TestConsecutiveAndRate:
    """'2 consecutive cycles' audit rule + frozen [5%, 15%] band."""

    def test_consecutive_trailing_run(self):
        assert consecutive_flagged_cycles([False, True, True]) == 2
        assert consecutive_flagged_cycles([True, True, False]) == 0
        assert consecutive_flagged_cycles([True, False, True, True, True]) == 3
        assert consecutive_flagged_cycles([]) == 0

    def test_rate(self):
        assert mad_trigger_rate([True, False, True, False]) == pytest.approx(0.5)
        assert mad_trigger_rate([]) == 0.0
        assert mad_trigger_rate([False, False]) == 0.0

    def test_band_inside(self):
        ok, tr, _ = mad_gate_check([True] + [False] * 9, CFG)   # 10%
        assert ok and tr == pytest.approx(0.10)

    def test_band_low_inert(self):
        ok, tr, r = mad_gate_check([False, False, False], CFG)
        assert not ok and tr == 0.0 and "inert" in r

    def test_band_high_overactive(self):
        ok, _, r = mad_gate_check([True] * 10, CFG)
        assert not ok and "over-active" in r

    def test_band_tail_values(self):
        # 1/10 = 10% in band; 0/10 fail-low; 6/10 = 60% fail-high
        assert mad_gate_check([True] + [False] * 9, CFG)[0]
        assert not mad_gate_check([False] * 10, CFG)[0]
        assert not mad_gate_check([True] * 6 + [False] * 4, CFG)[0]


class TestScConstraintGuard:
    """C7.2/C7.3: formula identity with narrative_valuation; sector map."""

    def test_frozen_sector_debt_ratios(self):
        # Industrial uses the frozen 0.2015
        sc = sc_constraint_guard(200.0, 1.0, 50.0, 0.0, 1000.0, 100.0, "Industrial", CFG)
        assert sc["credit_capacity"] == pytest.approx(0.2015 * 1000.0 - 100.0)
        assert sc["sector_target_debt_ratio_used"] == pytest.approx(0.2015)

    def test_unfunded_flag_and_punishments(self):
        sc = sc_constraint_guard(200.0, 1.0, 50.0, 0.0, 1000.0, 100.0, "Industrial", CFG)
        assert sc["unfunded_growth_flag"] is True
        assert sc["terminal_growth_haircut"] == 0.5
        assert sc["wacc_penalty_bps_applied"] == 200.0
        assert sc["required_reinvestment"] == pytest.approx(200.0)

    def test_funded(self):
        sc = sc_constraint_guard(50.0, 1.0, 50.0, 0.0, 1000.0, 100.0, "Industrial", CFG)
        assert sc["unfunded_growth_flag"] is False
        assert sc["wacc_penalty_bps_applied"] == 0.0

    def test_negative_delta_never_flags(self):
        sc = sc_constraint_guard(-10.0, 1.0, 50.0, 0.0, 1000.0, 100.0, "Industrial", CFG)
        assert sc["unfunded_growth_flag"] is False
        assert sc["required_reinvestment"] is None

    def test_formula_identity_with_narrative_module(self):
        # identical inputs -> identical flag block (single source of truth)
        from Quantitative.narrative_valuation import (
            credit_capacity, unfunded_growth_check,
        )
        cc, ratio = credit_capacity(0.2015, 1000.0, 100.0)
        flag, gap, hc, pen, avail = unfunded_growth_check(200.0, 1.0, 50.0, 0.0, cc)
        sc = sc_constraint_guard(200.0, 1.0, 50.0, 0.0, 1000.0, 100.0, "Industrial", CFG)
        assert sc["unfunded_growth_flag"] == flag
        assert sc["wacc_penalty_bps_applied"] == pen

    def test_unknown_sector_falls_back_default(self):
        sc = sc_constraint_guard(200.0, 1.0, 50.0, 0.0, 1000.0, 100.0, "Mystery", CFG)
        assert sc["sector_target_debt_ratio_used"] == pytest.approx(0.25)


class TestDataGate:
    """QUARANTINE / PROBATION / FULL-with-graduation; cap 0.010 pre-graduation."""

    def test_quarantine(self):
        assert data_gate_state(5, True, CFG) == ("QUARANTINE", 0.005, False)

    def test_probation(self):
        assert data_gate_state(10, True, CFG) == ("PROBATION", 0.010, False)

    def test_full_graduated(self):
        st, cap, unc = data_gate_state(13, True, CFG)
        assert st == "FULL" and unc and cap == 1.0

    def test_full_not_graduated_keeps_probation_cap(self):
        st, cap, unc = data_gate_state(13, False, CFG)
        assert st == "FULL" and not unc and cap == 0.010

    def test_full_boundary_12(self):
        st, cap, unc = data_gate_state(12, True, CFG)
        assert st == "FULL" and unc and cap == 1.0

    def test_graduation_requires_consecutive_trailing(self):
        assert not graduation_check([0.08, 0.11, 0.09], 0.10, 3)
        assert graduation_check([0.11, 0.12, 0.13], 0.10, 3)
        assert not graduation_check([0.15, 0.14, None, 0.13], 0.10, 3)  # missing breaks
        assert graduation_check([0.13, 0.14, 0.15, None], 0.10, 3)      # trailing run

    def test_graduation_missing_head_ok(self):
        assert graduation_check([None, None, 0.13, 0.14, 0.15], 0.10, 3)


class TestWeightCaps:
    """C7.4: caps final, no renormalization; remainder for trade audit."""

    def test_capped_with_remainder(self):
        capped, remainder = apply_weight_caps([0.03, 0.02, 0.01, 0.005],
                                              [0.010, 0.010, 0.005, 0.005])
        assert capped == [0.010, 0.010, 0.005, 0.005]
        assert remainder == pytest.approx(1.0 - 0.030)

    def test_no_redistribution(self):
        # a quarantined 0.50 target capped to 0.005 does NOT spread to others
        capped, remainder = apply_weight_caps([0.50, 0.25, 0.25],
                                              [0.005, 0.010, 0.010])
        assert capped == [0.005, 0.010, 0.010]
        assert remainder == pytest.approx(0.975)

    def test_negative_cap_nukes_name(self):
        capped, _ = apply_weight_caps([0.3, 0.7], [-1.0, 0.010])
        assert capped == [0.0, 0.010]

    def test_no_cap_change_when_under(self):
        capped, remainder = apply_weight_caps([0.005, 0.004], [0.010, 0.010])
        assert capped == [0.005, 0.004]
        assert remainder == pytest.approx(0.991)


class TestMergedConfig:
    """config file merge (phase8 section) keeps defaults for missing keys."""

    def test_config_path_does_not_exist(self):
        m = merged_config({"mad_filter": {}, "data_gate": {}, "sc_constraint": {}})
        assert m["mad"]["window_days"] == 90
        assert m["mad"]["mad_scale"] == pytest.approx(1.4826)

    def test_phase8_merge(self):
        # merged_config takes the phase8 section dict itself
        m = merged_config({
            "mad_filter": {"window_days": 60},
            "data_gate": {"min_quarters_full": 16},
        })
        assert m["mad"]["window_days"] == 60
        assert m["mad"]["mad_multiplier"] == 3.0
        assert m["data_gate"]["min_quarters_full"] == 16
        assert m["data_gate"]["max_weight_quarantine"] == 0.005
        # sc_constraint thresholds survive key-level merge
        assert m["sc"]["unfunded_threshold"] == 1.2