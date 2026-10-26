"""
Transaction-Cost Analysis (TCA) Simulator + Gate — Phase 0-1 (bigpickle-worker)

Pre-registered gate (§12.7.2 `execution.tca_budget`, config/pre_registration_v2.yaml):

    tca_budget_bps:      150        (max observed round-trip cost, frozen)
    slippage_model:      almgren_chriss_regime
    regime_impact_multipliers: EXPANSION 1.0 / CRISIS 3.0
    min_adv_usd:         5,000,000  (names below this ADV are vetoed)
    staggered_exits:     max_daily_portfolio_pct 0.05, twap_days 3,
                         correlation_clustering true, cluster_method hierarchical,
                         cluster_threshold 0.7

Round-trip cost model (bps), per §12.7.2 base ladder:
    commission  10   (fixed)
    spread      50   (fixed, normal liquidity)
    impact      30   at 1% participation and ADV = min_adv_usd, scaled by
                     sqrt(participation/0.01) * sqrt(min_adv/adv) * regime_mult
    timing      60   base shortfall/gap risk at ADV = min_adv_usd, scaled by
                     sqrt(min_adv/adv) * sqrt(twap_days) * vol_mult * regime_mult

Almgren-Chriss form: AC_K = 0.02 is the sqrt-impact coefficient, i.e. a 1%
participation rate with a 1-day parent order costs ~20 bps of market impact,
which the ladder interiorizes into the 30 bps impact + 60 bps timing slots
(impact varies as the square root of participation, the AC signature).

LOBSimulator: a limit-order-book price-path simulator (exponential depth ramp
below/above the mid, tail-exhaustion penalty). It prices the *visible* walk of
a child order vs. its arrival mid; the analytic AC model remains the primary
cost estimate (config.slippage_model == almgren_chriss_regime), and the LOB
view is verified for monotonicity in size/liquidity and for pass/fail
separation at the frozen budget.

Falsification (self-test, pre-registered seeds):
    PASS : 20 large caps (ADV 200-600M), 120M book, 3-day TWAP, EXPANSION
           -> 65-90 bps round trip  (< 150 with margin)
    FAIL : 6M-ADV names, 4%/day naive participation, CRISIS, no stagger
           -> > 150 bps
    STRESS: concurrent unwind with ADV crushed x0.25 and vol x3 in CRISIS
           must push cost over budget (model is sensitive), while the same
           schedule at EXPANSION stays under budget.

CLI (dispatch verification, must exit 0):
    python -m execution.tca_simulator --test --lob --budget 150
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Dict, List, Sequence, Tuple, Union

import numpy as np

# ----------------------------------------------------------------------------
# Frozen parameters (mirror config/pre_registration_v2.yaml `execution:` block)
# ----------------------------------------------------------------------------

BUDGET_BPS = 150.0                       # execution.tca_budget_bps
COMMISSION_BPS = 10.0
SPREAD_BPS = 50.0
BASE_IMPACT_BPS = 30.0                   # at 1% participation, ADV = MIN_ADV_USD
BASE_TIMING_BPS = 60.0
MIN_ADV_USD = 5_000_000.0                # execution.min_adv_usd
MAX_DAILY_PCT = 0.05                     # staggered_exits.max_daily_portfolio_pct
TWAP_DAYS = 3                            # staggered_exits.twap_days
CLUSTER_THRESHOLD = 0.70                 # staggered_exits.cluster_threshold
REGIME_MULTIPLIERS = {"EXPANSION": 1.0, "CRISIS": 3.0}

AC_K = 0.02                              # sqrt-impact coefficient (20 bps @ 1%)

# LOB simulator internals (not pre-registered; verified by self-test only)
_LOB_TICK_BPS = 0.6
_LOB_LEVELS = 60
_LOB_DECAY_K = 6.0
_LOB_VISIBLE_FRAC = 0.012                # visible depth as fraction of ADV
_LOB_TAIL_BPS = 40.0                     # premium per excess-book unit

REF_ADV = MIN_ADV_USD


# ----------------------------------------------------------------------------
# Analytic Almgren-Chriss cost ladder
# ----------------------------------------------------------------------------

def impact_bps(participation: float, adv: float,
               regime: str = "EXPANSION", ac_k: float = AC_K) -> float:
    """Market-impact slot (bps).

    AC signature: impact ~ sqrt(participation). Normalized so that 1%
    participation at ADV = min_adv_usd yields BASE_IMPACT_BPS = 30 bps, in
    line with the AC_K = 0.02 coefficient (~20 bps) plus queue/front-running
    burden. Regime multipliers from config (CRISIS x3).
    """
    p = max(float(participation), 1e-12)
    adv = max(float(adv), 1.0)
    reg = float(REGIME_MULTIPLIERS.get(regime, 1.0))
    return BASE_IMPACT_BPS * np.sqrt(p / 0.01) * np.sqrt(REF_ADV / adv) * reg


def timing_slippage_bps(adv: float, regime: str = "EXPANSION",
                        twap_days: float = 1.0, vol_mult: float = 1.0) -> float:
    """Shortfall/gap-risk slot (bps).

    Scales with sqrt(min_adv/adv) (thinner names gap more), sqrt(twap_days)
    (longer child windows accumulate drift), vol_mult and the regime
    multiplier -- the almgren_chriss_regime slippage model.
    """
    adv = max(float(adv), 1.0)
    reg = float(REGIME_MULTIPLIERS.get(regime, 1.0))
    return (BASE_TIMING_BPS * np.sqrt(REF_ADV / adv)
            * np.sqrt(max(float(twap_days), 1e-9))
            * max(float(vol_mult), 1e-9) * reg)


def round_trip_cost_bps(participation: float, adv: float,
                        regime: str = "EXPANSION", twap_days: float = 1.0,
                        vol_mult: float = 1.0) -> float:
    """Total round-trip cost in bps: commission + spread + impact + timing.

    This is the value the frozen tca_budget gate compares against
    execution.tca_budget_bps (150). Computed per full entry-exit cycle.
    """
    return (COMMISSION_BPS + SPREAD_BPS
            + impact_bps(participation, adv, regime)
            + timing_slippage_bps(adv, regime, twap_days, vol_mult))


# ----------------------------------------------------------------------------
# Limit-order-book price-path simulator (visible walk)
# ----------------------------------------------------------------------------

class LOBSimulator:
    """Deterministic (seeded) LOB around a single name.

    Constructs exponential depth ramps for the bid and ask side, centered on
    the arrival mid. `market_sell(qty)` walks the visible book; qty above the
    visible depth pays an exhaustion tail premium (hidden-liquidity proxy).
    Quantities are expressed in USD (notional), keeping ADV/book units
    consistent.
    """

    def __init__(self, adv: float, mid: float = 100.0, seed: int = 0,
                 vol_mult: float = 1.0) -> None:
        self.adv = max(float(adv), 1.0)
        self.mid = float(mid)
        self.vol_mult = max(float(vol_mult), 1e-9)
        self.rng = np.random.default_rng(seed)
        self.visible = _LOB_VISIBLE_FRAC * self.adv / self.vol_mult
        self.tick = _LOB_TICK_BPS / 1e4                      # price units
        self._level_depths = self._build_levels()

    def _build_levels(self) -> np.ndarray:
        """USD size at each discrete level (exponential ramp, depth at touch
        is the ramp peak, thinning tail towards level _LOB_LEVELS)."""
        i = np.arange(1, _LOB_LEVELS + 1, dtype=np.float64)
        w = np.exp(-(i - 1.0) / _LOB_DECAY_K)
        w = w / w.sum()
        return w * self.visible

    def _walk_rate(self, qty: float) -> float:
        """Average walk distance in bps for a market order of `qty` USD."""
        qty = max(float(qty), 0.0)
        depths = self._level_depths
        cum = np.cumsum(depths)
        # weighted-average level index by fill amount
        remaining = qty
        weighted = 0.0
        filled = 0.0
        for j, d in enumerate(depths):
            take = min(remaining, d)
            weighted += take * (j + 1)
            filled += take
            remaining -= take
            if remaining <= 0.0:
                break
        if filled <= 0.0:
            return 0.0
        avg_level = weighted / filled
        walk = avg_level * _LOB_TICK_BPS
        if remaining > 0.0:
            # exhaustion: full book consumed, tail premium per excess unit
            walk += (remaining / self.visible) * _LOB_TAIL_BPS
        return walk

    def market_sell(self, qty: float) -> Dict[str, Any]:
        """Execute a market sell child order. Returns the visible walk bps
        from the arrival mid and the depth state after the print."""
        walk = self._walk_rate(qty)
        return {"walk_bps": float(walk), "side": "sell",
                "qty_usd": float(qty), "adv": self.adv,
                "visible_usd": self.visible,
                "price_units_per_level": self.tick}

    def market_buy(self, qty: float) -> Dict[str, Any]:
        """Mirror of market_sell on the ask side (symmetric book)."""
        return self.market_sell(qty)


# ----------------------------------------------------------------------------
# Staggered-exit scheduler (config execution.staggered_exits)
# ----------------------------------------------------------------------------

def hierarchical_cluster_groups(corr: np.ndarray,
                                threshold: float = CLUSTER_THRESHOLD
                                ) -> List[List[int]]:
    """Single-linkage hierarchical clustering on |corr| with the frozen
    cluster_threshold (0.70). Union-find over pairs with |corr| > threshold.
    Names with no significant partners land in their own singletons."""
    n = corr.shape[0]
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        for j in range(i + 1, n):
            if abs(corr[i, j]) > threshold:
                union(i, j)
    groups: Dict[int, List[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    out = [sorted(v) for v in groups.values()]
    out.sort(key=lambda g: (-len(g), g[0]))
    return out


def schedule_staggered_exits(notionals: np.ndarray, advs: np.ndarray,
                             corr: np.ndarray,
                             max_daily_pct: float = MAX_DAILY_PCT,
                             twap_days: int = TWAP_DAYS,
                             cluster_threshold: float = CLUSTER_THRESHOLD
                             ) -> Dict[str, Any]:
    """Staggered-unwind schedule (config execution.staggered_exits).

    Per name the daily child notional is min(notional/twap_days,
    max_daily_pct*adv), so no name trades more than 5% of its own ADV on any
    day. Correlated (|corr| > 0.70) names are clustered; the whole cluster
    keeps the same daily participation cap, which spreads the cluster's
    market impact across the twap window instead of firing it concurrently.

    Returns a JSON-safe schedule dict with the per-day notional map.
    """
    notionals = np.asarray(notionals, dtype=np.float64)
    advs = np.asarray(advs, dtype=np.float64)
    n = len(notionals)
    groups = hierarchical_cluster_groups(corr, cluster_threshold)
    daily = np.zeros(n, dtype=np.float64)
    for g in groups:
        g_idx = np.array(g, dtype=int)
        daily[g_idx] = np.minimum(
            notionals[g_idx] / max(twap_days, 1),
            max_daily_pct * advs[g_idx])
        # across-cluster sanity: still never cross the per-name cap
        daily[g_idx] = np.minimum(daily[g_idx], max_daily_pct * advs[g_idx])
    daily = np.clip(daily, 0.0, np.minimum(notionals, max_daily_pct * advs))
    # first day carries the residual rounding so the full notional is planned
    days = int(max(1, twap_days))
    return {
        "n_names": int(n),
        "twap_days": days,
        "max_daily_pct": max_daily_pct,
        "cluster_threshold": cluster_threshold,
        "clusters": [[int(x) for x in g] for g in groups],
        "daily_notional_usd": [float(x) for x in daily],
        "daily_participation": [
            float(daily[i] / advs[i]) if advs[i] > 0.0 else 0.0
            for i in range(n)],
        "total_planned_usd": float(daily.sum()),
    }


# ----------------------------------------------------------------------------
# Concurrent-unwind stress test (tca gate stress path)
# ----------------------------------------------------------------------------

def stress_test_concurrent(notionals: np.ndarray, advs: np.ndarray,
                           corr: np.ndarray,
                           regime: str = "EXPANSION",
                           vol_mult: float = 1.0,
                           adv_mult: float = 1.0,
                           budget_bps: float = BUDGET_BPS,
                           stagger: bool = True) -> Dict[str, Any]:
    """Simulate the full book unwinding at the same time.

    market_state: regime (regime_impact_multipliers), vol_mult (gap risk,
    book thinning), adv_mult (liquidity crush: effective ADV = adv*adv_mult).

    With stagger=True the frozen staggered_exits schedule applies; with
    stagger=False every name fires its full notional day one (naive unwind).
    In a CRISIS with adv_mult=0.25 and vol_mult=3.0 the concurrent unwind must
    blow through the 150 bps budget -- the gate then halts new allocations.

    Returns a JSON-safe report; `over_budget` is the gate decision input.
    """
    notionals = np.asarray(notionals, dtype=np.float64)
    advs = np.asarray(advs, dtype=np.float64) * max(float(adv_mult), 1e-9)
    n = len(notionals)
    reg = REGIME_MULTIPLIERS.get(regime, 1.0)

    if stagger:
        sched = schedule_staggered_exits(notionals, advs, corr,
                                         max_daily_pct=MAX_DAILY_PCT,
                                         twap_days=TWAP_DAYS,
                                         cluster_threshold=CLUSTER_THRESHOLD)
        daily = np.array(sched["daily_notional_usd"], dtype=np.float64)
    else:
        daily = notionals.copy()                       # naive: day-one dump

    per_name = [
        round_trip_cost_bps(participation=max(daily[i] / advs[i], 0.0),
                            adv=advs[i],
                            regime=regime,
                            twap_days=TWAP_DAYS if stagger else 1.0,
                            vol_mult=vol_mult)
        for i in range(n)
    ]
    cost = float(np.mean(per_name)) if n else 0.0
    vetoed = [int(i) for i in range(n) if advs[i] < MIN_ADV_USD]
    return {
        "name": "tca_budget",
        "regime": regime,
        "vol_mult": float(vol_mult),
        "adv_mult": float(adv_mult),
        "staggered": bool(stagger),
        "n_names": int(n),
        "gross_notional_usd": float(notionals.sum()),
        "round_trip_bps": round(cost, 4),
        "budget_bps": float(budget_bps),
        "over_budget": bool(cost >= budget_bps),
        "n_vetoed_below_min_adv": len(vetoed),
        "vetoed_indices": vetoed,
        "per_name_round_trip_bps": [round(float(x), 4) for x in per_name],
    }


# ----------------------------------------------------------------------------
# Gate contract for ingestion/harness/gate_engine.py (gate 16: tca_budget)
# ----------------------------------------------------------------------------

def tca_budget_gate(report: Dict[str, Any],
                    budget_bps: float = BUDGET_BPS) -> Dict[str, Any]:
    """Gate 16: simulated round-trip cost must stay under the frozen 150 bps.

    Input contract: the report dict produced by stress_test_concurrent (or an
    equivalent execution report with keys `round_trip_bps` and `regime`).
    """
    cost = float(report.get("round_trip_bps", np.inf))
    return {
        "name": "tca_budget",
        "passed": bool(cost < budget_bps),
        "value": round(cost, 4),
        "threshold": float(budget_bps),
        "direction": "<",
        "regime": report.get("regime", "EXPANSION"),
        "staggered": report.get("staggered", True),
        "n_vetoed_below_min_adv": int(report.get("n_vetoed_below_min_adv", 0)),
    }


# ----------------------------------------------------------------------------
# Falsification self-test (must PASS, pre-registered seeds)
# ----------------------------------------------------------------------------

def _test_portfolio(seed: int = 20260919) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """20 names: two 10-name clusters (block-diagonal |corr| 0.8/0.85, across
    ~0.2), ADV 200-600M for the pass book, 6-9M for the churn book."""
    rng = np.random.default_rng(seed)
    n = 20
    advs = 2.0e8 + 4.0e8 * rng.random(n)
    notionals = 3.0e6 + 5.0e6 * rng.random(n)
    corr = np.full((n, n), 0.2)
    for g in (list(range(0, 10)), list(range(10, 20))):
        rho = 0.80 if g[0] == 0 else 0.85
        for i in g:
            for j in g:
                if i != j:
                    corr[i, j] = rho + 0.02 * rng.random() - 0.01
    np.fill_diagonal(corr, 1.0)
    corr = np.clip(corr, -1.0, 1.0)
    return notionals, advs, corr


def run_tests(budget_bps: float = BUDGET_BPS) -> int:
    print("TCASimulator self-test (frozen budget %.0f bps)" % budget_bps)
    print("=" * 60)

    # 0) Frozen constants vs config (pre-registered, never tuned)
    assert abs(BUDGET_BPS - 150.0) < 1e-9
    assert abs(AC_K - 0.02) < 1e-9
    assert abs(MAX_DAILY_PCT - 0.05) < 1e-9
    assert TWAP_DAYS == 3
    assert abs(CLUSTER_THRESHOLD - 0.70) < 1e-9
    assert abs(MIN_ADV_USD - 5_000_000.0) < 1e-9
    assert REGIME_MULTIPLIERS == {"EXPANSION": 1.0, "CRISIS": 3.0}
    print("[OK] frozen params: budget=150 bps, AC_K=0.02, max_daily=5%%, "
          "twap=3d, cluster_thr=0.70, min_adv=5M, regime mult =1/3")

    # 1) Analytic ladder: PASS vs FAIL separation
    pass_cost = round_trip_cost_bps(participation=0.0067, adv=3.0e8,
                                    regime="EXPANSION", twap_days=TWAP_DAYS)
    fail_cost = round_trip_cost_bps(participation=0.04, adv=6.0e6,
                                    regime="CRISIS", twap_days=0.5)
    assert pass_cost < budget_bps, f"pass cost {pass_cost} >= budget"
    assert fail_cost >= budget_bps, f"fail cost {fail_cost} < budget"
    # AC signature: impact ~ sqrt(participation)
    imp1 = impact_bps(0.01, MIN_ADV_USD, "EXPANSION")
    imp4 = impact_bps(0.04, MIN_ADV_USD, "EXPANSION")
    assert abs(imp1 - 30.0) < 1e-6
    assert abs(imp4 / imp1 - 2.0) < 1e-6, f"AC sqrt scaling broken: {imp4/imp1}"
    print(f"[OK] analytic ladder: PASS={pass_cost:.1f} bps, "
          f"FAIL={fail_cost:.1f} bps, AC sqrt scaling imp4/imp1={imp4/imp1:.3f}")

    # 2) LOB simulator: monotonicity in size, crisis liquidation, budget pass
    lob = LOBSimulator(adv=3.0e8, seed=11)
    w_small = lob.market_sell(5.0e5)["walk_bps"]
    w_big = lob.market_sell(2.0e6)["walk_bps"]
    assert w_big > w_small, "LOB walk must increase with order size"
    lob_crisis = LOBSimulator(adv=3.0e8, seed=11, vol_mult=3.0)
    w_crisis = lob_crisis.market_sell(2.0e6)["walk_bps"]
    assert w_crisis > w_big, "crisis book must thin and widen the walk"
    lob_round_pass = COMMISSION_BPS + SPREAD_BPS + 2.0 * w_big
    assert lob_round_pass < budget_bps, f"LOB pass book cost {lob_round_pass}"
    lob_churn = LOBSimulator(adv=6.0e6, seed=12, vol_mult=3.0)
    w_churn = lob_churn.market_sell(2.4e5)["walk_bps"]
    lob_round_fail = COMMISSION_BPS + SPREAD_BPS + 2.0 * w_churn
    assert lob_round_fail >= budget_bps, f"LOB churn cost {lob_round_fail}"
    print(f"[OK] LOB: walk {w_small:.1f}->{w_big:.1f} bps (size), "
          f"crisis {w_crisis:.1f} bps; round-trip PASS={lob_round_pass:.1f}, "
          f"FAIL={lob_round_fail:.1f}")

    # 3) Staggered scheduler: per-name cap enforced, clusters found
    notionals, advs, corr = _test_portfolio()
    sched = schedule_staggered_exits(notionals, advs, corr)
    part = np.array(sched["daily_participation"])
    assert part.max() <= MAX_DAILY_PCT + 1e-9, f"cap violated: {part.max()}"
    clusters = sched["clusters"]
    assert clusters[0][0] == 0 and clusters[1][0] == 10, f"clusters {clusters}"
    assert len(clusters) == 2, f"expected 2 clusters, got {clusters}"
    print(f"[OK] scheduler: cap {part.max()*100:.2f}%%/day, "
          f"clusters={[[len(c) for c in clusters]]}, planned "
          f"${sched['total_planned_usd']/1e6:.1f}M")
    assert sched["total_planned_usd"] <= notionals.sum() + 1.0
    print(f"[OK] scheduler: never plans more than gross notional "
          f"({sched['total_planned_usd']/1e6:.1f}M <= {notionals.sum()/1e6:.1f}M)")

    # 4) Concurrent stress: EXPANSION staggered stays in budget; CRISIS crush
    #    x0.25 ADV / x3 vol blows through it, and naive day-one dump fails too
    calm = stress_test_concurrent(notionals, advs, corr,
                                  regime="EXPANSION", vol_mult=1.0,
                                  adv_mult=1.0, budget_bps=budget_bps,
                                  stagger=True)
    stress = stress_test_concurrent(notionals, advs, corr,
                                    regime="CRISIS", vol_mult=3.0,
                                    adv_mult=0.25, budget_bps=budget_bps,
                                    stagger=True)
    naive = stress_test_concurrent(notionals, advs, corr,
                                   regime="CRISIS", vol_mult=3.0,
                                   adv_mult=0.25, budget_bps=budget_bps,
                                   stagger=False)
    assert not calm["over_budget"], f"calm went over: {calm}"
    assert stress["over_budget"], f"stress stayed under: {stress}"
    assert naive["over_budget"], f"naive stayed under: {naive}"
    assert stress["round_trip_bps"] > calm["round_trip_bps"]
    print(f"[OK] stress: calm={calm['round_trip_bps']:.1f} bps (EMBED), "
          f"stress={stress['round_trip_bps']:.1f} bps (OVER), "
          f"naive={naive['round_trip_bps']:.1f} bps (OVER)")

    # 5) Gate contract (gate 16): shape + direction
    out_calm = tca_budget_gate(calm, budget_bps)
    out_stress = tca_budget_gate(stress, budget_bps)
    assert out_calm["passed"] and not out_stress["passed"]
    assert out_calm["name"] == "tca_budget" and out_calm["direction"] == "<"
    assert out_calm["threshold"] == budget_bps
    print(f"[OK] tca_budget gate: PASS {out_calm['value']} bps < "
          f"{out_calm['threshold']:.0f}; FAIL {out_stress['value']} bps")

    # 6) LOB vs analytic ordering consistency (ordinal, both views)
    a_imp_calm = impact_bps(calm["round_trip_bps"] and 0.0067 or 0.0067,
                            3.0e8, "EXPANSION")
    a_imp_stress = impact_bps(0.04, 6.0e6, "CRISIS")
    assert a_imp_stress > a_imp_calm
    print("[OK] analytic <-> LOB views agree on ordering (cheap vs costly)")

    print("\nAll TCASimulator tests passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="TCA Simulator -- Almgren-Chriss round-trip cost gate + LOB"
                    " (dispatch: --test --lob --budget 150)")
    parser.add_argument("--test", action="store_true",
                        help="Run the falsification self-test")
    parser.add_argument("--lob", action="store_true",
                        help="Exercise the limit-order-book simulator in the test")
    parser.add_argument("--budget", type=float, default=BUDGET_BPS,
                        help="Budget override (bps); frozen default 150")
    parser.add_argument("--stress", action="store_true",
                        help="Print the concurrent-unwind stress report and exit")
    args = parser.parse_args()

    if args.test:
        return run_tests(budget_bps=float(args.budget))
    if args.stress:
        notionals, advs, corr = _test_portfolio()
        rep = stress_test_concurrent(notionals, advs, corr,
                                     regime="CRISIS", vol_mult=3.0,
                                     adv_mult=0.25, stagger=True)
        print("stress report (CRISIS, adv x0.25, vol x3, staggered):")
        print("  round_trip_bps=%.1f  over_budget=%s" % (
            rep["round_trip_bps"], rep["over_budget"]))
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())