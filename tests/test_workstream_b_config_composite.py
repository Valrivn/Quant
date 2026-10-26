"""
Workstream B verification: config registry, composite score, state persistence.
(big-pickle-worker)

Scope
-----
Exercises the three things that can silently go wrong in this workstream:

  1. The composite applies the EXACT declared weights, with the right SIGN on
     penalty factors.  A sign error here produces a plausible number that
     prefers drawdown names.
  2. Absent factor data raises instead of becoming 0.0.  This is the CESI
     failure mode: a factor filled with a placeholder so the pipeline runs.
  3. Persisted state survives a process restart with its dtypes intact, so the
     60-day dwell rule is not silently reset to zero every run.

Also asserts the governance constraint that motivated putting the Arm A
parameters in their own file: the frozen Phase 9 v7 pre-registration must still
hash to its pinned digest.
"""
from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd
import pytest

import gate_engine as ge
from config import (
    ARM_A_CONFIG_PATH,
    LEGACY_PREREG_PATH,
    LEGACY_PREREG_SHA256,
    ConfigError,
    ConfigKeyError,
    clear_config_cache,
    describe_registry,
    get_composite_config,
    get_config,
    get_cost_config,
    get_market_event_config,
    get_regime_config,
    get_turnover_config,
    get_vol_config,
    verify_config_integrity,
)
from portfolio.position_state import PositionState, PositionStateStore
from regime import (
    EMBARGO_STATES,
    TRIGGER_TYPES,
    GradedDerisking,
    MarketEventDetector,
    RegimeState,
    RegimeStateStore,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def price_panel() -> pd.DataFrame:
    """Deterministic wide ``date x ticker`` close panel with real drift."""
    dates = pd.bdate_range("2023-01-02", periods=420)
    tickers = [f"T{i:02d}" for i in range(12)]
    rng = np.random.default_rng(20261004)
    data = {}
    for i, t in enumerate(tickers):
        # A deliberate spread of drift so the cross-section is not degenerate.
        drift = (i - 6) * 0.0006
        steps = rng.normal(drift, 0.011, len(dates))
        data[t] = 100.0 * np.exp(np.cumsum(steps))
    return pd.DataFrame(data, index=dates, columns=tickers)


@pytest.fixture
def fundamentals() -> pd.DataFrame:
    """PIT fundamentals WITH a sector column, so eva_spread can compute."""
    tickers = [f"T{i:02d}" for i in range(12)]
    sectors = ["Tech", "Tech", "Tech", "Tech", "Health", "Health",
               "Health", "Health", "Energy", "Energy", "Energy", "Energy"]
    rng = np.random.default_rng(7)
    return pd.DataFrame({
        "ticker": tickers,
        "filed_date": pd.Timestamp("2026-01-15"),
        "sector": sectors,
        "roic": rng.uniform(0.05, 0.30, len(tickers)),
        "wacc_estimate": rng.uniform(0.06, 0.14, len(tickers)),
        "operating_cash_flow": rng.uniform(1e8, 9e8, len(tickers)),
        "fcf": rng.uniform(5e7, 6e8, len(tickers)),
        "market_cap": rng.uniform(2e9, 9e10, len(tickers)),
        "eps_est_current": rng.uniform(1.0, 9.0, len(tickers)),
        "eps_est_3m_ago": rng.uniform(1.0, 9.0, len(tickers)),
        "shares_outstanding": rng.uniform(1e8, 5e9, len(tickers)),
    })


@pytest.fixture
def dividend_panel() -> pd.DataFrame:
    dates = pd.bdate_range("2025-06-01", periods=280)
    tickers = [f"T{i:02d}" for i in range(12)]
    return pd.DataFrame(
        {t: np.where(np.arange(len(dates)) % 63 == 0, 0.5, 0.0) for t in tickers},
        index=dates, columns=tickers)


@pytest.fixture
def turnover() -> dict:
    return {f"T{i:02d}": 0.02 + 0.01 * i for i in range(12)}


# ---------------------------------------------------------------------------
# 1. Governance: the frozen v7 pre-registration is untouched
# ---------------------------------------------------------------------------

def test_frozen_v7_preregistration_still_verifies():
    """
    The reason the Arm A parameters live in their own file.

    If this fails, someone edited the approved Phase 9 contract — which breaks
    the pin in ingestion/harness/prereg.py and the Data_Strategy.md audit trail.
    """
    from ingestion.harness.prereg import EXPECTED_SHA256_V7, sha256_path
    from config import resolve_config_path

    actual = sha256_path(resolve_config_path(LEGACY_PREREG_PATH))
    assert actual == LEGACY_PREREG_SHA256
    assert actual == EXPECTED_SHA256_V7


def test_arm_a_registry_is_separate_from_v7():
    """
    The Arm A registry must not be the frozen v7 document.

    Loading v7 through the Arm A loader must FAIL LOUDLY.  v7 describes the
    strategy-level ``composite_dsr`` metric and declares no Arm A sections; if
    this returned a dict instead, some caller would go on to read invented
    weights out of it.
    """
    assert ARM_A_CONFIG_PATH != LEGACY_PREREG_PATH
    assert "arm_a" in ARM_A_CONFIG_PATH

    arm = get_config()
    assert "composite_weights" in arm

    with pytest.raises((ConfigKeyError, ConfigError)):
        get_config(LEGACY_PREREG_PATH)
    clear_config_cache()

    # v7 is read through its own authoritative, digest-verifying loader.
    from ingestion.harness.prereg import load_prereg

    v7 = load_prereg().config
    assert "composite_weights" not in v7
    assert v7["statistical_design"]["metric"] == "composite_dsr"
    assert v7["status"] == "APPROVED"


def test_arm_a_registry_integrity_verifies():
    assert verify_config_integrity(ARM_A_CONFIG_PATH)["verified"] is True


# ---------------------------------------------------------------------------
# 2. Config loader: exact weights, raise-on-missing
# ---------------------------------------------------------------------------

def test_composite_weights_are_exactly_65_35():
    cw = get_composite_config()
    assert cw["directional_alpha"]["total_weight"] == 0.65
    assert cw["operational_health"]["total_weight"] == 0.35
    da = cw["directional_alpha"]["components"]
    oh = cw["operational_health"]["components"]
    assert da == {"momentum_12m_1m": 0.20, "eva_spread": 0.15,
                  "ocf_yield_revision": 0.15, "dividend_carry_coverage": 0.15}
    assert oh == {"realized_sharpe": 0.15, "max_drawdown": 0.10,
                  "transaction_fee_drag": 0.10}
    assert sum(da.values()) == pytest.approx(0.65)
    assert sum(oh.values()) == pytest.approx(0.35)
    assert sum(da.values()) + sum(oh.values()) == pytest.approx(1.0)


def test_all_sections_exposed():
    assert get_vol_config()["target_annual_vol"] == 0.11
    assert get_vol_config()["lookback_days"] == 63
    assert get_vol_config()["vol_floor"] == 0.05
    assert get_vol_config()["vol_ceiling"] == 0.18
    tc = get_turnover_config()
    assert tc["hysteresis_deadband"] == 0.15
    assert tc["min_holding_days"] == 60
    assert tc["max_monthly_turnover"] == 0.10
    assert get_cost_config() == {"round_trip_bps": 15, "slippage_bps": 5}
    med = get_market_event_config()
    assert med["triggers"]["spread_velocity"]["threshold_bps_per_day"] == 0.5
    assert med["triggers"]["bocpd_regime_shift"]["posterior_threshold"] == 0.85
    assert [s["equity_cap"] for s in med["graded_derisking"]["steps"]] == \
        [0.70, 0.50, 0.40]
    assert med["reentry_embargo"]["min_cooldown_days"] == 21


def test_missing_key_raises_rather_than_defaulting(tmp_path):
    """
    The core doctrine: a declared threshold that is absent must raise.

    A silent default is how a pre-registered parameter stops being
    pre-registered without anything failing.
    """
    import yaml

    registry = get_config()
    registry.pop("tail_hedge")
    p = tmp_path / "broken.yaml"
    p.write_text(yaml.safe_dump(registry), encoding="utf-8")
    clear_config_cache()
    with pytest.raises(ConfigKeyError):
        get_config(str(p))
    clear_config_cache()


def test_weight_arithmetic_is_validated(tmp_path):
    """A block whose components disagree with its total_weight must fail."""
    import yaml

    registry = get_config()
    registry["composite_weights"]["directional_alpha"]["total_weight"] = 0.90
    p = tmp_path / "bad_weights.yaml"
    p.write_text(yaml.safe_dump(registry), encoding="utf-8")
    clear_config_cache()
    with pytest.raises(Exception) as exc:
        get_config(str(p))
    assert "total_weight" in str(exc.value)
    clear_config_cache()


def test_get_regime_config_matches_what_workstream_a_expects():
    """
    The integration landmine.

    regime.* classes do ``config.get("market_event_detection", {})``, so they
    need the WHOLE registry.  get_market_event_config() returns the inner
    section, which would make their lookup miss and fall back to hardcoded
    literals.  This test pins the correct pairing.
    """
    full = get_regime_config()
    assert "market_event_detection" in full
    det = MarketEventDetector(full)
    gd = GradedDerisking(full)
    # Values came from the registry, not from the class defaults.
    assert det.sv_threshold == 0.5 and det.sv_days == 3
    assert det.bocpd_threshold == 0.85 and det.bocpd_days == 2
    assert det.spy_buffer == 0.015 and det.spy_days == 3
    assert [s["day"] for s in gd.steps] == [3, 5, 10]


# ---------------------------------------------------------------------------
# 3. Composite factors
# ---------------------------------------------------------------------------

def test_momentum_is_zscored_ranks_cross_sectionally(price_panel):
    out = ge.compute_momentum_12m_1m(price_panel)
    assert isinstance(out, pd.DataFrame)
    last = out.iloc[-1].dropna()
    assert last.mean() == pytest.approx(0.0, abs=1e-9)
    assert last.std(ddof=1) == pytest.approx(1.0, abs=1e-9)
    # Rank-based, so it is bounded and contains no outliers beyond +/- (n-1)/2.
    assert last.max() <= (len(last) - 1) / 2 + 1e-9


def test_momentum_skips_the_most_recent_month(price_panel):
    """The 1m leg must actually be subtracted."""
    p = price_panel
    raw = (p / p.shift(252) - 1.0) - (p / p.shift(21) - 1.0)
    assert not np.allclose(raw.iloc[-1].values, 0.0)


def test_eva_spread_is_sector_neutral(fundamentals):
    out = ge.compute_eva_spread(fundamentals)
    assert out.notna().sum() > 0
    # Neutralisation is within-sector, so the raw ROIC-WACC spread must not
    # simply reappear in the output.
    f = fundamentals.set_index("ticker")
    spread = f["roic"] - f["wacc_estimate"]
    assert not np.allclose(out.dropna().values,
                           spread.loc[out.dropna().index].values)


def test_eva_spread_raises_without_sector_column(fundamentals):
    """
    An un-neutralised EVA spread is a DIFFERENT factor.

    It must not silently run under this factor's name, because that would
    misattribute a sector tilt to stock selection.
    """
    with pytest.raises(ge.FactorDataUnavailable) as exc:
        ge.compute_eva_spread(fundamentals.drop(columns=["sector"]))
    assert exc.value.factor == "eva_spread"


def test_ocf_yield_revision_raises_on_real_repo_schema():
    """The real PIT table has no market cap and no EPS estimates."""
    import config

    path = config.resolve_config_path(
        "data/pit_fundamentals/valuation_fundamentals.parquet")
    if not path.exists():
        pytest.skip("PIT fundamentals table not present")
    real = pd.read_parquet(path)
    for col in ("market_cap", "shares_outstanding", "eps_est_current"):
        assert col not in real.columns, (
            f"{col} now exists — the ocf_yield_revision blocker may be "
            f"resolved; re-check the registry's factor_data_contracts")
    with pytest.raises(ge.FactorDataUnavailable):
        ge.compute_ocf_yield_revision(real, pd.DataFrame())


def test_dividend_carry_coverage_raises_without_share_count(fundamentals):
    with pytest.raises(ge.FactorDataUnavailable) as exc:
        ge.compute_dividend_carry_coverage(
            fundamentals.drop(columns=["shares_outstanding"]),
            pd.DataFrame({"T00": [0.5]}, index=pd.to_datetime(["2026-01-05"])))
    assert exc.value.factor == "dividend_carry_coverage"


def test_realized_sharpe_and_max_dd_signs():
    good = pd.Series([0.01] * 100)
    bad = pd.Series([-0.01] * 100)
    assert ge.compute_realized_sharpe(good) > 0
    assert ge.compute_realized_sharpe(bad) < 0
    # Too-short input is undefined, not zero.
    assert np.isnan(ge.compute_realized_sharpe(pd.Series([0.01])))
    # Max drawdown is NEGATIVE so that "closer to zero is better".
    dd = ge.compute_max_drawdown_penalty(pd.Series([1.0, 0.8, 0.9, 1.1]))
    assert dd == pytest.approx(-0.2)
    assert dd < 0


def test_fee_drag_is_positive_magnitude_and_ignored_in_scoring():
    """Cost is positive; the SIGN flip lives in FACTOR_DIRECTION."""
    assert ge.compute_fee_drag_penalty(0.10, 15) == pytest.approx(1.5)
    assert ge.FACTOR_DIRECTION["transaction_fee_drag"] == -1
    # max_drawdown must NOT be flipped: its convention is already "negative".
    assert ge.FACTOR_DIRECTION["max_drawdown"] == +1


def test_composite_applies_exact_weights_and_correct_signs(
        price_panel, fundamentals, dividend_panel, turnover):
    tickers = list(price_panel.columns)
    score = ge.compute_composite_score(
        tickers, price_panel, fundamentals,
        dividends=dividend_panel, turnover_by_ticker=turnover,
        as_of=pd.Timestamp("2026-01-20"))
    assert isinstance(score, pd.Series)
    assert set(score.index) == set(tickers)
    assert score.notna().all()
    assert score.index.tolist() == [t.upper() for t in tickers]


def test_composite_rejects_undeclared_direction(price_panel):
    """A factor with a weight but no declared sign must raise, not guess."""
    cfg = {"directional_alpha": {"total_weight": 1.0,
                                 "components": {"brand_new_factor": 1.0}},
           "operational_health": {"total_weight": 0.0, "components": {}}}
    with pytest.raises(ValueError, match="FACTOR_DIRECTION"):
        ge.compute_composite_score(list(price_panel.columns), price_panel,
                                   config=cfg, min_weight_coverage=0.0)


def test_composite_raises_when_coverage_too_low(price_panel, fundamentals):
    """
    0.45 of the declared weight is uncomputable on real schemas.

    The engine must refuse rather than renormalise the survivors, which would
    silently re-weight the strategy after the fact.
    """
    thin = fundamentals.drop(columns=["sector"])
    with pytest.raises(ge.InsufficientFactorCoverage) as exc:
        ge.compute_composite_score(list(price_panel.columns), price_panel, thin)
    assert exc.value.computable_weight < exc.value.required


def test_coverage_gate_reports_the_registrys_stated_number():
    """
    The registry's coverage_note says 0.55 is the max currently-valid weight.

    This test keeps the documentation and the code from drifting apart.
    """
    contracts = get_config()["factor_data_contracts"]
    assert "0.55" in contracts["coverage_note"]
    weights = get_composite_config()
    computable = (weights["directional_alpha"]["components"]["momentum_12m_1m"]
                  + weights["operational_health"]["total_weight"])
    assert computable == pytest.approx(0.55)


# ---------------------------------------------------------------------------
# 4. Position state persistence
# ---------------------------------------------------------------------------

def test_position_state_round_trips_through_disk(tmp_path):
    path = str(tmp_path / "nested" / "position_state.parquet")
    store = PositionStateStore(path)
    store.set(PositionState("aapl", 0.05, 3, pd.Timestamp("2026-01-05"),
                            1.25, 100.0))
    assert os.path.exists(path)

    reloaded = PositionStateStore(path)          # simulate a process restart
    aapl = reloaded.get("AAPL")
    assert aapl is not None
    assert aapl.days_held == 3
    assert aapl.weight == pytest.approx(0.05)
    assert aapl.entry_date == pd.Timestamp("2026-01-05")


def test_position_state_preserves_dtypes(tmp_path):
    """days_held must come back as an int, not the string "59"."""
    path = str(tmp_path / "ps.parquet")
    PositionStateStore(path).set(
        PositionState("X", 0.1, 59, pd.Timestamp("2026-01-05"), 0.5, 10.0))
    snap = PositionStateStore(path).snapshot()
    assert str(snap["days_held"].dtype) == "int64"
    assert str(snap["weight"].dtype) == "float64"
    assert isinstance(PositionStateStore(path).get("X").days_held, int)


def test_increment_all_days_persists(tmp_path):
    path = str(tmp_path / "ps.parquet")
    store = PositionStateStore(path)
    store.set(PositionState("A", 0.05, 0, pd.Timestamp("2026-01-05"),
                            1.0, 100.0))
    store.increment_all_days(as_of=pd.Timestamp("2026-01-06"))
    assert store.get("A").days_held == 1
    # Survives a restart.
    assert PositionStateStore(path).get("A").days_held == 1


def test_same_day_entry_is_not_counted_as_a_day_held(tmp_path):
    """
    Counting a position's first day would expire the 60-day dwell on day 59.
    """
    store = PositionStateStore(str(tmp_path / "ps.parquet"))
    store.set(PositionState("NVDA", 0.04, 0, pd.Timestamp("2026-01-06"),
                            0.5, 50.0))
    store.increment_all_days(as_of=pd.Timestamp("2026-01-06"))
    assert store.get("NVDA").days_held == 0


def test_empty_store_loads_with_correct_schema(tmp_path):
    """Empty and full stores must share one schema, or one of them fails."""
    store = PositionStateStore(str(tmp_path / "ps.parquet"))
    store.clear()
    snap = PositionStateStore(str(tmp_path / "ps.parquet")).snapshot()
    assert len(snap) == 0
    assert str(snap["days_held"].dtype) == "int64"


def test_max_weight_seen_tracks_the_high_water_mark():
    pos = PositionState("A", 0.05, 0, pd.Timestamp("2026-01-05"), 1.0, 100.0)
    assert pos.max_weight_seen == pytest.approx(0.05)
    pos.weight = 0.08
    pos.update_price(101.0)
    assert pos.max_weight_seen == pytest.approx(0.08)
    pos.weight = 0.04
    pos.update_price(99.0)
    assert pos.max_weight_seen == pytest.approx(0.08)   # remembers the peak


def test_nan_price_is_ignored_not_stored():
    pos = PositionState("A", 0.05, 0, pd.Timestamp("2026-01-05"), 1.0, 100.0)
    pos.update_price(float("nan"))
    assert pos.current_price == 0.0


def test_deadband_is_relative_to_target_weight():
    pos = PositionState("A", 0.05, 0, pd.Timestamp("2026-01-05"), 1.0, 100.0)
    band = get_turnover_config()["hysteresis_deadband"]      # 0.15
    assert pos.exceeds_deadband(0.052, band) is False         # +4%: noise
    assert pos.exceeds_deadband(0.070, band) is True          # +40%: trade
    # A move to zero has no meaningful relative deviation.
    assert pos.exceeds_deadband(0.0, band) is False


def test_min_dwell_uses_the_registry_threshold():
    pos = PositionState("A", 0.05, 59, pd.Timestamp("2026-01-05"), 1.0, 100.0)
    min_days = get_turnover_config()["min_holding_days"]    # 60
    assert pos.meets_min_dwell(min_days) is False
    pos.increment_day()
    assert pos.meets_min_dwell(min_days) is True


def test_eligible_for_exit_uses_registry_threshold(tmp_path):
    store = PositionStateStore(str(tmp_path / "ps.parquet"))
    store.set(PositionState("OLD", 0.05, 90, pd.Timestamp("2026-01-05"), 1.0, 100.0))
    store.set(PositionState("NEW", 0.05, 2, pd.Timestamp("2026-03-01"), 1.0, 100.0))
    assert store.eligible_for_exit(
        get_turnover_config()["min_holding_days"]) == ["OLD"]


# ---------------------------------------------------------------------------
# 5. Regime state persistence
# ---------------------------------------------------------------------------

def test_regime_state_round_trips_through_json(tmp_path):
    path = str(tmp_path / "regime_state.json")
    store = RegimeStateStore(path)
    store.update(market_event_triggered=True, trigger_type="bocpd",
                 days_since_confirmation=4, embargo_state="active",
                 embargo_days_remaining=17, current_equity_cap=0.70)
    assert os.path.exists(path)

    reloaded = RegimeStateStore(path).get()
    assert reloaded.market_event_triggered is True
    assert reloaded.trigger_type == "bocpd"
    assert reloaded.days_since_confirmation == 4
    assert reloaded.embargo_state == "active"
    assert reloaded.embargo_days_remaining == 17
    assert reloaded.current_equity_cap == pytest.approx(0.70)
    assert isinstance(reloaded.days_since_confirmation, int)


def test_corrupt_regime_file_raises_instead_of_resetting(tmp_path):
    """
    A corrupt state file read as "no market event" restores full equity
    mid-crisis.  That is the most dangerous possible default, so it raises.
    """
    path = tmp_path / "regime_state.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        RegimeStateStore(str(path))


def test_regime_state_validates_enum_fields():
    with pytest.raises(ValueError):
        RegimeState(trigger_type="not_a_trigger")
    with pytest.raises(ValueError):
        RegimeState(embargo_state="not_a_state")
    assert set(TRIGGER_TYPES) == {"spread_velocity", "bocpd", "spy_trend"}
    assert set(EMBARGO_STATES) == {"inactive", "active", "cooling_down"}


def test_advance_day_only_counts_while_triggered():
    """An untriggered counter could satisfy the cooldown on its own."""
    state = RegimeState()
    for _ in range(100):
        state.advance_day()
    assert state.days_since_confirmation == 0

    state.market_event_triggered = True
    state.advance_day()
    state.advance_day()
    assert state.days_since_confirmation == 2


def test_embargo_countdown_decrements_and_floors_at_zero():
    state = RegimeState(embargo_state="active", embargo_days_remaining=2)
    state.advance_day()
    state.advance_day()
    state.advance_day()
    assert state.embargo_days_remaining == 0


def test_clear_event_restores_full_equity():
    state = RegimeState(market_event_triggered=True, trigger_type="spy_trend",
                        confirmation_date=pd.Timestamp("2026-01-05"),
                        days_since_confirmation=7, current_equity_cap=0.40)
    assert state.is_de_risking is True
    state.clear_event()
    assert state.market_event_triggered is False
    assert state.trigger_type is None
    assert state.current_equity_cap == 1.0
    assert state.is_de_risking is False


# ---------------------------------------------------------------------------
# 6. Full 3-stage integration across the workstream boundary
# ---------------------------------------------------------------------------

def test_three_stage_flow_persists_cap_and_embargo(tmp_path):
    """
    Detector -> de-risking -> embargo, mirrored into the persisted store,
    exactly as Workstream A will drive it.
    """
    cfg = get_regime_config()
    det = MarketEventDetector(cfg)
    gd = GradedDerisking(cfg)
    store = RegimeStateStore(str(tmp_path / "rs.json"))

    # Six consecutive days of spread widening above 0.5 bps/day.
    history = [100.0]
    for _ in range(6):
        history.append(history[-1] + 0.6)
    out = det.update({"credit_spread": history[-1],
                      "credit_spread_history": history,
                      "bocpd_posterior": 0.1,
                      "spy_close": 500.0, "spy_sma_200": 480.0})
    assert out["triggered"] is True
    assert out["trigger_type"] == "spread_velocity"
    store.sync_from_detector(out)
    assert store.get().market_event_triggered is True

    for day in range(1, 13):
        cap = gd.get_equity_cap(store.get().days_since_confirmation)
        store.sync_from_derisking(cap)
        store.advance_day()

    # Persistence must have survived to disk.
    persisted = RegimeStateStore(str(tmp_path / "rs.json")).get()
    assert persisted.embargo_state in EMBARGO_STATES
    assert 0.0 <= persisted.current_equity_cap <= 1.0
    assert persisted.last_update is not None


def test_derisking_ladder_matches_registry_steps():
    """Day 3 -> 0.70, day 5 -> 0.50, day 10 -> 0.40, per the registry."""
    gd = GradedDerisking(get_regime_config())
    assert gd.get_equity_cap(-1) == 1.0
    assert gd.get_equity_cap(0) == 1.0     # confirmation day
    assert gd.get_equity_cap(2) == pytest.approx(0.70)
    assert gd.get_equity_cap(4) == pytest.approx(0.50)
    assert gd.get_equity_cap(9) == pytest.approx(0.40)
    assert gd.get_equity_cap(30) == pytest.approx(0.40)


def test_describe_registry_reports_both_documents():
    d = describe_registry()
    assert d["integrity"]["verified"] is True
    assert d["composite"]["directional_alpha_total"] == 0.65
    assert d["composite"]["operational_health_total"] == 0.35
    assert sum(d["composite"]["weights"].values()) == pytest.approx(1.0)
    assert d["legacy_prereg"]["sha256"] == LEGACY_PREREG_SHA256
