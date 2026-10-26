"""
Unit Tests for Three-Arm Harness Runner (Arm A, Arm B, Arm C)
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ingestion.harness.three_arm_runner import (
    COST_DRAG,
    MAX_TURNOVER,
    QUAL_FACTOR_COLS,
    QUANT_FACTOR_COLS,
    RISK_COLS,
    ThreeArmRunner,
    _synthetic_universe,
)


def test_three_arm_runner_execution_and_keys():
    data = _synthetic_universe(n_tickers=70, n_months=24, seed=42)
    runner = ThreeArmRunner(
        universe_T=data["universe"],
        qual_factors_T=data["qual"],
        returns_T=data["rets"],
        top_n=30,
    )
    res = runner.run()
    expected_keys = {
        "arm_a_weights", "arm_b_weights", "arm_c_weights",
        "arm_a_returns", "arm_b_returns", "arm_c_returns",
        "arm_a_returns_net", "arm_b_returns_net", "arm_c_returns_net",
        "arm_a_costs", "arm_b_costs", "arm_c_costs",
        "sector_returns", "sector_provenance",
    }
    assert expected_keys.issubset(set(res.keys()))


def test_weights_sum_to_one_all_arms():
    data = _synthetic_universe(n_tickers=70, n_months=24, seed=42)
    runner = ThreeArmRunner(
        universe_T=data["universe"],
        qual_factors_T=data["qual"],
        returns_T=data["rets"],
        top_n=30,
    )
    res = runner.run()
    for arm in ("a", "b", "c"):
        w = res[f"arm_{arm}_weights"]
        assert not w.empty
        sums = w.sum(axis=1)
        assert np.allclose(sums, 1.0, atol=1e-6)


def test_neutral_fill_for_missing_qual():
    data = _synthetic_universe(n_tickers=50, n_months=12, seed=42)
    qual = data["qual"].copy()
    # Introduce NaN in qual factors
    qual.loc[qual.index % 2 == 0, "mda_tone"] = np.nan

    runner = ThreeArmRunner(universe_T=data["universe"], qual_factors_T=qual)
    snap = runner._merge_snapshot(pd.Timestamp("2020-01-31"))
    comp = runner.qual_composite(snap)
    assert not comp.isna().any()


def test_turnover_capping_30_percent():
    data = _synthetic_universe(n_tickers=70, n_months=24, seed=42)
    runner = ThreeArmRunner(
        universe_T=data["universe"],
        qual_factors_T=data["qual"],
        returns_T=data["rets"],
        max_turnover=0.30,
    )
    res = runner.run()
    for arm in ("a", "b", "c"):
        w = res[f"arm_{arm}_weights"].fillna(0.0)
        for i in range(1, len(w)):
            p = w.iloc[i - 1].to_numpy()
            c = w.iloc[i].to_numpy()
            tov = 0.5 * np.abs(c - p).sum()
            assert tov <= 0.30 + 1e-5


def test_pit_compliance_asof_date_less_than_rebalance():
    data = _synthetic_universe(n_tickers=50, n_months=12, seed=42)
    runner = ThreeArmRunner(universe_T=data["universe"], qual_factors_T=data["qual"])
    res = runner.run()
    schedule = runner.last_run["schedule"]
    for entry in schedule:
        assert entry["as_of_date"] < entry["rebalance_date"]


def test_arm_c_risk_penalty_calculation():
    data = _synthetic_universe(n_tickers=50, n_months=6, seed=42)
    runner = ThreeArmRunner(universe_T=data["universe"])
    snap = runner._merge_snapshot(pd.Timestamp("2020-01-31"))
    pen = runner.risk_penalty(snap, snap["ticker"].tolist())
    assert (pen >= 0.0).all()
    assert (pen <= 1.0).all()


def test_statistical_fingerprint_dispersion():
    data = _synthetic_universe(n_tickers=100, n_months=36, seed=42)
    runner = ThreeArmRunner(
        universe_T=data["universe"],
        qual_factors_T=data["qual"],
        returns_T=data["rets"],
    )
    res = runner.run()
    std_a = res["arm_a_returns"].std()
    std_b = res["arm_b_returns"].std()
    std_c = res["arm_c_returns"].std()
    stds = [std_a, std_b, std_c]

    # Standard deviations between arms show non-zero dispersion
    dispersion = np.std(stds) / np.mean(stds)
    assert dispersion > 0.01


def test_sector_returns_output_structure():
    data = _synthetic_universe(n_tickers=100, n_months=24, seed=42)
    runner = ThreeArmRunner(
        universe_T=data["universe"],
        qual_factors_T=data["qual"],
        returns_T=data["rets"],
    )
    res = runner.run()
    sec_rets = res["sector_returns"]
    assert isinstance(sec_rets, dict)
    assert len(sec_rets) >= 2
    for sector, arm_map in sec_rets.items():
        assert "A" in arm_map
        assert "B" in arm_map
        assert "C" in arm_map


def test_net_returns_deduct_turnover_costs():
    data = _synthetic_universe(n_tickers=70, n_months=24, seed=42)
    runner = ThreeArmRunner(
        universe_T=data["universe"],
        qual_factors_T=data["qual"],
        returns_T=data["rets"],
    )
    res = runner.run()
    for arm in ("a", "b", "c"):
        gross = res[f"arm_{arm}_returns"]
        net = res[f"arm_{arm}_returns_net"]
        assert (net <= gross + 1e-9).all()


def test_arm_a_dual_track_qualified_set():
    data = _synthetic_universe(n_tickers=100, n_months=12, seed=42)
    runner = ThreeArmRunner(universe_T=data["universe"])
    snap = runner._merge_snapshot(pd.Timestamp("2020-01-31"))
    q = runner.screen_quant(snap)
    assert not q.empty
    assert "ticker" in q.columns
    assert len(q) >= 20
