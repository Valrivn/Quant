"""P0B regression tests for validation.composite_score.

These tests pin the correction of the P0B defect:
  * the composite series is MONTHLY (84 rows), never the 1,760-row daily artifact;
  * it is derived from Workstream A's rebalance-period rolling metrics;
  * weights come from the frozen pre-registration, cross-checked against code;
  * the DSR is computed on the monthly composite series with Fold-4 reported.

Run: python -m pytest tests/validation/test_composite_score.py -q
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingestion.harness.gate_engine import COMPONENT_NAMES  # noqa: E402
from ingestion.harness.prereg import MODULE_PREREG  # noqa: E402
from validation import composite_score as cs  # noqa: E402

SEED = 20260926
EXPECTED_ROWS = 84
EXPECTED_WEIGHTS = {
    "sharpe": 0.25,
    "max_drawdown": 0.20,
    "turnover": 0.15,
    "hmm_ari": 0.15,
    "swap_churn": 0.10,
    "tca_bps": 0.10,
    "probation_pass_rate": 0.05,
}


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def monthly_dates(n: int = EXPECTED_ROWS) -> pd.DatetimeIndex:
    return pd.date_range("2018-01-31", periods=n, freq="ME")


def rolling_metrics_fixture(n: int = EXPECTED_ROWS, arm: str = "a",
                            seed: int = SEED) -> pd.DataFrame:
    """A deterministic, contract-valid monthly rolling-metrics frame."""
    rng = np.random.default_rng(seed)
    dates = monthly_dates(n)
    return pd.DataFrame({
        "rebalance_date": dates,
        "arm": [arm] * n,
        "sharpe_rolling_12m": rng.normal(0.8, 0.3, n),
        "max_dd_rolling_12m": -np.abs(rng.normal(0.25, 0.05, n)),
        "turnover_mom": np.clip(rng.normal(0.40, 0.10, n), 0.05, 0.90),
        "ari_concat": np.clip(rng.normal(0.62, 0.08, n), 0.30, 0.90),
        "swaps_annualized": np.clip(rng.normal(9.0, 3.0, n), 0.0, 30.0),
        "tca_bps": np.clip(rng.normal(180.0, 40.0, n), 50.0, 350.0),
        "probation_rate": np.clip(rng.normal(0.50, 0.10, n), 0.0, 1.0),
        "regime": [("risk_on" if i % 2 else "risk_off") for i in range(n)],
        "ann_vol_12m": np.clip(rng.normal(0.15, 0.03, n), 0.02, 0.40),
    })


def daily_defect_fixture() -> pd.DataFrame:
    """Approximates the P0B artifact: 1,760 trading-day rows, near-static."""
    rng = np.random.default_rng(SEED + 1)
    dates = pd.bdate_range("2018-01-02", periods=1760)
    n = len(dates)
    return pd.DataFrame({
        "rebalance_date": dates,
        "arm": ["a"] * n,
        "sharpe_rolling_12m": rng.normal(0.8, 0.02, n),
        "max_dd_rolling_12m": -np.abs(rng.normal(0.25, 0.005, n)),
        "turnover_mom": np.clip(rng.normal(0.40, 0.01, n), 0.05, 0.90),
        "ari_concat": np.clip(rng.normal(0.62, 0.01, n), 0.30, 0.90),
        "swaps_annualized": np.clip(rng.normal(9.0, 0.2, n), 0.0, 30.0),
        "tca_bps": np.clip(rng.normal(180.0, 5.0, n), 50.0, 350.0),
        "probation_rate": np.clip(rng.normal(0.50, 0.01, n), 0.0, 1.0),
        "regime": ["risk_on"] * n,
        "ann_vol_12m": np.full(n, 0.15),
    })


# ---------------------------------------------------------------------------
# Weights / frozen pre-registration
# ---------------------------------------------------------------------------

def test_weights_match_frozen_preregistration():
    assert cs.component_weights() == EXPECTED_WEIGHTS
    assert abs(sum(cs.component_weights().values()) - 1.0) < 1e-9


def test_preregistration_digest_is_intact():
    # The frozen pre-registration must not have been edited.
    assert MODULE_PREREG.integrity_verified is True
    assert MODULE_PREREG.declared_sha256 == (
        "c80a47bf6ae8d1c595f3b5e607c2588478bec0c47c4f0f97eb6c374b277a1685")


def test_weights_are_read_from_yaml_not_hardcoded():
    declared = {c["name"]: c["weight"] for c in MODULE_PREREG.composite_components}
    assert declared == EXPECTED_WEIGHTS
    assert list(cs.component_weights()) == list(declared)


# ---------------------------------------------------------------------------
# Shape / monthly frequency
# ---------------------------------------------------------------------------

def test_composite_score_has_exact_output_contract():
    out = cs.composite_score(rolling_metrics_fixture(), "a")
    assert list(out.columns) == ["rebalance_date", "arm", "composite_score"]
    assert len(out) == EXPECTED_ROWS
    assert (out["arm"] == "a").all()
    assert out["composite_score"].between(0.0, 1.0).all()


def test_composite_is_monthly_not_daily():
    out = cs.composite_score(rolling_metrics_fixture(), "A")
    assert len(out) == EXPECTED_ROWS  # NOT 1760
    spacing = pd.to_datetime(out["rebalance_date"]).diff().dt.days.dropna()
    assert spacing.median() >= cs.MIN_MONTHLY_SPACING_DAYS


def test_composite_is_not_static():
    out = cs.composite_score(rolling_metrics_fixture(), "a")
    assert out["composite_score"].std(ddof=1) > 0.0


def test_component_scores_column_order_and_bounds():
    scores = cs.component_scores(rolling_metrics_fixture(), "a")
    assert list(scores.columns) == ["rebalance_date", *COMPONENT_NAMES]
    for name in COMPONENT_NAMES:
        assert scores[name].between(0.0, 1.0).all()


# ---------------------------------------------------------------------------
# Contract rejection
# ---------------------------------------------------------------------------

def test_rejects_missing_column():
    df = rolling_metrics_fixture().drop(columns=["tca_bps"])
    with pytest.raises(cs.RollingMetricsContractError):
        cs.composite_score(df, "a")


def test_rejects_non_finite_metric():
    df = rolling_metrics_fixture()
    df.loc[df.index[0], "sharpe_rolling_12m"] = np.nan
    with pytest.raises(cs.RollingMetricsContractError):
        cs.composite_score(df, "a")


def test_rejects_duplicate_rebalance_dates():
    df = rolling_metrics_fixture()
    df.loc[df.index[1], "rebalance_date"] = df.loc[df.index[0], "rebalance_date"]
    with pytest.raises(cs.RollingMetricsContractError):
        cs.composite_score(df, "a")


def test_rejects_too_few_rows():
    with pytest.raises(cs.RollingMetricsContractError):
        cs.composite_score(rolling_metrics_fixture(n=12), "a")


def test_rejects_empty_frame():
    with pytest.raises(cs.RollingMetricsContractError):
        cs.composite_score(pd.DataFrame(), "a")


def test_rejects_daily_1760_row_defect():
    with pytest.raises(cs.NonMonthlyFrequencyError):
        cs.composite_score(daily_defect_fixture(), "a")


def test_rejects_arm_mismatch():
    with pytest.raises(cs.RollingMetricsContractError):
        cs.composite_score(rolling_metrics_fixture(arm="b"), "a")


def test_rejects_constant_composite_unless_allowed():
    df = rolling_metrics_fixture()
    for col, value in {
        "sharpe_rolling_12m": 0.8, "max_dd_rolling_12m": -0.25,
        "turnover_mom": 0.40, "ari_concat": 0.62, "swaps_annualized": 9.0,
        "tca_bps": 180.0, "probation_rate": 0.50,
    }.items():
        df[col] = value
    with pytest.raises(cs.StaticCompositeError):
        cs.composite_score(df, "a")
    out = cs.composite_score(df, "a", allow_static=True)
    assert len(out) == EXPECTED_ROWS


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------

def test_sharpe_normalizer_bounds_and_monotonic():
    assert cs._norm_sharpe(0.5) == 0.0
    assert cs._norm_sharpe(1.0) == 1.0
    assert cs._norm_sharpe(2.0) == 1.0
    assert cs._norm_sharpe(0.0) == 0.0
    assert cs._norm_sharpe(0.75) == pytest.approx(0.5)
    seq = [cs._norm_sharpe(v) for v in np.linspace(0.4, 1.1, 20)]
    assert all(b >= a for a, b in zip(seq, seq[1:]))


def test_max_drawdown_normalizer_bounds():
    assert cs._norm_max_drawdown(-0.20) == 1.0
    assert cs._norm_max_drawdown(-0.40) == 0.0
    assert cs._norm_max_drawdown(-0.30) == pytest.approx(0.5)
    assert cs._norm_max_drawdown(-0.10) == 1.0
    assert cs._norm_max_drawdown(-0.50) == 0.0
    # shallower must never score below deeper (direction="shallower")
    shallow = cs._norm_max_drawdown(-0.15)
    deep = cs._norm_max_drawdown(-0.35)
    assert shallow > deep


def test_max_drawdown_direction_agrees_with_component_specs():
    from ingestion.harness.gate_engine import COMPONENT_SPECS
    spec = next(c for c in COMPONENT_SPECS if c["name"] == "max_drawdown")
    assert spec["direction"] == "shallower"
    # our normalizer follows the declared direction
    assert cs._norm_max_drawdown(-0.15) > cs._norm_max_drawdown(-0.35)


def test_turnover_normalizer_bounds():
    assert cs._norm_turnover(0.30) == 1.0
    assert cs._norm_turnover(0.60) == 0.0
    assert cs._norm_turnover(0.45) == pytest.approx(0.5)
    assert cs._norm_turnover(0.10) == 1.0
    assert cs._norm_turnover(0.90) == 0.0


# ---------------------------------------------------------------------------
# DSR evaluation
# ---------------------------------------------------------------------------

def test_evaluate_monthly_composite_reports_dsr_and_fold4():
    res = cs.evaluate_monthly_composite(rolling_metrics_fixture(), "a")
    assert res.n_obs == EXPECTED_ROWS
    assert res.n_trials == 513
    assert res.track_record_days == 2520
    assert res.threshold == 0.95
    assert np.isfinite(res.composite_dsr)
    assert res.fold_4_window == ("2024-01-01", "2024-12-31")
    assert res.fold_4_dsr is not None  # 12 months in Fold-4
    assert isinstance(res.passed, bool)
    assert set(res.component_means) == set(COMPONENT_NAMES)
    payload = res.to_dict()
    assert payload["detail"]["frequency"] == "monthly"
    assert payload["detail"]["no_daily_expansion"] is True


# ---------------------------------------------------------------------------
# Artifact writer
# ---------------------------------------------------------------------------

def test_write_artifacts_into_new_dir(tmp_path):
    res = cs.evaluate_monthly_composite(rolling_metrics_fixture(arm="b"), "b")
    paths = cs.write_composite_artifacts(res, tmp_path / "out")
    for key in ("composite", "provenance", "dsr"):
        assert paths[key].exists()

    assert paths["composite"].name == "composite_score_arm_b.parquet"
    written = pd.read_parquet(paths["composite"])
    assert list(written.columns) == ["rebalance_date", "arm", "composite_score"]
    assert len(written) == EXPECTED_ROWS

    side = json.loads(paths["provenance"].read_text(encoding="utf-8"))
    assert side["row_count"] == EXPECTED_ROWS
    assert side["source"] == "PRODUCER_MARKER_WORKSTREAM_B_MONTHLY_COMPOSITE"
    assert side["sha256"] == hashlib.sha256(paths["composite"].read_bytes()).hexdigest()
    assert side["preregistration_sha256"] == MODULE_PREREG.declared_sha256

    dsr = json.loads(paths["dsr"].read_text(encoding="utf-8"))
    assert dsr["arm"] == "b"
    assert dsr["n_obs"] == EXPECTED_ROWS


def test_writer_never_touches_arm_a_defect(tmp_path):
    arm_a = tmp_path / "arm_a"
    arm_a.mkdir()
    sentinel = arm_a / "composite_score.parquet"
    sentinel.write_bytes(b"untouched-defect")
    res = cs.evaluate_monthly_composite(rolling_metrics_fixture(), "a")
    cs.write_composite_artifacts(res, tmp_path / "composite_scores_20261008")
    assert sentinel.read_bytes() == b"untouched-defect"
