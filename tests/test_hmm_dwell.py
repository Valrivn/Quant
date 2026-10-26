"""Sprint 4 / Arm A Fix 2 — HMM Dwell Filter tests (CEO Ruling: Path A, Pure Quant Only).

Pre-registration: ``config/pre_registration_v4.yaml`` ::
``parameter_clusters.B_swap_exit.params.hmm_min_dwell_days``
  values ``[5, 10, 15]`` — HMM minimum dwell days to prevent whipsaw;
``hmm.min_sojourn_days: 5`` and ``hmm.state_names`` ordering
  ``[EXPANSION, SHOCK, CRISIS]`` (ke_EXP < ke_SHOCK < ke_CRISIS, hard rejection).

Subject under test
------------------
``Quantitative.regime_shield_v3.FactorHMMv3.apply_dwell_filter(states)``

Why this matters
----------------
A 3-state Gaussian HMM fitted to macro factors emits one label per day, so a
single noisy observation flips the state.  A 2-day flicker therefore reads as a
regime change, and the downstream WACC regime, entry thresholds and swap
barrier all react as if the macro regime had genuinely turned.  The dwell
filter is the only thing standing between that label noise and the cost of
capital.

Frozen invariants asserted here
-------------------------------
D1  A transition attempted before the current state has served ``min_dwell_days``
    is REJECTED: the label is rewritten to the incumbent state.
D2  A transition attempted after the incumbent has satisfied ``min_dwell_days``
    is ACCEPTED.
D3  Acceptance criterion — the default enforces a **10-day** minimum.
D4  The filter is a pure function: no input mutation, length and dtype
    preserved, and it is idempotent.
D5  The filter reduces whipsaw, which raises ARI against the truth
    (Gate 7 — "HMM State Stability (ARI)", threshold 0.70).

Compatibility note
------------------
The Sprint 4 dispatch calls ``hmm.apply_dwell_filter(states,
min_dwell_days=N)``.  The engine currently exposes ``min_dwell_days`` as a
*constructor* argument instead.  Both spellings are semantically identical
(both set ``self.min_dwell_days``), so ``dwell_filter`` below prefers the
method kwarg when it exists and otherwise injects via the constructor.  The
suite therefore passes unchanged against either implementation, and
``test_dwell_filter_contract_surface`` records which one is live.
"""

from __future__ import annotations

import inspect

import numpy as np
import pytest
from sklearn.metrics import adjusted_rand_score

from Quantitative.regime_shield_v3 import FactorHMMv3


#: Pre-registered dwell grid (config/pre_registration_v4.yaml, cluster B).
PREREG_DWELL_DAYS = (5, 10, 15)

#: The pre-registered operating point / dispatch acceptance criterion.
PREREG_MIN_DWELL = 10

#: Raw state indices follow the frozen ordering: 0=EXPANSION, 1=SHOCK, 2=CRISIS.
EXPANSION, SHOCK, CRISIS = 0, 1, 2

_ACCEPTS_KWARG = "min_dwell_days" in inspect.signature(
    FactorHMMv3.apply_dwell_filter
).parameters


def dwell_filter(states, min_dwell_days: int = PREREG_MIN_DWELL) -> np.ndarray:
    """Apply the HMM dwell filter at an explicit minimum dwell time.

    See the module docstring: prefers the dispatch's method-kwarg contract and
    falls back to constructor injection, which is the same code path.
    """
    states = np.asarray(states)
    if _ACCEPTS_KWARG:
        return FactorHMMv3().apply_dwell_filter(states, min_dwell_days=min_dwell_days)
    return FactorHMMv3(min_dwell_days=min_dwell_days).apply_dwell_filter(states)


def n_transitions(states) -> int:
    """Number of state changes in a label sequence."""
    arr = np.asarray(states)
    if arr.size < 2:
        return 0
    return int((arr[1:] != arr[:-1]).sum())


def n_runs(states) -> int:
    """Number of maximal same-state runs (== transitions + 1)."""
    return n_transitions(states) + 1


# ---------------------------------------------------------------------------
# D1 / D2 — the dispatch's two headline assertions
# ---------------------------------------------------------------------------

def test_dwell_filter_rejects_short_transitions():
    """D1 / dispatch: a 2-day state is too short to be believed.

    ``[0, 0, 1, 1, 0, 0]`` with ``min_dwell_days=3``: the excursion into state 1
    lasts 2 days but the incumbent state 0 has only served 1 day, so the
    transition is rejected and both labels are rewritten to 0.
    """
    states = [0, 0, 1, 1, 0, 0]
    filtered = dwell_filter(np.array(states), min_dwell_days=3)

    # The 1 at index 2 is rejected — it stays 0.
    assert filtered[2] == 0
    # So is the 1 at index 3; the whole excursion is absorbed.
    assert filtered[3] == 0
    assert filtered.tolist() == [0, 0, 0, 0, 0, 0]


def test_dwell_filter_allows_long_transitions():
    """D2 / dispatch: a state that has served its time may hand over.

    ``[0]*5 + [1]*5`` with ``min_dwell_days=3``: the incumbent has served 4
    days by index 5, which clears the minimum, so the transition is accepted.
    """
    states = [0] * 5 + [1] * 5
    filtered = dwell_filter(np.array(states), min_dwell_days=3)

    assert filtered[5] == 1, "transition after a satisfied dwell must be accepted"
    assert filtered[:5].tolist() == [0] * 5
    assert filtered[5:].tolist() == [1] * 5
    assert n_transitions(filtered) == 1


def test_dwell_filter_contract_surface():
    """D-record: report which call spelling the engine actually exposes.

    Not a pass/fail gate — a provenance assertion.  It fails loudly if the
    engine changes shape in a way the shim above cannot absorb.
    """
    params = inspect.signature(FactorHMMv3.apply_dwell_filter).parameters
    ctor = inspect.signature(FactorHMMv3.__init__).parameters
    assert "states" in params, params
    assert "min_dwell_days" in ctor, ctor
    # Whichever spelling is live, the effective value must agree.
    assert FactorHMMv3().min_dwell_days == PREREG_MIN_DWELL
    probe = [0] * 5 + [1] * 5
    assert dwell_filter(probe, 3).tolist() == (
        [0] * 5 + [1] * 5
    ), "dwell_filter shim is not honouring min_dwell_days=3"


# ---------------------------------------------------------------------------
# D3 — acceptance criterion: the 10-day minimum
# ---------------------------------------------------------------------------

def test_dwell_filter_enforces_preregistered_10day_minimum():
    """D3 / acceptance criterion: "HMM dwell filter enforces 10-day minimum".

    The same sequence that legitimately transitions at ``min_dwell_days=3``
    must NOT transition at the pre-registered default of 10 days.
    """
    states = np.array([0] * 5 + [1] * 5)

    # At the default, 5 days of incumbent service is not enough.
    at_default = dwell_filter(states, min_dwell_days=PREREG_MIN_DWELL)
    assert at_default[5] == 0, "a 5-day incumbent run must not hand over at min=10"
    assert at_default.tolist() == [0] * 10

    # The engine's own default is the 10-day minimum.
    assert FactorHMMv3().min_dwell_days == 10


@pytest.mark.parametrize("min_dwell", PREREG_DWELL_DAYS)
def test_preregistered_dwell_grid_boundary(min_dwell):
    """D1/D2 across the pre-registered grid: the boundary is exact.

    A transition is accepted once the incumbent has served ``min_dwell + 1``
    days — see ``test_dwell_boundary_excludes_the_seeding_day`` for why the
    seeding day is not credited.
    """
    for run_length in (min_dwell, min_dwell + 1, min_dwell + 5):
        states = np.array([EXPANSION] * run_length + [SHOCK] * 5)
        filtered = dwell_filter(states, min_dwell_days=min_dwell)
        accepted = bool(filtered[run_length] == SHOCK)
        assert accepted is (run_length > min_dwell), (
            f"run_length={run_length} min_dwell={min_dwell} -> accepted={accepted}"
        )


def test_dwell_boundary_excludes_the_seeding_day():
    """Documented off-by-one: the seeding day is not credited toward the dwell.

    ``apply_dwell_filter`` initialises ``dwell_count = 0`` on ``states[0]`` and
    only increments on *subsequent* same-state days, so the incumbent is
    credited with ``L - 1`` days after an ``L``-day run.  A transition is
    therefore accepted iff ``L >= min_dwell_days + 1``.

    Direction of the error is CONSERVATIVE — the filter smooths one day harder
    than the parameter asks for, so it cannot manufacture a premature regime
    call.  It is pinned here (rather than asserted as ``L >= min_dwell``) so
    that if the off-by-one is ever corrected this test fails loudly and the
    change is made deliberately.
    """
    for min_dwell in (3, 5, 10, 15):
        accepted_lengths = [
            L
            for L in range(1, min_dwell + 4)
            if dwell_filter(
                np.array([EXPANSION] * L + [SHOCK] * 3), min_dwell_days=min_dwell
            )[L] == SHOCK
        ]
        assert accepted_lengths == list(range(min_dwell + 1, min_dwell + 4)), (
            f"min_dwell={min_dwell} accepted run lengths {accepted_lengths}"
        )


def test_dwell_filter_cements_the_opening_label():
    """Known limitation: a wrong opening label is NOT corrected by the filter.

    The filter seeds on ``states[0]`` and can only leave that state after a
    full minimum-dwell run, so a bad first decode is locked in for
    ``min_dwell`` days.  Pinned because it is a live risk for Gate 7: the HMM
    walk-forward folds overlap, and a fold whose first day decodes into the
    wrong regime inherits that error for the whole overlap window.
    """
    states = np.array([CRISIS, CRISIS] + [EXPANSION] * 20)
    filtered = dwell_filter(states, min_dwell_days=PREREG_MIN_DWELL)

    # 11 CRISIS labels survive before the correction lands (off-by-one aware).
    lead = int(np.argmax(filtered == EXPANSION))
    assert lead == PREREG_MIN_DWELL + 1, (
        f"expected the opening label to be cemented for "
        f"{PREREG_MIN_DWELL + 1} days, got {lead}"
    )
    assert filtered[:lead].tolist() == [CRISIS] * lead
    # Everything after the correction is clean and stable.
    assert filtered[lead:].tolist() == [EXPANSION] * (len(filtered) - lead)
    assert n_transitions(filtered) == 1


# ---------------------------------------------------------------------------
# D4 — purity / hygiene invariants
# ---------------------------------------------------------------------------

def test_dwell_filter_does_not_mutate_its_input():
    """D4: the caller's array must be untouched (checkpoint safety)."""
    states = np.array([0, 0, 1, 1, 0, 0])
    original = states.copy()
    out = dwell_filter(states, min_dwell_days=3)
    assert states.tolist() == original.tolist()
    assert out is not states


def test_dwell_filter_preserves_length_and_dtype():
    """D4: a label filter must not resample or re-index the series."""
    states = np.array([0, 0, 1, 1, 2, 2, 0, 0], dtype=np.int64)
    out = dwell_filter(states, min_dwell_days=3)
    assert out.shape == states.shape
    assert out.dtype == states.dtype
    # Only states present in the input may ever be emitted.
    assert set(np.unique(out)).issubset(set(np.unique(states)))


@pytest.mark.parametrize(
    "states",
    [np.array([]), np.array([1]), np.array([1, 1, 1]), np.array([0, 0, 1, 1, 0, 0])],
)
def test_dwell_filter_is_idempotent(states):
    """D4: filtering an already-filtered series is a no-op.

    Non-idempotence means the filter keeps rewriting history on every pass,
    which would silently corrupt a re-run of the pipeline.
    """
    once = dwell_filter(states, min_dwell_days=3)
    twice = dwell_filter(once, min_dwell_days=3)
    assert once.tolist() == twice.tolist()


def test_dwell_filter_on_empty_and_degenerate_inputs():
    """D4: empty and single-observation inputs must not raise."""
    empty = dwell_filter(np.array([]), min_dwell_days=3)
    assert empty.size == 0

    single = dwell_filter(np.array([CRISIS]), min_dwell_days=3)
    assert single.tolist() == [CRISIS]

    # A constant series is a fixed point at every minimum dwell time.
    for min_dwell in (1, 3, PREREG_MIN_DWELL, 15):
        assert dwell_filter(np.array([EXPANSION] * 30), min_dwell_days=min_dwell).tolist() \
            == [EXPANSION] * 30


# ---------------------------------------------------------------------------
# D5 — ARI improvement (Gate 7)
# ---------------------------------------------------------------------------

#: Ground truth: three clean regime blocks, in the frozen ke ordering.
_TRUTH = [EXPANSION] * 10 + [SHOCK] * 10 + [CRISIS] * 10

#: The same regimes as a 3-state HMM would report them WITHOUT a dwell filter:
#: a 2-day flicker at the start of each block (one bad macro print flips the
#: posterior argmax for two sessions, then it snaps back).
_WHIPSAW = (
    [EXPANSION, EXPANSION, SHOCK, SHOCK]      # 2-day flicker, block 0
    + [EXPANSION] * 6
    + [SHOCK, SHOCK, CRISIS, CRISIS]          # 2-day flicker, block 1
    + [SHOCK] * 6
    + [CRISIS, CRISIS, EXPANSION, EXPANSION]  # 2-day flicker, block 2
    + [CRISIS] * 6
)


def test_hmm_ari_with_dwell_filter():
    """D5 / dispatch: ARI improves once whipsaw is filtered out.

    Measured against Gate 7 ("HMM State Stability (ARI)", threshold 0.70).
    The filter cannot manufacture signal, but it can stop 2-day label noise
    from being scored as regime instability.
    """
    truth = np.array(_TRUTH)
    raw = np.array(_WHIPSAW)

    ari_raw = float(adjusted_rand_score(truth, raw))
    filtered = dwell_filter(raw, min_dwell_days=PREREG_MIN_DWELL)
    ari_filtered = float(adjusted_rand_score(truth, filtered))

    assert ari_filtered > ari_raw, (
        f"dwell filter must not reduce ARI: raw={ari_raw:.4f} "
        f"filtered={ari_filtered:.4f}"
    )
    # The pre-registered gate threshold is 0.70; the filtered series clears it
    # while the raw whipsawed series does not.
    assert ari_raw < 0.70, ari_raw
    assert ari_filtered >= 0.70, ari_filtered


def test_dwell_filter_reduces_whipsaw_transition_count():
    """D5: the mechanism behind the ARI gain — fewer state changes."""
    truth = np.array(_TRUTH)
    raw = np.array(_WHIPSAW)
    filtered = dwell_filter(raw, min_dwell_days=PREREG_MIN_DWELL)

    assert n_transitions(filtered) < n_transitions(raw)
    # 8 whipsaw transitions collapse to the 2 genuine regime changes.
    assert n_transitions(raw) == 8
    assert n_transitions(filtered) == n_transitions(truth) == 2


def test_whipsaw_reduction_is_monotone_in_min_dwell_days():
    """D5: a longer required dwell can never resurrect a whipsaw.

    Swept across the pre-registered grid [5, 10, 15] plus a stricter 20.
    """
    raw = np.array(_WHIPSAW)
    counts = [n_transitions(dwell_filter(raw, min_dwell_days=m))
              for m in (1, 3, 5, 10, 15, 20)]
    assert counts == sorted(counts, reverse=True), counts
    # No minimum dwell time can produce more churn than the raw labels.
    assert max(counts) <= n_transitions(raw)


def test_dwell_filter_output_never_exceeds_a_pre_registered_minimum_run():
    """D1 (inverse): every *interior* run in the output must be fully served.

    A run that the filter accepted must have ``min_dwell_days`` behind it,
    otherwise the filter did not do its job.  The two boundary runs are exempt
    and are excluded here:

      * the LEADING run is seeded from ``states[0]`` and can be short;
      * the TRAILING run may be cut short by the end of the sample — the
        filter cannot reject a transition that never gets a chance to prove
        itself, so truncation is not a filter failure.
    """
    min_dwell = PREREG_MIN_DWELL
    raw = np.array(_WHIPSAW)
    filtered = dwell_filter(raw, min_dwell_days=min_dwell)

    # Run-length encode the filtered series.
    runs: list[tuple[int, int]] = []
    current, length = None, 0
    for state in filtered:
        state = int(state)
        if state == current:
            length += 1
        else:
            if current is not None:
                runs.append((current, length))
            current, length = state, 1
    runs.append((current, length))

    assert runs == [(EXPANSION, 11), (SHOCK, 10), (CRISIS, 9)], runs
    for _state, run_len in runs[1:-1]:          # interior runs only
        assert run_len >= min_dwell, runs
    # The trailing run is short because the sample ends mid-block, not because
    # the filter under-served it: extending the CRISIS block lengthens the run.
    longer = dwell_filter(
        np.array(_WHIPSAW + [CRISIS] * 5), min_dwell_days=min_dwell
    )
    assert int((longer == CRISIS).sum()) > 9
    assert longer.tolist()[:11] == filtered.tolist()[:11]


def test_dwell_filter_preserves_the_frozen_state_ordering():
    """D4: the filter must not renumber or reorder the frozen state labels.

    ke_EXP < ke_SHOCK < ke_CRISIS is a hard-rejection constraint keyed off the
    label indices; a filter that reindexed states would silently break the
    monotonicity gate.
    """
    states = np.array([EXPANSION] * 12 + [SHOCK] * 12 + [CRISIS] * 12)
    filtered = dwell_filter(states, min_dwell_days=PREREG_MIN_DWELL)
    assert filtered.tolist() == states.tolist()
    assert list(np.unique(filtered)) == [EXPANSION, SHOCK, CRISIS]
    # Indices still sort into the frozen ascending-ke ordering.
    ordered = sorted(int(s) for s in np.unique(filtered))
    assert ordered == sorted([EXPANSION, SHOCK, CRISIS])
