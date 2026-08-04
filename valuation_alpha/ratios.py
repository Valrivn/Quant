"""Lifecycle metric computation and cross-sectional peer statistics."""

import numpy as np
import pandas as pd

from Quantitative.stochastic.markov_lifecycle import LifecycleMetrics

_METRIC_KEYS = [
    "reinvestment_rate",
    "roic",
    "revenue_growth",
    "margin_variance_10y",
    "operating_margin",
    "debt_to_capital",
    "interest_coverage_ratio",
    "cash_burn_months",
]


def _last_valid(series: pd.Series):
    vals = series.dropna()
    if vals.empty:
        return np.nan
    return vals.iloc[-1]


def _trailing4q(quarterly: pd.DataFrame, col: str) -> float:
    """Sum of the last up-to-4 non-NaN values of a quarterly column."""
    if col not in quarterly.columns:
        return np.nan
    s = quarterly[col].dropna()
    if len(s) < 2:
        return np.nan
    return float(s.tail(min(4, len(s))).sum())


def _metric_value(metrics, key):
    if isinstance(metrics, dict):
        return metrics.get(key)
    return getattr(metrics, key, None)


def compute_lifecycle_metrics(quarterly: pd.DataFrame) -> LifecycleMetrics:
    """Compute LifecycleMetrics from a quarterly financials DataFrame.

    Uses last available non-NaN values for point metrics. revenue_growth is the
    trailing 4-quarter revenue vs the prior 4 quarters pct change.
    margin_variance_10y is the std of operating_margin over all quarters,
    annualized via sqrt(4). NaN-safe on short history.
    """
    if quarterly is None or quarterly.empty:
        return LifecycleMetrics(
            reinvestment_rate=np.nan,
            roic=np.nan,
            revenue_growth=np.nan,
            margin_variance_10y=np.nan,
            operating_margin=np.nan,
            debt_to_capital=np.nan,
            interest_coverage_ratio=np.nan,
            cash_burn_months=np.nan,
        )

    def _val(col):
        if col in quarterly.columns:
            return _last_valid(quarterly[col])
        return np.nan

    revenue_growth = np.nan
    if "revenue" in quarterly.columns:
        rev = quarterly["revenue"].dropna()
        if len(rev) >= 8:
            last4 = rev.iloc[-4:].sum()
            prev4 = rev.iloc[-8:-4].sum()
            if prev4 != 0:
                revenue_growth = last4 / prev4 - 1.0
        elif len(rev) >= 2:
            if rev.iloc[-2] != 0:
                revenue_growth = rev.iloc[-1] / rev.iloc[-2] - 1.0

    margin_variance_10y = np.nan
    if "operating_margin" in quarterly.columns:
        om = quarterly["operating_margin"].dropna()
        if len(om) >= 2:
            margin_variance_10y = om.std(ddof=1) * np.sqrt(4)

    debt_to_capital = np.nan
    if "debt" in quarterly.columns and "equity" in quarterly.columns:
        debt = _last_valid(quarterly["debt"])
        equity = _last_valid(quarterly["equity"])
        if debt == debt and equity == equity and (debt + equity) != 0:
            debt_to_capital = debt / (debt + equity)
    if debt_to_capital != debt_to_capital and "debt_to_capital" in quarterly.columns:
        debt_to_capital = _last_valid(quarterly["debt_to_capital"])

    # Reinvestment rate (D-20260802-002): trailing-4Q (capex + R&D) / OCF.
    # Profit-agnostic by design — negative OCF (cash burn while reinvesting)
    # yields a negative rate, which is the gamble profile, not an error.
    reinvestment_rate = np.nan
    if "capex" in quarterly.columns and "ocf" in quarterly.columns:
        capex4 = _trailing4q(quarterly, "capex")
        ocf4 = _trailing4q(quarterly, "ocf")
        rd4 = _trailing4q(quarterly, "rd") if "rd" in quarterly.columns else 0.0
        if ocf4 == ocf4 and abs(ocf4) > 1e-9:
            capex_mag = abs(capex4) if capex4 == capex4 else 0.0
            reinvestment_rate = (capex_mag + (rd4 if rd4 == rd4 else 0.0)) / ocf4
    if reinvestment_rate != reinvestment_rate and "reinvestment_rate" in quarterly.columns:
        reinvestment_rate = _last_valid(quarterly["reinvestment_rate"])
    if reinvestment_rate != reinvestment_rate and "reinvestment" in quarterly.columns:
        reinvestment_rate = _last_valid(quarterly["reinvestment"])

    return LifecycleMetrics(
        reinvestment_rate=reinvestment_rate,
        roic=_val("roic"),
        revenue_growth=revenue_growth,
        margin_variance_10y=margin_variance_10y,
        operating_margin=_val("operating_margin"),
        debt_to_capital=debt_to_capital,
        interest_coverage_ratio=_val("interest_coverage"),
        cash_burn_months=_val("cash_burn"),
    )


def _percentile(val, vals):
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return np.nan
    valid = [v for v in vals if v is not None and not (isinstance(v, float) and np.isnan(v))]
    if not valid:
        return np.nan
    n = len(valid)
    if n == 1:
        return 50.0
    below = sum(1 for v in valid if v < val)
    return below / (n - 1) * 100.0


def peer_percentiles(
    metrics_by_ticker: dict,
    sector_by_ticker: dict,
    metric_keys: list,
    lookback_years: int = 5,
) -> pd.DataFrame:
    """Cross-sectional percentile rank (0-100) of each metric within sector.

    Returns a DataFrame indexed by ticker with one column per metric key
    suffixed "_pct".
    """
    rows = {}
    for ticker, metrics in metrics_by_ticker.items():
        sector = sector_by_ticker.get(ticker)
        peers = [
            p for p in metrics_by_ticker if sector_by_ticker.get(p) == sector
        ]
        row = {}
        for key in metric_keys:
            val = _metric_value(metrics, key)
            vals = [_metric_value(metrics_by_ticker[p], key) for p in peers]
            row[f"{key}_pct"] = _percentile(val, vals)
        rows[ticker] = row
    return pd.DataFrame(rows).T


def mahalanobis_state(metrics_by_ticker: dict, metric_keys: list) -> pd.DataFrame:
    """Mahalanobis distance of each ticker from the cross-sectional centroid.

    Metrics are z-scored cross-sectionally; distance uses the covariance of the
    standardized values. Falls back to Euclidean distance on singular
    covariance. Returns an empty DataFrame on failure.
    """
    try:
        if not metrics_by_ticker or not metric_keys:
            return pd.DataFrame(columns=["mahalanobis"])
        tickers = list(metrics_by_ticker.keys())
        X = np.full((len(tickers), len(metric_keys)), np.nan)
        for i, t in enumerate(tickers):
            m = metrics_by_ticker[t]
            for j, key in enumerate(metric_keys):
                v = _metric_value(m, key)
                X[i, j] = v if v is not None else np.nan
        means = np.nanmean(X, axis=0)
        stds = np.nanstd(X, axis=0)
        Z = np.where(stds > 0, (X - means) / np.where(stds > 0, stds, 1.0), 0.0)
        Z = np.nan_to_num(Z, nan=0.0)
        if len(tickers) < 2:
            dist = np.zeros(len(tickers))
            return pd.DataFrame({"mahalanobis": dist}, index=tickers)
        S = np.cov(Z, rowvar=False)
        if S.ndim == 0:
            S = S.reshape(1, 1)
        if np.linalg.matrix_rank(S) < len(metric_keys):
            dist = np.sqrt(np.sum(Z ** 2, axis=1))
        else:
            Sinv = np.linalg.pinv(S)
            dist = np.sqrt(np.einsum("ij,jk,ik->i", Z, Sinv, Z))
        return pd.DataFrame({"mahalanobis": dist}, index=tickers)
    except Exception:
        return pd.DataFrame(columns=["mahalanobis"])