"""Phase 4 — cross-sectional survival-duration backtest for the lifecycle sieve.

Pre-registered design (lifecycle_sieve_phi_surface_plan_20261010.md §6 D4.2):
  * Universe: phi_panel membership at each month-end rebalance date.
  * Selection: top decile by expected remaining survival (Signal 4).
  * Weights: proportional to 1/Phi (fragility-aware: more fragile -> less weight).
  * Overlay: regime-conditioned category rotation re-weights favored category x1.5.
  * Fill:   weights formed at month-end m, returns earned in month m+1 (T+1).
  * Costs:  53 bps/yr applied on one-sided turnover (measured house figure).
  * Benchmarks: SPY (master_prices), Arm A (data/backtests/arm_a), Arm V skipped
    (only has a monthly screen panel, no daily returns).
"""

from __future__ import annotations

import os
import json
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

MONTHS_PER_YEAR = 12
TOP_DECILE = 0.90
ROTATION_OVERLAY = 0.5
COST_BPS_PER_YEAR = 53.0


# ---------------------------------------------------------------------------
# Return inputs
# ---------------------------------------------------------------------------

def real_monthly_returns_from_path(prices_path: str) -> pd.DataFrame:
    px = pd.read_parquet(prices_path)[["ticker", "date", "close", "source"]]
    px = px[px["source"] != "synthetic_fallback"]
    px["date"] = pd.to_datetime(px["date"])
    px = px.dropna(subset=["close"])
    px["ym"] = px["date"].dt.to_period("M")
    month_close = px.groupby(["ticker", "ym"])["close"].last().unstack()
    return month_close.pct_change(axis=1, fill_method=None)


def spy_monthly_returns(spy_path: str) -> pd.DataFrame:
    spy = pd.read_parquet(spy_path)
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
    return m.pct_change(fill_method=None).dropna()


def arm_daily_to_monthly(ret_path: str) -> pd.Series:
    df = pd.read_parquet(ret_path)
    df["date"] = pd.to_datetime(df["date"])
    df["ym"] = df["date"].dt.to_period("M")
    return df.assign(x=(1.0 + df["ret"])).groupby("ym")["x"].prod() - 1.0


# ---------------------------------------------------------------------------
# Portfolio construction
# ---------------------------------------------------------------------------

def build_monthly_portfolio(
    signals: pd.DataFrame, weight_fun: Optional[str] = "1phi"
) -> pd.DataFrame:
    """Return weights DataFrame index=ym, columns=ticker (realized month m).

    Each row of ``signals`` is a (ticker, rebalance_date=month-end) signal;
    the portfolio weights decided there apply to the *next* month's return.
    """
    sig = signals.copy()
    sig["ym"] = sig["rebalance_date"].dt.to_period("M")
    sig["ret_ym"] = sig["ym"] + 1  # T+1 fill month

    good = (
        sig["expected_survival_years"].notna()
        & ~sig["is_dummy_cohort"].fillna(True)
        & (sig["phi"] > 0.001)
    )
    sig = sig[good].copy()

    weights = {}
    records = []
    for ym, grp in sig.groupby("ym"):
        n = len(grp)
        if n == 0:
            records.append({"ret_ym": ym + 1, "ticker": np.nan, "w": 0.0, "n": 0})
            continue
        ntop = max(int(np.ceil(n * (1 - TOP_DECILE))), 1)
        sel = grp.nlargest(ntop, "expected_survival_years")
        if weight_fun == "1phi":
            w = 1.0 / sel["phi"].clip(lower=0.001).values
        else:
            w = np.ones(len(sel))
        if ROTATION_OVERLAY > 0:
            tilt = sel["rotation_tilt"].fillna(0.0).clip(0.0, 1.0)
            w = w * (1.0 + ROTATION_OVERLAY * tilt)
        w = w / w.sum()
        for (t, wi) in zip(sel["ticker"], w):
            records.append({"ret_ym": ym + 1, "ticker": t, "w": float(wi), "n": n})
    wdf = pd.DataFrame(records)
    if len(wdf) == 0:
        return pd.DataFrame(index=pd.PeriodIndex([], freq="M"))
    pivot = wdf.pivot_table(index="ret_ym", columns="ticker", values="w", aggfunc="sum")
    pivot = pivot.fillna(0.0)
    pivot.index.name = "ym"
    return pivot


def portfolio_monthly_returns(
    weights: pd.DataFrame, month_ret: pd.DataFrame
) -> pd.Series:
    """Portfolio return for each ym in weights.index (returns earned in ym)."""
    rets = []
    for ym in weights.index:
        tickers = weights.columns[weights.loc[ym] > 0]
        w = weights.loc[ym, tickers]
        r = month_ret.loc[
            [t for t in tickers if t in month_ret.index], ym
        ] if ym in month_ret.columns else pd.Series(dtype=float)
        keep = w.index.intersection(r.index)
        if len(keep) == 0:
            rets.append((ym, np.nan))
        else:
            ww = w[keep] / w[keep].sum()
            rets.append((ym, float((ww * r[keep]).sum())))
    s = pd.Series(dict(rets), dtype=float)
    s.index.name = "ym"
    return s


def apply_costs(net_ret: pd.Series, weights: pd.DataFrame) -> pd.Series:
    """53 bps/yr applied on one-sided turnover each rebalance."""
    prev = None
    out = []
    turnover = 0.0
    for ym in weights.index:
        cur = weights.loc[ym]
        if prev is None:
            to = cur.abs().sum()
        else:
            to = (cur - prev).abs().sum() / 2.0
        turnover = to
        cost = COST_BPS_PER_YEAR / 1e4 * to / MONTHS_PER_YEAR
        r = net_ret.get(ym, np.nan)
        out.append((ym, r - cost if pd.notna(r) else np.nan))
        prev = cur
    s = pd.Series(dict(out), dtype=float)
    s.index.name = "ym"
    return s, turnover


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def perf_metrics(monthly_ret: pd.Series, rf_annual: float = 0.0) -> dict:
    r = monthly_ret.dropna()
    if len(r) < 3:
        return {"n_months": int(len(r))}
    rf_m = rf_annual / MONTHS_PER_YEAR
    ex = r - rf_m
    ann_ret = (1.0 + r).prod() ** (MONTHS_PER_YEAR / len(r)) - 1.0
    ann_vol = ex.std(ddof=1) * np.sqrt(MONTHS_PER_YEAR)
    sharpe = (ex.mean() / ex.std(ddof=1)) * np.sqrt(MONTHS_PER_YEAR) if ex.std(ddof=1) > 0 else np.nan
    downside = ex[ex < 0]
    sortino = (ex.mean() / downside.std(ddof=1)) * np.sqrt(MONTHS_PER_YEAR) if len(downside) > 1 and downside.std(ddof=1) > 0 else np.nan
    cum = (1.0 + r).cumprod()
    peak = cum.cummax()
    dd = cum / peak - 1.0
    max_dd = float(dd.min())
    calmar = ann_ret / abs(max_dd) if max_dd < 0 else np.nan
    return {
        "n_months": int(len(r)),
        "annual_return": float(ann_ret),
        "annual_vol": float(ann_vol),
        "sharpe": float(sharpe) if pd.notna(sharpe) else None,
        "sortino": float(sortino) if pd.notna(sortino) else None,
        "max_drawdown": float(max_dd),
        "calmar": float(calmar) if pd.notna(calmar) else None,
        "total_return": float(cum.iloc[-1] - 1.0),
    }


def align_series(s: pd.Series, other: pd.Series) -> pd.DataFrame:
    df = pd.concat([s.rename("s"), other.rename("o")], axis=1).dropna()
    return df


# ---------------------------------------------------------------------------
# Attribution (per-signal contribution, pre-registered, no re-tuning)
# ---------------------------------------------------------------------------

def signal_attribution(
    signals: pd.DataFrame, month_ret: pd.DataFrame
) -> dict:
    """Per-signal checks, all monthly-frequency, forward (month m+1)."""
    sig = signals.copy()
    sig["ym"] = sig["rebalance_date"].dt.to_period("M")
    sig["ret_ym"] = sig["ym"] + 1
    sig["fwd_ret"] = [
        month_ret.at[t, ym] if t in month_ret.index and ym in month_ret.columns
        else np.nan for t, ym in zip(sig["ticker"], sig["ret_ym"])
    ]
    out = {}

    # Signal 4: survival decile spread (top vs bottom decile forward returns)
    d = sig.dropna(subset=["expected_survival_years", "fwd_ret"])
    top = d[d["survival_rank"] >= TOP_DECILE]["fwd_ret"]
    bot = d[d["survival_rank"] <= (1 - TOP_DECILE)]["fwd_ret"]
    out["survival_top_decile"] = {
        "mean_next_month": float(top.mean()) if len(top) else None,
        "n": int(len(top)),
    }
    out["survival_bottom_decile"] = {
        "mean_next_month": float(bot.mean()) if len(bot) else None,
        "n": int(len(bot)),
    }
    if len(top) and len(bot):
        out["survival_decile_spread"] = float(top.mean() - bot.mean())

    # Signal 1: break proximity (runway names z_break < -1 vs past-cliff > 0)
    p = sig.dropna(subset=["z_break", "fwd_ret"])
    far = p[p["z_break"] < -1]["fwd_ret"]
    past = p[p["z_break"] > 1]["fwd_ret"]
    out["break_far_from_cliff"] = {"mean_next_month": float(far.mean()) if len(far) else None, "n": int(len(far))}
    out["break_past_cliff"] = {"mean_next_month": float(past.mean()) if len(past) else None, "n": int(len(past))}

    # Signal 2: fragility divergence convergence (|z_phi| > 2)
    f = sig.dropna(subset=["fwd_ret"])
    flagged = f[f["frag_divergence_flag"] == True]["fwd_ret"]  # noqa: E712
    out["frag_divergence_flagged"] = {"mean_next_month": float(flagged.mean()) if len(flagged) else None, "n": int(len(flagged))}
    out["frag_divergence_all"] = {"mean_next_month": float(f["fwd_ret"].mean()) if len(f) else None, "n": int(len(f))}

    # Pre-registered falsification #6: pooled cross-sectional regression t-stats
    # (next-month return on the signal; bivariate OLS, conservative pooled LOS).
    def _pooled_tstat(x: pd.Series, y: pd.Series) -> float:
        d = pd.concat([x.rename("x"), y.rename("y")], axis=1).dropna()
        if len(d) < 30 or d["x"].std(ddof=1) == 0:
            return float("nan")
        x0, y0 = d["x"].values, d["y"].values
        b = np.cov(x0, y0, ddof=1)[0, 1] / np.var(x0, ddof=1)
        resid = y0 - (b * x0 + (y0.mean() - b * x0.mean()))
        se = np.sqrt((resid ** 2).sum() / (len(d) - 2) / ((x0 - x0.mean()) ** 2).sum())
        return float(b / se) if se > 0 else float("nan")

    rr = sig.dropna(subset=["fwd_ret", "z_phi"])
    out["fragility_divergence_tstat"] = _pooled_tstat(rr["z_phi"], rr["fwd_ret"])
    ss = sig.dropna(subset=["fwd_ret", "expected_survival_years"])
    out["survival_duration_tstat"] = _pooled_tstat(ss["expected_survival_years"], ss["fwd_ret"])
    bb = sig.dropna(subset=["fwd_ret", "break_proximity_score"])
    out["break_proximity_tstat"] = _pooled_tstat(bb["break_proximity_score"], bb["fwd_ret"])

    # Signal 3: rotation overlay — favored category vs unfavored within survival top half
    r = sig.dropna(subset=["rotation_category", "fwd_ret"])
    keep = r[r["rotation_tilt"] == 1]["fwd_ret"]
    rest = r[r["rotation_tilt"] == 0]["fwd_ret"]
    out["rotation_favored"] = {"mean_next_month": float(keep.mean()) if len(keep) else None, "n": int(len(keep))}
    out["rotation_unfavored"] = {"mean_next_month": float(rest.mean()) if len(rest) else None, "n": int(len(rest))}
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_full_backtest(
    signals: pd.DataFrame,
    month_ret: pd.DataFrame,
    spy_ret: pd.Series,
    arm_a_ret: pd.Series,
) -> dict:
    weights = build_monthly_portfolio(signals)
    net = portfolio_monthly_returns(weights, month_ret)
    gross, last_turnover = apply_costs(net, weights)

    # aligned-window benchmarks: same months as the portfolio
    lo, hi = net.index.min(), net.index.max()
    spy_win = spy_ret[(spy_ret.index >= lo) & (spy_ret.index <= hi)]
    arm_win = arm_a_ret[(arm_a_ret.index >= lo) & (arm_a_ret.index <= hi)]

    # DIAGNOSTIC variants (not tradable claims; they locate the source of the edge):
    #  (a) equal-weight top decile  -> isolates the 1/Phi weighting
    #  (b) equal-weight full cross-section -> baseline breadth
    ew_top = portfolio_monthly_returns(
        build_monthly_portfolio(signals, weight_fun=None), month_ret)
    full_ew = _full_universe_monthly_returns(signals, month_ret)

    results = {
        "arm_L_lifecycle": perf_metrics(net),
        "arm_L_lifecycle_after_costs": perf_metrics(gross),
        "spy_benchmark": perf_metrics(spy_ret),
        "arm_a_benchmark": perf_metrics(arm_a_ret),
        "spy_benchmark_aligned_window": perf_metrics(spy_win),
        "arm_a_benchmark_aligned_window": perf_metrics(arm_win),
        "diagnostic_equal_weight_top_decile": perf_metrics(ew_top),
        "diagnostic_equal_weight_full_universe": perf_metrics(full_ew),
        "avg_one_sided_turnover_per_month": float(
            weights.diff().abs().sum(axis=1).mean() / 2.0
        ) if len(weights) else None,
        "cost_model": {"bps_per_year": COST_BPS_PER_YEAR, "applied_on_one_sided_turnover": True},
        "arm_v_benchmark": {
            "skipped": True,
            "reason": "v1_intrinsic has a monthly screen panel only (no daily returns)",
        },
        "weights": weights,
    }
    return results


def _full_universe_monthly_returns(
    signals: pd.DataFrame, month_ret: pd.DataFrame
) -> pd.Series:
    """Equal-weight return of every name with a signal and a real return."""
    sig = signals.copy()
    sig["ym"] = sig["rebalance_date"].dt.to_period("M")
    sig["ret_ym"] = sig["ym"] + 1
    rows = []
    for (ym, t) in zip(sig["ret_ym"], sig["ticker"]):
        if t in month_ret.index and ym in month_ret.columns:
            v = month_ret.at[t, ym]
            if np.isfinite(v):
                rows.append((ym, v))
    s = pd.Series(dict(rows), dtype=float)
    s.index.name = "ym"
    return s


def write_attribution_md(
    attr: dict, results: dict, out_path: str, extra: Optional[dict] = None
) -> None:
    lines = [
        "# Lifecycle Sieve — Per-Signal Attribution (Phase 4)",
        "",
        "Forward monthly-frequency checks. All statistics are computed on the "
        "month *after* the signal (T+1 fill). No lookahead.",
        "",
    ]
    for k, v in attr.items():
        if isinstance(v, dict):
            lines.append(f"- **{k}**: {v}")
        else:
            lines.append(f"- **{k}**: {v:.4f}" if isinstance(v, float) else f"- **{k}**: {v}")
    lines += ["", "## Benchmarks (pre-cost)", ""]
    for name, m in results.items():
        if name in ("weights", "arm_v_benchmark", "cost_model",
                    "avg_one_sided_turnover_per_month"):
            continue
        if not isinstance(m, dict):
            continue
        lines.append(f"- **{name}**: Sharpe {m.get('sharpe')}, AnnRet "
                     f"{m.get('annual_return')}, MaxDD {m.get('max_drawdown')}, "
                     f"Calmar {m.get('calmar')}")
    if extra:
        lines += ["", "## Notes", ""]
        for k, v in extra.items():
            lines.append(f"- {k}: {v}")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--signals", default="data/research/lifecycle/alpha_signals.parquet")
    p.add_argument("--prices", default="data/prices/price_cache.parquet")
    p.add_argument("--spy", default="data/master_prices/SPY.parquet")
    p.add_argument("--arm-a", default="data/backtests/arm_a/returns.parquet")
    p.add_argument("--output", default="data/research/lifecycle/backtest_results.json")
    args = p.parse_args()
    sig = pd.read_parquet(args.signals)
    mret = real_monthly_returns_from_path(args.prices)
    spy = spy_monthly_returns(args.spy)
    arm_a = arm_daily_to_monthly(args.arm_a)
    res = run_full_backtest(sig, mret, spy, arm_a)
    metrics = {k: v for k, v in res.items() if k not in ("weights",)}
    with open(args.output, "w") as f:
        json.dump(metrics, f, indent=2, default=str)
    print(f"Wrote {args.output}")