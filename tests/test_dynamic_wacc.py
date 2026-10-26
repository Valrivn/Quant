"""Sprint 4 / Arm A Fix 3 — Dynamic WACC tests (CEO Ruling: Path A, Pure Quant Only).

Pre-registration: ``config/pre_registration_v4.yaml`` ::
``parameter_clusters.B_swap_exit.params.wacc_regime_adjustment`` (``bool``)
and ``parameter_clusters.A_wacc_regime`` ("WACC & Regime Engine", high
correlation rho>0.7, ``ke_ordering: [EXPANSION, SHOCK, CRISIS]`` with
``monotonicity_ke: hard_rejection``).

Subject under test
------------------
``Quantitative.wacc_panel.compute_dynamic_wacc(base_wacc, regime, risk_free_rate,
equity_risk_premium, beta, cost_of_debt=0.05, debt_weight=0.20,
equity_weight=0.80, tax_rate=0.21) -> float``

and its wiring into the DCF screen (``Quantitative.dcf_screen.run_dcf_for_ticker``,
which imports it lazily from ``Quantitative.wacc_panel``).

Frozen contract asserted here
-----------------------------
W1  EXPANSION is the NEUTRAL regime: the returned WACC equals ``base_wacc``.
    The engine is regime-*conditional*, so a calm macro must leave the
    discount rate exactly where the base case put it.
W2  SHOCK raises the WACC above ``base_wacc`` (the comment in the pre-registration
    is explicit: "Higher ERP, higher RF").
W3  CRISIS raises it further than SHOCK — the regime ladder is strictly ordered
    ``EXPANSION < RECOVERY < SHOCK < CRISIS``, matching
    ``ke_ordering`` + ``monotonicity_ke: hard_rejection``.
W4  ``base_wacc`` is the EXPANSION anchor and is genuinely consumed.
W5  The regime adjustment table is monotone in the frozen state order.
W6  Beta, ERP and the risk-free rate are each monotone-increasing in the WACC.
W7  The DCF screen discounts at the regime-adjusted rate and does not mutate
    the caller's config.

W1 and W4 are the falsification instruments for the Sprint 4 contract; see the
module-level note in ``VALIDATION_NOTES`` below and the accompanying report.
"""

from __future__ import annotations

import copy

import numpy as np
import pytest

from Quantitative.wacc_panel import (
    REGIME_WACC_ADJUSTMENT,
    compute_dynamic_wacc,
)


# ---------------------------------------------------------------------------
# Canonical fixture (identical to the Sprint 4 dispatch)
# ---------------------------------------------------------------------------

BASE_WACC = 0.09
RISK_FREE = 0.04
ERP = 0.05
BETA = 1.0

#: The pre-registered regime ladder (config/pre_registration_v4.yaml).
REGIME_LADDER = ("EXPANSION", "RECOVERY", "SHOCK", "CRISIS")

VALIDATION_NOTES = """
Sprint 4 finding (bigpickle-worker) — reported, not worked around
==============================================================
compute_dynamic_wacc() accepts a ``base_wacc`` argument and then never reads
it: the body rebuilds WACC bottom-up from (risk_free_rate, beta,
equity_risk_premium, cost_of_debt, weights) via CAPM.  For the dispatch's own
fixture that reconstruction lands at 0.0799, not the 0.09 passed in, so the
EXPANSION regime moves the discount rate by -101bps instead of leaving it
alone.  A parameter that is accepted and ignored is a defect on its own
terms, and it is the reason test_wacc_expansion_unchanged /
test_base_wacc_is_the_expansion_anchor fail.

Both tests are written to the pre-registered contract and are left failing on
purpose.  The minimal fix that satisfies W1-W4 while preserving the existing
erp_mult / rf_adj / beta_adj mechanism is to treat ``base_wacc`` as the
EXPANSION anchor and add the regime *shift* on top of it:

    b0    = REGIME_WACC_ADJUSTMENT["EXPANSION"]
    adj   = REGIME_WACC_ADJUSTMENT[regime]
    d_rf  = adj["rf_adj"]
    d_erp = (beta * adj["beta_adj"]) * erp * adj["erp_mult"] \\
            - (beta * b0["beta_adj"]) * erp
    wacc  = base_wacc \\
            + (equity_weight + debt_weight * (1 - tax_rate)) * d_rf \\
            + equity_weight * d_erp

Verified values for the dispatch fixture (base 0.09, rf 0.04, erp 0.05,
beta 1.0) under that form — each figure below was computed, not estimated:

    EXPANSION 0.090000   (unchanged, W1)
    RECOVERY  0.098116
    SHOCK     0.109790   (> base, W2)
    CRISIS    0.136370   (> SHOCK, W3)

which satisfies W1, W2, W3 and W4 together and keeps every monotonicity
property asserted in this module.  Two traps to avoid while implementing it:

  * Do NOT fold the passed ``risk_free_rate`` into the increment as
    ``(rf + adj["rf_adj"]) - rf``.  The rf terms cancel, ``rf_adj`` becomes a
    dead lever, and the "Higher ERP, higher RF" mechanism named in the
    pre-registration silently stops doing anything.
  * ``risk_free_rate`` becomes redundant with ``base_wacc`` under anchor
    semantics, because ``base_wacc`` already embeds the base risk-free rate.
    Re-applying it would double count.  This is expected, and is why the
    monotonicity tests in this module are written against ERP and beta, with
    the risk-free lever asserted on the published regime table instead.

The exact numbers asserted in the failing tests are deliberately NOT relaxed
to match the current behaviour: a pre-registered falsification test that is
edited to match the code it is meant to audit is not a test.
"""


# ---------------------------------------------------------------------------
# W1 / W2 / W3 — the dispatch's three headline assertions
# ---------------------------------------------------------------------------

def test_wacc_expansion_unchanged():
    """W1 / dispatch: EXPANSION must not move the discount rate.

    This is the anchor test for the whole regime ladder.  If EXPANSION already
    shifts the WACC then "unchanged" has no meaning for SHOCK and CRISIS
    either, and the ke_ORDERING monotonicity gate cannot be read off the
    output.
    """
    wacc = compute_dynamic_wacc(BASE_WACC, "EXPANSION", RISK_FREE, ERP, BETA)
    assert abs(wacc - BASE_WACC) < 0.001, (
        f"EXPANSION must be the neutral regime: got {wacc:.6f} vs base "
        f"{BASE_WACC:.6f} (drift {wacc - BASE_WACC:+.6f}). "
        f"See VALIDATION_NOTES in this module."
    )


def test_wacc_shock_increases():
    """W2 / dispatch: SHOCK raises the WACC (higher ERP, higher risk-free rate)."""
    wacc = compute_dynamic_wacc(BASE_WACC, "SHOCK", RISK_FREE, ERP, BETA)
    assert wacc > BASE_WACC, (
        f"SHOCK must raise the discount rate: got {wacc:.6f} vs base {BASE_WACC:.6f}"
    )


def test_wacc_crisis_max_increase():
    """W3 / dispatch: CRISIS is the largest increase of the ladder."""
    shock = compute_dynamic_wacc(BASE_WACC, "SHOCK", RISK_FREE, ERP, BETA)
    crisis = compute_dynamic_wacc(BASE_WACC, "CRISIS", RISK_FREE, ERP, BETA)
    assert crisis > shock, (
        f"CRISIS must exceed SHOCK: crisis={crisis:.6f} shock={shock:.6f}"
    )


def test_base_wacc_is_the_expansion_anchor():
    """W4: ``base_wacc`` must actually be consumed by the function.

    The objective half of the W1 finding: whatever the regime arithmetic, the
    ``base_wacc`` argument is part of the published signature, so varying it
    must move the answer.  A regime engine that ignores its base rate is a
    regime engine with a misleading API.
    """
    waccs = [
        compute_dynamic_wacc(base, "EXPANSION", RISK_FREE, ERP, BETA)
        for base in (0.07, 0.09, 0.12, 0.15)
    ]
    assert len(set(np.round(waccs, 10))) == len(waccs), (
        f"base_wacc is ignored — all anchors produced {waccs}"
    )
    assert waccs == sorted(waccs), f"WACC must rise with base_wacc: {waccs}"
    # 100bps of base must not move the WACC by less than 1bp.
    span = waccs[-1] - waccs[0]
    assert span > 0.0001, f"base_wacc sensitivity is degenerate: span={span}"


# ---------------------------------------------------------------------------
# W3 — full ladder ordering
# ---------------------------------------------------------------------------

def test_regime_ladder_is_strictly_ordered():
    """W3: EXPANSION < RECOVERY < SHOCK < CRISIS, strictly.

    Mirrors ``ke_ordering`` + ``monotonicity_ke: hard_rejection`` in the
    pre-registration: a non-monotone ladder is a hard rejection upstream, so
    the WACC output must never invert it.
    """
    values = [
        compute_dynamic_wacc(BASE_WACC, r, RISK_FREE, ERP, BETA)
        for r in REGIME_LADDER
    ]
    for lower, higher, lo_v, hi_v in zip(
        REGIME_LADDER, REGIME_LADDER[1:], values, values[1:]
    ):
        assert hi_v > lo_v, (
            f"regime ladder inverted: {higher}={hi_v:.6f} !> {lower}={lo_v:.6f}"
        )
    assert values == sorted(values)


def test_regime_adjustment_table_is_monotone_in_the_frozen_order():
    """W5: the table itself must be ordered by ke, not by insertion order.

    Guards the three levers against a copy/paste transposition, which would
    leave the function monotone by accident while the table claimed otherwise.
    """
    for lever in ("erp_mult", "rf_adj", "beta_adj"):
        values = [REGIME_WACC_ADJUSTMENT[r][lever] for r in REGIME_LADDER]
        assert values == sorted(values), (
            f"{lever} is not monotone along {REGIME_LADDER}: {values}"
        )
    # The frozen ke ordering also has to agree with the HMM state indices used
    # by Quantitative.regime_shield_v3 (0=EXPANSION, 1=SHOCK, 2=CRISIS).
    assert REGIME_WACC_ADJUSTMENT["EXPANSION"]["erp_mult"] < \
        REGIME_WACC_ADJUSTMENT["SHOCK"]["erp_mult"] < \
        REGIME_WACC_ADJUSTMENT["CRISIS"]["erp_mult"]


def test_unknown_regime_falls_back_to_the_neutral_row():
    """An unrecognised regime label must degrade to the neutral row, not crash.

    The HMM emits labels through ``RegimeState``/``STATE_BY_INDEX``, but the
    WACC layer must not raise if a new state name ever lands upstream.
    """
    unknown = compute_dynamic_wacc(BASE_WACC, "MELTDOWN", RISK_FREE, ERP, BETA)
    neutral = compute_dynamic_wacc(BASE_WACC, "EXPANSION", RISK_FREE, ERP, BETA)
    assert unknown == pytest.approx(neutral)
    # A non-string regime must not raise either.
    assert compute_dynamic_wacc(BASE_WACC, None, RISK_FREE, ERP, BETA) == \
        pytest.approx(neutral)


@pytest.mark.parametrize("regime", REGIME_LADDER)
def test_regime_label_is_case_insensitive(regime):
    """Lower-case labels come straight out of some FRED/HMM config paths."""
    upper = compute_dynamic_wacc(BASE_WACC, regime, RISK_FREE, ERP, BETA)
    lower = compute_dynamic_wacc(BASE_WACC, regime.lower(), RISK_FREE, ERP, BETA)
    assert upper == pytest.approx(lower)


# ---------------------------------------------------------------------------
# W6 — single-lever monotonicity
#
# Anchor semantics: EXPANSION is the neutral row, so the regime *adjustment*
# terms vanish there and the output is invariant to the levers.  The adjusting
# regimes (RECOVERY / SHOCK / CRISIS) must be strictly monotone.
# ---------------------------------------------------------------------------

ADJUSTING_REGIMES = ("RECOVERY", "SHOCK", "CRISIS")

@pytest.mark.parametrize("regime", REGIME_LADDER)
def test_wacc_increases_with_beta(regime):
    """W6: a riskier name must not be discounted at a lower rate.

    Strict monotonicity is asserted for the adjusting regimes.  EXPANSION is
    only held to the non-decreasing property here: full invariance at the
    anchor is the job of ``test_wacc_expansion_unchanged``, and duplicating it
    would report the same root-cause defect as N separate failures instead of
    one.  See VALIDATION_NOTES.
    """
    waccs = [
        compute_dynamic_wacc(BASE_WACC, regime, RISK_FREE, ERP, beta)
        for beta in (0.0, 0.5, 1.0, 1.5, 2.5)
    ]
    assert waccs == sorted(waccs), (regime, waccs)
    if regime in ADJUSTING_REGIMES:
        assert waccs[-1] > waccs[0], (regime, waccs)


@pytest.mark.parametrize("regime", REGIME_LADDER)
def test_wacc_increases_with_equity_risk_premium(regime):
    """W6: a wider equity risk premium must not lower the discount rate."""
    waccs = [
        compute_dynamic_wacc(BASE_WACC, regime, RISK_FREE, erp, BETA)
        for erp in (0.01, 0.03, 0.05, 0.08)
    ]
    assert waccs == sorted(waccs), (regime, waccs)
    if regime in ADJUSTING_REGIMES:
        assert waccs[-1] > waccs[0], (regime, waccs)


def test_risk_free_regime_shift_is_ordered_and_non_degenerate():
    """W6: the "higher RF" half of the SHOCK/CRISIS mechanism must be live.

    Under anchor semantics the *passed* ``risk_free_rate`` is redundant with
    ``base_wacc`` (which already embeds the base rate), so the risk-free lever
    is asserted where it actually acts: the per-regime ``rf_adj`` shift.
    """
    shifts = [REGIME_WACC_ADJUSTMENT[r]["rf_adj"] for r in REGIME_LADDER]
    assert shifts == sorted(shifts), shifts
    assert shifts[0] == 0.0, "EXPANSION must carry no risk-free shift"
    assert shifts[-1] > shifts[0], "CRISIS must carry a strictly positive rf shift"
    # The mechanism has to be big enough to matter at a 10bp reporting grid.
    assert shifts[-1] >= 0.010, shifts

    # And the shift must actually reach the output, not just the table: a
    # regime with a larger rf shift must not discount below one with a smaller.
    crisis = compute_dynamic_wacc(BASE_WACC, "CRISIS", RISK_FREE, ERP, BETA)
    shock = compute_dynamic_wacc(BASE_WACC, "SHOCK", RISK_FREE, ERP, BETA)
    recovery = compute_dynamic_wacc(BASE_WACC, "RECOVERY", RISK_FREE, ERP, BETA)
    assert recovery < shock < crisis


@pytest.mark.parametrize("regime", REGIME_LADDER)
def test_wacc_is_finite_and_ordered_across_regimes_at_every_lever(regime):
    """No regime/leverage combination may produce a non-finite or negative WACC.

    A negative discount rate would flip the sign of every duration adjustment
    downstream (Quantitative.iv_nowcast.analytical_duration_adjustment).
    """
    for beta in (0.0, 1.0, 3.0, 5.0):
        for erp in (0.0, 0.05, 0.20):
            w = compute_dynamic_wacc(BASE_WACC, regime, RISK_FREE, erp, beta)
            assert np.isfinite(w), (regime, beta, erp, w)
            assert w >= 0.0, (regime, beta, erp, w)


def test_equity_weight_raises_and_debt_weight_lowers_the_wacc():
    """The capital-structure weights must still behave like weights."""
    equity_heavy = compute_dynamic_wacc(
        BASE_WACC, "SHOCK", RISK_FREE, ERP, BETA, debt_weight=0.05,
        equity_weight=0.95,
    )
    debt_heavy = compute_dynamic_wacc(
        BASE_WACC, "SHOCK", RISK_FREE, ERP, BETA, debt_weight=0.50,
        equity_weight=0.50,
    )
    assert equity_heavy > debt_heavy


# ---------------------------------------------------------------------------
# W7 — DCF screen wiring
# ---------------------------------------------------------------------------

def _dcf_config(base_wacc: float = BASE_WACC) -> dict:
    """Minimal JSON-serialisable DCF config (run_dcf_for_ticker deep-copies it)."""
    return {
        "monte_carlo": {
            "wacc_dist": {"mean": base_wacc, "std": 0.01, "min": 0.05, "max": 0.15},
            "rd_capitalization_rate": 0.15,
            "n_passes": 100,
        },
        "risk_free_rate": RISK_FREE,
        "equity_risk_premium": ERP,
    }


@pytest.fixture
def wired_dcf(monkeypatch):
    """Stub out the DCF's data + Monte Carlo layers, keeping the WACC wiring real.

    ``run_dcf_for_ticker`` is the only consumer of ``compute_dynamic_wacc``
    outside its own unit test, so this is where the regime actually reaches the
    valuation.  Everything downstream of the WACC is stubbed so the assertion
    is about the wiring, not about the Monte Carlo noise.
    """
    import Quantitative.dcf_screen as dcf_mod
    import Quantitative.wacc_panel as wacc_mod

    calls: dict = {"dynamic_wacc": [], "monte_carlo": []}

    def spy_dynamic_wacc(**kwargs):
        calls["dynamic_wacc"].append(kwargs)
        return wacc_mod.__dict__["_REAL_compute_dynamic_wacc"](**kwargs)

    # run_dcf_for_ticker does a lazy `from Quantitative.wacc_panel import ...`
    # inside the function body, so patching the module attribute is enough.
    real = wacc_mod.compute_dynamic_wacc
    wacc_mod._REAL_compute_dynamic_wacc = real
    monkeypatch.setattr(wacc_mod, "compute_dynamic_wacc", spy_dynamic_wacc)

    inputs = dcf_mod.DCFInputs(
        ticker="TST", revenue=1_000e6, ebit=120e6, tax_rate=0.21,
        capex=40e6, depreciation=30e6, change_nwc=5e6, rd_expense=50e6,
        total_debt=300e6, cash=100e6, shares_outstanding=50e6,
        current_price=25.0, sector="Technology", degraded=False,
    )
    monkeypatch.setattr(dcf_mod, "load_financial_inputs", lambda t: inputs)
    monkeypatch.setattr(dcf_mod, "load_sector_sigma", lambda t: 0.30)
    monkeypatch.setattr(dcf_mod, "capitalize_rd", lambda rd, rate=0.15: rd * rate)
    monkeypatch.setattr(dcf_mod, "compute_fcf", lambda i, rd, cfg: 100e6)
    monkeypatch.setattr(dcf_mod, "compute_invested_capital", lambda i, rd: 500e6)
    monkeypatch.setattr(dcf_mod, "compute_roic", lambda f, ic: 0.20)
    monkeypatch.setattr(dcf_mod, "compute_solvency_ratio", lambda i: 2.5)
    monkeypatch.setattr(dcf_mod, "compute_margin_health", lambda i: 0.30)
    monkeypatch.setattr(dcf_mod, "compute_sector_discount", lambda s, cfg: 0.10)
    monkeypatch.setattr(dcf_mod, "evaluate_dcf_screen", lambda o, c: True)

    def spy_monte_carlo(fcf, invested, roic, discount_rate, config, inputs, **kw):
        calls["monte_carlo"].append(config)
        percentiles = {k: 30.0 for k in ("mean", "p25", "p10", "p50", "p75", "p90", "std")}
        return np.array([30.0]), percentiles

    monkeypatch.setattr(dcf_mod, "monte_carlo_dcf", spy_monte_carlo)
    return calls


def test_dcf_uses_dynamic_wacc(wired_dcf):
    """W7 / dispatch: the DCF discounts at the regime-adjusted WACC.

    Asserts the full chain: the regime reaches ``compute_dynamic_wacc``, the
    result lands in the Monte Carlo WACC distribution, and the caller's config
    is left untouched.
    """
    import Quantitative.dcf_screen as dcf_mod

    config = _dcf_config()
    original = copy.deepcopy(config)

    dcf_mod.run_dcf_for_ticker("TST", config, regime="CRISIS")

    # 1. The regime was actually threaded into the WACC call.
    assert len(wired_dcf["dynamic_wacc"]) == 1
    kwargs = wired_dcf["dynamic_wacc"][0]
    assert kwargs["regime"] == "CRISIS"
    assert kwargs["base_wacc"] == pytest.approx(BASE_WACC)
    assert kwargs["risk_free_rate"] == pytest.approx(RISK_FREE)
    assert kwargs["equity_risk_premium"] == pytest.approx(ERP)

    # 2. The regime-adjusted rate, not the base rate, reached the Monte Carlo.
    assert len(wired_dcf["monte_carlo"]) == 1
    mc_wacc = wired_dcf["monte_carlo"][0]["monte_carlo"]["wacc_dist"]["mean"]
    expected = compute_dynamic_wacc(BASE_WACC, "CRISIS", RISK_FREE, ERP, BETA)
    assert mc_wacc == pytest.approx(expected)
    assert mc_wacc != pytest.approx(BASE_WACC), (
        "CRISIS must not be discounted at the EXPANSION/base rate"
    )

    # 3. The caller's config is not mutated in place.
    assert config == original


@pytest.mark.parametrize("regime", REGIME_LADDER)
def test_dcf_discount_rate_increases_with_the_regime(wired_dcf, regime):
    """W7: the regime ladder has to survive the trip through the DCF screen.

    A monotone ``compute_dynamic_wacc`` that the DCF then flattens (for
    instance by overwriting the config mean with the base value) would pass the
    unit tests above and still discount every regime identically.
    """
    import Quantitative.dcf_screen as dcf_mod

    config = _dcf_config()
    dcf_mod.run_dcf_for_ticker("TST", config, regime=regime)

    mc_wacc = wired_dcf["monte_carlo"][0]["monte_carlo"]["wacc_dist"]["mean"]
    expected = compute_dynamic_wacc(BASE_WACC, regime, RISK_FREE, ERP, BETA)
    assert mc_wacc == pytest.approx(expected), regime
    assert np.isfinite(mc_wacc) and mc_wacc > 0.0, regime


def test_dcf_degraded_ticker_short_circuits_before_the_wacc(wired_dcf,
                                                           monkeypatch):
    """A degraded ticker must not consume a WACC call.

    Degraded inputs mean no valuation, so the regime must not touch the
    discount rate at all — otherwise a coverage gap would be silently priced.
    """
    import Quantitative.dcf_screen as dcf_mod

    monkey_inputs = dcf_mod.DCFInputs(
        ticker="TST", revenue=None, degraded=True, missing_fields=["revenue", "ebit"]
    )
    # monkeypatch, not a bare assignment: a direct rebind of the module global
    # would survive the test and silently poison every later DCF test in the
    # session that relies on the real loader.
    monkeypatch.setattr(dcf_mod, "load_financial_inputs", lambda t: monkey_inputs)

    outputs = dcf_mod.run_dcf_for_ticker("TST", _dcf_config(), regime="CRISIS")

    assert outputs.degraded is True
    assert outputs.iv_p25 == 0.0
    assert wired_dcf["dynamic_wacc"] == []
    assert wired_dcf["monte_carlo"] == []
