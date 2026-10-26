"""Phase 4 — Statistical alpha signals from the lifecycle sieve + survival surface.

Implements the four pre-registered (frozen) alpha signals from
``lifecycle_sieve_phi_surface_plan_20261010.md`` Section 6, Phase 4:

Signal 1  Break-boundary proximity   z_break = (t_current - t*) / sigma(t*)
Signal 2  Fragility divergence       z_phi   = (Phi_i - Phi_peer_mean) / Phi_peer_std
Signal 3  Category rotation          regime-conditioned category Sharpe (walk-forward)
Signal 4  Survival duration          expected remaining survival = int_t^10 S(u) du

These are structural/statistical tilts, NOT discretionary sell signals.
No-lookahead: every signal at a row's ``rebalance_date`` uses only data known at
that date (fundamentals PIT, prices <= rebalance_date, trailing regime history).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

MAX_HORIZON_YEARS = 10
COST_BPS_PER_YEAR = 53.0
RANK_LOOKBACK_MONTHS = 36

PHI_BINS = [-np.inf, 1.0, 2.0, 3.0, 5.0, np.inf]
PHI_LABELS = ["[0,1)", "[1,2)", "[2,3)", "[3,5)", "[5,inf)"]
CATEGORIES = ["FAST_GROWER", "STALWART", "CYCLICAL", "SLOW_GROWER"]

# Dummy-cohort signature written by build_survival_surface.py when a
# (category, phi_bin) cohort has < 5 rows: S(t) = max(1 - 0.05*t, 0.1).
_DUMMY_S = lambda t: float(np.clip(1.0 - 0.05 * t, 0.1, None))  # noqa: E731


# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------

def load_phi_panel(path: str) -> pd.DataFrame:
    return prepare_phi_panel(pd.read_parquet(path))


def prepare_phi_panel(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["rebalance_date"] = pd.to_datetime(df["rebalance_date"])
    df["phi"] = pd.to_numeric(df["phi"], errors="coerce")
    df["phi_bin"] = pd.cut(df["phi"], bins=PHI_BINS, labels=PHI_LABELS)
    return df


def load_surface(surface_path: str, break_points_path: str):
    """Return (surface_df, break_df, dummy_cohorts).

    Dummy cohorts (built for <5-row cells) are detected by exact equality of
    their S(t) profile to the dummy monotonic pattern and are returned as a set
    of (category, phi_bin) tuples so downstream signals can exclude them.
    """
    surf = pd.read_parquet(surface_path)
    brk = pd.read_parquet(break_points_path)

    def _is_dummy(grp: pd.DataFrame) -> bool:
        s = grp.set_index("t_year")["S_t"]
        if len(s) < 5:
            return True
        expected = np.array([_DUMMY_S(t) for t in range(1, 11)])
        actual = np.array([s.get(t, np.nan) for t in range(1, 11)])
        return bool(np.allclose(actual, expected, atol=1e-9))

    dummies = {
        (cat, pbin)
        for (cat, pbin), grp in surf.groupby(["category", "phi_bin"])
        if _is_dummy(grp)
    }
    surf["is_dummy_cohort"] = surf.set_index(["category", "phi_bin"]).index.isin(dummies)
    brk = brk.drop_duplicates(subset=["category", "phi_bin"], keep="first")
    return surf, brk, dummies


def real_monthly_returns(prices: pd.DataFrame) -> pd.DataFrame:
    """Ticker x month matrix of simple monthly returns from REAL prices only."""
    px = prices.copy()
    if "source" in px.columns:
        px = px[px["source"] != "synthetic_fallback"]
    px = px[["ticker", "date", "close"]].copy()
    px["date"] = pd.to_datetime(px["date"])
    px = px.dropna(subset=["close"])
    px["ym"] = px["date"].dt.to_period("M")
    month_close = px.groupby(["ticker", "ym"])["close"].last().unstack()
    return month_close.pct_change(axis=1, fill_method=None)


# ---------------------------------------------------------------------------
# Signal 1 — break-boundary proximity
# ---------------------------------------------------------------------------

def ticker_elapsed_years(phi_df: pd.DataFrame) -> pd.Series:
    """Years since a ticker's first rebalance row in the panel (plateau age)."""
    first = phi_df.groupby("ticker")["rebalance_date"].transform("min")
    elapsed = (phi_df["rebalance_date"] - first).dt.days / 365.25
    return elapsed.clip(lower=0.0)


def signal_break_proximity(
    phi_df: pd.DataFrame, break_df: pd.DataFrame
) -> pd.DataFrame:
    """z_break = (t_current - t*) / sigma(t*).

    sigma(t*) is the dispersion of break points across cohorts *within the same
    category* (frozen definition; 1.0 when only one cohort is available).
    Long runway when z_break < -1 (far from the cliff); fragile when > 0.
    """
    t_stat = break_df.groupby("category")["t_star"]
    t_star_map = break_df.set_index(["category", "phi_bin"])["t_star"]
    sigma_map = t_stat.std().to_dict()

    t_current = ticker_elapsed_years(phi_df)
    # map (category, phi_bin) -> cohort t_star, preserving row order
    lookup_index = phi_df.set_index(["category", "phi_bin"]).index
    t_star = pd.Series(
        np.asarray([t_star_map.get(k, np.nan) for k in lookup_index], dtype=float),
        index=phi_df.index,
    )
    sigma = phi_df["category"].map(sigma_map).fillna(1.0).clip(lower=0.5)

    z_break = (t_current - t_star) / sigma
    out = pd.DataFrame({
        "t_current_years": t_current,
        "t_star": t_star,
        "z_break": z_break,
        # positive = upside runway on the plateau, negative = past the cliff
        "break_proximity_score": (-z_break).clip(-3.0, 3.0),
        "near_break": (z_break > 0.0) & z_break.notna(),
    }, index=phi_df.index)
    return out


# ---------------------------------------------------------------------------
# Signal 2 — fragility divergence
# ---------------------------------------------------------------------------

def signal_fragility_divergence(phi_df: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of Phi within (rebalance_date, category) peers.

    |z_phi| > 2 flags names whose fragility deviates >2 sigma from peers —
    mean-reversion / break candidates. Signal tilt is -z_phi (over-fragile names
    trade toward the peer mean).
    """
    phi_capped = phi_df["phi"].clip(upper=np.nanquantile(phi_df["phi"].dropna(), 0.99))
    grp = phi_capped.groupby([phi_df["rebalance_date"], phi_df["category"]])
    mean = grp.transform("mean")
    std = grp.transform("std").fillna(0.0)
    z_phi = (phi_capped - mean) / std.replace(0.0, np.nan)
    z_phi = z_phi.fillna(0.0).clip(-6.0, 6.0)
    out = pd.DataFrame({
        "z_phi": z_phi,
        "phi_peer_mean": mean,
        "frag_tilt": -z_phi,
        "frag_divergence_flag": z_phi.abs() > 2.0,
    }, index=phi_df.index)
    return out


# ---------------------------------------------------------------------------
# Signal 4 — survival duration (expected remaining survival)
# ---------------------------------------------------------------------------

def signal_survival_duration(
    phi_df: pd.DataFrame, surface_df: pd.DataFrame
) -> pd.DataFrame:
    """Expected remaining survival = integral_{t_current}^{10} S(u) du (trapezoid).

    Dummy cohorts get NaN (unknown survival) and are excluded from ranking.
    Surface S(u) is sampled yearly; trapezoid on yearly nodes.
    """
    surf = surface_df[~surface_df["is_dummy_cohort"]]
    t_current = ticker_elapsed_years(phi_df)

    def _survival_integral(cat, pbin, t0):
        sub = surf[(surf["category"] == cat) & (surf["phi_bin"] == pbin)]
        if len(sub) == 0:
            return np.nan
        s_by_year = {int(r["t_year"]): float(r["S_t"]) for _, r in sub.iterrows()}
        # piecewise-linear survival over integer nodes 0..10 with S(0) = 1.0
        s_nodes = np.array(
            [1.0] + [s_by_year.get(y, np.nan) for y in range(1, MAX_HORIZON_YEARS + 1)]
        )
        if np.isnan(s_nodes).any():
            return np.nan
        t_nodes = np.arange(MAX_HORIZON_YEARS + 1)
        if t0 >= MAX_HORIZON_YEARS:
            return 0.0
        k = int(np.floor(min(max(t0, 0.0), MAX_HORIZON_YEARS - 1)))
        s_at_t0 = s_nodes[k] + (s_nodes[k + 1] - s_nodes[k]) * (t0 - k)
        grid_t = np.concatenate([[t0], np.arange(k + 1, MAX_HORIZON_YEARS + 1)])
        grid_s = np.concatenate([[s_at_t0], s_nodes[k + 1:]])
        return float(np.trapezoid(grid_s, x=grid_t))

    keys = list(zip(phi_df["category"], phi_df["phi_bin"]))
    exp_survival = pd.Series(
        [_survival_integral(cat, pbin, t0) for cat, pbin, t0 in
         zip(phi_df["category"], phi_df["phi_bin"], t_current)],
        index=phi_df.index,
    )
    out = pd.DataFrame({
        "t_current_years": t_current,
        "expected_survival_years": exp_survival,
        "is_dummy_cohort": phi_df["phi_bin"].isna()
        | pd.Series(
            list(zip(phi_df["category"], phi_df["phi_bin"])),
            index=phi_df.index,
        ).isin(surface_df[surface_df["is_dummy_cohort"]]
               .set_index(["category", "phi_bin"]).index),
    }, index=phi_df.index)
    # rank deciles within each rebalance date (survival only, exclude dummy)
    out["survival_rank"] = (
        out["expected_survival_years"]
        .groupby(phi_df["rebalance_date"])
        .rank(pct=True)
    )
    return out


# ---------------------------------------------------------------------------
# Signal 3 — regime-conditioned category rotation (walk-forward)
# ---------------------------------------------------------------------------

def regime_category_rotation(
    phi_df: pd.DataFrame,
    monthly_ret: pd.DataFrame,
    regime_df: pd.DataFrame,
    lookback: int = RANK_LOOKBACK_MONTHS,
) -> pd.DataFrame:
    """Walk-forward favored category per month.

    For each month m, using only history <= m-1, compute per-(category, regime)
    mean/std of next-month category returns and pick the category with the
    highest regime-conditioned Sharpe for the regime active at m.
    Regime states are mapped SHOCK -> CHOPPY (pre-registered).
    """
    regime = regime_df.copy()
    regime["dt"] = pd.to_datetime(regime["timestamp_utc"]).dt.tz_localize(None)
    regime["regime_state"] = regime["regime_state"].replace({"SHOCK": "CHOPPY"})
    reg_month = (
        regime.set_index("dt")["regime_state"]
        .resample("ME").last()
    )
    reg_month.index = reg_month.index.to_period("M")

    cat_map = phi_df.set_index(["ticker", "rebalance_date"])["category"].to_dict()
    # category monthly returns: melt the ticker x month return matrix
    m = monthly_ret.copy()
    m["ticker"] = m.index
    long = m.melt(id_vars="ticker", var_name="ym", value_name="ret")
    long = long.dropna(subset=["ret"])
    long["category"] = long.apply(
        lambda r: _lookup_category(cat_map, r["ticker"], r["ym"]), axis=1
    )
    long = long.dropna(subset=["category"])
    long["regime"] = long["ym"].map(reg_month)
    long = long.dropna(subset=["regime"])

    grid = long.groupby(["ym", "category"]).agg(
        ret=("ret", "mean"), n=("ret", "size")
    ).reset_index()
    grid["regime"] = grid["ym"].map(reg_month)

    rows = []
    for ym in sorted(grid["ym"].unique()):
        cur_regime = reg_month.get(ym)
        if cur_regime is None:
            continue
        hist = grid[grid["ym"] < ym]
        hist = hist[hist["ym"] >= ym - lookback]
        if len(hist) == 0:
            continue
        stats = hist.groupby(["category", "regime"])["ret"].agg(["mean", "std"])
        stats["sharpe"] = stats["mean"] / stats["std"].replace(0.0, np.nan)
        stats["sharpe"] = stats["sharpe"].fillna(0.0)
        if (cur_regime) not in stats.index.get_level_values("regime"):
            continue
        cur = stats.xs(cur_regime, level="regime")
        if len(cur) == 0:
            continue
        favored = cur["sharpe"].idxmax()
        rows.append({"ym": ym, "regime": cur_regime, "rotation_category": favored,
                     "rotation_sharpe": float(cur["sharpe"].max()),
                     "n_categories": int(cur.shape[0])})
    return pd.DataFrame(rows)


def _lookup_category(cat_map, ticker, ym) -> Optional[str]:
    """Category of ticker at month-end ym (closest rebalance <= ym)."""
    best = None
    for (t, rd), cat in cat_map.items():
        if t != ticker:
            continue
        if rd.to_period("M") <= ym:
            if best is None or rd > best[0]:
                best = (rd, cat)
    return best[1] if best else None


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def build_alpha_signals(
    phi_panel: pd.DataFrame,
    surface_df: pd.DataFrame,
    break_df: pd.DataFrame,
    month_ret: pd.DataFrame,
    regime_df: pd.DataFrame,
) -> pd.DataFrame:
    """Combine the four signals into the alpha_signals panel.

    ``phi_panel`` must be a raw phi-panel DataFrame (the caller loads it via
    :func:`load_phi_panel` / :func:`prepare_phi_panel`). Returns a DataFrame
    keyed by (ticker, rebalance_date) with signal columns.
    """
    phi = prepare_phi_panel(phi_panel)
    s1 = signal_break_proximity(phi, break_df)
    s2 = signal_fragility_divergence(phi)
    s4 = signal_survival_duration(phi, surface_df)
    rot = regime_category_rotation(phi, month_ret, regime_df)

    out = pd.concat([
        phi[["ticker", "rebalance_date", "category", "phi_bin", "phi"]],
        s1[["t_current_years", "t_star", "z_break", "break_proximity_score", "near_break"]],
        s2[["z_phi", "frag_tilt", "frag_divergence_flag"]],
        s4[["expected_survival_years", "survival_rank", "is_dummy_cohort"]],
    ], axis=1)

    rot_map = rot.set_index("ym")["rotation_category"]
    ym = out["rebalance_date"].dt.to_period("M")
    out["rotation_category"] = ym.map(rot_map)
    out["rotation_tilt"] = (out["rotation_category"] == out["category"]).astype(float)
    return out


def provenance_json(extra: dict) -> dict:
    return {
        "source": "IN_HOUSE_PHI_PANEL_AND_SURFACE",
        "retrieval_timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "transformations": ["z_break", "z_phi", "expected_survival_integral", "regime_category_rotation"],
        "no_lookahead": "each signal uses only data known at rebalance_date",
        **extra,
    }


if __name__ == "__main__":
    import argparse
    from research.lifecycle_backtest import real_monthly_returns_from_path  # noqa

    p = argparse.ArgumentParser()
    p.add_argument("--phi-panel", default="data/research/lifecycle/phi_panel.parquet")
    p.add_argument("--surface", default="data/research/lifecycle/surface_3d.parquet")
    p.add_argument("--break-points", default="data/research/lifecycle/break_points.parquet")
    p.add_argument("--regime", default="data/regime/regime_daily_nowcast.parquet")
    p.add_argument("--prices", default="data/prices/price_cache.parquet")
    p.add_argument("--output", default="data/research/lifecycle/alpha_signals.parquet")
    args = p.parse_args()
    for f in [args.phi_panel, args.surface, args.break_points, args.regime, args.prices]:
        if not os.path.exists(f):
            raise SystemExit(f"missing input: {f}")
    phi = load_phi_panel(args.phi_panel)
    surf, brk, _ = load_surface(args.surface, args.break_points)
    month_ret = real_monthly_returns(pd.read_parquet(args.prices))
    regime = pd.read_parquet(args.regime)
    out = build_alpha_signals(phi, surf, brk, month_ret, regime)
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    out.to_parquet(args.output, index=False)
    print(f"Wrote {args.output} ({len(out)} rows)")