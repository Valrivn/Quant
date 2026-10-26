"""Phase 4 orchestrator — fragility alpha signals + lifecycle backtest.

Produces, when ``phi_panel`` and the survival surface exist:
  alpha_signals.parquet  (4 pre-registered signals, T+1 fill, no lookahead)
  backtest_results.json  (Arm L vs SPY vs Arm A; Arm V noted as not runnable)
  attribution.md         (per-signal forward checks)

If phi_panel / surface are absent (Workstream A still running), writes
PENDING_PHI_PANEL.json (same marker convention as run_similarity.py) and exits 0.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from research.fragility_alpha import (
    build_alpha_signals,
    load_phi_panel,
    load_surface,
    provenance_json,
)
from research.lifecycle_backtest import (
    arm_daily_to_monthly,
    real_monthly_returns_from_path,
    run_full_backtest,
    signal_attribution,
    spy_monthly_returns,
    write_attribution_md,
)

DEFAULT_OUTPUT_DIR = "data/research/lifecycle"


def pending_marker(output_dir: str, missing: list) -> None:
    os.makedirs(output_dir, exist_ok=True)
    marker = {
        "status": "PENDING",
        "missing_inputs": missing,
        "written_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "note": "phase_4_code_complete; execution pending phi_panel + surface from Workstream A",
    }
    path = os.path.join(output_dir, "PENDING_PHI_PANEL.json")
    with open(path, "w") as f:
        json.dump(marker, f, indent=2)
    print(f"[Phase4] Wrote {path} (missing: {missing})")


def run_alpha(args) -> None:
    output_dir = args.output if args.output.endswith(os.sep) or os.path.isdir(args.output) or args.output.endswith("/") else os.path.dirname(args.output)
    missing = [f for f in (args.phi_panel, args.surface, args.break_points)
               if not os.path.exists(f)]
    if missing:
        pending_marker(output_dir, missing)
        return

    print("[Phase4] Loading phi panel + survival surface ...")
    phi = load_phi_panel(args.phi_panel)
    surf, brk, dummies = load_surface(args.surface, args.break_points)
    print(f"[Phase4] phi_panel rows: {len(phi)} | dummy cohorts excluded: {len(dummies)}")

    print("[Phase4] Loading prices / regime / benchmarks ...")
    month_ret = real_monthly_returns_from_path(args.prices)
    regime = pd.read_parquet(args.regime)
    spy = spy_monthly_returns(args.spy)
    arm_a = arm_daily_to_monthly(args.arm_a)

    print("[Phase4] Building alpha signals (4 pre-registered) ...")
    signals = build_alpha_signals(phi, surf, brk, month_ret, regime)
    signals_path = os.path.join(output_dir, "alpha_signals.parquet")
    os.makedirs(output_dir, exist_ok=True)
    signals.to_parquet(signals_path, index=False)
    with open(signals_path + ".provenance.json", "w") as f:
        json.dump(provenance_json({"row_count": len(signals),
                                   "phi_panel": args.phi_panel}), f, indent=2)
    print(f"[Phase4] Wrote {signals_path} ({len(signals)} rows)")

    print("[Phase4] Running lifecycle backtest (top decile, 1/Phi weights, 53bps/yr) ...")
    results = run_full_backtest(signals, month_ret, spy, arm_a)
    metrics = {k: v for k, v in results.items() if k not in ("weights",)}
    results_path = os.path.join(output_dir, "backtest_results.json")
    with open(results_path, "w") as f:
        json.dump(metrics, f, indent=2, default=str)
    print(f"[Phase4] Wrote {results_path}")

    print("[Phase4] Attribution (per-signal) ...")
    attr = signal_attribution(signals, month_ret)
    attr_path = os.path.join(output_dir, "attribution.md")
    write_attribution_md(
        attr, metrics, attr_path,
        extra={
            "phi_panel": args.phi_panel,
            "surface": args.surface,
            "regime_used": args.regime,
            "cost_model": "53bps/yr on one-sided turnover",
            "fill": "T+1 (weights at month-end m, returns in month m+1)",
            "price_source": "real rows only (synthetic_fallback excluded)",
            "in_sample_note": ("survival surface + phi panel are estimated over the "
                               "same 2018-2024 window as the backtest (plan Window 1 "
                               "FULL is the in-sample calibration window); headline "
                               "Sharpe should be read as optimistic. OOS 2025-26 "
                               "validation is not runnable: price_cache ends "
                               "2024-12-31 (only SPY extends to 2026-09)."),
            "survival_signal_flat": ("attribution shows top-vs-bottom survival decile "
                                     "spread ~0.0002/month — the pre-registered survival "
                                     "duration signal alone does not explain Arm L's "
                                     "edge; diagnostic equal-weight and 1/Phi variants "
                                     "in backtest_results.json isolate the weighting "
                                     "contribution."),
        },
    )
    print(f"[Phase4] Wrote {attr_path}")

    arm = metrics["arm_L_lifecycle_after_costs"]
    print(f"[Phase4] Arm L after costs: Sharpe {arm.get('sharpe')}, "
          f"AnnRet {arm.get('annual_return'):.4f}, MaxDD {arm.get('max_drawdown'):.4f}")
    if arm.get("sharpe") is not None and arm["sharpe"] < 0.5:
        print("[Phase4] NOTE: Arm L Sharpe < 0.5 — falsification condition #7 "
              "(category-rotation alpha not significant after costs) may trigger.")
    to = metrics.get("avg_one_sided_turnover_per_month")
    if to is not None and to > 0.10:
        print(f"[Phase4] NOTE: turnover {to:.1%}/mo exceeds the <10%/mo target "
              "(plan §6 D4.2) — backtest is not implementable at this churn.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--phi-panel", default=os.path.join(DEFAULT_OUTPUT_DIR, "phi_panel.parquet"))
    p.add_argument("--surface", default=os.path.join(DEFAULT_OUTPUT_DIR, "surface_3d.parquet"))
    p.add_argument("--break-points", default=os.path.join(DEFAULT_OUTPUT_DIR, "break_points.parquet"))
    p.add_argument("--regime", default="data/regime/regime_daily_nowcast.parquet")
    p.add_argument("--prices", default="data/prices/price_cache.parquet")
    p.add_argument("--spy", default="data/master_prices/SPY.parquet")
    p.add_argument("--arm-a", default="data/backtests/arm_a/returns.parquet")
    p.add_argument("--output", default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--wait-for-phi", action="store_true")
    p.add_argument("--max-wait-seconds", type=int, default=1800)
    args = p.parse_args()

    if not args.wait_for_phi:
        run_alpha(args)
        sys.exit(0)

    deadline = time.time() + args.max_wait_seconds
    while time.time() < deadline:
        if os.path.exists(args.phi_panel) and os.path.exists(args.surface):
            print("[Phase4] phi_panel + surface detected; running ...")
            run_alpha(args)
            sys.exit(0)
        print("[Phase4] waiting for phi_panel/surface ...")
        time.sleep(20)
    pending_marker(args.output, ["phi_panel", "surface_3d", "break_points"])
    print("[Phase4] max wait elapsed; pending marker written")
    sys.exit(0)