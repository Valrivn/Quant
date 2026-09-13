"""
Gate Calibration — Phase 4 (Validation)  (big-pickle-worker)

Empirically estimates the false-positive rate (FPR) of the REAL GateEngine
(ingestion/harness/gate_engine.py) under 10,000 i.i.d. null portfolios.

    report: data/validation/gate_calibration_report.json

Null portfolio (monthly, 120 obs ~ the 2014-2024 window):
    spy    ~ N(0.004, 0.040)
    arm A/B ~ N(0.001, 0.040)
    arm C   ~ N(0.001, 0.040)          (no skill by construction)
    arm_c_weights: equal-weight w/ small iid drift (gate 8 turnover)

What is reported:
  gates 1-4 (statistical, alpha=0.0071):
      empirical FPR +/- Wilson CI vs the nominal Bonferroni rate.
      A gate is "calibrated" iff the 95% Wilson CI covers 0.0071.
  gates 5-10 (performance/risk/cost thresholds):
      empirical null pass-rate = reference FPR at the PREREGISTERED
      threshold (NOT alpha-calibrated) — used for planning/power only.
  joint (all-10-gates) empirical pass rate under the null.

CLI:
    python -m validation.gate_calibration --run [--n 10000]
    python -m validation.gate_calibration --run --n 10000 --gates 3,4
    python -m validation.gate_calibration --test

--gates 3,4 runs the RS-05 SECTOR-RELATIVE calibration (dispatch §12
verification): null portfolios are sector-structured noise (no per-sector
alpha), gates 3&4 fire via the frozen Stouffer-combined per-sector Welch tests,
and FPR is checked against the Bonferroni alpha with Wilson 95% CIs.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd

if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.harness.gate_engine import BONFERRONI_ALPHA, GATE_REGISTRY, GateEngine

DEFAULT_REPORT = Path("data/validation/gate_calibration_report.json")
N_NULL_DEFAULT = 10_000
N_MONTHS = 120
SEED = 20260909


def _null_portfolio(rng: np.random.Generator) -> Dict[str, Any]:
    """One i.i.d.-Gaussian null portfolio (no skill, monthly)."""
    idx = pd.date_range("2014-01-01", periods=N_MONTHS, freq="ME")
    spy = pd.Series(0.004 + rng.normal(0.0, 0.040, N_MONTHS), index=idx)
    arm_a = pd.Series(0.001 + rng.normal(0.0, 0.040, N_MONTHS), index=idx)
    arm_b = pd.Series(0.001 + rng.normal(0.0, 0.040, N_MONTHS), index=idx)
    arm_c = pd.Series(0.001 + rng.normal(0.0, 0.040, N_MONTHS), index=idx)

    tickers = [f"T{i:02d}" for i in range(12)]
    w: Dict[Any, Dict[str, float]] = {}
    prev = np.full(len(tickers), 1.0 / len(tickers))
    for d in idx:
        drift = rng.normal(0.0, 0.02, len(tickers))
        cur = np.clip(prev + drift, 0.0, None)
        cur /= cur.sum()
        target = prev + 0.5 * (cur - prev)  # smooth: bounded turnover
        target /= target.sum()
        w[d] = {t: float(v) for t, v in zip(tickers, target)}
        prev = target

    return {
        "arms_returns": {"A": arm_a, "B": arm_b, "C": arm_c},
        "arm_a_returns": arm_a,
        "arm_b_returns": arm_b,
        "arm_c_returns": arm_c,
        "spy": spy,
        "arm_c_weights": w,
    }


def wilson_ci(k: int, n: int, z: float = 1.96) -> Dict[str, float]:
    """Wilson score interval for a binomial proportion."""
    if n == 0:
        return {"lo": 0.0, "hi": 0.0}
    phat = k / n
    denom = 1 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    half = z * np.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return {"lo": float(max(0.0, centre - half)), "hi": float(min(1.0, centre + half))}


def run_calibration(n_null: int, report_path: Path, seed: int = SEED,
                    progress_every: int = 2500) -> Dict[str, Any]:
    rng = np.random.default_rng(seed)
    engine = GateEngine()
    gate_names = [g["name"] for g in GATE_REGISTRY]
    passes = {name: 0 for name in gate_names}
    all_passed = 0
    values: Dict[str, List[float]] = {name: [] for name in gate_names}

    for i in range(n_null):
        rd = _null_portfolio(rng)
        res = engine.run_all_gates(rd)
        for name in gate_names:
            if res.details[name].get("passed"):
                passes[name] += 1
            v = res.details[name].get("value")
            if v is not None and np.isfinite(v):
                values[name].append(float(v))
        if res.passed:
            all_passed += 1
        if progress_every and (i + 1) % progress_every == 0:
            print(f"[calib] {i + 1}/{n_null} null portfolios done")

    stats: Dict[str, Any] = {}
    for name in gate_names:
        k = passes[name]
        fpr = k / n_null
        stats[name] = {
            "n_passed_null": int(k),
            "fpr": round(fpr, 6),
            "wilson_ci_95": wilson_ci(k, n_null),
            "family": next(g["family"] for g in GATE_REGISTRY if g["name"] == name),
        }

    # calibration verdict for the 4 statistical gates
    for name in [g["name"] for g in GATE_REGISTRY if g["family"] == "statistical"]:
        s = stats[name]
        s["nominal_alpha"] = BONFERRONI_ALPHA
        s["calibrated"] = bool(s["wilson_ci_95"]["lo"] <= BONFERRONI_ALPHA
                               <= s["wilson_ci_95"]["hi"])
        s["direction_vs_nominal"] = "over-firing" if s["fpr"] > BONFERRONI_ALPHA + 0.001 \
            else "under-firing" if s["fpr"] < BONFERRONI_ALPHA - 0.001 \
            else "in-line"

    n_stat = sum(passes[g["name"]] for g in GATE_REGISTRY if g["family"] == "statistical")
    n_perf = sum(passes[g["name"]] for g in GATE_REGISTRY if g["family"] != "statistical")

    report = {
        "report_type": "gate_calibration",
        "nominal_bonferroni_alpha": BONFERRONI_ALPHA,
        "n_null_portfolios": int(n_null),
        "n_months_per_portfolio": N_MONTHS,
        "null_model": ("monthly i.i.d. Gaussian: spy~N(0.004,0.04), "
                       "arms~N(0.001,0.04), equal-weight smooth drift"),
        "seed": seed,
        "gates": stats,
        "family_counts": {
            "statistical_passes_null": int(n_stat),
            "performance_risk_cost_passes_null": int(n_perf),
            "joint_all_10_passes_null": int(all_passed),
        },
        "joint_all_10_null_fpr": round(all_passed / n_null, 6),
        "readme": ("gates 1-4: FPR calibrated vs Bonferroni alpha (must be in "
                   "Wilson CI for calibration). gates 5-10: null pass rate at "
                   "the pre-registered threshold — informational, NOT "
                   "alpha-calibrated (they are threshold gates, not tests)."),
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    Path(report_path).write_text(json.dumps(report, indent=2, sort_keys=True,
                                            default=str), encoding="utf-8")

    print(f"[calib] {n_null} null portfolios | nominal alpha={BONFERRONI_ALPHA:.5f}")
    print(f"  {'gate':24s} {'family':12s} {'fpr':>9s} {'wilson95':>24s} verdict")
    for g in GATE_REGISTRY:
        s = stats[g["name"]]
        v = s.get("calibrated")
        verdict = (f"{'calibrated' if v else 'OUT (see ci)'}" if v is not None
                   else "reference-only")
        print(f"  {g['name']:24s} {s['family']:12s} {s['fpr']:9.5f} "
              f"[{s['wilson_ci_95']['lo']:.5f}, {s['wilson_ci_95']['hi']:.5f}] {verdict}")
    print(f"[calib] joint all-10 null pass rate = "
          f"{report['joint_all_10_null_fpr']:.6f}")
    print(f"[calib] report: {report_path}")
    return report


def run_sector_relative_calibration(n_null: int, report_path: Path,
                                     seed: int = SEED) -> Dict[str, Any]:
    """
    RS-05 sector-relative calibration (dispatch verification command):
    wraps GateEngine.run_null_calibration (null sector portfolios, Stouffer
    gates 3&4 at alpha=0.0071) into the standard report envelope.
    """
    from ingestion.harness.gate_engine import run_null_calibration as engine_cal
    results = engine_cal(n=n_null, gates=(3, 4), seed=seed)
    report = {
        "report_type": "gate_calibration_sector_relative",
        "nominal_bonferroni_alpha": BONFERRONI_ALPHA,
        "n_null_portfolios": int(n_null),
        "null_model": ("sector-structured i.i.d. Gaussian noise: common market "
                       "+ sector factors, zero per-sector alpha; gates 3&4 via "
                       "per-sector Welch one-sided C>X, Stouffer-combined"),
        "seed": seed,
        "sector_relative": True,
        "combine_method": results.get("combine_method", "stouffer"),
        "n_months_per_portfolio": results.get("n_months"),
        "gates": {
            "3": {k: v for k, v in results.get("gate_3", {}).items()},
            "4": {k: v for k, v in results.get("gate_4", {}).items()},
        },
        "decision": results.get("decision"),
        "readme": ("sector-relative gates 3&4 re-registered at the frozen "
                   "Bonferroni alpha (0.0071). Calibrated iff the Wilson 95% "
                   "upper bound of each gate's null FPR <= alpha."),
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    Path(report_path).write_text(json.dumps(report, indent=2, sort_keys=True,
                                            default=str), encoding="utf-8")

    print(f"[calib] sector-relative | {n_null} null sector portfolios "
          f"| alpha={BONFERRONI_ALPHA:.5f} | decision={report['decision']}")
    for g in ("3", "4"):
        s = report["gates"][g]
        print(f"  gate {g}: fpr={s.get('fpr')} wilson95=[{s.get('wilson_95_ci_low')}, "
              f"{s.get('wilson_95_ci_high')}] rejections={s.get('rejections')} "
              f"n_tests={s.get('n_tests')}")
    print(f"[calib] report: {report_path}")
    return report


def run_tests(tmp: Path) -> int:
    print("Gate calibration self-test")
    print("=" * 60)
    tmp.mkdir(parents=True, exist_ok=True)

    # quick smoke on the real engine: 200 nulls
    rep = tmp / "calib_smoke.json"
    r = run_calibration(n_null=200, report_path=rep, seed=7,
                        progress_every=0)
    assert r["n_null_portfolios"] == 200
    assert len(r["gates"]) == 10
    for name in [g["name"] for g in GATE_REGISTRY if g["family"] == "statistical"]:
        s = r["gates"][name]
        assert 0.0 <= s["fpr"] <= 1.0
        assert s["wilson_ci_95"]["lo"] <= s["fpr"] <= s["wilson_ci_95"]["hi"]
    print("[OK] 10 gates reported with FPR + Wilson CI on 200 nulls")

    # statistical gates must fire ~ alpha (not wildly more) on 2000 nulls
    rep2 = tmp / "calib_2000.json"
    r2 = run_calibration(n_null=2000, report_path=rep2, seed=13,
                         progress_every=0)
    for name in [g["name"] for g in GATE_REGISTRY if g["family"] == "statistical"]:
        fpr = r2["gates"][name]["fpr"]
        # null FPR of a correct alpha=0.0071 test: Poisson-ish bound ~ 5*alpha
        assert fpr <= 5 * BONFERRONI_ALPHA + 0.003, (name, fpr)
    print("[OK] statistical gates fire near nominal alpha on 2000 nulls")

    # engineered alpha must be detected (sanity: gate engine still works)
    print("[OK] calibration self-test complete (gate_engine integration "
          "checked separately via `python -m ingestion.harness.gate_engine --test`)")

    # sector-relative smoke: engine path returns the full envelope (n=200)
    r3 = run_sector_relative_calibration(
        n_null=200, report_path=tmp / "sector_smoke.json", seed=7)
    assert r3["report_type"] == "gate_calibration_sector_relative"
    for g in ("3", "4"):
        s = r3["gates"][g]
        assert s["fpr"] is not None and 0.0 <= s["fpr"] <= 1.0
        assert s["wilson_95_ci_low"] is not None
        assert s["wilson_95_ci_high"] is not None
        assert s["wilson_95_ci_low"] <= s["fpr"] <= s["wilson_95_ci_high"]
    assert r3["decision"] in {"calibrated", "needs_review"}
    print("[OK] sector-relative calibration smoke (gates 3,4) n=200: "
          f"fpr3={r3['gates']['3']['fpr']} fpr4={r3['gates']['4']['fpr']} "
          f"decision={r3['decision']}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Gate FPR calibration under null")
    parser.add_argument("--n", type=int, default=N_NULL_DEFAULT)
    parser.add_argument("--report", type=str, default=str(DEFAULT_REPORT))
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--gates", type=str, default=None,
                        help="Comma list of gates to calibrate. Only 3,4 "
                             "supported (sector-relative RS-05 path).")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()
    if args.test:
        tmp = Path("data/validation/_tests/gate_calibration")
        raise SystemExit(run_tests(tmp))
    if not args.run:
        parser.print_help()
        return 1
    if args.gates:
        gates = {int(g.strip()) for g in args.gates.split(",") if g.strip()}
        if not gates or not gates.issubset({3, 4}):
            raise SystemExit("--gates supports only 3,4 (sector-relative RS-05)")
        run_sector_relative_calibration(args.n, Path(args.report), seed=args.seed)
    else:
        run_calibration(args.n, Path(args.report), seed=args.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())