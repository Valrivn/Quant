"""
End-to-end dry-run: ThreeArmRunner (synthetic §6 dual-track universe)
                -> GateEngine (frozen RS-05 sector-relative gates 3 & 4)

Two variants:
  (a) honest synthetic — alpha names are shared by Arm A (sector-quartile)
      and Arm C (composite z), so C - A per sector is noise and the gates
      must FAIL (no fabricated passes);
  (b) engineered variant — realized returns for the tickers Arm C already
      holds are re-authored so C truly beats A within every sector; the
      frozen sector-relative gates must PASS.

Run:  python scripts/dry_run_rs05_gates.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from ingestion.harness.gate_engine import (  # noqa: E402
    BONFERRONI_ALPHA, GateEngine, load_gates_config,
)
from ingestion.harness.three_arm_runner import ThreeArmRunner, _synthetic_universe  # noqa: E402


def _summarize_gate3_4(gr) -> dict:
    out = {}
    for g in ("welch_c_vs_a", "welch_c_vs_b"):
        d = gr.details[g]
        out[g] = {
            "passed": d.get("passed"),
            "p_value": d.get("p_value"),
            "z_stouffer": d.get("z_stouffer"),
            "sectors_usable": d.get("sectors_usable"),
            "sectors_total": d.get("sectors_total"),
            "combine_method": d.get("combine_method"),
            "reason": d.get("reason"),
            "effect_size": d.get("effect_size"),
        }
    return out


def main() -> int:
    print("=" * 78)
    print("Dry-run: ThreeArmRunner -> GateEngine (frozen RS-05 gates 3,4)")
    print("=" * 78)

    cfg = load_gates_config()
    print(f"gates_v2: frozen={cfg.get('frozen')} sector_relative={cfg.get('sector_relative')} "
          f"version={cfg.get('version')}")
    assert cfg.get("frozen") and cfg.get("sector_relative"), \
        "config/gates_v2.yaml must be frozen sector-relative (run --re-register-gates 3,4)"

    syn = _synthetic_universe(n_tickers=175, n_months=48, seed=11)
    print(f"universe: {len(syn['universe'])} rows spanning "
          f"{syn['universe']['date'].min().date()} .. {syn['universe']['date'].max().date()}"
          f" | sectors={sorted(syn['universe']['sector'].astype(str).unique())}")

    reports = {}
    for variant, engineered in (("honest", False), ("engineered", True)):
        run_rets = syn["rets"]
        work_universe = syn["universe"]
        work_qual = syn["qual"]
        if engineered:
            # Distinct cohort (T*2xx, ~5 names/sector) gets a QUAL-signal
            # blowout so Arm C (composite z, qual-driven) selects it, while
            # Arm A (quant sector-quartile) does not; returns carry a real
            # edge (+1.5%/mo) for that cohort only.
            cohort = work_qual["ticker"].str.extract(r"T(\d+)")[0].astype(int) % 5 == 2
            work_qual = work_qual.copy()
            for col in ("rpo_growth_yoy", "gh_velocity_90d", "ats_velocity_90d",
                        "app_velocity_90d", "patent_accel_1y", "mda_tone"):
                work_qual.loc[cohort, col] += 4.0
            run_rets = run_rets.copy()
            run_rets["ret"] = np.where(
                run_rets["ticker"].str.extract(r"T(\d+)")[0].astype(int) % 5 == 2,
                run_rets["ret"] + 0.030, run_rets["ret"] - 0.004)

        runner = ThreeArmRunner(
            universe_T=work_universe, qual_factors_T=work_qual,
            quant_factors_T=work_universe, returns_T=run_rets, top_n=50)
        res = runner.run()
        gr = GateEngine().run_all_gates(res)

        print(f"\n--- variant: {variant} ---")
        print(f"runner: {res['arm_a_returns'].shape[0]} rebalances | "
              f"arm_c_net mean={res['arm_c_returns_net'].mean():.4%} | "
              f"sectors packaged={len(res['sector_returns'])}")
        meta = gr.details["meta"]
        print(f"gates: passed={gr.passed} ({gr.n_passed}/{gr.n_gates})")
        print(f"  config_sha256 (sidecar) : {meta['config_sha256']}")
        print(f"  sector_relative         : {meta['sector_relative']}")
        print(f"  sector_provenance       : {meta['sector_provenance']}")
        for g, d in _summarize_gate3_4(gr).items():
            z = d.get("z_stouffer")
            print(f"  {'PASS' if d.get('passed') else 'FAIL'} {g}: "
                  f"p={d.get('p_value')} z_stouffer={None if z is None else round(z, 3)} "
                  f"sectors={d.get('sectors_usable')}/{d.get('sectors_total')} "
                  f"(reason={d.get('reason', '-')})")
        if engineered:
            assert gr.details["welch_c_vs_a"].get("passed") and \
                   gr.details["welch_c_vs_b"].get("passed"), \
                "engineered variant must pass gates 3 & 4"
        reports[variant] = {
            "n_gates_passed": int(gr.n_passed),
            "n_gates_total": int(gr.n_gates),
            "arm_c_net_mean": float(res["arm_c_returns_net"].mean()),
            "gates_3_4": _summarize_gate3_4(gr),
            "meta_provenance": meta,
        }

    out_path = Path("data/validation/dry_run_gates_rs05.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "dry_run": "ThreeArmRunner -> GateEngine (synthetic)",
        "alpha_bonferroni": BONFERRONI_ALPHA,
        "variants": reports,
    }, indent=2, default=str, sort_keys=True), encoding="utf-8")
    print(f"\nDry-run gate report: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())