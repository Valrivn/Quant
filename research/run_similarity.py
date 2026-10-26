"""Phase 3 CLI â€” orchestrate top-performer identification, feature matrix,
consensus clustering and sector-independence testing.

Two modes
---------
1. Phi-independent (runs immediately):
       python -m research.run_similarity \
           --fundamentals data/pit_fundamentals/dense_fundamentals.parquet \
           --prices data/prices/price_cache.parquet \
           --universe data/universe/monthly_reconstitution.parquet \
           --output data/research/lifecycle/
   Writes ``top_performer_list.parquet`` + ``features_independent.parquet``.

2. Phi-dependent (runs once Workstream A emits ``phi_panel.parquet``):
       python -m research.run_similarity \
           --phi-panel data/research/lifecycle/phi_panel.parquet
   Merges phi + category, clusters, runs the sector test and writes all
   Phase 3 artifacts.

If ``--phi-panel`` is not supplied and no default phi panel exists, a
``PENDING_PHI_PANEL.json`` marker is written and the process exits 0 without
fabricating any Phi values.
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

from research.top_performer_similarity import (
    DBSCAN_EPS,
    DBSCAN_MIN_SAMPLES,
    DEFAULT_FUNDAMENTALS,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_PRICES,
    DEFAULT_SPY,
    DEFAULT_UNIVERSE,
    END,
    RISK_FEATURES,
    START,
    build_archetype_report,
    build_feature_matrix,
    cluster_top_performers,
    identify_top_performers,
    sector_correlation_test,
    write_provenance,
)


def _provenance_dir(output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    return output_dir


def run_independent(args) -> tuple:
    print("[Phase3] Loading fundamentals / prices / universe ...")
    fundamentals = pd.read_parquet(args.fundamentals)
    prices = pd.read_parquet(args.prices)
    universe = pd.read_parquet(args.universe)

    real_only = args.price_filter != "all"
    print(f"[Phase3] Identifying top performers (5yr ROIC>WACC + >150% return, "
          f"prices={'real' if real_only else 'ALL incl synthetic'}) ...")
    tp = identify_top_performers(fundamentals, prices, START, END,
                                 universe=universe, real_only=real_only)
    print(f"[Phase3] Top performers identified: {len(tp)}")

    out_dir = _provenance_dir(args.output)
    tp_path = os.path.join(out_dir, "top_performer_list.parquet")
    tp.to_parquet(tp_path, index=False)
    write_provenance(tp_path, tp, ["top_performer_id"],
                     extra={"criteria": {
                         "roic_wacc_consecutive_years": 5,
                         "cum_return_threshold": 1.50,
                         "window": [START, END]},
                         "price_filter": args.price_filter})
    print(f"[Phase3] Wrote {tp_path}")

    print("[Phase3] Building Phi-independent feature matrix (8 features) ...")
    feats = build_feature_matrix(fundamentals, prices, universe, phi_panel=None,
                                 asof=END, spy_path=args.spy)
    # 8 Phi-independent features + identifiers
    ind_cols = ["ticker", "sigma_roic", "max_drawdown", "beta_unlevered", "icr",
                "debt_to_equity", "payout", "reinvestment", "sector_custom",
                "cap_bucket", "market_cap"]
    feats = feats[[c for c in ind_cols if c in feats.columns]]
    feat_path = os.path.join(out_dir, "features_independent.parquet")
    feats.to_parquet(feat_path, index=False)
    write_provenance(feat_path, feats, [
        "independent_features", "sigma_roic", "max_drawdown", "beta_unlevered",
        "icr", "debt_to_equity", "payout", "reinvestment", "sector_custom"],
        extra={"feature_count": 8, "note": "phi/category deliberately excluded"})
    print(f"[Phase3] Wrote {feat_path} ({len(feats)} tickers)")

    return tp, feats


def _resolve_phi_panel(args) -> str:
    if args.phi_panel:
        return args.phi_panel
    candidate = os.path.join(DEFAULT_OUTPUT_DIR, "phi_panel.parquet")
    if os.path.exists(candidate):
        return candidate
    return None


def run_full(args, tp: pd.DataFrame, feats_ind: pd.DataFrame, phi_path: str) -> dict:
    print(f"[Phase3] Loading phi_panel from {phi_path} ...")
    phi_panel = pd.read_parquet(phi_path)

    print("[Phase3] Merging phi + category into feature matrix ...")
    fundamentals = pd.read_parquet(args.fundamentals)
    prices = pd.read_parquet(args.prices)
    universe = pd.read_parquet(args.universe)
    feats = build_feature_matrix(fundamentals, prices, universe,
                                 phi_panel=phi_panel, asof=END, spy_path=args.spy)

    # restrict clustering to top performers
    tp_tickers = set(tp["ticker"])
    top_feats = feats[feats["ticker"].isin(tp_tickers)].copy()
    n_phi = top_feats["phi"].notna().sum()
    print(f"[Phase3] Top performers with phi: {n_phi} / {len(top_feats)}")

    if n_phi < 4:
        raise RuntimeError(
            f"Only {n_phi} top performers have phi values â€” cannot cluster. "
            "phi_panel may be incomplete."
        )

    res = cluster_top_performers(top_feats)
    df = res["df"]

    out_dir = _provenance_dir(args.output)

    # cluster assignments
    clusters = pd.DataFrame({
        "ticker": df["ticker"],
        "cluster_kmeans": res["labels_kmeans"],
        "cluster_dbscan": res["labels_dbscan"],
        "cluster_hierarchical": res["labels_hierarchical"],
        "cluster_consensus": res["labels_consensus"],
        "consensus_strength": res["consensus_strength"],
        "phi": df["phi"].values,
        "category": df["category"].values if "category" in df.columns else None,
        "sector_custom": df["sector_custom"].values,
        "cap_bucket": df["cap_bucket"].values,
    })
    cl_path = os.path.join(out_dir, "top_performer_clusters.parquet")
    clusters.to_parquet(cl_path, index=False)
    write_provenance(cl_path, clusters, ["consensus_clustering"],
                     extra={"methods": ["kmeans", "dbscan", "hierarchical"],
                            "kmeans_k": res["kmeans_k"],
                            "dbscan_eps": DBSCAN_EPS,
                            "dbscan_min_samples": DBSCAN_MIN_SAMPLES})
    print(f"[Phase3] Wrote {cl_path}")

    # similarity matrix (cosine, standardized risk features)
    cos = pd.DataFrame(res["cosine"], index=df["ticker"].tolist(),
                       columns=df["ticker"].tolist())
    cos.index.name = "ticker"
    sim_path = os.path.join(out_dir, "similarity_matrix.parquet")
    cos.reset_index().to_parquet(sim_path, index=False)
    write_provenance(sim_path, cos.reset_index(), ["cosine_similarity"],
                     extra={"metric": "cosine", "space": "standardized_risk_features"})
    print(f"[Phase3] Wrote {sim_path}")

    # sector correlation test
    print("[Phase3] Sector x cluster independence test (+ permutation) ...")
    sec = sector_correlation_test(
        pd.Series(res["labels_consensus"]),
        pd.Series(df["sector_custom"].values),
        n_permutations=args.n_permutations,
    )
    # acceptance criteria
    ari = res["bootstrap_ari"]
    ari_mean = float(np.mean(ari)) if len(ari) else float("nan")
    n_archetypes = int(len(np.unique(res["labels_consensus"])))
    acceptance = {
        "top_performers_ge_30": bool(len(tp) >= 30),
        "n_top_performers": int(len(tp)),
        "consensus_clusters_ge_2": bool(n_archetypes >= 2),
        "n_consensus_clusters": n_archetypes,
        "ari_ge_0.5": bool(np.isfinite(ari_mean) and ari_mean >= 0.5),
        "bootstrap_ari_mean": ari_mean,
        "cramers_v_lt_0.5": bool(sec["cramers_v"] is not None and sec["cramers_v"] < 0.5),
        "cramers_v": sec["cramers_v"],
        "permutation_p_lt_0.05": bool(sec["permutation_pvalue"] is not None
                                      and sec["permutation_pvalue"] < 0.05),
        "permutation_pvalue": sec["permutation_pvalue"],
    }
    sec_out = {k: v for k, v in sec.items() if k != "contingency"}
    sec_out["contingency"] = sec["contingency"].to_dict()
    sec_out["acceptance"] = acceptance
    sec_out["bootstrap_ari"] = {
        "mean": ari_mean,
        "median": float(np.median(ari)) if len(ari) else None,
        "p5": float(np.percentile(ari, 5)) if len(ari) else None,
        "p95": float(np.percentile(ari, 95)) if len(ari) else None,
        "n": int(len(ari)),
    }
    sec_path = os.path.join(out_dir, "sector_correlation.json")
    with open(sec_path, "w") as fh:
        json.dump(sec_out, fh, indent=2, default=str)
    print(f"[Phase3] Wrote {sec_path}")

    # archetype report
    report = build_archetype_report(res, sec, tp, benchmark=acceptance)
    rep_path = os.path.join(out_dir, "archetype_report.md")
    with open(rep_path, "w") as fh:
        fh.write(report)
    print(f"[Phase3] Wrote {rep_path}")

    print("[Phase3] Acceptance criteria:")
    for k, v in acceptance.items():
        print(f"    {k}: {v}")

    return {"clusters": res, "sector": sec, "acceptance": acceptance}


def _write_pending(args, reason: str):
    out_dir = _provenance_dir(args.output)
    marker = os.path.join(out_dir, "PENDING_PHI_PANEL.json")
    payload = {
        "status": "PENDING_PHI_PANEL",
        "reason": reason,
        "expected_path": os.path.join(DEFAULT_OUTPUT_DIR, "phi_panel.parquet"),
        "owning_workstream": "Workstream A (gemini-worker)",
        "written_at": datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
        "message": "Phase 3+4 code complete, execution pending phi_panel from Workstream A",
        "independent_artifacts": ["top_performer_list.parquet", "features_independent.parquet"],
    }
    with open(marker, "w") as fh:
        json.dump(payload, fh, indent=2)
    print(f"[Phase3] Wrote {marker}")
    return marker


def main(argv=None):
    p = argparse.ArgumentParser(description="Phase 3 top-performer similarity")
    p.add_argument("--fundamentals", default=DEFAULT_FUNDAMENTALS)
    p.add_argument("--prices", default=DEFAULT_PRICES)
    p.add_argument("--universe", default=DEFAULT_UNIVERSE)
    p.add_argument("--spy", default=DEFAULT_SPY)
    p.add_argument("--phi-panel", dest="phi_panel", default=None)
    p.add_argument("--output", default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--price-filter", choices=["real", "all"], default="real",
                   help="'real' drops synthetic_fallback price rows (default)")
    p.add_argument("--n-permutations", type=int, default=1000)
    p.add_argument("--wait-for-phi", action="store_true",
                   help="poll for phi_panel before exiting")
    p.add_argument("--max-wait-seconds", type=int, default=0,
                   help="0 = do not poll")
    args = p.parse_args(argv)

    tp, feats_ind = run_independent(args)

    phi_path = _resolve_phi_panel(args)
    if phi_path is None and args.wait_for_phi and args.max_wait_seconds > 0:
        deadline = time.time() + args.max_wait_seconds
        candidate = os.path.join(DEFAULT_OUTPUT_DIR, "phi_panel.parquet")
        print(f"[Phase3] Polling for {candidate} (max {args.max_wait_seconds}s) ...")
        while time.time() < deadline:
            if os.path.exists(candidate):
                phi_path = candidate
                break
            time.sleep(min(60, max(5, int(deadline - time.time()))))
        if phi_path is None:
            _write_pending(args, "phi_panel not produced within wait budget")
            print("[Phase3] Phase 3+4 code complete, execution pending phi_panel "
                  "from Workstream A")
            return 0

    if phi_path is None:
        _write_pending(args, "phi_panel not provided and default path absent")
        print("[Phase3] Phase 3+4 code complete, execution pending phi_panel "
              "from Workstream A")
        return 0

    run_full(args, tp, feats_ind, phi_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
