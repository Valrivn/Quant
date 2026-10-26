"""
tests/test_zero_synthetic_cesi_removal.py

Proves the CESI removal and the zero-synthetic guards are real, not cosmetic.

Test strategy: the repo's own FRED cache is ragged (STLFSI2 ends 2022-01-07
while BAMLH0A0HYM2 starts 2023-09-26), so the canonical builder correctly
aborts on it. To prove the builder actually computes correct features when
coverage IS available — rather than just proving it can fail — these tests build
a temporary FRED cache with known, hand-checkable values and assert the
canonical features are derived from them exactly.

Run:  python -m pytest tests/test_zero_synthetic_cesi_removal.py -v
      python tests/test_zero_synthetic_cesi_removal.py     (standalone)
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from Quantitative.canonical_features import (  # noqa: E402
    CANONICAL_FEATURE_SOURCES,
    NON_FRED_JSON_SOURCES,
    build_canonical_features,
    unanticipated_inflation,
)
from Quantitative.contracts.feature_contract import (  # noqa: E402
    CANONICAL_FEATURES,
    FORBIDDEN_SERIES,
    N_CANONICAL_FEATURES,
    assert_canonical_names,
    assert_no_forbidden_series,
)
from Quantitative.data.fred_feed import (  # noqa: E402
    SyntheticDataViolation,
    assert_authentic_pit,
    assert_common_coverage,
    load_fred_series,
    synthetic_fallback_enabled,
)

REPO_CACHED_SERIES = sorted(
    {s for v in CANONICAL_FEATURE_SOURCES.values() for s in v} - set(NON_FRED_JSON_SOURCES)
)


# ═══════════════════════════════════════════════════════════════════════════
# 1. NAME LAYER — CESI is gone from the contract and cannot come back
# ═══════════════════════════════════════════════════════════════════════════

def test_canonical_contract_has_no_cesi():
    assert len(CANONICAL_FEATURES) == N_CANONICAL_FEATURES == 6
    for banned in FORBIDDEN_SERIES:
        assert banned not in CANONICAL_FEATURES


def test_canonical_contract_exact_order():
    assert CANONICAL_FEATURES == [
        "CREDIT_SPREAD_VELOCITY_21D",
        "YIELD_CURVE_10Y2Y",
        "REALIZED_VOLATILITY_21D",
        "FINANCIAL_STRESS_INDEX",
        "REAL_RATE_10Y",
        "UNANTICIPATED_INFLATION",
    ]


def test_assert_no_forbidden_series_rejects_cesi():
    with pytest.raises(AssertionError, match="CESI"):
        assert_no_forbidden_series(["BAA10Y", "CESI"])


def test_assert_canonical_names_rejects_legacy_six():
    """The old v6 feature list must be rejected at the module boundary."""
    legacy = ["BAA10Y", "10Y2Y", "CP_TBill", "RealizedVol21d", "HY_OAS", "CESI"]
    with pytest.raises(AssertionError):
        assert_canonical_names(legacy)


def test_hmm_regime_feature_names_are_canonical():
    from Quantitative.hmm_regime import FEATURE_NAMES, _F_CREDIT

    assert FEATURE_NAMES == CANONICAL_FEATURES
    # The state-label sort key must be index 0. It was hard-coded to 3, which
    # pointed at REALIZED_VOLATILITY_21D and ordered states by volatility.
    assert _F_CREDIT == 0
    assert FEATURE_NAMES[_F_CREDIT] == "CREDIT_SPREAD_VELOCITY_21D"


def test_regime_shield_layer1_features_are_canonical():
    from Quantitative.regime_shield_v3 import LAYER1_FEATURES, TwoLayerRegimeEngine

    assert LAYER1_FEATURES == CANONICAL_FEATURES
    assert TwoLayerRegimeEngine({}).layer1_features == CANONICAL_FEATURES


def test_macro_pca_has_no_cesi():
    from Quantitative.macro_pca import MACRO_INDICATORS, generate_pit_macro_data

    assert "CESI" not in MACRO_INDICATORS
    assert MACRO_INDICATORS == CANONICAL_FEATURES
    cols = generate_pit_macro_data("2020-01-01", "2020-12-31").columns
    assert "CESI" not in cols


def test_no_synthetic_cesi_in_live_source():
    """No module that feeds a pre-registered model may construct a CESI column."""
    import re

    offenders = []
    for rel in (
        "Quantitative/hmm_regime.py",
        "Quantitative/regime_shield_v3.py",
        "Quantitative/macro_pca.py",
        "Quantitative/canonical_features.py",
        "Quantitative/backtest_harness.py",
        "Quantitative/portfolio_orchestrator.py",
        "ingestion/harness/feature_gate.py",
    ):
        src = (REPO / rel).read_text(encoding="utf-8")
        # Strip docstrings/comments: prose ABOUT the removal is required.
        code = "\n".join(
            line for line in src.splitlines()
            if not line.lstrip().startswith("#")
        )
        code = re.sub(r'""".*?"""', "", code, flags=re.S)
        code = re.sub(r"'''.*?'''", "", code, flags=re.S)
        for m in re.finditer(r'["\']CESI["\']', code):
            line_no = code[: m.start()].count("\n") + 1
            offenders.append(f"{rel} (code line ~{line_no}): {m.group(0)}")
    assert not offenders, "CESI string literal in executable code:\n" + "\n".join(offenders)


# ═══════════════════════════════════════════════════════════════════════════
# 2. GUARD LAYER — the requested RuntimeError pattern actually fires
# ═══════════════════════════════════════════════════════════════════════════

def test_synthetic_fallback_enabled_parses_v7_string_value():
    """The v6/v7 flag is the STRING "disabled".

    The naive identity test `config.get("synthetic_fallback", False) is False`
    evaluates `"disabled" is False` -> False and SKIPS the guard. Prove the
    naive form is broken and the shipped form is not.

    Note the naive form must be applied to the INNER mapping
    (`config["optimization"]`), which is the realistic mistake — reading the
    top-level key on a full prereg yields the default and the guard happens to
    fire, which is why the inversion survived review.
    """
    full = {"optimization": {"synthetic_fallback": "disabled"}}
    inner = full["optimization"]

    naive_says_disabled = inner.get("synthetic_fallback", False) is False
    assert naive_says_disabled is False, (
        "premise broken: if this ever becomes True the naive guard would start "
        "working and this test's meaning changes"
    )
    # The shipped parser must reach the same conclusion from either slice.
    assert synthetic_fallback_enabled(full) is False
    assert synthetic_fallback_enabled(inner) is False


def test_synthetic_fallback_enabled_fails_closed():
    assert synthetic_fallback_enabled(None) is False
    assert synthetic_fallback_enabled({}) is False
    assert synthetic_fallback_enabled({"synthetic_fallback": "disabled"}) is False
    assert synthetic_fallback_enabled({"optimization": {"synthetic_fallback": "disabled"}}) is False
    assert synthetic_fallback_enabled({"synthetic_fallback": False}) is False
    assert synthetic_fallback_enabled({"synthetic_fallback": 0}) is False
    assert synthetic_fallback_enabled({"synthetic_fallback": object()}) is False
    # Only an explicit opt-in enables it, at either scope.
    assert synthetic_fallback_enabled({"synthetic_fallback": True}) is True
    assert synthetic_fallback_enabled({"synthetic_fallback": "enabled"}) is True
    assert synthetic_fallback_enabled({"optimization": {"synthetic_fallback": "enabled"}}) is True


def test_guard_raises_on_nan_column():
    df = pd.DataFrame({"CREDIT_SPREAD_VELOCITY_21D": [0.1, np.nan, 0.3]})
    with pytest.raises(RuntimeError, match="Missing authentic PIT data"):
        assert_authentic_pit(df, ["CREDIT_SPREAD_VELOCITY_21D"])


def test_guard_raises_on_all_zero_column():
    """This is the exact shape of the retired synthetic-CESI defect."""
    df = pd.DataFrame({"CREDIT_SPREAD_VELOCITY_21D": [0.0] * 500})
    with pytest.raises(RuntimeError, match="all zeros"):
        assert_authentic_pit(df, ["CREDIT_SPREAD_VELOCITY_21D"])


def test_guard_raises_on_constant_column():
    """A zero-variance column standardises to a dead 0.0 in the HMM."""
    df = pd.DataFrame({"X": [0.42] * 500})
    with pytest.raises(RuntimeError, match="constant"):
        assert_authentic_pit(df, ["X"])


def test_guard_raises_on_empty_frame():
    with pytest.raises(RuntimeError, match="empty"):
        assert_authentic_pit(pd.DataFrame({"X": []}), ["X"])


def test_guard_raises_on_missing_column():
    with pytest.raises(RuntimeError, match="absent"):
        assert_authentic_pit(pd.DataFrame({"A": [1.0, 2.0]}), ["B"])


def test_guard_refuses_when_fallback_enabled():
    df = pd.DataFrame({"X": [0.0] * 5})
    with pytest.raises(RuntimeError, match="synthetic_fallback"):
        assert_authentic_pit(df, ["X"], {"synthetic_fallback": True})


def test_guard_passes_on_real_looking_data():
    df = pd.DataFrame({"X": np.linspace(-1.0, 1.0, 300)})
    assert_authentic_pit(df, ["X"])  # must not raise


def test_harness_feature_gate_blocks_cesi():
    from ingestion.harness.feature_gate import assert_features_admitted

    frame = pd.DataFrame(np.random.default_rng(0).normal(size=(300, 2)), columns=["A", "B"])
    with pytest.raises(AssertionError, match="CESI"):
        assert_features_admitted(
            pd.DataFrame(np.random.default_rng(0).normal(size=(300, 2)), columns=["A", "CESI"])
        )


def test_harness_feature_gate_blocks_enabled_fallback():
    from ingestion.harness.feature_gate import assert_features_admitted

    frame = pd.DataFrame(np.random.default_rng(0).normal(size=(300, 2)), columns=["A", "B"])
    with pytest.raises(RuntimeError, match="ENABLES synthetic_fallback"):
        assert_features_admitted(
            frame, config={"optimization": {"synthetic_fallback": "enabled"}}
        )


def test_harness_feature_gate_admits_real_frame():
    from ingestion.harness.feature_gate import assert_features_admitted

    rng = np.random.default_rng(7)
    frame = pd.DataFrame(rng.normal(size=(300, 6)), columns=list(CANONICAL_FEATURES))
    res = assert_features_admitted(frame, expected=CANONICAL_FEATURES)
    assert res.admitted and res.features == CANONICAL_FEATURES


# ═══════════════════════════════════════════════════════════════════════════
# 3. BUILDER LAYER — real arithmetic on a known cache
# ═══════════════════════════════════════════════════════════════════════════

def _write_series(cache: Path, series_id: str, dates, values):
    payload = {
        "observations": [
            {"date": pd.Timestamp(d).strftime("%Y-%m-%d"), "value": v}
            for d, v in zip(dates, values)
        ]
    }
    (cache / f"{series_id}.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def known_cache(tmp_path):
    """A FRED cache with overlapping coverage and values chosen to be checkable.

    Deltas are chosen so the derived features are exact, not approximate.
    """
    cache = tmp_path / "fred_cache"
    cache.mkdir()
    dates = pd.bdate_range("2020-01-01", periods=900)  # ~3.5y, > MIN_OBS

    # HY OAS: a slow rise plus genuine cycle noise, so the 21d velocity is
    # exactly 0.01 on average but varies row-to-row. A perfectly linear ramp
    # would make the velocity a constant column, which the guard correctly
    # rejects as a dead dimension.
    n = len(dates)
    noise = np.random.default_rng(3).normal(0.0, 0.002, n)
    hy = 3.00 + 0.01 * (np.arange(n) // 21) + noise
    _write_series(cache, "BAMLH0A0HYM2", dates, [round(v * 100, 6) for v in hy])

    # DGS10/DGS2 must VARY: the guard rejects a constant column as a dead HMM
    # dimension, and a real yield curve is never constant. DGS10 ramps 0..+2pp
    # and DGS2 ramps 0..+0.5pp, so the 10Y2Y spread is exactly checkable at any
    # date while still having genuine variance.
    _write_series(cache, "DGS10", dates, np.linspace(2.00, 4.00, n))
    _write_series(cache, "DGS2", dates, np.linspace(0.50, 1.00, n))

    _write_series(cache, "STLFSI2", dates, np.linspace(-1.0, 2.0, n))
    _write_series(cache, "DFII10", dates, np.linspace(0.0, 2.5, n))

    # T10YIE: exact, and paired with CPI below.
    _write_series(cache, "T10YIE", dates, [2.00] * n)

    # CPIAUCSL: +0.5 index points per month -> steady YoY. Use 3 years of
    # month-start stamps so shift(12) is meaningful.
    cpi_dates = pd.date_range("2019-01-01", "2023-12-01", freq="MS")
    cpi_vals = [100.0 + 0.5 * i for i in range(len(cpi_dates))]
    _write_series(cache, "CPIAUCSL", cpi_dates, cpi_vals)

    # FF5 parquet with a deterministic return path.
    ff5_dir = cache
    _write_ff5(ff5_dir, dates)
    return cache


def _write_ff5(cache: Path, dates):
    rng = np.random.default_rng(11)
    rets = rng.normal(0.0004, 0.011, len(dates))
    pd.DataFrame(
        {
            "Mkt-RF": rets,
            "SMB": np.zeros(len(dates)),
            "HML": np.zeros(len(dates)),
            "RMW": np.zeros(len(dates)),
            "CMA": np.zeros(len(dates)),
            "RF": np.full(len(dates), 0.0002),
        },
        index=pd.DatetimeIndex(dates),
    ).to_parquet(cache / "ff5_factors_daily.parquet")


def test_builder_returns_canonical_contract(known_cache):
    frame = build_canonical_features(cache_dir=known_cache, context="test")
    assert list(frame.columns) == CANONICAL_FEATURES
    assert not frame.isna().any().any()


def test_builder_computes_curve_exactly(known_cache):
    """DGS10 ramps 2.00->4.00 and DGS2 ramps 0.50->1.00 over 900 business days,
    so the spread is exactly linspace(2.00-0.50, 4.00-1.00) = linspace(1.50, 3.00).

    Checked on the business days of the emitted frame: the builder returns a
    calendar-daily grid (monthly CPI leg) with the daily series as-of
    forward-filled across weekends, and it starts after the longest warm-up
    (12 months of CPI plus the 45-day publication lag).
    """
    frame = build_canonical_features(cache_dir=known_cache, context="test")
    n = len(pd.bdate_range("2020-01-01", periods=900))
    full = pd.Series(
        np.linspace(1.50, 3.00, n), index=pd.bdate_range("2020-01-01", periods=n)
    )
    bdays = frame.index[frame.index.dayofweek < 5]
    got = frame["YIELD_CURVE_10Y2Y"].reindex(bdays)
    expected = full.reindex(bdays)
    assert expected.notna().all(), "frame business days must all come from the fixture grid"
    assert np.allclose(got.to_numpy(), expected.to_numpy(), atol=1e-9), (
        f"curve mismatch: got {got.iloc[:3].to_numpy()}..{got.iloc[-3:].to_numpy()}, "
        f"expected {expected.iloc[:3].to_numpy()}..{expected.iloc[-3:].to_numpy()}"
    )
    assert frame["YIELD_CURVE_10Y2Y"].std() > 0, "curve must have variance"


def test_builder_drops_warmup_head(known_cache):
    """The first emitted row must be after the CPI YoY + publication-lag warm-up.

    A 2019-01-01..2023-12-01 CPI cache needs 12 months plus 45 days before the
    first computable observation, so nothing before ~2020-03 may be emitted.
    """
    frame = build_canonical_features(cache_dir=known_cache, context="test")
    assert frame.index[0] >= pd.Timestamp("2020-02-01"), (
        f"frame starts at {frame.index[0].date()}, before the CPI warm-up is complete"
    )


def test_builder_computes_credit_velocity(known_cache):
    """HY OAS rises ~0.01pp per 21 business days, so the velocity is small,
    positive on average, and has genuine variance."""
    frame = build_canonical_features(cache_dir=known_cache, context="test")
    vel = frame["CREDIT_SPREAD_VELOCITY_21D"].to_numpy()
    assert vel.mean() > 0.0, "velocity should be positive in a widening-spread fixture"
    assert vel.max() <= 0.01 + 6 * 0.002 + 1e-9
    assert vel.min() >= -0.06 - 1e-9
    assert vel.std() > 1e-4, "velocity must vary, else it is a dead HMM dimension"


def test_guard_rejects_float_noise_degenerate_column():
    """Two values differing only in the last ULP must still be caught.

    A bare `nunique() <= 1` test passes this column, and standardizing by its
    std (~1e-16) turns rounding error into unit-scale noise that is
    indistinguishable from a fabricated signal.
    """
    col = [0.01, 0.010000000000000002] * 250
    assert len(set(col)) == 2, "premise: the column is not exactly constant"
    assert pd.Series(col).std() < 1e-15, "premise: the variance is float noise"
    with pytest.raises(RuntimeError, match="degenerate"):
        assert_authentic_pit(pd.DataFrame({"X": col}), ["X"])


def test_guard_rejects_inf_column():
    col = np.linspace(-1.0, 1.0, 300).tolist()
    col[10] = float("inf")
    with pytest.raises(RuntimeError, match="inf"):
        assert_authentic_pit(pd.DataFrame({"X": col}), ["X"])


def test_builder_computes_unanticipated_inflation(known_cache):
    """CPI +0.5/mo on a level base -> CPI YoY is constant; minus 2.00 breakeven."""
    ui = unanticipated_inflation(cache_dir=known_cache)
    assert len(ui) > 0
    # 0.5 per month * 12 months = +6.0 index pts on a ~100 base = ~6% YoY,
    # and the ratio moves as the base grows, so assert the sign and range
    # rather than a single value.
    assert (ui < 8.0).all() and (ui > 3.0).all()


def test_builder_realized_vol_is_positive_and_plausible(known_cache):
    frame = build_canonical_features(cache_dir=known_cache, context="test")
    vol = frame["REALIZED_VOLATILITY_21D"].to_numpy()
    assert (vol > 0).all()
    assert (vol < 1.0).all()  # annualized fraction


def test_builder_zscore_optional(known_cache):
    frame = build_canonical_features(cache_dir=known_cache, zscore=True, context="test")
    assert np.allclose(frame.mean().to_numpy(), 0.0, atol=1e-8)
    assert np.allclose(frame.std().to_numpy(), 1.0, atol=1e-8)


@pytest.fixture
def ragged_cache(tmp_path):
    """A FRED cache with NO common window across the mandatory series.

    Chunky non-overlapping date ranges guarantee an empty intersection, as
    opposed to the repo's real cache — which was restored to a clean
    2003..2026 window and must STAY clean; the abort contract is exercised
    here, never against live data.
    """
    cache = tmp_path / "fred_cache"
    cache.mkdir()
    _write_series(cache, "BAMLH0A0HYM2",
                  pd.date_range("2020-01-01", periods=400), [3.0] * 400)
    _write_series(cache, "CPIAUCSL",
                  pd.date_range("1980-01-01", "1983-12-01", freq="MS"),
                  [100.0] * 48)
    _write_series(cache, "DFII10",
                  pd.date_range("2018-01-01", periods=300), [1.0] * 300)
    _write_series(cache, "DGS10",
                  pd.date_range("2020-01-01", periods=400), [3.0] * 400)
    _write_series(cache, "DGS2",
                  pd.date_range("2020-01-01", periods=400), [1.0] * 400)
    _write_series(cache, "STLFSI2",
                  pd.date_range("2022-01-01", periods=300), [0.0] * 300)
    _write_series(cache, "T10YIE",
                  pd.date_range("2020-01-01", periods=400), [2.0] * 400)
    return cache


def test_builder_aborts_on_ragged_cache(ragged_cache):
    """A cache without a common window must abort cleanly."""
    with pytest.raises(SyntheticDataViolation) as exc:
        build_canonical_features(cache_dir=ragged_cache, context="test_ragged_cache")
    msg = str(exc.value)
    assert "Aborting per zero-synthetic policy" in msg
    # It must name the binding series, not just fail opaquely.
    assert "STLFSI2" in msg or "BAMLH0A0HYM2" in msg


def test_assert_common_coverage_names_binding_series(ragged_cache):
    with pytest.raises(SyntheticDataViolation) as exc:
        assert_common_coverage(REPO_CACHED_SERIES, cache_dir=ragged_cache)
    assert "NO COMMON DATE RANGE" in str(exc.value)


def test_load_fred_series_missing_is_empty_not_noise():
    s = load_fred_series("DEFINITELY_NOT_A_SERIES_XYZ")
    assert s.empty, "a missing series must return empty, never fabricated values"


def test_load_fred_series_required_raises():
    with pytest.raises(SyntheticDataViolation, match="Missing authentic PIT data"):
        load_fred_series("DEFINITELY_NOT_A_SERIES_XYZ", required=True)


# ═══════════════════════════════════════════════════════════════════════════
# 4. CLI — the engine exits 2 (data abort), not 1 (validation fail)
# ═══════════════════════════════════════════════════════════════════════════

def test_cli_coverage_exits_2_on_ragged_cache(ragged_cache):
    """A ragged cache must make the engine exit 2 (data abort), not 1."""
    proc = subprocess.run(
        [sys.executable, "-m", "Quantitative.regime_shield_v3", "--coverage",
         "--cache-dir", str(ragged_cache)],
        cwd=REPO, capture_output=True, text=True,
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "NO COMMON DATE RANGE" in proc.stdout


def test_cli_coverage_exits_0_on_clean_repo_cache():
    """The restored repo cache must pass the coverage guard, not fake it.

    The FRED cache was repaired to a real 2003..2026 common window; if this
    ever stops exiting 0, the data estate has regressed (or gone synthetic).
    """
    proc = subprocess.run(
        [sys.executable, "-m", "Quantitative.regime_shield_v3", "--coverage"],
        cwd=REPO, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Common window:" in proc.stdout


def test_prereg_v7_pinned_and_cesi_free():
    import hashlib
    import yaml

    p = REPO / "config" / "pre_registration_v7.yaml"
    side = REPO / "config" / "pre_registration_v7.yaml.sha256"
    assert p.exists() and side.exists()
    assert side.read_text(encoding="utf-8").strip() == hashlib.sha256(p.read_bytes()).hexdigest()

    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert str(doc["version"]) == "7.0"
    feats = doc["hmm"]["layer1_features"]
    assert feats == CANONICAL_FEATURES
    assert "CESI" not in feats
    # The gate must NOT have been relaxed to make the change.
    assert doc["hmm"]["validation"]["ari_threshold"] == 0.45
    assert doc["optimization"]["synthetic_fallback"] == "disabled"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
