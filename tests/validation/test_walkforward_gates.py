"""
test_walkforward_gates.py — Phase 4 walk-forward certification unit tests.

Covers:
  * the v7 REMOVAL contract for the Phase-4 3-arm family (see
    ``test_p4_walkforward_family_is_removed_not_faked``): the composite-DSR
    freeze deleted the 16-gate Holm walkforward machinery, and the estate
    must not resurrect or fake it.
  * hmm_gates.run_walkforward_ari — per-fold ARI contract, degenerate
    single-state folds scored honestly, fail rule >=2 folds below 0.70.
  * wacc_gates coverage extension at report level (gate 12 contract).
  * provenance_audit.check_backtest_chain — sidecar chain + consolidated
    parquet verification, tamper detection.
  * valuation_schema walk-forward schema contract (build/validate).

Pure-function tests on synthetic in-memory frames; writes only under
pytest's tmp_path.
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from ingestion.harness import gate_engine
from ingestion import valuation_schema
from validation import hmm_gates, provenance_audit, wacc_gates


def _write_phase8_cfg(tmp_path) -> str:
    """Minimal frozen phase8 config with the backtest family params."""
    cfg = {
        "phase8": {
            "backtest": {
                "walkforward_folds": 4,
                "holm_bonferroni_n": 16,
                "holm_bonferroni_alpha": 0.003125,
                "probation_pass_band": [0.40, 0.60],
                "ari_fail_rule": ">=2_folds",
                "wacc_coverage": "100%_tradable",
            }
        }
    }
    p = tmp_path / "pre_registration_phase8.yaml"
    p.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------------------
# Phase-4 3-arm walkforward family: REMOVED by the v7 freeze
# ---------------------------------------------------------------------------

def test_p4_walkforward_family_is_removed_not_faked():
    """
    v7 froze the single composite-DSR decision (no gate hierarchy), which
    deleted the Phase-4 3-arm walkforward family: ``run_walkforward_gates``,
    the arm-source resolver ``_resolve_arm``, the Holm 16-gate step-down
    (``holm_bonferroni`` / ``P4_N_GATES`` / ``P4_ALPHA_FAMILY``) and the
    ``_p4_spy_monthly`` fixture shim no longer exist.

    These removals are load-bearing, not tidy-up: the estate must NOT
    fabricate an arm-comparison verdict when the machinery is gone — the
    honest state is 'no such gate'.  If any of these attributes ever return,
    this test fails and the return must be a deliberate, logged contract
    change rather than a silent revival.
    """
    import ingestion.harness.gate_engine as ge

    for gone in ("run_walkforward_gates", "_resolve_arm",
                 "holm_bonferroni", "P4_N_GATES", "P4_ALPHA_FAMILY",
                 "_p4_spy_monthly"):
        assert not hasattr(ge, gone), (
            f"v7 removed {gone}; its return must be a logged contract change."
        )

    # The v7 engine answers with the composite DSR gate instead.
    assert hasattr(ge, "CompositeGateEngine")
    assert ge.DSR_THRESHOLD == 0.95
    assert ge.DSR_N_TRIALS == 513


# ---------------------------------------------------------------------------
# HMM walk-forward ARI (gate 11)
# ---------------------------------------------------------------------------

def _make_fold_dir(tmp_path, k, n=60, label="EXPANSION") -> Path:
    """Materializes a walk-forward fold dir with regime_states.parquet."""
    fd = tmp_path / f"fold{k}"
    fd.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({
        "obs_date": pd.bdate_range("2022-01-01", periods=n, freq="W-FRI"),
        "detected_state": [label] * n,
    }).to_parquet(fd / "regime_states.parquet", engine="pyarrow", index=False)
    return fd


def test_walkforward_ari_fail_rule_two_of_four(tmp_path):
    """Frozen fail rule: >=2 of 4 folds below 0.70 => FAIL.

    Deterministic scheme: folded states are single-label (degenerate) so a
    refit that is also single-state scores 1.0 and a mixed refit scores 0.0.
    Seeds 20260921/20260923 (odd) yield mixed refits (bad), even seeds
    (20260922/20260924) yield single-state refits (good) => exactly 2 below.
    """
    cfgs = _write_phase8_cfg(tmp_path)
    for k in range(1, 5):
        _make_fold_dir(tmp_path, k)

    import importlib
    v3 = importlib.import_module("validation.hmm_gates_v3")

    good = {
        "BAA10Y_spread_bps": pd.Series(150.0, index=pd.date_range("2021-01-01", "2024-12-31")),
        "credit_diff_bps_BAA_minus_AAA": pd.Series(90.0, index=pd.date_range("2021-01-01", "2024-12-31")),
        "DGS10_10Y2Y_slope_bps": pd.Series(-12.0, index=pd.date_range("2021-01-01", "2024-12-31")),
    }

    orig_load = v3.load_fred_observables
    orig_fit = v3._fit_gaussian_hmm
    try:
        v3.load_fred_observables = lambda: good

        def fake_fit(obs, k=3, max_iter=60, seed=0):
            n = obs.shape[0]
            odd = int(seed) % 2 == 1        # seeds 20260921/23 -> mixed
            path = np.zeros(n, dtype=int)
            if odd:
                path[np.arange(n) % 2 == 1] = 1
            return {"path": path,
                    "means": np.array([[0.0, 0.0, 0.0],
                                       [5.0, 5.0, 5.0],
                                       [10.0, 10.0, 10.0]]),
                    "vars": np.ones((3, 3)), "transitions": None,
                    "log_likelihood": 0.0}

        v3._fit_gaussian_hmm = fake_fit
        res = hmm_gates.run_walkforward_ari(
            fold_root=str(tmp_path / "fold{k}"),
            seed=20260920, config_path=cfgs)
    finally:
        v3.load_fred_observables = orig_load
        v3._fit_gaussian_hmm = orig_fit

    assert res["name"] == "walkforward_ari"
    assert res["status"] == "evaluated"
    assert res["mean_ari"] == pytest.approx(0.5, abs=0.01)
    assert res["n_folds_below_bar"] == 2
    assert res["fail_rule"] == ">=2_folds below 0.7"
    assert res["passed"] is False


def test_walkforward_ari_all_matching_single_state(tmp_path):
    """All four folds single-state and refit single-state => ARI 1.0, PASS."""
    cfgs = _write_phase8_cfg(tmp_path)
    for k in range(1, 5):
        _make_fold_dir(tmp_path, k)

    import importlib
    v3 = importlib.import_module("validation.hmm_gates_v3")

    good = {
        "BAA10Y_spread_bps": pd.Series(150.0, index=pd.date_range("2021-01-01", "2024-12-31")),
        "credit_diff_bps_BAA_minus_AAA": pd.Series(90.0, index=pd.date_range("2021-01-01", "2024-12-31")),
        "DGS10_10Y2Y_slope_bps": pd.Series(-12.0, index=pd.date_range("2021-01-01", "2024-12-31")),
    }

    orig_load = v3.load_fred_observables
    orig_fit = v3._fit_gaussian_hmm
    try:
        v3.load_fred_observables = lambda: good

        def fake_fit(obs, k=3, max_iter=60, seed=0):
            return {"path": np.zeros(obs.shape[0], dtype=int),
                    "means": np.array([[0.0, 0.0, 0.0],
                                       [5.0, 5.0, 5.0],
                                       [10.0, 10.0, 10.0]]),
                    "vars": np.ones((3, 3)), "transitions": None,
                    "log_likelihood": 0.0}

        v3._fit_gaussian_hmm = fake_fit
        res = hmm_gates.run_walkforward_ari(
            fold_root=str(tmp_path / "fold{k}"),
            seed=20260920, config_path=cfgs)
    finally:
        v3.load_fred_observables = orig_load
        v3._fit_gaussian_hmm = orig_fit

    assert res["status"] == "evaluated"
    assert res["n_folds_below_bar"] == 0
    assert res["passed"] is True
    assert res["n_degenerate_folds"] == 4


def test_walkforward_ari_missing_fold_reported_honestly(tmp_path):
    """Missing regime_states.parquet -> per-fold reason, evaluator never lies."""
    cfgs = _write_phase8_cfg(tmp_path)
    _make_fold_dir(tmp_path, 1)
    # fold 2 never materialized

    import importlib
    v3 = importlib.import_module("validation.hmm_gates_v3")
    good = {
        "BAA10Y_spread_bps": pd.Series(150.0, index=pd.date_range("2021-01-01", "2024-12-31")),
        "credit_diff_bps_BAA_minus_AAA": pd.Series(90.0, index=pd.date_range("2021-01-01", "2024-12-31")),
        "DGS10_10Y2Y_slope_bps": pd.Series(-12.0, index=pd.date_range("2021-01-01", "2024-12-31")),
    }
    orig_load = v3.load_fred_observables
    orig_fit = v3._fit_gaussian_hmm
    try:
        v3.load_fred_observables = lambda: good
        v3._fit_gaussian_hmm = lambda obs, k=3, max_iter=60, seed=0: {
            "path": np.zeros(obs.shape[0], dtype=int),
            "means": np.zeros((3, 3)), "vars": np.ones((3, 3)),
            "transitions": None, "log_likelihood": 0.0}
        res = hmm_gates.run_walkforward_ari(
            fold_root=str(tmp_path / "fold{k}"),
            seed=20260920, config_path=cfgs)
    finally:
        v3.load_fred_observables = orig_load
        v3._fit_gaussian_hmm = orig_fit

    assert res["per_fold"]["fold2"]["reason"] == "missing_regime_states"
    assert res["per_fold_ari"][1] is None        # fold 2 slot honest None
    assert res["n_folds_below_bar"] == 0
    # frozen rule counts folds *below bar*; a missing fold is exposed in
    # per_fold but cannot fabricate an ARI — factual, transparent result
    assert res["passed"] is True
    # nothing hidden: the report must surface the missing fold explicitly
    assert "missing_regime_states" in json.dumps(res["per_fold"])
    assert res["status"] == "evaluated"


# ---------------------------------------------------------------------------
# WACC coverage extension (gate 12)
# ---------------------------------------------------------------------------

def test_wacc_report_surfaces_coverage_fraction():
    p = pd.DataFrame({
        "asof_date": pd.to_datetime(["2026-09-01"] * 4),
        "ticker": [f"T{i}" for i in range(4)],
        "ke_v2": [0.09, 0.10, 0.11, 0.08],
        "wacc_v2": [0.09, 0.10, 0.11, 0.08],
        "erp_blend": [0.04] * 4,
        "beta_source": ["bloomberg_60m"] * 4,
        "retrieval_ts": pd.to_datetime(["2026-09-02T00:00:00Z"] * 4),
    })
    report = wacc_gates.run_gates(p, pd.DataFrame(), pd.DataFrame())
    assert "coverage_fraction" in report
    assert report["coverage_fraction"] == pytest.approx(1.0)
    assert report["coverage_tradable_rows"] == 4


def test_wacc_coverage_missing_panel_fails_honestly():
    """No panel: coverage gate SKIPs and the report-level coverage_fraction
    is None — the engine must NOT bind it as a pass (honest FAIL)."""
    report = wacc_gates.run_gates(pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
    g = report["gates"]["coverage"]
    assert g["status"] == "SKIP"
    assert g["metric_value"] is None
    assert report["coverage_fraction"] is None
    # engine binding contract for gate 12 (100%_tradable)
    engine_passed = bool(report["coverage_fraction"] is not None
                         and report["coverage_fraction"] >= 1.0 - 1e-12)
    assert engine_passed is False


# ---------------------------------------------------------------------------
# Provenance backtest chain (gate 16)
# ---------------------------------------------------------------------------

def _write_folder(fd, artifacts, run_id="20260919-hybrid_fractal_v1-fold1"):
    fd.mkdir(parents=True, exist_ok=True)
    listing = {name: provenance_audit.sha256_file(path) for name, path in artifacts.items()}
    (fd / ".provenance.json").write_text(
        json.dumps({"run_id": run_id, "files": listing}), encoding="utf-8")
    for name, path in artifacts.items():
        own = {"sha256": provenance_audit.sha256_file(path),
               "source": f"test:{name}", "pit_timestamp_column": "date"}
        (fd / f"{name}.provenance.json").write_text(
            json.dumps(own), encoding="utf-8")


def test_backtest_chain_valid(tmp_path):
    backtests = tmp_path / "backtests"
    fold = backtests / "20260919-hybrid_fractal_v1-fold1"
    rp = fold / "returns.parquet"
    _write_folder(fold, {})                       # create dirs first
    pd.DataFrame({"date": ["2022-01-01"], "period_return": [0.01]}).to_parquet(
        rp, engine="pyarrow", index=False)
    _write_folder(fold, {"returns.parquet": rp})
    cons = backtests / "walkforward_hybrid_fractal_v1.parquet"
    pd.DataFrame({"date": ["2022-01-01"], "fold": [1]}).to_parquet(
        cons, engine="pyarrow", index=False)
    (Path(str(cons) + ".provenance.json")).write_text(
        json.dumps({"sha256": provenance_audit.sha256_file(cons)}), encoding="utf-8")
    chain = provenance_audit.check_backtest_chain(tmp_path, consolidated=cons,
                                                  fold_root=backtests)
    assert chain["valid"] is True
    assert chain["folds_verified"] == ["20260919-hybrid_fractal_v1-fold1"]
    assert chain["violations"] == []


def test_backtest_chain_detects_tamper(tmp_path):
    backtests = tmp_path / "backtests"
    fold = backtests / "20260919-hybrid_fractal_v1-fold1"
    rp = fold / "returns.parquet"
    _write_folder(fold, {})
    pd.DataFrame({"date": ["2022-01-01"], "period_return": [0.01]}).to_parquet(
        rp, engine="pyarrow", index=False)
    _write_folder(fold, {"returns.parquet": rp})
    # tamper: mutate the parquet bytes after the listing was written
    with open(rp, "rb") as fh:
        data = bytearray(fh.read())
    data[3] ^= 0xFF
    with open(rp, "wb") as fh:
        fh.write(bytes(data))
    chain = provenance_audit.check_backtest_chain(tmp_path, fold_root=backtests,
                                                  consolidated=backtests / "missing.parquet")
    assert chain["valid"] is False
    assert any("sha256 mismatch" in v for v in chain["violations"])


def test_backtest_chain_missing_consolidated(tmp_path):
    backtests = tmp_path / "backtests"
    backtests.mkdir(parents=True)
    chain = provenance_audit.check_backtest_chain(tmp_path, fold_root=backtests,
                                                  consolidated=backtests / "missing.parquet")
    assert chain["valid"] is False
    assert any("missing" in v for v in chain["violations"])


# ---------------------------------------------------------------------------
# Valuation schema walk-forward contract (Task 5)
# ---------------------------------------------------------------------------

def test_walkforward_schema_valid_frame():
    wf = valuation_schema.empty_walkforward_frame(4)
    wf["date"] = pd.to_datetime(["2022-07-01", "2022-07-08", "2022-07-15", "2022-07-22"])
    wf["stratum"] = ["Fold_1"] * 4
    wf["fold"] = [1, 1, 1, 1]
    wf["period_return"] = [0.01, -0.02, 0.015, 0.005]
    wf["portfolio_value"] = [1e6, 1.01e6, 0.99e6, 1.005e6]
    wf["n_positions"] = [40] * 4
    wf["regime_state"] = ["EXPANSION", "EXPANSION", "SHOCK", "SHOCK"]
    wf["qcs_score"] = [0.6] * 4
    wf["omega_stress"] = [1.0, 1.0, 1.2, 1.2]
    assert valuation_schema.validate_walkforward(wf) == []


def test_walkforward_schema_rejects_bad_enum_and_bounds():
    wf = valuation_schema.empty_walkforward_frame(4)
    wf["date"] = pd.to_datetime(["2022-07-01", "2022-07-08", "2022-07-15", "2022-07-22"])
    wf["stratum"] = ["Fold_1"] * 4
    wf["fold"] = [1, 1, 1, 9]                     # fold outside 1..4
    wf["period_return"] = [0.01, None, 0.015, 0.005]
    wf["regime_state"] = ["EXPANSION", "BANANA", "SHOCK", "SHOCK"]
    wf["qcs_score"] = [0.6, 0.6, 1.7, 0.6]         # > 1.0
    wf["omega_stress"] = [1.0, 1.0, 4.0, 1.2]      # > 3.0
    viol = valuation_schema.validate_walkforward(wf)
    joined = "\n".join(viol)
    assert any("fold outside" in v for v in viol), joined
    assert any("regime_state" in v for v in viol), joined
    assert any("qcs_score" in v for v in viol), joined
    assert any("omega_stress" in v for v in viol), joined
    assert any("period_return" in v for v in viol), joined


def test_build_walkforward_parquet_contract(tmp_path):
    """build_walkforward_parquet assembles a schema-clean frame."""
    fd = tmp_path / "fold1"
    fd.mkdir(parents=True)
    pd.DataFrame({
        "date": pd.to_datetime(["2022-07-01", "2022-07-08"]),
        "period_return": [0.01, -0.005],
        "portfolio_value": [1e6, 1.01e6],
        "cash": [0.2e6, 0.21e6],
        "equity_value": [0.8e6, 0.8e6],
        "n_positions": [40, 41],
    }).to_parquet(fd / "returns.parquet", engine="pyarrow", index=False)
    pd.DataFrame({
        "obs_date": pd.to_datetime(["2022-07-01", "2022-07-08"]),
        "detected_state": ["EXPANSION", "SHOCK"],
        "model_hash": ["sticky_hmm_v3_frozen"] * 2,
    }).to_parquet(fd / "regime_states.parquet", engine="pyarrow", index=False)
    frame = valuation_schema.build_walkforward_parquet(fd, 1, out_path=None)
    assert "regime_state" in frame.columns
    assert frame["regime_state"].tolist() == ["EXPANSION", "SHOCK"]
    assert "period_return" in frame.columns
    assert valuation_schema.validate_walkforward(frame) == []