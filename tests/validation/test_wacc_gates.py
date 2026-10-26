"""
test_wacc_gates.py — unit tests for validation/wacc_gates.py (Phase 3 WACC gates).

Pure-function tests on synthetic frames; no disk dependencies.  Mirrors the
frozen thresholds from config/pre_registration_phase8.yaml:
    coverage 100% tradable, ke dispersion <= 50bps, ke regime shift >= 200bps,
    erp [0.5%, 8%], total_beta <= 5.0 & rho >= 0.25, spread [63, 1900]bps
    (distressed floor 8.85%), omega oracle [1.0, 3.0] with EXT-01 = 1.675.
"""

import numpy as np
import pandas as pd
import pytest

from validation import wacc_gates


def _panel(ke):
    n = len(ke)
    return pd.DataFrame({
        "asof_date": pd.to_datetime(["2026-09-01"] * n),
        "ticker": [f"T{i}" for i in range(n)],
        "ke_v2": ke,
        "wacc_v2": np.linspace(0.08, 0.10, n),
        "erp_blend": np.linspace(0.04, 0.05, n),
        "iv_nowcast": np.linspace(0.20, 0.30, n),
        "beta_source": ["bloomberg_60m"] * n,
        "retrieval_ts": pd.to_datetime(["2026-09-02T00:00:00Z"] * n),
    })


# --- Gate 1: coverage -------------------------------------------------------

def test_coverage_gate_pass_full():
    p = _panel(ke=[0.09, 0.10, 0.11, 0.08])
    res = wacc_gates.coverage_gate(p)
    assert res["status"] == "PASS"
    assert res["metric_value"] == pytest.approx(1.0)


def test_coverage_gate_fail_partial():
    p = _panel(ke=[0.09, 0.10, 0.11, 0.08])
    p.loc[2, "ke_v2"] = np.nan  # incomplete row
    res = wacc_gates.coverage_gate(p)
    assert res["status"] == "FAIL"
    assert res["metric_value"] == pytest.approx(0.75)
    assert res["rows_complete"] == 3


def test_coverage_gate_tradable_universe():
    p = _panel(ke=[0.09, 0.10, 0.11])
    trad = pd.DataFrame({
        "asof_date": pd.to_datetime(["2026-09-01"] * 3),
        "ticker": ["T0", "T1", "T2"],
        "price_stale_flag": [False, False, False],
        "illiquid_flag": [False, False, False],
    })
    assert wacc_gates.coverage_gate(p, trad)["status"] == "PASS"
    # drop one tradable name not covered by the panel -> 2/3
    p2 = p.iloc[:2].copy()
    res = wacc_gates.coverage_gate(p2, trad)
    assert res["status"] == "FAIL"
    assert res["metric_value"] == pytest.approx(2 / 3)


def test_coverage_gate_skips_empty_panel():
    assert wacc_gates.coverage_gate(pd.DataFrame())["status"] == "SKIP"


# --- Gate 2: ke dispersion ----------------------------------------------------

def test_ke_dispersion_pass_tight():
    p = _panel(ke=[0.08, 0.081, 0.082, 0.083])  # sigma ~1.3bps <= 50bps
    res = wacc_gates.ke_dispersion_gate(p)
    assert res["status"] == "PASS"
    assert res["metric_value"] <= 0.005


def test_ke_dispersion_fail_wide():
    p = _panel(ke=list(np.linspace(0.02, 0.22, 6)))  # sigma ~7.5% > 50bps
    res = wacc_gates.ke_dispersion_gate(p)
    assert res["status"] == "FAIL"
    assert res["metric_value"] > 0.005


def test_ke_dispersion_skips_single_row():
    p = _panel(ke=[0.10])
    assert wacc_gates.ke_dispersion_gate(p)["status"] == "SKIP"


def test_ke_dispersion_skips_constant_ke():
    p = _panel(ke=[0.10, 0.10, 0.10])
    assert wacc_gates.ke_dispersion_gate(p)["status"] == "SKIP"  # std == 0


# --- Gate 3: ke regime shift --------------------------------------------------

def _regime():
    return pd.DataFrame({
        "date": pd.to_datetime(["2026-09-01"] * 2 + ["2026-08-28"] * 2),
        "regime_state": ["SHOCK", "SHOCK", "EXPANSION", "EXPANSION"],
    })


def test_regime_shift_pass_responsive():
    p = pd.DataFrame({
        "asof_date": pd.to_datetime(["2026-09-01"] * 5 + ["2026-08-28"] * 5),
        "ticker": [f"T{i}" for i in range(10)],
        "ke_v2": [0.08, 0.09, 0.10, 0.11, 0.10] + [0.15] * 5,
    })
    res = wacc_gates.regime_ke_shift_gate(p, _regime(), min_obs=5)
    assert res["status"] == "PASS"
    assert res["metric_value"] >= 0.02


def test_regime_shift_fail_frozen_ke():
    p = pd.DataFrame({
        "asof_date": pd.to_datetime(["2026-09-01"] * 5 + ["2026-08-28"] * 5),
        "ticker": [f"T{i}" for i in range(10)],
        "ke_v2": [0.10] * 10,  # Ke identical across regime -> no response
    })
    res = wacc_gates.regime_ke_shift_gate(p, _regime(), min_obs=5)
    assert res["status"] == "FAIL"
    assert res["reason"].startswith("Ke regime shift")


def test_regime_shift_skips_low_obs():
    p = pd.DataFrame({
        "asof_date": pd.to_datetime(["2026-09-01"] * 2),
        "ticker": ["T0", "T1"],
        "ke_v2": [0.08, 0.15],
    })
    res = wacc_gates.regime_ke_shift_gate(p, _regime(), min_obs=5)
    assert res["status"] == "SKIP"


# --- Gate 4: panel bounds ------------------------------------------------------

def test_panel_bounds_pass():
    p = _panel(ke=[0.09, 0.10])
    assert wacc_gates.panel_bounds_gate(p)["status"] == "PASS"


def test_panel_bounds_fail_erp_iv_beta():
    p = _panel(ke=[0.09, 0.10])
    p.loc[0, "erp_blend"] = 0.09           # > 8% ceiling
    p.loc[1, "iv_nowcast"] = 0.001         # < 1% iv floor
    p.loc[1, "beta_source"] = None         # null coverage field
    res = wacc_gates.panel_bounds_gate(p)
    assert res["status"] == "FAIL"
    assert any("erp_blend" in v for v in res["violations"])
    assert any("iv_nowcast" in v for v in res["violations"])
    assert any("beta_source" in v for v in res["violations"])


def test_panel_bounds_fail_wacc_negative():
    p = _panel(ke=[0.09, 0.10])
    p.loc[0, "wacc_v2"] = -0.02
    assert wacc_gates.panel_bounds_gate(p)["status"] == "FAIL"


# --- Gate 5: beta ceiling ------------------------------------------------------

def _val(total_beta=None, rho=None):
    return pd.DataFrame({
        "total_beta": total_beta or [1.5, 3.0, 4.9],
        "rho_market_60m": rho or [0.6, 0.5, 0.31],
    })


def test_beta_ceiling_pass():
    assert wacc_gates.beta_ceiling_gate(_val())["status"] == "PASS"


def test_beta_ceiling_fail_over_beta():
    res = wacc_gates.beta_ceiling_gate(_val(total_beta=[1.5, 6.0, 4.9]))
    assert res["status"] == "FAIL"
    assert "total_beta > 5.0" in res["reason"]


def test_beta_ceiling_fail_low_rho():
    res = wacc_gates.beta_ceiling_gate(_val(rho=[0.6, 0.1, 0.31]))
    assert res["status"] == "FAIL"
    assert "rho_market_60m < rho_floor" in res["reason"]


def test_beta_ceiling_skips_no_cols():
    assert wacc_gates.beta_ceiling_gate(pd.DataFrame({"x": [1]}))["status"] == "SKIP"


# --- Gate 6: spread bounds ------------------------------------------------------

def test_spread_bounds_pass():
    v = pd.DataFrame({"credit_spread_bps": [100.0, 800.0, 1500.0]})
    assert wacc_gates.spread_bounds_gate(v)["status"] == "PASS"


def test_spread_bounds_fail_out_of_band():
    v = pd.DataFrame({"credit_spread_bps": [50.0, 800.0, 2500.0]})
    res = wacc_gates.spread_bounds_gate(v)
    assert res["status"] == "FAIL"
    assert "spread outside" in res["reason"]


def test_spread_bounds_distressed_floor():
    v = pd.DataFrame({
        "credit_spread_bps": [500.0],      # below 885 distressed floor
        "ebit": [-5.0],
        "synthetic_rating": ["CCC"],
    })
    res = wacc_gates.spread_bounds_gate(v)
    assert res["status"] == "FAIL"
    assert any("distressed" in x for x in res["violations"])


def test_spread_bounds_distressed_ok_when_high():
    v = pd.DataFrame({
        "credit_spread_bps": [1200.0],
        "ebit": [-5.0],
        "synthetic_rating": ["CC"],
    })
    assert wacc_gates.spread_bounds_gate(v)["status"] == "PASS"


# --- Gate 7: omega oracle -------------------------------------------------------

def test_omega_oracle_frozen_cases():
    res = wacc_gates.omega_oracle_gate()
    assert res["status"] == "PASS"
    by_case = {c["case"]: c["omega"] for c in res["cases"]}
    assert by_case["ext01_frozen"] == pytest.approx(1.675, abs=1e-9)
    assert by_case["neutral_healthy"] == pytest.approx(1.0, abs=1e-9)
    assert by_case["extreme_burn"] == pytest.approx(3.0, abs=1e-9)


def test_omega_range_guaranteed():
    res = wacc_gates.omega_oracle_gate()
    for c in res["cases"]:
        assert 1.0 - 1e-12 <= c["omega"] <= 3.0 + 1e-12