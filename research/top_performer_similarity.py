"""Phase 3 â€” Top-Performer Similarity Analysis.

Tests the pre-registered hypothesis that top-performing companies share a common
*risk-profile geometry* rather than a common sector.

Frozen definitions (see
``.agents/project/org/decisions/_drafts/lifecycle_sieve_phi_surface_plan_20261010.md``):

Top performer
    (a) sustained ROIC > WACC for >= 5 consecutive years (annual aggregation), AND
    (b) cumulative return > 150% over 2018-01-31 -> 2024-12-31.

Feature vector (10 features, sector excluded from the distance metric):
    [phi, sigma_roic, max_drawdown, beta_unlevered, icr, debt_to_equity,
     payout, reinvestment, category, sector_custom]

Of these, 8 are Phi-independent and can be computed *before* Workstream A emits
``phi_panel.parquet``; ``phi`` and ``category`` are merged in afterwards.

WACC note
---------
``dense_fundamentals.parquet`` does not carry a ``wacc`` column (the plan's
assumption that it does is factually incorrect for this snapshot).  WACC is
therefore reconstructed from the *same* in-house dense file using the house's
frozen flat cost-of-equity estimate of 9% (``cost_of_equity_est`` in the sibling
``valuation_fundamentals.parquet`` is flat 0.08-0.09 for every row):

    WACC = w_equity * 0.09 + w_debt * after_tax_cost_of_debt    (clamped to
           [0.03, 0.30], fallback 0.09 when components are missing).

No external downloads are performed.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd
from scipy import sparse
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import squareform
from scipy.stats import chi2_contingency, spearmanr
from sklearn.cluster import DBSCAN, AgglomerativeClustering, KMeans
from sklearn.metrics import adjusted_rand_score, silhouette_score
from sklearn.preprocessing import StandardScaler

# --------------------------------------------------------------------------- #
# Frozen constants / defaults
# --------------------------------------------------------------------------- #
START = "2018-01-31"
END = "2024-12-31"

DEFAULT_FUNDAMENTALS = "data/pit_fundamentals/dense_fundamentals.parquet"
DEFAULT_PRICES = "data/prices/price_cache.parquet"
DEFAULT_UNIVERSE = "data/universe/monthly_reconstitution.parquet"
DEFAULT_SPY = "data/master_prices/SPY.parquet"
DEFAULT_OUTPUT_DIR = "data/research/lifecycle"

# House frozen flat cost of equity (matches cost_of_equity_est in the in-house
# valuation_fundamentals.parquet, which is 0.09 for >= 75% of rows).
COST_OF_EQUITY = 0.09
CONST_FALLBACK_WACC = 0.09
WACC_BOUNDS = (0.03, 0.30)

# 10-feature vector. ``sector_custom`` and ``category`` are categorical and are
# NOT used for the distance metric; ``phi`` and ``category`` come from
# Workstream A's phi_panel.
PHI_INDEPENDENT_FEATURES = [
    "sigma_roic",
    "max_drawdown",
    "beta_unlevered",
    "icr",
    "debt_to_equity",
    "payout",
    "reinvestment",
    "sector_custom",
]
RISK_FEATURES = [
    "phi",
    "sigma_roic",
    "max_drawdown",
    "beta_unlevered",
    "icr",
    "debt_to_equity",
    "payout",
    "reinvestment",
]

# Clustering pre-registration
KMEANS_K_RANGE = range(3, 9)          # k = 3..8, chosen by silhouette
DBSCAN_EPS = 0.5
DBSCAN_MIN_SAMPLES = 3
HIER_CUT_RANGE = range(3, 6)          # Ward cut at 3..5 clusters
N_BOOTSTRAP = 100
N_CONSENSUS_METHODS = 3


# --------------------------------------------------------------------------- #
# Provenance helper
# --------------------------------------------------------------------------- #
def write_provenance(
    output_path: str,
    df: pd.DataFrame,
    transformations,
    source: str = "IN_HOUSE_DENSE_FUNDAMENTALS",
    source_version: str = "dense_fundamentals_20261010",
    date_range=None,
    mapping_confidence: str = "high",
    extra: Optional[dict] = None,
) -> dict:
    """Write ``<output_path>.provenance.json`` sidecar with a sha256 of the file."""
    h = hashlib.sha256()
    with open(output_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    sha256_val = h.hexdigest()

    if date_range is None:
        date_range = {"min": START, "max": END}

    meta = {
        "source": source,
        "retrieval_timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
        "source_version": source_version,
        "pit_timestamp_column": "filed_date",
        "entity_key": "ticker",
        "transformations": list(transformations),
        "sha256": sha256_val,
        "row_count": int(len(df)),
        "date_range": {"min": str(date_range["min"]), "max": str(date_range["max"])},
        "mapping_confidence": mapping_confidence,
    }
    if extra:
        meta.update(extra)

    sidecar = output_path + ".provenance.json"
    with open(sidecar, "w") as fh:
        json.dump(meta, fh, indent=2)
    return meta


# --------------------------------------------------------------------------- #
# Low-level loaders / helpers
# --------------------------------------------------------------------------- #
def _to_date(s):
    return pd.to_datetime(s)


def filter_real_prices(prices: pd.DataFrame) -> pd.DataFrame:
    """Drop ``synthetic_fallback`` rows when a ``source`` column is present.

    The house price cache mixes real vendor rows (``master_prices`` / ``yahoo``)
    with ``synthetic_fallback`` rows.  Synthetic prices have ~0 cross-sectional
    correlation with the market and MUST NOT be used for returns, drawdowns or
    beta estimation.
    """
    if "source" not in prices.columns:
        return prices
    n0 = len(prices)
    out = prices[prices["source"] != "synthetic_fallback"].copy()
    removed = n0 - len(out)
    if removed:
        print(f"[prices] filtered {removed:,} synthetic_fallback rows "
              f"({removed / n0:.1%}); {len(out):,} real rows remain")
    return out


def compute_wacc_series(fund: pd.DataFrame) -> pd.Series:
    """Reconstruct a PIT WACC from dense fundamentals (house flat Ke = 9%)."""
    we = pd.to_numeric(fund.get("weight_equity"), errors="coerce")
    wd = pd.to_numeric(fund.get("weight_debt"), errors="coerce")
    atcd = pd.to_numeric(fund.get("after_tax_cost_of_debt"), errors="coerce")
    wacc = we.fillna(0.0) * COST_OF_EQUITY + wd.fillna(0.0) * atcd.fillna(0.0)
    valid = np.isfinite(wacc) & (wacc > 0) & (we.notna() | wd.notna())
    wacc = wacc.where(valid, np.nan)
    wacc = wacc.clip(*WACC_BOUNDS)
    return wacc.fillna(CONST_FALLBACK_WACC)


def _max_consecutive_run(flags) -> int:
    best = cur = 0
    for f in flags:
        if bool(f):
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return int(best)


def annual_roic_table(fund: pd.DataFrame) -> pd.DataFrame:
    """Annual ROIC / WACC aggregation by (ticker, fiscal_year)."""
    f = fund.copy()
    f["filed_date"] = _to_date(f["filed_date"])
    f["wacc"] = compute_wacc_series(f)
    f["roic"] = pd.to_numeric(f["roic"], errors="coerce")
    f = f.dropna(subset=["roic", "fiscal_year"])
    ann = (
        f.groupby(["ticker", "fiscal_year"])
        .agg(roic=("roic", "mean"), wacc=("wacc", "mean"))
        .reset_index()
    )
    ann["beat"] = ann["roic"] > ann["wacc"]
    return ann.sort_values(["ticker", "fiscal_year"])


def cumulative_returns(
    prices: pd.DataFrame,
    start: str,
    end: str,
    real_only: bool = True,
    min_obs: int = 250,
    require_start: str = "2018-06-30",
) -> pd.DataFrame:
    """Total cumulative return per ticker over [start, end] using first/last close.

    Requires ``min_obs`` real observations and data starting no later than
    ``require_start`` so a ticker's move is genuinely measured across the window.
    """
    px = prices.copy()
    if real_only:
        px = filter_real_prices(px)
    px = px[["ticker", "date", "close"]].copy()
    px["date"] = _to_date(px["date"])
    px["close"] = pd.to_numeric(px["close"], errors="coerce")
    px = px.dropna(subset=["close"]).sort_values(["ticker", "date"])
    s = pd.Timestamp(start)
    e = pd.Timestamp(end)
    win = px[(px["date"] >= s) & (px["date"] <= e)]
    grp = win.groupby("ticker")
    first = grp["close"].first()
    last = grp["close"].last()
    n = grp.size()
    first_date = grp["date"].first()
    cum = (last / first) - 1.0
    out = pd.DataFrame({"cum_return": cum, "n_obs": n, "first_date": first_date})
    invalid = (out["n_obs"] < min_obs) | (out["first_date"] > pd.Timestamp(require_start))
    out.loc[invalid, "cum_return"] = np.nan
    return out.reset_index()


def _monthly_returns(prices: pd.DataFrame, real_only: bool = True) -> pd.DataFrame:
    """Ticker x month matrix of simple monthly returns (month-end close)."""
    px = prices.copy()
    if real_only:
        px = filter_real_prices(px)
    px = px[["ticker", "date", "close"]].copy()
    px["date"] = _to_date(px["date"])
    px = px.dropna(subset=["close"])
    px["ym"] = px["date"].dt.to_period("M")
    month_close = px.groupby(["ticker", "ym"])["close"].last().unstack()
    return month_close.pct_change(axis=1, fill_method=None)


def _spy_monthly_returns(spy_path: str) -> pd.Series:
    spy = pd.read_parquet(spy_path)
    # SPY is stored with a DatetimeIndex and a 'Close' column.
    if "Close" in spy.columns:
        close = spy["Close"]
        idx = spy.index
    else:
        close = spy.iloc[:, -1]
        idx = spy.iloc[:, 0]
    idx = pd.to_datetime(idx)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    close.index = idx
    close = close.sort_index()
    m = close.resample("ME").last()
    m.index = m.index.to_period("M")
    return m.pct_change(fill_method=None)


def compute_beta_unlevered_table(
    prices: pd.DataFrame,
    fund: pd.DataFrame,
    spy_path: str = DEFAULT_SPY,
    asof: str = END,
    window_months: int = 60,
    min_obs: int = 24,
) -> pd.DataFrame:
    """Trailing beta vs SPY (monthly), unlevered with PIT tax and D/E."""
    mret = _monthly_returns(prices)
    spy = _spy_monthly_returns(spy_path)
    asof_p = pd.Period(pd.Timestamp(asof), freq="M")
    cols = [c for c in mret.columns if c <= asof_p]
    mret = mret[cols]
    spy = spy.reindex(cols)

    # latest PIT tax/de per ticker as of asof
    f = fund.copy()
    f["filed_date"] = _to_date(f["filed_date"])
    f = f[f["filed_date"] <= pd.Timestamp(asof)].sort_values("filed_date")
    latest = f.groupby("ticker").last()

    def _tax_de(row):
        tax = row.get("effective_tax_rate", np.nan)
        if pd.isna(tax) or tax < 0 or tax > 0.6:
            tax = 0.21
        debt = row.get("total_debt", np.nan)
        eq = row.get("equity", np.nan)
        if pd.isna(debt) or pd.isna(eq) or eq <= 0:
            de = 10.0
        else:
            de = float(np.clip(debt / eq, 0.0, 10.0))
        return float(tax), de

    spy_var = float(np.nanvar(spy.values, ddof=1))
    records = []
    for ticker, row in mret.iterrows():
        series = row.dropna()
        # keep the most recent window_months
        series = series.tail(window_months)
        joined = pd.concat([series.rename("r"), spy.rename("m")], axis=1).dropna()
        joined = joined.tail(window_months)
        if len(joined) >= min_obs and spy_var > 0:
            cov = float(np.cov(joined["r"], joined["m"], ddof=1)[0, 1])
            beta_lev = cov / spy_var
        else:
            beta_lev = 1.0
        tax, de = _tax_de(latest.loc[ticker]) if ticker in latest.index else (0.21, 0.0)
        beta_u = beta_lev / (1.0 + (1.0 - tax) * de)
        if not np.isfinite(beta_u):
            beta_u = 1.0
        beta_u = max(beta_u, 0.1)
        records.append(
            {
                "ticker": ticker,
                "beta_levered": float(beta_lev),
                "beta_unlevered": float(beta_u),
                "tax_rate": tax,
                "debt_to_equity": de,
                "beta_n_obs": int(len(joined)),
            }
        )
    return pd.DataFrame(records)


def max_drawdown_table(
    prices: pd.DataFrame,
    start: str = START,
    end: str = END,
    real_only: bool = True,
) -> pd.DataFrame:
    """Per-ticker max drawdown (negative number) over [start, end]."""
    px = prices.copy()
    if real_only:
        px = filter_real_prices(px)
    px = px[["ticker", "date", "close"]].copy()
    px["date"] = _to_date(px["date"])
    px = px[(px["date"] >= pd.Timestamp(start)) & (px["date"] <= pd.Timestamp(end))]
    px = px.dropna(subset=["close"]).sort_values(["ticker", "date"])
    px["run_max"] = px.groupby("ticker")["close"].cummax()
    px["dd"] = px["close"] / px["run_max"] - 1.0
    mdd = px.groupby("ticker")["dd"].min()
    return pd.DataFrame({"max_drawdown": mdd}).reset_index()


# --------------------------------------------------------------------------- #
# Top-performer identification
# --------------------------------------------------------------------------- #
def identify_top_performers(
    fundamentals: pd.DataFrame,
    prices: pd.DataFrame,
    start: str = START,
    end: str = END,
    universe: Optional[pd.DataFrame] = None,
    real_only: bool = True,
    min_obs: int = 250,
) -> pd.DataFrame:
    """Return the pre-registered top-performer list.

    (a) sustained ROIC > WACC for >= 5 consecutive fiscal years
    (b) cumulative price return > 150% over [start, end]

    ``real_only`` drops ``synthetic_fallback`` price rows (default) â€” see
    :func:`filter_real_prices`.
    """
    ann = annual_roic_table(fundamentals)
    runs = (
        ann.groupby("ticker")["beat"]
        .apply(lambda s: _max_consecutive_run(s.values))
        .rename("years_compounding")
        .reset_index()
    )
    sustained = runs[runs["years_compounding"] >= 5][["ticker", "years_compounding"]]

    cum = cumulative_returns(prices, start, end, real_only=real_only, min_obs=min_obs)
    tp = sustained.merge(cum[["ticker", "cum_return", "n_obs"]], on="ticker", how="inner")
    tp = tp[tp["cum_return"] > 1.50].copy()
    tp["price_source"] = "real" if real_only else "all_incl_synthetic"

    if universe is not None:
        u = universe.copy()
        u["rebalance_date"] = _to_date(u["rebalance_date"])
        u = u[u["rebalance_date"] <= pd.Timestamp(end)].sort_values("rebalance_date")
        latest = u.groupby("ticker").last()
        tp["sector_custom"] = tp["ticker"].map(latest["sector_custom"])
        tp["cap_bucket"] = tp["ticker"].map(latest["cap_bucket"])
        tp["market_cap"] = tp["ticker"].map(latest["market_cap"])
    else:
        tp["sector_custom"] = np.nan
        tp["cap_bucket"] = np.nan
        tp["market_cap"] = np.nan

    tp = tp[["ticker", "cum_return", "years_compounding", "sector_custom",
             "cap_bucket", "market_cap", "n_obs", "price_source"]]
    return tp.sort_values("cum_return", ascending=False).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Feature matrix
# --------------------------------------------------------------------------- #
def _latest_fundamentals(fund: pd.DataFrame, asof: str) -> pd.DataFrame:
    f = fund.copy()
    f["filed_date"] = _to_date(f["filed_date"])
    f = f[f["filed_date"] <= pd.Timestamp(asof)].sort_values("filed_date")
    return f.groupby("ticker").last()


def _trailing_ratios(fund: pd.DataFrame, asof: str) -> pd.DataFrame:
    """Trailing-3yr payout and reinvestment per ticker (mirrors lifecycle_sieve)."""
    f = fund.copy()
    f["filed_date"] = _to_date(f["filed_date"])
    f = f[f["filed_date"] <= pd.Timestamp(asof)].sort_values("filed_date")
    rows = []
    for ticker, grp in f.groupby("ticker"):
        t = grp.tail(12)
        div = pd.to_numeric(t.get("dividends_paid"), errors="coerce").sum(skipna=True)
        ni = pd.to_numeric(t.get("net_income"), errors="coerce").sum(skipna=True)
        payout = div / ni if (pd.notna(ni) and ni > 0 and pd.notna(div)) else np.nan
        capex = pd.to_numeric(t.get("capex"), errors="coerce").abs().sum(skipna=True)
        rd = pd.to_numeric(t.get("rd_expense"), errors="coerce").abs().sum(skipna=True)
        acq = pd.to_numeric(t.get("acquisitions"), errors="coerce").abs().sum(skipna=True)
        ocf = pd.to_numeric(t.get("operating_cash_flow"), errors="coerce").sum(skipna=True)
        reinv = (capex + rd + acq) / ocf if (pd.notna(ocf) and ocf > 0) else np.nan
        rows.append({"ticker": ticker, "payout": payout, "reinvestment": reinv})
    return pd.DataFrame(rows)


def build_feature_matrix(
    fundamentals: pd.DataFrame,
    prices: pd.DataFrame,
    universe: pd.DataFrame,
    phi_panel: Optional[pd.DataFrame] = None,
    asof: str = END,
    spy_path: str = DEFAULT_SPY,
) -> pd.DataFrame:
    """Build the 10-feature risk-profile matrix for every ticker.

    The 8 Phi-independent features are always produced; ``phi`` and ``category``
    are merged from ``phi_panel`` when supplied.  Returns one row per ticker.
    """
    # 1. sigma(ROIC)
    ann = annual_roic_table(fundamentals)
    sigma = ann.groupby("ticker")["roic"].agg(["std", "count"])
    sigma = sigma[sigma["count"] >= 3].rename(columns={"std": "sigma_roic"})[["sigma_roic"]]

    # 2. ICR, 3. D/E from latest PIT row
    latest = _latest_fundamentals(fundamentals, asof)

    def _icr(row):
        ebit = row.get("ebit", np.nan)
        ie = row.get("interest_expense", np.nan)
        if pd.isna(ie) or abs(ie) < 1e-9:
            return 20.0
        if pd.isna(ebit):
            return np.nan
        return float(np.clip(ebit / abs(ie), 0.0, 20.0))

    icr = latest.apply(_icr, axis=1).rename("icr")

    def _de(row):
        debt = row.get("total_debt", np.nan)
        eq = row.get("equity", np.nan)
        if pd.isna(debt) or pd.isna(eq) or eq <= 0:
            return 10.0
        return float(np.clip(debt / eq, 0.0, 10.0))

    de = latest.apply(_de, axis=1).rename("debt_to_equity")

    # 4/5. payout / reinvestment (trailing 3yr)
    ratios = _trailing_ratios(fundamentals, asof).set_index("ticker")

    # 6. max drawdown
    mdd = max_drawdown_table(prices).set_index("ticker")

    # 7. beta unlevered
    beta = compute_beta_unlevered_table(prices, fundamentals, spy_path=spy_path, asof=asof)
    beta = beta.set_index("ticker")

    # 8. sector / cap from universe
    u = universe.copy()
    u["rebalance_date"] = _to_date(u["rebalance_date"])
    u = u[u["rebalance_date"] <= pd.Timestamp(asof)].sort_values("rebalance_date")
    u = u.groupby("ticker").last()

    feats = pd.DataFrame(index=sorted(fundamentals["ticker"].unique()))
    feats.index.name = "ticker"
    feats["sigma_roic"] = sigma["sigma_roic"]
    feats["icr"] = icr
    feats["debt_to_equity"] = de
    feats["payout"] = ratios["payout"]
    feats["reinvestment"] = ratios["reinvestment"]
    feats["max_drawdown"] = mdd["max_drawdown"]
    feats["beta_unlevered"] = beta["beta_unlevered"]
    feats["sector_custom"] = u["sector_custom"]
    feats["cap_bucket"] = u["cap_bucket"]
    feats["market_cap"] = u["market_cap"]

    if phi_panel is not None:
        pp = phi_panel.copy()
        pp["rebalance_date"] = _to_date(pp["rebalance_date"])
        pp = pp[pp["rebalance_date"] <= pd.Timestamp(asof)]
        if len(pp):
            pp_latest = pp.sort_values("rebalance_date").groupby("ticker").last()
            feats["phi"] = pp_latest["phi"]
            feats["category"] = pp_latest["category"]
        else:
            feats["phi"] = np.nan
            feats["category"] = np.nan

    return feats.reset_index()


# --------------------------------------------------------------------------- #
# Clustering
# --------------------------------------------------------------------------- #
def _prepare_matrix(features: pd.DataFrame):
    """Impute + standardize the risk features. Returns (X, df, imputed_cols)."""
    missing = [c for c in RISK_FEATURES if c not in features.columns]
    if missing:
        raise ValueError(
            f"Feature matrix missing required risk features: {missing}. "
            "Phi-dependent features require phi_panel from Workstream A."
        )
    df = features.dropna(subset=["phi"]).copy().reset_index(drop=True)
    # Robust preprocessing: clip each feature at the 1st/99th percentile of the
    # cross-section (standard descriptive winsorisation, uniform across features,
    # fixed levels — NOT tuned to outcomes). Originally frozen as absolute bounds
    # (0, 50) for phi, but Workstream A's phi_panel exports Phi on a 0..2200 scale
    # (median ~750) that the plan's [0,5)-style bins do not anticipate; absolute
    # bounds would collapse the pre-registered fragility variable to a constant.
    # Percentile clipping preserves Phi's ordinal content while taming tails.
    imputed = {}
    for c in RISK_FEATURES:
        col = pd.to_numeric(df[c], errors="coerce")
        lo, hi = col.quantile([0.01, 0.99])
        if pd.isna(lo):
            lo, hi = -np.inf, np.inf
        col = col.clip(lo, hi)
        med = col.median()
        n_imp = int(col.isna().sum())
        if n_imp:
            imputed[c] = n_imp
        df[c] = col.fillna(med if pd.notna(med) else 0.0)
    X = StandardScaler().fit_transform(df[RISK_FEATURES].values.astype(float))
    return X, df, imputed


def _fit_methods(X, n):
    """Fit KMeans / DBSCAN / Ward and return a dict of label arrays."""
    out = {}
    # KMeans k=3..8 by silhouette
    k_lo = 3
    k_hi = min(8, n - 1)
    best_k, best_sil, best_lab = None, -np.inf, None
    if k_hi >= k_lo + 1:
        for k in range(k_lo, k_hi + 1):
            km = KMeans(n_clusters=k, n_init=10, random_state=42)
            lab = km.fit_predict(X)
            if len(set(lab)) < 2:
                continue
            sil = silhouette_score(X, lab)
            if sil > best_sil:
                best_k, best_sil, best_lab = k, sil, lab
    if best_lab is None:  # degenerate tiny sample
        best_k = 2
        best_lab = KMeans(n_clusters=2, n_init=10, random_state=42).fit_predict(X)
        best_sil = np.nan
    out["kmeans"] = best_lab
    out["kmeans_k"] = best_k
    out["kmeans_silhouette"] = float(best_sil)

    # DBSCAN (pre-registered eps/min_samples)
    db = DBSCAN(eps=DBSCAN_EPS, min_samples=DBSCAN_MIN_SAMPLES)
    out["dbscan"] = db.fit_predict(X)

    # Hierarchical Ward, cut at 3..5 by silhouette
    Z = linkage(X, method="ward")
    h_lo = 3
    h_hi = min(5, n - 1)
    best_h, best_hsil, best_hlab = None, -np.inf, None
    if h_hi >= 2:
        for k in range(min(h_lo, h_hi), h_hi + 1):
            lab = fcluster(Z, t=k, criterion="maxclust")
            if len(set(lab)) < 2:
                continue
            sil = silhouette_score(X, lab)
            if sil > best_hsil:
                best_h, best_hsil, best_hlab = k, sil, lab
    if best_hlab is None:
        best_hlab = fcluster(Z, t=2, criterion="maxclust")
        best_h = 2
        best_hsil = np.nan
    out["hierarchical"] = best_hlab
    out["hier_n"] = best_h
    out["hier_silhouette"] = float(best_hsil)
    return out


def _coassociation(labels_dict, n):
    """Pairwise co-association across the three methods (fraction co-clustered)."""
    C = np.zeros((n, n), dtype=float)
    valid_methods = 0
    for key in ("kmeans", "dbscan", "hierarchical"):
        lab = labels_dict[key]
        if lab is None:
            continue
        valid_methods += 1
        same = lab[:, None] == lab[None, :]
        # ignore DBSCAN noise labels (-1): a noise point co-clusters with nothing
        if key == "dbscan":
            noise = (lab == -1)
            same[noise, :] = False
            same[:, noise] = False
        C += same.astype(float)
    if valid_methods:
        C /= valid_methods
    np.fill_diagonal(C, 1.0)
    return C


def _consensus_labels(C, n_clusters):
    """Average-linkage consensus clustering on the co-association distance."""
    dist = 1.0 - C
    np.fill_diagonal(dist, 0.0)
    dist = np.clip(dist, 0.0, 1.0)
    try:
        agg = AgglomerativeClustering(
            n_clusters=n_clusters, metric="precomputed", linkage="average"
        )
        lab = agg.fit_predict(squareform(dist, checks=False))
    except Exception:
        lab = KMeans(n_clusters=n_clusters, n_init=10, random_state=42).fit_predict(C)
    return lab


def _bootstrap_stability(X, base_labels, k, n_boot=N_BOOTSTRAP, seed=7):
    """Bootstrap KMeans stability (ARI) and pairwise co-occurrence frequency."""
    rng = np.random.default_rng(seed)
    n = X.shape[0]
    aris = []
    # accumulate co-occurrence counts on original index pairs that were sampled
    co = np.zeros((n, n), dtype=float)
    co_denom = np.zeros((n, n), dtype=float)
    for _ in range(n_boot):
        idx = rng.integers(0, n, size=n)
        uniq, inv = np.unique(idx, return_inverse=True)
        Xb = X[uniq]
        if len(uniq) < k:
            continue
        try:
            lab_b = KMeans(n_clusters=k, n_init=5, random_state=42).fit_predict(Xb)
        except Exception:
            continue
        # ARI against base on the sampled original indices
        try:
            aris.append(adjusted_rand_score(base_labels[idx], lab_b[inv]))
        except Exception:
            pass
        same = (lab_b[:, None] == lab_b[None, :]).astype(float)
        oi = uniq
        co[np.ix_(oi, oi)] += same
        co_denom[np.ix_(oi, oi)] += 1.0
    with np.errstate(invalid="ignore", divide="ignore"):
        co_freq = np.where(co_denom > 0, co / co_denom, np.nan)
    return np.array(aris), co_freq


def cluster_top_performers(features: pd.DataFrame) -> dict:
    """Consensus clustering of top performers (KMeans + DBSCAN + Ward)."""
    X, df, imputed = _prepare_matrix(features)
    n = df.shape[0]
    if n < 4:
        raise ValueError(f"Too few top performers to cluster: n={n}")

    methods = _fit_methods(X, n)
    C = _coassociation(methods, n)

    # consensus number of clusters: use KMeans choice, bounded to [2, n-1]
    k_cons = int(np.clip(methods["kmeans_k"], 2, n - 1))
    consensus = _consensus_labels(C, k_cons)

    # consensus strength: mean co-association with own consensus cluster mates
    strength = np.zeros(n)
    for lab in np.unique(consensus):
        mask = consensus == lab
        if mask.sum() <= 1:
            strength[mask] = 1.0
            continue
        sub = C[np.ix_(mask, mask)].copy()
        np.fill_diagonal(sub, np.nan)
        strength[mask] = np.nanmean(sub, axis=1)

    aris, co_freq = _bootstrap_stability(X, methods["kmeans"], methods["kmeans_k"])

    # cosine similarity on standardized features
    norm = np.linalg.norm(X, axis=1, keepdims=True)
    norm[norm == 0] = 1.0
    Xn = X / norm
    cosine = Xn @ Xn.T

    out = {
        "df": df,
        "X": X,
        "labels_kmeans": methods["kmeans"],
        "labels_dbscan": methods["dbscan"],
        "labels_hierarchical": methods["hierarchical"],
        "labels_consensus": consensus,
        "consensus_strength": strength,
        "kmeans_k": methods["kmeans_k"],
        "kmeans_silhouette": methods["kmeans_silhouette"],
        "hier_n": methods["hier_n"],
        "hier_silhouette": methods["hier_silhouette"],
        "dbscan_n_clusters": int(len(set(methods["dbscan"])) - (1 if -1 in methods["dbscan"] else 0)),
        "dbscan_noise": int((methods["dbscan"] == -1).sum()),
        "coassociation": C,
        "bootstrap_ari": aris,
        "bootstrap_cooccurrence": co_freq,
        "cosine": cosine,
        "imputed": imputed,
    }
    return out


# --------------------------------------------------------------------------- #
# Sector correlation test
# --------------------------------------------------------------------------- #
def _cramers_v(table: np.ndarray) -> float:
    if table.size == 0 or table.shape[0] < 2 or table.shape[1] < 2:
        return np.nan
    chi2 = chi2_contingency(table)[0]
    n = table.sum()
    if n == 0:
        return np.nan
    r, c = table.shape
    denom = n * min(r - 1, c - 1)
    return float(np.sqrt(chi2 / denom)) if denom > 0 else np.nan


def sector_correlation_test(
    clusters: pd.Series,
    sectors: pd.Series,
    n_permutations: int = 1000,
    seed: int = 11,
) -> dict:
    """Chi-square independence of cluster x sector + CramÃ©r's V + permutation."""
    df = pd.DataFrame({"cluster": clusters, "sector": sectors}).dropna()
    contingency = pd.crosstab(df["cluster"], df["sector"])
    table = contingency.values
    chi2, p, dof, _ = chi2_contingency(table) if table.size else (np.nan, np.nan, np.nan, None)
    v = _cramers_v(table)

    # Herfindahl concentration index per cluster (sector shares squared)
    herf = {}
    for cluster, row in contingency.iterrows():
        shares = row.values.astype(float)
        total = shares.sum()
        herf[str(cluster)] = float(np.sum((shares / total) ** 2)) if total > 0 else np.nan

    # permutation: shuffle sector labels, recompute V
    rng = np.random.default_rng(seed)
    sector_vals = df["sector"].values
    cluster_vals = df["cluster"].values
    perm_vs = []
    for _ in range(n_permutations):
        shuffled = rng.permutation(sector_vals)
        t = pd.crosstab(cluster_vals, shuffled).values
        perm_vs.append(_cramers_v(t))
    perm_vs = np.array([x for x in perm_vs if np.isfinite(x)])
    perm_p = float((np.sum(perm_vs >= v) + 1) / (len(perm_vs) + 1)) if len(perm_vs) else np.nan

    return {
        "contingency": contingency,
        "chi2_stat": float(chi2) if np.isfinite(chi2) else None,
        "chi2_pvalue": float(p) if np.isfinite(p) else None,
        "dof": int(dof) if dof is not None and np.isfinite(dof) else None,
        "cramers_v": float(v) if np.isfinite(v) else None,
        "herfindahl_per_cluster": herf,
        "permutation_pvalue": perm_p,
        "n_permutations": int(len(perm_vs)),
    }


# --------------------------------------------------------------------------- #
# Archetype naming / report
# --------------------------------------------------------------------------- #
def name_archetype(centroid: pd.Series, phi_saturated: bool = False) -> str:
    """Heuristic archetype naming from a cluster centroid (raw feature space).

    ``phi_saturated``: Workstream A's phi_panel exports Phi on a scale (median
    ~750, cap 2200) where the plan's Fortress <1 / Fragile >3 thresholds are
    never reached — the Phi legs are skipped and naming falls back to the
    risk-profile features.
    """
    phi = centroid.get("phi", np.nan)
    payout = centroid.get("payout", np.nan)
    de = centroid.get("debt_to_equity", np.nan)
    icr = centroid.get("icr", np.nan)
    beta = centroid.get("beta_unlevered", np.nan)
    sigma = centroid.get("sigma_roic", np.nan)

    if not phi_saturated and pd.notna(phi) and phi < 1.0:
        base = "Fortress Compounder"
    elif not phi_saturated and pd.notna(phi) and phi > 3.0:
        base = "Fragile Stress"
    elif pd.notna(sigma) and sigma > 0.08:
        base = "Efficient Cyclical"
    elif pd.notna(payout) and payout > 0.50:
        base = "Covered Yielder"
    else:
        base = "Balanced Compounder"

    tags = []
    if pd.notna(payout) and payout > 0.50:
        tags.append("Covered Yielder")
    if pd.notna(de) and de < 0.3:
        tags.append("Low-Leverage")
    if pd.notna(icr) and icr >= 15:
        tags.append("Cash-Rich")
    if pd.notna(beta) and beta > 1.2:
        tags.append("High-Beta")
    if tags:
        base = f"{base} / {' + '.join(tags)}"
    return base


def build_archetype_report(
    cluster_res: dict,
    sector_res: dict,
    top_performers: pd.DataFrame,
    benchmark: Optional[dict] = None,
) -> str:
    df = cluster_res["df"]
    labels = cluster_res["labels_consensus"]
    strength = cluster_res["consensus_strength"]
    df = df.assign(cluster_consensus=labels, consensus_strength=strength)

    lines = []
    lines.append("# Phase 3 â€” Top-Performer Archetype Report")
    lines.append("")
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}")
    lines.append("")
    lines.append(f"- Top performers identified: **{len(top_performers)}**")
    lines.append(f"- Consensus clusters: **{len(np.unique(labels))}** "
                 f"(KMeans k={cluster_res['kmeans_k']}, silhouette={cluster_res['kmeans_silhouette']:.3f})")
    lines.append(f"- DBSCAN clusters: {cluster_res['dbscan_n_clusters']} "
                 f"({cluster_res['dbscan_noise']} noise points, eps={DBSCAN_EPS}, min_samples={DBSCAN_MIN_SAMPLES})")
    lines.append(f"- Ward clusters: {cluster_res['hier_n']} (silhouette={cluster_res['hier_silhouette']:.3f})")
    ari = cluster_res["bootstrap_ari"]
    if len(ari):
        lines.append(f"- Bootstrap ARI stability (KMeans, {len(ari)} resamples): "
                     f"mean={np.mean(ari):.3f}, median={np.median(ari):.3f}, "
                     f"5-95pct=[{np.percentile(ari,5):.3f}, {np.percentile(ari,95):.3f}]")
    lines.append(f"- Imputed feature cells: {cluster_res['imputed']}")
    lines.append("")
    lines.append("## Sector independence (chi-square / CramÃ©r's V)")
    lines.append("")
    lines.append(f"- chi2 = {sector_res['chi2_stat']}")
    lines.append(f"- p-value = {sector_res['chi2_pvalue']}")
    lines.append(f"- CramÃ©r's V = {sector_res['cramers_v']}")
    lines.append(f"- Permutation p-value = {sector_res['permutation_pvalue']} "
                 f"({sector_res['n_permutations']} shuffles)")
    lines.append("")
    lines.append("## Clusters")
    lines.append("")
    phi_saturated = bool(df["phi"].median() > 3.0) if "phi" in df.columns else False
    if phi_saturated:
        lines.append("> Note: Workstream A's Phi scale saturates the plan's "
                     "Fortress/Fragile thresholds for every name (median Phi "
                     f"={df['phi'].median():.1f} > 3.0). Archetype names below fall "
                     "back to risk-profile features (ROIC vol, payout, leverage, ICR).")
        lines.append("")
    for lab in sorted(df["cluster_consensus"].unique()):
        sub = df[df["cluster_consensus"] == lab]
        cent = sub[RISK_FEATURES].mean()
        archetype = name_archetype(cent, phi_saturated=phi_saturated)
        lines.append(f"### Cluster {lab} â€” {archetype} (n={len(sub)})")
        lines.append("")
        lines.append(f"- mean consensus strength: {sub['consensus_strength'].mean():.3f}")
        lines.append("- feature means: " + ", ".join(
            f"{c}={cent[c]:.3f}" for c in RISK_FEATURES))
        if "sector_custom" in sub.columns:
            secc = sub["sector_custom"].value_counts(normalize=True)
            lines.append("- sector mix: " + ", ".join(
                f"{k} {v:.0%}" for k, v in secc.items()))
        lines.append("- members: " + ", ".join(sorted(sub["ticker"].tolist())[:40])
                     + (" ..." if len(sub) > 40 else ""))
        lines.append("")

    if benchmark:
        lines.append("## Acceptance criteria")
        lines.append("")
        for k, v in benchmark.items():
            lines.append(f"- {k}: {v}")
        lines.append("")

    return "\n".join(lines)
