"""Sprint 4 / Arm A Fix 1 — Trailing Stop tests (CEO Ruling: Path A, Pure Quant Only).

Pre-registration: ``config/pre_registration_v4.yaml`` ::
``parameter_clusters.B_swap_exit.params.trailing_pct``
  values ``[0.10, 0.15, 0.20]`` — trailing stop percentage from high watermark.

Subject under test
------------------
``Quantitative.exit_engine.evaluate_trailing_stop(current_price, high_watermark,
trailing_pct=0.15) -> (triggered, trailing_stop_level, new_high_watermark)``

Frozen invariants asserted here
-------------------------------
I1  The high watermark only ever RATCHETS UP: ``new_hw == max(hw, price)``.
I2  The stop level sits exactly ``trailing_pct`` below the ratcheted watermark.
I3  The stop is INCLUSIVE: it fires at the level itself and is silent strictly
    above it.  (Off-by-one here is a real-money bug — a stop that misses by one
    cent is a stop that does not exist.)
I4  A new high never fires the stop.
I5  Exit priority chain (Qualitative dispatch §3):
    ``STRUCTURAL_BREAK > HYSTERIA > TRAILING_STOP > TAKE_PROFIT``.
I6  A triggered trailing stop forces IMMEDIATE (1-day) liquidation and is NOT
    subject to the regime TWAP window or the illiquidity extension.

Gate interaction (see ``validation/gate_calibration.py``)
--------------------------------------------------------
I6 is the mechanism by which the trailing stop moves Gate 3 (max drawdown,
shallower) at the cost of Gate 14 (swap churn) and Gate 15 (TCA budget).
"""

from __future__ import annotations

import numpy as np
import pytest

from Quantitative.exit_engine import (
    evaluate_exit_signal,
    evaluate_trailing_stop,
)
from Quantitative.exit_scheduler import ExitScheduler


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

#: Pre-registered trailing_pct grid (config/pre_registration_v4.yaml, cluster B).
PREREG_TRAILING_PCT = (0.10, 0.15, 0.20)

#: The Sprint 4 operating point asserted by the dispatch.
TRAILING_PCT = 0.15


@pytest.fixture
def isolated_exit_engine(monkeypatch):
    """Hermetic ``evaluate_exit_signal``.

    ``load_financial_inputs`` reads ``data/source/sec/companyfacts/<t>.json``
    from the live working tree; without this stub the structural-break leg of
    the priority chain silently depends on whatever cache happens to be on
    disk.  Forcing ``degraded=True`` means margin_decay/eva stay ``None`` and
    the STRUCTURAL_BREAK branch is provably unreachable, which is exactly the
    fixture needed to isolate the TRAILING_STOP vs TAKE_PROFIT ordering.
    """
    import Quantitative.exit_engine as exit_mod

    monkeypatch.setattr(
        exit_mod, "load_financial_inputs",
        lambda ticker: {"degraded": True, "warnings": ["test-stub: financials degraded"]},
    )
    # load_hype_data already returns (None, False, False); pin it so a future
    # implementation cannot quietly start emitting a hype_p95 and preempt the
    # trailing stop with the HYSTERIA branch.
    monkeypatch.setattr(
        exit_mod, "load_hype_data", lambda ticker: (None, False, False)
    )
    return exit_mod


# ---------------------------------------------------------------------------
# I1/I2/I3 — the dispatch's headline assertion
# ---------------------------------------------------------------------------

def test_trailing_stop_triggers_at_15pct_from_high():
    """Dispatch acceptance criterion: "Trailing stop triggers at 15% from high watermark".

    Path: 100 -> 120 (+20%) -> 102.  The watermark ratchets to 120 on the way
    up, so the stop level becomes 102.0 and 102.00 fires (inclusive boundary).
    """
    # At the watermark: level 15% below, not yet triggered.
    assert evaluate_trailing_stop(100, 100, 0.15) == (False, 85.0, 100)

    # New high of 120 ratchets the watermark; 120 is 18% above the 102 level.
    assert evaluate_trailing_stop(120, 100, 0.15) == (False, 102.0, 120)

    # 15.0% drawdown from the 120 watermark => exactly at the 102 level => fires.
    assert evaluate_trailing_stop(102, 120, 0.15) == (True, 102.0, 120)


def test_trailing_stop_does_not_fire_just_above_the_level():
    """I3: strictly above the level is silent.  One cent of air must survive."""
    triggered, level, _ = evaluate_trailing_stop(102.01, 120.0, 0.15)
    assert level == pytest.approx(102.0)
    assert triggered is False


def test_trailing_stop_boundary_is_inclusive_at_exact_level():
    """I3: the stop fires AT the level, not one tick below it."""
    triggered, level, new_hw = evaluate_trailing_stop(102.0, 120.0, 0.15)
    assert new_hw == pytest.approx(120.0)
    assert level == pytest.approx(102.0)
    assert triggered is True, "stop must be inclusive of the level itself"


# ---------------------------------------------------------------------------
# I1 — watermark ratchet
# ---------------------------------------------------------------------------

def test_watermark_ratchets_up_and_never_down():
    """I1: the high watermark is monotone non-decreasing.

    A watermark that can fall turns a trailing stop into a stop that follows
    the price down and never fires — the classic silent-failure bug.
    """
    watermark = 100.0
    for price in (100.0, 130.0, 90.0, 125.0, 80.0, 140.0, 95.0):
        _, _, new_hw = evaluate_trailing_stop(price, watermark, 0.15)
        assert new_hw >= watermark, f"watermark fell at price={price}"
        assert new_hw == pytest.approx(max(watermark, price))
        watermark = new_hw
    assert watermark == pytest.approx(140.0)


def test_trailing_stop_never_fires_on_a_new_high():
    """I4: making a new high can never simultaneously trigger the stop."""
    for price in (100.0, 101.0, 250.0, 1000.0):
        triggered, level, new_hw = evaluate_trailing_stop(price, price, 0.15)
        assert new_hw == pytest.approx(price)
        assert level == pytest.approx(price * 0.85)
        assert triggered is False


# ---------------------------------------------------------------------------
# I2 — the pre-registered trailing_pct grid
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("trailing_pct", PREREG_TRAILING_PCT)
def test_preregistered_trailing_pct_grid(trailing_pct):
    """I2: every value in the pre-registered grid behaves as specified."""
    high, expected_level = 200.0, 200.0 * (1.0 - trailing_pct)

    triggered, level, new_hw = evaluate_trailing_stop(high, high, trailing_pct)
    assert new_hw == pytest.approx(high)
    assert level == pytest.approx(expected_level)
    assert triggered is False

    # Exactly at the level => fires; a hair above => does not.
    assert evaluate_trailing_stop(expected_level, high, trailing_pct)[0] is True
    assert evaluate_trailing_stop(expected_level * 1.0001, high, trailing_pct)[0] is False


def test_tighter_stop_fires_earlier():
    """I2: a 20% trailing band is strictly tighter than a 10% band.

    Guards against the multipliers being transposed in the regime table.
    """
    high = 100.0
    levels = [evaluate_trailing_stop(high, high, p)[1] for p in PREREG_TRAILING_PCT]
    # PREREG_TRAILING_PCT is ascending, so the levels must be descending.
    assert levels == sorted(levels, reverse=True), levels
    assert levels == [pytest.approx(v) for v in (90.0, 85.0, 80.0)]
    # The 20% band sits 10pp below the 10% band on a 100 watermark.
    assert levels[0] - levels[-1] == pytest.approx(10.0)


def test_trailing_stop_uses_the_default_15pct_when_omitted():
    """The default argument is the pre-registered operating point."""
    assert evaluate_trailing_stop(102.0, 120.0) == evaluate_trailing_stop(
        102.0, 120.0, 0.15
    )


# ---------------------------------------------------------------------------
# I5 — exit priority
# ---------------------------------------------------------------------------

def test_trailing_stop_priority_over_take_profit(isolated_exit_engine):
    """I5 / dispatch: the trailing stop outranks take profit.

    Fixture design — ``iv_fair = 100`` so the three bands are disjoint:
        take profit   fires when price >  115.0
        hysteria      fires when price >  125.0   (1.25 x IV_Fair)
        trailing      fires when price <= 127.5   (high watermark 150, -15%)

    At price 120 the trailing stop AND take profit are both armed while
    hysteria is not, so the chain resolves to whichever of the two the
    implementation checks first.
    """
    dcf = {"iv_p50": 100.0, "iv_p25": 90.0, "iv_p10": 80.0}

    # Both conditions armed -> TRAILING_STOP must win.
    both = evaluate_exit_signal(
        "TST", 120.0, dcf, None, high_watermark=150.0, trailing_pct=0.15
    )
    assert both.trailing_stop_triggered is True
    assert both.take_profit_triggered is True
    assert both.hysteria_triggered is False
    assert both.structural_break_triggered is False
    assert both.exit_triggered is True
    assert both.exit_type == "TRAILING_STOP", (
        f"trailing stop must outrank take profit, got {both.exit_type!r}"
    )

    # Control: same price, low watermark -> trailing silent, take profit fires.
    tp_only = evaluate_exit_signal(
        "TST", 120.0, dcf, None, high_watermark=100.0, trailing_pct=0.15
    )
    assert tp_only.trailing_stop_triggered is False
    assert tp_only.take_profit_triggered is True
    assert tp_only.exit_type == "TAKE_PROFIT"

    # The distinction is the watermark, nothing else.
    assert tp_only.current_price == both.current_price
    assert both.high_watermark > tp_only.high_watermark


def test_full_exit_priority_chain(isolated_exit_engine):
    """I5: the whole documented chain, verified end to end.

    STRUCTURAL_BREAK > HYSTERIA > TRAILING_STOP > TAKE_PROFIT.
    """
    exit_mod = isolated_exit_engine
    dcf = {"iv_p50": 100.0, "iv_p25": 90.0, "iv_p10": 80.0}

    # 1. STRUCTURAL_BREAK preempts an armed trailing stop + take profit.
    exit_mod.load_financial_inputs = lambda t: {
        "degraded": False, "warnings": [],
        "gross_profit": 40e9, "revenue": 100e9,
        "gross_profit_prior": 45e9, "revenue_prior": 100e9,   # 500bps decay
        "ebit": 25e9, "tax_rate": 0.21, "total_debt": 50e9, "cash": 20e9,
        "shares_outstanding": 1e9,
    }
    structural = evaluate_exit_signal(
        "TST", 120.0, dcf, None, high_watermark=150.0, trailing_pct=0.15
    )
    assert structural.structural_break_triggered is True
    assert structural.trailing_stop_triggered is True
    assert structural.take_profit_triggered is True
    assert structural.exit_type == "STRUCTURAL_BREAK"

    # 2. HYSTERIA preempts an armed trailing stop (price 127.5 > 1.25 x 100).
    exit_mod.load_financial_inputs = lambda t: {"degraded": True, "warnings": []}
    hysteria = evaluate_exit_signal(
        "TST", 127.5, dcf, None, high_watermark=150.0, trailing_pct=0.15
    )
    assert hysteria.hysteria_triggered is True
    assert hysteria.trailing_stop_triggered is True
    assert hysteria.exit_type == "HYSTERIA"

    # 3. Nothing armed -> no exit, and the reason says so.
    quiet = evaluate_exit_signal(
        "TST", 110.0, dcf, None, high_watermark=100.0, trailing_pct=0.15
    )
    assert quiet.exit_triggered is False
    assert quiet.exit_type == ""
    assert "No exit condition met" in quiet.exit_reason


def test_exit_signal_propagates_watermark_and_level(isolated_exit_engine):
    """The signal must publish the ratcheted watermark and the exact level."""
    sig = evaluate_exit_signal(
        "TST", 102.0, {"iv_p50": 100.0, "iv_p25": 90.0, "iv_p10": 80.0},
        None, high_watermark=120.0, trailing_pct=0.15,
    )
    assert sig.high_watermark == pytest.approx(120.0)
    assert sig.trailing_stop_level == pytest.approx(102.0)
    assert sig.trailing_stop_pct == pytest.approx(0.15)
    assert sig.trailing_stop_triggered is True
    # Serialised form must round-trip — the exit checkpoint writes sig.to_dict().
    assert sig.to_dict()["trailing_stop_triggered"] is True
    assert sig.to_dict()["exit_type"] == "TRAILING_STOP"


# ---------------------------------------------------------------------------
# I6 — scheduler integration
# ---------------------------------------------------------------------------

def _signal(**overrides):
    base = {
        "ticker": "TST", "shares": 10_000.0, "price": 100.0,
        "adv_usd": 10_000_000.0, "exit_type": "TWAP", "reason": "standard_exit",
    }
    base.update(overrides)
    return base


def test_trailing_stop_with_exit_scheduler():
    """I6 / dispatch: a triggered trailing stop liquidates in 1 day, not TWAP.

    The reference case is a liquid name in EXPANSION, which the regime table
    (REGIME_TWAP_WINDOWS) would otherwise stretch to 5 days.  The trailing
    stop must short-circuit that entirely.
    """
    sched = ExitScheduler()

    standard = sched.schedule_exits([_signal()], regime="EXPANSION")
    triggered = sched.schedule_exits(
        [_signal(trailing_stop_triggered=True, exit_type="TRAILING_STOP",
                 reason="trailing_exit")],
        regime="EXPANSION",
    )

    assert len(standard) == 5, "EXPANSION liquid baseline is a 5-day TWAP"
    assert {s["twap_days_total"] for s in standard} == {5}

    assert len(triggered) == 1, "trailing stop must collapse the schedule to one slice"
    only = triggered[0]
    assert only["twap_days_total"] == 1
    assert only["day_in_twap"] == 1
    assert only["exec_day"] == 1
    assert only["pct_slice"] == pytest.approx(1.0)
    assert only["shares_slice"] == pytest.approx(10_000.0)
    assert only["trailing_stop_triggered"] is True
    assert only["exit_type"] == "TRAILING_STOP"


def test_trailing_stop_acceleration_is_independent_of_regime():
    """I6: the 1-day liquidation holds in every regime, crisis or not."""
    sched = ExitScheduler()
    for regime in ("EXPANSION", "RECOVERY", "SHOCK", "CRISIS"):
        sched_rows = sched.schedule_exits(
            [_signal(trailing_stop_triggered=True, exit_type="TRAILING_STOP")],
            regime=regime,
        )
        assert len(sched_rows) == 1, regime
        assert sched_rows[0]["twap_days_total"] == 1, regime


def test_trailing_stop_bypasses_illiquidity_extension_and_adv_cap():
    """I6: an urgent exit outranks both the ADV participation cap and the
    illiquid-name window extension.

    This name is deliberately illiquid (ADV $1M, far below the $5M threshold)
    and the 5%-of-ADV cap would otherwise stretch the schedule to 10 days.
    A trailing stop is a risk event, not a rebalance.
    """
    sched = ExitScheduler()

    # Non-urgent illiquid name: the ADV cap binds and stretches the schedule.
    deferred = sched.schedule_exits(
        [_signal(adv_usd=1_000_000.0)], regime="EXPANSION"
    )
    assert max(s["twap_days_total"] for s in deferred) > 1, (
        "fixture assumption failed: the ADV cap should bind for a non-urgent exit"
    )

    urgent = sched.schedule_exits(
        [_signal(adv_usd=1_000_000.0, trailing_stop_triggered=True,
                 exit_type="TRAILING_STOP")],
        regime="EXPANSION",
    )
    assert len(urgent) == 1
    assert urgent[0]["twap_days_total"] == 1


def test_scheduler_derives_acceleration_from_exit_type_alone():
    """I6: ``exit_type == "TRAILING_STOP"`` is sufficient to accelerate.

    Downstream producers do not all set the boolean flag; the type string is
    the authoritative signal, so both paths must accelerate.
    """
    sched = ExitScheduler()
    rows = sched.schedule_exits(
        [_signal(exit_type="TRAILING_STOP")], regime="EXPANSION"
    )
    assert len(rows) == 1
    assert rows[0]["trailing_stop_triggered"] is True


def test_scheduler_never_oversells_the_position():
    """Slices must sum to the position regardless of the acceleration path."""
    sched = ExitScheduler()
    for overrides, regime in (
        ({}, "EXPANSION"),
        ({"trailing_stop_triggered": True, "exit_type": "TRAILING_STOP"}, "CRISIS"),
        ({"adv_usd": 1_000_000.0}, "SHOCK"),
    ):
        rows = sched.schedule_exits([_signal(**overrides)], regime=regime)
        total = sum(s["shares_slice"] for s in rows)
        assert total == pytest.approx(10_000.0), (overrides, regime)
        assert sum(s["pct_slice"] for s in rows) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Monotone invariant sweep (property-style, cheap)
# ---------------------------------------------------------------------------

def test_stop_level_is_monotone_in_trailing_pct():
    """I2: a wider band always sits lower, for any watermark."""
    for high in (10.0, 100.0, 1_000.0, 12_345.67):
        levels = [evaluate_trailing_stop(high, high, p)[1]
                  for p in (0.0, 0.05, 0.10, 0.15, 0.20, 0.50, 1.0)]
        assert levels == sorted(levels, reverse=True), (high, levels)
        # trailing_pct = 0 collapses the level onto the watermark.
        assert levels[0] == pytest.approx(high)
        # trailing_pct = 1.0 collapses it to zero.
        assert levels[-1] == pytest.approx(0.0)


@pytest.mark.parametrize("trailing_pct", PREREG_TRAILING_PCT)
def test_trigger_region_is_exactly_the_sub_level_interval(trailing_pct):
    """I3 swept over the pre-registered grid: fires iff price <= level.

    Swept on a fine grid so a floating-point boundary error surfaces as a
    single failing cell rather than passing by luck on one hand-picked number.
    """
    high = 137.0
    level = high * (1.0 - trailing_pct)
    for step in range(0, 41):
        price = level * (1.0 - 0.02 * step)          # 2% below -> 20% above level
        triggered, computed_level, _ = evaluate_trailing_stop(price, high, trailing_pct)
        assert computed_level == pytest.approx(level)
        assert triggered is (price <= computed_level), (price, computed_level)


def test_watermark_survives_a_nan_free_price_path():
    """A full up-then-down path must still ratchet correctly and fire once.

    This is the shape of a real trailing-stop life cycle: mark to a new high,
    then hand back a full 15% and stop out.
    """
    prices = [100.0, 115.0, 140.0, 132.0, 125.0, 118.0, 119.0]
    watermark = prices[0]
    fired_at = None
    for i, price in enumerate(prices):
        triggered, level, watermark = evaluate_trailing_stop(price, watermark, 0.15)
        if triggered and fired_at is None:
            fired_at = i
    assert watermark == pytest.approx(140.0)
    # The 140 watermark sets a 119.0 level; 118.0 is the first close at or
    # below it, so the stop fires on day 5 (index) and not before.
    assert fired_at == 5, f"expected the stop to fire on day 5, got {fired_at}"
    triggered, level, _ = evaluate_trailing_stop(prices[fired_at], 140.0, 0.15)
    assert level == pytest.approx(119.0)
    assert prices[fired_at] == pytest.approx(118.0)
    assert prices[fired_at] <= level
    assert triggered is True
    # 132 and 125 (days 3 and 4) were both above the level — no early trigger.
    for earlier in prices[2:5]:
        assert evaluate_trailing_stop(earlier, 140.0, 0.15)[0] is False, earlier
    # And it stays fired on a further leg down.
    assert evaluate_trailing_stop(110.0, 140.0, 0.15)[0] is True
