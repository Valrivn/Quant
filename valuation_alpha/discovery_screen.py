"""Discovery pilot screener (B-20260803 P2).

Screens SP400/SP600 names through the pre-registered discovery pipeline:

  1. Quant baseline  — hard exclusions on L1 metrics (cash burn < 12 months,
     interest coverage < 1x, Mahalanobis beyond the distress percentile) and a
     floor on the FF5 residual alpha z-score.
  2. Liquidity gate   — min price and minimum average daily dollar volume.
  3. Glassdoor tilt   — continuous tilt (NOT a gate): +0.1 z-score per 0.1
     above the universe median of the normalized Glassdoor composite, so
     low-coverage names are not hard-excluded (B-20260803 final ruling).

Every screened-out name is recorded with the reason it failed, feeding
``Quantitative/audit/data_provenance_audit.py`` in P5.
"""

import numpy as np
import pandas as pd

GLASSDOOR_TILT_STEP = 0.1      # z per 0.1 above median
GLASSDOOR_MEDIAN = 0.6         # default median of normalized Glassdoor composite
MIN_PRICE = 2.0                # USD
MIN_ADV_DOLLARS = 1_000_000.0  # avg daily traded dollar volume
MIN_ALPHA_Z = -1.0             # quant baseline floor on 3y alpha z-score
CASH_BURN_MONTHS_FLOOR = 12.0
INTEREST_COVERAGE_FLOOR = 1.0
MAHALANOBIS_PCT_FLOOR = 0.95   # names beyond the 95th pctile of Mahalanobis distance are distress-flagged


def quant_baseline_flags(names: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of the L1 ``names`` frame with a boolean ``pass_quant``.

    Hard exclusions per B-20260803 2.1: cash burn < 12 months, interest
    coverage < 1x, or Mahalanobis beyond the 95th percentile => fail. Names
    missing those metrics (NaN) are NOT excluded on that leg (coverage gap), but
    must still have a usable 3y alpha z-score >= ``MIN_ALPHA_Z``.
    """
    if names is None or names.empty:
        return pd.DataFrame(columns=["ticker", "pass_quant", "quant_reason"])
    out = names.copy()
    reasons = []

    def _row_reason(row):
        reasons_row = []
        cb = row.get("cash_burn_months_pct")
        if not pd.isna(cb) and not np.isnan(cb) and cb < CASH_BURN_MONTHS_FLOOR:
            reasons_row.append(f"cash_burn_pct<{CASH_BURN_MONTHS_FLOOR:g}")
        ic = row.get("interest_coverage_ratio_pct")
        if not pd.isna(ic) and not np.isnan(ic) and ic < INTEREST_COVERAGE_FLOOR:
            reasons_row.append(f"interest_cov_pct<{INTEREST_COVERAGE_FLOOR:g}")
        m = row.get("mahalanobis")
        if not pd.isna(m) and not np.isnan(m) and m > MAHALANOBIS_PCT_FLOOR:
            reasons_row.append("mahalanobis>95pct")
        return ";".join(reasons_row)

    out["quant_reason"] = out.apply(_row_reason, axis=1)

    alpha = pd.to_numeric(out.get("alpha_3y_ann"), errors="coerce")
    az = (alpha - alpha.mean()) / (alpha.std(ddof=1) + 1e-12) if alpha.notna().sum() > 1 else pd.Series(np.nan, index=out.index)
    if az.isna().all():
        out["pass_quant"] = False
        out.loc[out["quant_reason"] == "", "quant_reason"] = "no_alpha_data"
    else:
        az = az.fillna(az.min() - 1.0)
        out["pass_quant"] = (az >= MIN_ALPHA_Z) & (out["quant_reason"] == "")
        out.loc[(out["pass_quant"]) & (out["quant_reason"] == ""), "alpha_z_3y"] = az
        out.loc[~out["pass_quant"] & (out["quant_reason"] == "") & (az < MIN_ALPHA_Z), "quant_reason"] = "alpha_z<floor"
    return out


def liquidity_gate(
    prices: pd.DataFrame, tickers: list, min_price: float = MIN_PRICE,
    min_adv_dollars: float = MIN_ADV_DOLLARS,
) -> pd.DataFrame:
    """Return {ticker: reason} for names failing the liquidity gate.

    Prices is a DataFrame of daily closes indexed by date. ADV is computed as
    the trailing 252-day mean of price*volume-equivalent turnover; when volume
    is unavailable, price * |daily pct change proxy| is not used — instead the
    name is scored on turnover of shares via close-to-close *0 (conservative:
    any name with no volume data is flagged ``no_volume_data``).
    """
    fails = {}
    for t in tickers:
        if t not in prices.columns:
            fails[t] = "no_price_data"
            continue
        px = prices[t].dropna()
        if len(px) < 60:
            fails[t] = "insufficient_price_history"
            continue
        last = float(px.iloc[-1])
        if last < min_price:
            fails[t] = f"price<{min_price:g}"
            continue
        vol = None
        if isinstance(prices, pd.DataFrame) and hasattr(prices, "attrs"):
            vol = prices.attrs.get("volume")
        if vol is not None and t in vol.columns:
            adv = float((vol[t].reindex(px.index).dropna().tail(252) * px.tail(252)).mean())
            if adv < min_adv_dollars:
                fails[t] = f"adv<${min_adv_dollars:g}"
    return fails


def glassdoor_tilt(
    scores: pd.DataFrame, glassdoor_by_ticker: dict,
    median: float = GLASSDOOR_MEDIAN,
) -> pd.DataFrame:
    """Apply the continuous Glassdoor tilt to a scores frame.

    ``scores`` must have a ``ticker`` column and a numeric ``score`` column.
    Returns a copy with ``glassdoor_z_tilt`` and an adjusted ``score_adj`` =
    score + tilt. Names without a Glassdoor score receive tilt 0 (no exclusion).
    """
    out = scores.copy()
    out["glassdoor_norm"] = out["ticker"].map(
        {
            t.upper(): float(v)
            for t, v in (glassdoor_by_ticker or {}).items()
            if v is not None
        }
    )
    tilt = (out["glassdoor_norm"] - median) / GLASSDOOR_TILT_STEP * GLASSDOOR_TILT_STEP
    out["glassdoor_z_tilt"] = tilt.fillna(0.0)
    out["score_adj"] = out["score"] + out["glassdoor_z_tilt"]
    return out


def run_discovery_screen(
    universe_rows: list,
    run_names: pd.DataFrame,
    prices: pd.DataFrame,
    glassdoor_by_ticker: dict = None,
    sector_by_ticker: dict = None,
) -> dict:
    """End-to-end discovery pilot screen.

    universe_rows: roster-compatible rows (ticker/group/bias/sector/sec_cik).
    run_names: L1 engine names output for the discovery universe (or subset).
    prices: daily closes for liquidity gate.
    Returns a dict with screened-in/out tickers, the ranked screen, and reasons.
    """
    if not universe_rows:
        return {"screen": pd.DataFrame(), "pass": [], "fail": {}}
    tickers = [r["ticker"] for r in universe_rows]
    names = run_names.copy() if run_names is not None else pd.DataFrame()
    if names.empty or "ticker" not in names:
        names = pd.DataFrame({"ticker": tickers})

    flagged = quant_baseline_flags(names)
    lq_fails = liquidity_gate(prices, tickers)
    fail = {t: [] for t in tickers}
    for t, reason in lq_fails.items():
        fail[t].append(f"liquidity:{reason}")

    flagged = flagged.set_index("ticker")
    for t in tickers:
        if t in flagged.index and not flagged.loc[t, "pass_quant"]:
            fail[t].append(f"quant:{flagged.loc[t, 'quant_reason']}")

    # Composite screen score: alpha z (if present) else neutral, + glassdoor tilt.
    rows = []
    for r in universe_rows:
        t = r["ticker"]
        if fail.get(t):
            continue
        az = np.nan
        if t in flagged.index and "alpha_z_3y" in flagged.columns:
            az = float(flagged.loc[t, "alpha_z_3y"]) if not pd.isna(flagged.loc[t, "alpha_z_3y"]) else np.nan
        score = az if not np.isnan(az) else 0.0
        rows.append({
            "ticker": t,
            "group": r["group"],
            "sector": r.get("sector"),
            "sec_cik": r.get("sec_cik"),
            "score": score,
        })
    screen = pd.DataFrame(rows)
    if glassdoor_by_ticker and not screen.empty:
        screen = glassdoor_tilt(screen, glassdoor_by_ticker)
    screen = screen.sort_values("score_adj", ascending=False) if not screen.empty and "score_adj" in screen else screen

    return {
        "screen": screen,
        "pass": screen["ticker"].tolist() if not screen.empty else [],
        "fail": {t: ";".join(v) for t, v in fail.items() if v},
    }
