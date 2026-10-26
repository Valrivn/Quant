"""Phase 3 significance validation for the similarity clusters.

Checks:
  * permutation test — is sector x cluster concentration greater than a random
    sector shuffle? (1000 shuffles by default)
  * bootstrap 95% CIs on Cramér's V
  * cluster stability across 100 bootstraps (adjusted Rand index)

Writes ``data/research/lifecycle/similarity_significance.json``.

CLI:
    python -m validation.similarity_significance \
        --clusters data/research/lifecycle/top_performer_clusters.parquet \
        --n-permutations 1000
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from scipy.stats import chi2_contingency
from sklearn.cluster import KMeans
from sklearn.metrics import adjusted_rand_score
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research.top_performer_similarity import (  # noqa: E402
    DEFAULT_OUTPUT_DIR,
    RISK_FEATURES,
    _cramers_v,
)

N_BOOTSTRAP = 100


def _load_features(clusters: pd.DataFrame, clusters_path: str) -> pd.DataFrame:
    """Reconstruct the numeric risk-feature matrix for the clustered tickers."""
    base_dir = os.path.dirname(os.path.abspath(clusters_path))
    feat_path = os.path.join(base_dir, "features_independent.parquet")
    feats = pd.read_parquet(feat_path)
    merged = clusters[["ticker", "cluster_consensus"]].merge(feats, on="ticker", how="left")
    # phi available from the clusters file (merged from Workstream A)
    merged["phi"] = clusters.set_index("ticker")["phi"].reindex(merged["ticker"]).values
    return merged


def _standardize(df: pd.DataFrame):
    X = df[RISK_FEATURES].copy()
    for c in RISK_FEATURES:
        X[c] = pd.to_numeric(X[c], errors="coerce")
        X[c] = X[c].fillna(X[c].median())
    X = X.fillna(0.0)
    return StandardScaler().fit_transform(X.values.astype(float))


def bootstrap_cramers_v_ci(cluster, sector, n_boot=1000, seed=13, alpha=0.05):
    rng = np.random.default_rng(seed)
    cluster = np.asarray(cluster)
    sector = np.asarray(sector)
    n = len(cluster)
    vs = []
    for _ in range(n_boot):
        idx = rng.integers(0, n, size=n)
        t = pd.crosstab(cluster[idx], sector[idx]).values
        v = _cramers_v(t)
        if np.isfinite(v):
            vs.append(v)
    if not vs:
        return None, None, None
    vs = np.array(vs)
    lo = float(np.percentile(vs, 100 * alpha / 2))
    hi = float(np.percentile(vs, 100 * (1 - alpha / 2)))
    return float(np.mean(vs)), lo, hi


def permutation_pvalue(cluster, sector, observed_v, n_perm=1000, seed=11):
    rng = np.random.default_rng(seed)
    cluster = np.asarray(cluster)
    sector = np.asarray(sector)
    perm = []
    for _ in range(n_perm):
        shuffled = rng.permutation(sector)
        v = _cramers_v(pd.crosstab(cluster, shuffled).values)
        if np.isfinite(v):
            perm.append(v)
    if not perm:
        return None, None
    perm = np.array(perm)
    p = float((np.sum(perm >= observed_v) + 1) / (len(perm) + 1))
    return p, float(np.mean(perm))


def cluster_stability_ari(X, base_labels, k, n_boot=N_BOOTSTRAP, seed=17):
    rng = np.random.default_rng(seed)
    n = X.shape[0]
    aris = []
    for _ in range(n_boot):
        idx = rng.integers(0, n, size=n)
        uniq, inv = np.unique(idx, return_inverse=True)
        if len(uniq) < k:
            continue
        try:
            lab = KMeans(n_clusters=k, n_init=5, random_state=42).fit_predict(X[uniq])
        except Exception:
            continue
        try:
            aris.append(adjusted_rand_score(base_labels[idx], lab[inv]))
        except Exception:
            continue
    return aris


def main(argv=None):
    p = argparse.ArgumentParser(description="Phase 3 similarity significance")
    p.add_argument("--clusters", required=True)
    p.add_argument("--n-permutations", type=int, default=1000)
    p.add_argument("--n-bootstrap", type=int, default=1000)
    p.add_argument("--output", default=None)
    args = p.parse_args(argv)

    clusters = pd.read_parquet(args.clusters)
    if "cluster_consensus" not in clusters.columns:
        raise SystemExit("clusters file must contain 'cluster_consensus'")

    out_arg = args.output or "similarity_significance.json"
    if out_arg.endswith(".json"):
        out_path = out_arg
    else:
        out_dir = out_arg
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "similarity_significance.json")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    cluster = clusters["cluster_consensus"].values
    sector = clusters["sector_custom"].values
    feats = _load_features(clusters, args.clusters)

    # observed Cramér's V
    table = pd.crosstab(cluster, sector).values
    observed_v = _cramers_v(table)
    if np.isfinite(observed_v):
        chi2, chi2p, dof, _ = chi2_contingency(table)
    else:
        chi2 = chi2p = dof = None

    perm_p, perm_mean = permutation_pvalue(
        cluster, sector, observed_v if np.isfinite(observed_v) else 0.0,
        n_perm=args.n_permutations)

    v_mean, v_lo, v_hi = bootstrap_cramers_v_ci(
        cluster, sector, n_boot=args.n_bootstrap)

    # stability ARI vs consensus labels
    k = int(len(np.unique(cluster)))
    X = _standardize(feats)
    aris = cluster_stability_ari(X, cluster, max(k, 2))
    aris_arr = np.array(aris) if aris else np.array([np.nan])

    result = {
        "n_top_performers": int(len(clusters)),
        "n_clusters": k,
        "cramers_v_observed": float(observed_v) if np.isfinite(observed_v) else None,
        "cramers_v_bootstrap": {
            "mean": v_mean, "ci_low_95": v_lo, "ci_high_95": v_hi,
            "n_boot": int(args.n_bootstrap),
        },
        "chi2_stat": float(chi2) if chi2 is not None and np.isfinite(chi2) else None,
        "chi2_pvalue": float(chi2p) if chi2p is not None and np.isfinite(chi2p) else None,
        "dof": int(dof) if dof is not None else None,
        "permutation_pvalue": perm_p,
        "permutation_mean_cramers_v": perm_mean,
        "n_permutations": int(args.n_permutations),
        "cluster_stability_ari": {
            "mean": float(np.nanmean(aris_arr)),
            "median": float(np.nanmedian(aris_arr)),
            "p5": float(np.nanpercentile(aris_arr, 5)),
            "p95": float(np.nanpercentile(aris_arr, 95)),
            "n": int(len(aris)),
        },
        "verdict": {
            "cramers_v_lt_0.5": bool(observed_v is not None and np.isfinite(observed_v) and observed_v < 0.5),
            "permutation_p_lt_0.05": bool(perm_p is not None and perm_p < 0.05),
            "ari_ge_0.5": bool(np.isfinite(np.nanmean(aris_arr)) and np.nanmean(aris_arr) >= 0.5),
        },
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_clusters": args.clusters,
    }

    with open(out_path, "w") as fh:
        json.dump(result, fh, indent=2, default=str)
    print(f"[Phase3-val] Wrote {out_path}")
    for key in ("n_top_performers", "n_clusters", "cramers_v_observed",
                "permutation_pvalue"):
        print(f"    {key}: {result[key]}")
    print(f"    verdict: {result['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
