"""
Fundamental Coverage Audit (Deliverable 4)

Audits ``data/pit_fundamentals/dense_fundamentals.parquet`` produced by
``ingestion.build_dense_fundamentals``.

Reports, for every factor and for the acceptance-critical factors
(ROIC / interest coverage / FCF / WACC components):

  * coverage over the *full* universe of ticker-months,
  * coverage over the *covered* universe (tickers that have any filings —
    i.e. fallback tier < 3),
  * coverage over *filers* (rows where at least one factor is present).

It also reports the fallback-tier distribution (the runbook's tier-2 gate),
point-in-time integrity (filed_date <= rebalance_date) and a set of
acceptance gates.

CLI:
    python -m validation.fundamental_coverage_audit
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

import polars as pl

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "data/pit_fundamentals/dense_fundamentals.parquet"
DEFAULT_OUT = ROOT / "data/validation/coverage_report_20261010.json"

KEY_COLS = [
    "ticker", "cik", "fiscal_end", "filed_date", "rebalance_date",
    "fiscal_year", "concept_coverage_pct", "fallback_tier", "n_factors_present",
]

# Acceptance-critical factors and their grouping.
KEY_FACTORS = {
    "roic": ["roic"],
    "interest_coverage": ["interest_coverage"],
    "fcf": ["fcf"],
    "cost_of_debt": ["cost_of_debt"],
    "wacc_weights": ["weight_debt", "weight_equity"],
    "weight_debt": ["weight_debt"],
    "weight_equity": ["weight_equity"],
    "wacc_components": ["cost_of_debt", "weight_debt", "weight_equity"],
}

TIER_FILED, TIER_FORWARD, TIER_SECTOR_MEDIAN, TIER_CONSTANT = 0, 1, 2, 3

# Coverage targets / reject floors from the pre-registered plan
# (fundamental_scraping_plan_20261010.md §1).  Measured at ticker level over
# the covered universe (fallback tier < 3, i.e. tickers that actually filed).
COVERAGE_TARGETS: Dict[str, Dict[str, float]] = {
    "roic": {"target": 0.80, "reject": 0.60},
    "interest_coverage": {"target": 0.80, "reject": 0.60},
    "fcf": {"target": 0.80, "reject": 0.60},
    "wacc_weights": {"target": 0.70, "reject": 0.50},
}
TIER2_GATE = 0.20


def _cov(frame: pl.DataFrame, cols: List[str]) -> float:
    """Fraction of rows where every column in ``cols`` is non-null."""
    if frame.height == 0:
        return 0.0
    expr = pl.all_horizontal([pl.col(c).is_not_null() for c in cols])
    return float(frame.select(expr.sum()).item()) / frame.height


def _cov_tickers(frame: pl.DataFrame, cols: List[str], n_tickers: int) -> float:
    """Fraction of distinct tickers ever exposing every column in ``cols``."""
    if frame.height == 0 or n_tickers == 0:
        return 0.0
    expr = pl.all_horizontal([pl.col(c).is_not_null() for c in cols])
    return frame.filter(expr)["ticker"].n_unique() / n_tickers


def audit(dataset: Path) -> Dict[str, object]:
    df = pl.read_parquet(dataset)
    n_total = df.height
    factors = [c for c in df.columns if c not in KEY_COLS]

    covered = df.filter(pl.col("fallback_tier") < TIER_CONSTANT)
    n_covered = covered.height
    n_covered_tickers = covered["ticker"].n_unique()
    filers = df.filter(pl.col("n_factors_present") > 0)
    n_filers = filers.height

    # ---- per-factor coverage -------------------------------------------
    factor_coverage = {}
    for f in factors:
        factor_coverage[f] = {
            "total_pct": _cov(df, [f]),
            "covered_pct": _cov(covered, [f]),
            "filer_pct": _cov(filers, [f]),
            "ticker_pct": _cov_tickers(covered, [f], n_covered_tickers),
        }

    # ---- acceptance-critical factors -----------------------------------
    key_coverage = {}
    for name, cols in KEY_FACTORS.items():
        if not all(c in df.columns for c in cols):
            key_coverage[name] = {"present": False}
            continue
        key_coverage[name] = {
            "present": True,
            "total_pct": _cov(df, cols),
            "covered_pct": _cov(covered, cols),
            "filer_pct": _cov(filers, cols),
            "ticker_pct": _cov_tickers(covered, cols, n_covered_tickers),
        }

    # ---- fallback tiers -------------------------------------------------
    tier_counts = {
        int(r["fallback_tier"]): int(r["count"])
        for r in df["fallback_tier"].value_counts().sort("fallback_tier").to_dicts()
    }
    tier2_rate = tier_counts.get(TIER_SECTOR_MEDIAN, 0) / n_total if n_total else 0.0
    missing_rate = sum(tier_counts.get(t, 0) for t in (TIER_SECTOR_MEDIAN, TIER_CONSTANT)) / n_total if n_total else 0.0

    # ---- PIT integrity --------------------------------------------------
    pit_violations = df.filter(
        pl.col("filed_date").is_not_null() & (pl.col("filed_date") > pl.col("rebalance_date"))
    ).height
    # forward-filled tiers must never exceed 120d except tier1 (<=730d)
    age = (pl.col("rebalance_date") - pl.col("filed_date")).dt.total_days()
    bad_fresh = df.filter(
        pl.col("fallback_tier").is_in([TIER_FILED, TIER_FORWARD])
        & (pl.col("filed_date").is_not_null())
        & (age > 730)
    ).height

    # ---- hard gates (plan falsification conditions) --------------------
    reject_gates = {
        "tier2_rate_lt_20pct": {
            "value": tier2_rate,
            "threshold": TIER2_GATE,
            "pass": tier2_rate < TIER2_GATE,
        },
        "pit_violations_zero": {
            "value": pit_violations,
            "threshold": 0,
            "pass": pit_violations == 0,
        },
    }
    # ---- coverage targets & reject floors ------------------------------
    targets: Dict[str, Dict[str, object]] = {}
    for name, spec in COVERAGE_TARGETS.items():
        tk = key_coverage.get(name, {}).get("ticker_pct", 0.0)
        covered_pct = key_coverage.get(name, {}).get("covered_pct", 0.0)
        status = "TARGET_MET" if tk >= spec["target"] else (
            "PARTIAL" if tk >= spec["reject"] else "REJECTED"
        )
        targets[name] = {
            "ticker_pct": tk,
            "covered_row_pct": covered_pct,
            "target": spec["target"],
            "reject": spec["reject"],
            "target_met": tk >= spec["target"],
            "reject_floor_met": tk >= spec["reject"],
            "status": status,
        }
        reject_gates[f"coverage_{name}_ge_{int(spec['reject']*100)}pct_tickers"] = {
            "value": tk,
            "threshold": spec["reject"],
            "pass": tk >= spec["reject"],
        }

    # ---- per-ticker factor richness ------------------------------------
    per_ticker = (
        df.filter(pl.col("n_factors_present") > 0)
        .group_by("ticker")
        .agg(pl.col("n_factors_present").mean().alias("avg_factors"))
    )

    report = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "dataset": str(dataset).replace("\\", "/"),
        "rows": n_total,
        "columns": df.width,
        "factors": len(factors),
        "tickers": df["ticker"].n_unique(),
        "filer_tickers": filers["ticker"].n_unique(),
        "date_range": {
            "min": str(df["rebalance_date"].min()),
            "max": str(df["rebalance_date"].max()),
        },
        "denominators": {
            "total_rows": n_total,
            "covered_rows_tier_lt_3": n_covered,
            "filer_rows": n_filers,
            "covered_tickers": n_covered_tickers,
        },
        "fallback_tiers": {str(k): v for k, v in tier_counts.items()},
        "tier2_rate": tier2_rate,
        "missing_rate_tier_ge_2": missing_rate,
        "pit_violations": pit_violations,
        "stale_fresh_tier_rows": bad_fresh,
        "avg_factors_per_filer_ticker": float(per_ticker["avg_factors"].mean() or 0.0),
        "key_factor_coverage": key_coverage,
        "factor_coverage": factor_coverage,
        "reject_gates": reject_gates,
        "all_reject_gates_pass": all(g["pass"] for g in reject_gates.values()),
        "coverage_targets": targets,
        "targets_met": sum(1 for t in targets.values() if t["target_met"]),
        "targets_total": len(targets),
    }
    return report


def _print_report(rep: Dict[str, object]) -> None:
    print("=" * 80)
    print("FUNDAMENTAL COVERAGE AUDIT")
    print("=" * 80)
    print(f"Dataset           : {rep['dataset']}")
    print(f"Rows x Columns    : {rep['rows']:,} x {rep['columns']}")
    print(f"Factors           : {rep['factors']}")
    print(f"Universe tickers  : {rep['tickers']}  (with factors: {rep['filer_tickers']})")
    print(f"Date range        : {rep['date_range']['min']} .. {rep['date_range']['max']}")
    d = rep["denominators"]
    print(f"Denominators      : total={d['total_rows']:,}  "
          f"covered(tier<3)={d['covered_rows_tier_lt_3']:,}  filers={d['filer_rows']:,}  "
          f"covered_tickers={d['covered_tickers']:,}")

    print("\n" + "-" * 80)
    print("FALLBACK TIER DISTRIBUTION")
    print("-" * 80)
    labels = {0: "filed", 1: "forward_filled", 2: "sector_median", 3: "no_data"}
    for k, v in sorted(rep["fallback_tiers"].items(), key=lambda kv: int(kv[0])):
        print(f"  tier {k} {labels.get(int(k), ''):<15} {v:>8,}  ({v / rep['rows']:.1%})")
    print(f"  tier-2 (sector median) rate : {rep['tier2_rate']:.2%}")
    print(f"  tier>=2 (no filed data) rate: {rep['missing_rate_tier_ge_2']:.2%}")

    print("\n" + "-" * 80)
    print("ACCEPTANCE-CRITICAL FACTOR COVERAGE")
    print("-" * 80)
    print(f"  {'factor':<20} {'total':>8} {'covered':>9} {'filers':>8} {'tickers':>8}")
    for name in KEY_FACTORS:
        kc = rep["key_factor_coverage"].get(name, {})
        if not kc.get("present"):
            print(f"  {name:<20} {'MISSING':>8}")
            continue
        print(f"  {name:<20} {kc['total_pct']:>7.1%} {kc['covered_pct']:>9.1%} "
              f"{kc['filer_pct']:>8.1%} {kc['ticker_pct']:>8.1%}")

    print("\n" + "-" * 80)
    print("PIT INTEGRITY")
    print("-" * 80)
    print(f"  filed_date > rebalance_date violations : {rep['pit_violations']}")
    print(f"  fresh/forward rows older than 730d      : {rep['stale_fresh_tier_rows']}")

    print("\n" + "-" * 80)
    print("HARD GATES (plan falsification conditions)")
    print("-" * 80)
    for name, g in rep["reject_gates"].items():
        status = "PASS" if g["pass"] else "FAIL"
        val = g["value"]
        val_s = f"{val:.2%}" if isinstance(val, float) and val <= 1.0 else str(val)
        print(f"  [{status}] {name:<46} value={val_s} threshold={g['threshold']}")

    print("\n" + "-" * 80)
    print("COVERAGE TARGETS (ticker-level, covered universe)")
    print("-" * 80)
    print(f"  {'factor':<20} {'ticker':>8} {'covered':>9} {'target':>8} {'reject':>8}  status")
    for name, t in rep["coverage_targets"].items():
        print(f"  {name:<20} {t['ticker_pct']:>7.1%} {t['covered_row_pct']:>9.1%} "
              f"{t['target']:>7.1%} {t['reject']:>8.1%}  {t['status']}")
    print(f"\n  targets met: {rep['targets_met']}/{rep['targets_total']}")
    if rep["all_reject_gates_pass"]:
        print("  PLAN NOT REJECTED (all hard gates pass)")
    else:
        print("  PLAN REJECTED (hard gate failure)")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--output", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)

    dataset = Path(args.dataset)
    if not dataset.exists():
        print(f"[ERROR] dataset not found: {dataset}")
        return 2

    rep = audit(dataset)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    _print_report(rep)
    print(f"\nReport written to {out}")
    return 0 if rep["all_reject_gates_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
