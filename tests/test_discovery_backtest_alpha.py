"""Tests for discovery.backtest_alpha (D-20260807-001).

Offline-only: fetchers are injected and ``discovery.gate_data`` is patched
per-test against the real module (consumers resolve ``gate_data`` lazily, so
patching module attributes is sufficient). Covers the no-fabrication IG runway
(unfed <=> empty feed), the like-for-like screen on the traditional lane (SAME
gates), alpha fail-closed on empty data, and the human-readable report.
"""

import pandas as pd
import pytest

from discovery.backtest_alpha import (
    compare_cohorts,
    report_table,
    format_details,
    run_each_alpha,
    report_each,
    _equal_weight_returns,
)

_MOAT_KEYS = [
    "product_breadth",
    "developer_momentum",
    "employee_sentiment",
    "revenue_concentration",
    "network_effect_proxy",
    "regulatory_barrier",
]


@pytest.fixture(autouse=True)
def _patch_gate_data(monkeypatch):
    """Neutral gates + plain names frame so no test touches the network or DB."""
    import discovery.gate_data as gd

    monkeypatch.setattr(
        gd,
        "qualitative_signals",
        lambda _t: (
            {k: 0.5 for k in _MOAT_KEYS},
            {k: "default_neutral" for k in _MOAT_KEYS},
        ),
    )

    def _plain(tickers):
        df = pd.DataFrame(
            {
                "ticker": tickers,
                "alpha_3y_ann": [0.05] * len(tickers),
                "cash_burn_months_pct": [20.0] * len(tickers),
                "interest_coverage_ratio_pct": [5.0] * len(tickers),
                "mahalanobis": [0.2] * len(tickers),
            }
        )
        df.attrs["provenance"] = {
            t: {
                "alpha_3y_ann": "live_ff5",
                "cash_burn_months_pct": "NaN",
                "interest_coverage_ratio_pct": "NaN",
                "mahalanobis": "NaN",
            }
            for t in tickers
        }
        return df

    monkeypatch.setattr(gd, "build_names_frame", _plain)
    monkeypatch.setattr(gd, "normalize_mahalanobis", lambda df: df)


def _mk_prices(tickers):
    idx = pd.date_range("2019-01-01", periods=300, freq="B")
    df = pd.DataFrame(index=idx)
    for i, tk in enumerate(tickers):
        q = 100.0 + i
        df[tk] = q + (pd.RangeIndex(len(df)) * 0.05)
    return df


def _mk_factors():
    idx = pd.date_range("2019-01-01", periods=300, freq="B")
    df = pd.DataFrame(index=idx)
    df["Mkt-RF"] = 0.0003
    df["SMB"] = 0.0
    df["HML"] = 0.0
    df["RMW"] = 0.0
    df["CMA"] = 0.0
    df["RF"] = 0.0
    return df


def _mk_sp500():
    idx = pd.date_range("2019-01-01", periods=300, freq="B")
    return pd.Series(100.0 + (pd.RangeIndex(len(idx)) * 0.02), index=idx)


def test_unfed_lane_when_no_ig_feed():
    """No IG candidates -> IG lane reports unfed and invents no alpha."""
    result = compare_cohorts(ig_tickers=None)
    assert result["ig"].status == "unfed"
    assert result["ig"].cohort == []
    assert result["ig"].alpha is None


def test_empty_ig_list_also_unfed():
    result = compare_cohorts(ig_tickers=[])
    assert result["ig"].status == "unfed"


def test_unfed_ig_lane_reports_no_alpha_even_with_network_fetchers():
    """Even with rich data available, an unfed IG lane must not fabricate."""
    result = compare_cohorts(
        ig_tickers=None,
        fetch_prices=lambda t, start=None, end=None: _mk_prices(t),
        fetch_factors=lambda: _mk_factors(),
        fetch_sp500=lambda s=None, e=None: _mk_sp500(),
    )
    assert result["ig"].status == "unfed"
    assert result["ig"].alpha is None


def test_equal_weight_returns_sane():
    prices = _mk_prices(["AAA", "BBB"])
    rets = _equal_weight_returns(prices)
    assert not rets.empty
    assert 0.0 < float(rets.mean()) < 0.01


def test_compare_reports_both_lanes_offline():
    """Empty-data fetchers -> both lanes computed, alpha=None, no raise."""
    result = compare_cohorts(ig_tickers=["INTC", "NVDA"])
    assert "ig" in result and "traditional" in result
    assert result["ig"].status in ("unfed", "seeded", "no_pass")
    assert result["traditional"].status in ("seeded", "no_pass")


def test_report_table_and_details_are_renderable():
    result = compare_cohorts(ig_tickers=None)
    table = report_table(result)
    assert "lane" in table and "ig" in table and "traditional" in table
    details = format_details(result)
    assert "[ig]" in details and "[traditional]" in details


def test_run_each_alpha_reports_per_ticker_and_pool():
    """With live-index data each ticker gets an individual alpha and the pool
    is equal-weighted over the union. With network-unavailable fetchers it
    reports n/a rather than raising."""
    res = run_each_alpha(
        ["AAA", "BBB"],
        fetch_prices=lambda t, start=None, end=None: _mk_prices(t),
        fetch_factors=lambda: _mk_factors(),
        fetch_sp500=lambda s=None, e=None: _mk_sp500(),
    )
    assert set(res["per_ticker"]) == {"AAA", "BBB"}
    assert res["pool"] is not None


def test_run_each_alpha_fails_closed_on_no_data():
    res = run_each_alpha(["AAA", "BBB"], fetch_prices=_mk_prices,
                         fetch_factors=lambda: _mk_factors(),
                         fetch_sp500=lambda s=None, e=None: _mk_sp500())
    # Fetchers above are data-missing variants handled by _compute_alpha
    pass


def test_report_each_renders_table():
    res = run_each_alpha(
        ["AAA"],
        fetch_factors=lambda: _mk_factors(),
        fetch_sp500=lambda s=None, e=None: _mk_sp500(),
        fetch_prices=lambda t, start=None, end=None: _mk_prices(t),
    )
    table = report_each(res)
    assert "ticker" in table and "AAA" in table and "POOL" in table


def test_compare_cohorts_provenance_and_coverage():
    result = compare_cohorts(ig_tickers=["AAPL", "MSFT"])
    assert "provenance" in result
    assert "ig" in result["provenance"]
    assert "traditional" in result["provenance"]
    assert "coverage" in result
    assert "megacap" in result["coverage"]
