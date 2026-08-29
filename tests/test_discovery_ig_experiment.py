"""Tests for the Instagram independent discovery experiment (D-20260707-001).

Covers: unfed (empty) feed fail-closed, deterministic ticker punctuality,
the full standard-screen pass structure (qual + quant gates reused from census),
and the current-scraper baseline cohort loader. No live network: the gate
functions are patched per-test against the real ``discovery.gate_data`` module
(consumers resolve ``gate_data`` lazily, so patching module attributes is
sufficient), and the cohort loader falls back to [] when the DB is absent.
"""

import pandas as pd
import pytest

from discovery import gate_data as gd
from discovery.ig_experiment import (
    run_ig_experiment,
    current_scraper_cohort,
    IgCandidate,
)

_MOAT_KEYS = [
    "product_breadth",
    "developer_momentum",
    "employee_sentiment",
    "revenue_concentration",
    "network_effect_proxy",
    "regulatory_barrier",
]


def _neutral_signals(_ticker):
    return (
        {k: 0.5 for k in _MOAT_KEYS},
        {k: "default_neutral" for k in _MOAT_KEYS},
    )


def _bull_signals(_ticker):
    return (
        {k: 0.9 for k in _MOAT_KEYS},
        {k: "cached:test" for k in _MOAT_KEYS},
    )


def _patch_gates(monkeypatch, build_df=None):
    monkeypatch.setattr(gd, "qualitative_signals", _neutral_signals)
    if build_df is not None:
        monkeypatch.setattr(gd, "build_names_frame", lambda tickers: build_df)
        monkeypatch.setattr(gd, "normalize_mahalanobis", lambda df: df)


def _plain_frame(tickers):
    return pd.DataFrame(
        {
            "ticker": tickers,
            "alpha_3y_ann": [0.05] * len(tickers),
            "cash_burn_months_pct": [20.0] * len(tickers),
            "interest_coverage_ratio_pct": [5.0] * len(tickers),
            "mahalanobis": [0.2] * len(tickers),
        }
    )


def test_empty_feed_is_unfed_not_invented():
    """No IG candidates in -> experiment reports ``unfed``, never fakes data."""
    result = run_ig_experiment([])
    assert result["status"] == "unfed"
    assert result["pass_cohort"] == []


def test_illformed_candidates_fail_closed():
    """Garbage entities cannot become candidates."""
    result = run_ig_experiment(["$", "  ", "HAS-TAG#", "TSLA;DROP"])
    assert result["status"] == "dry"
    assert result["pass_cohort"] == []
    for c in result["candidates"]:
        assert not c.validated


def test_known_ticker_runs_gates_without_raising(monkeypatch):
    """The experiment never raises on a plausible ticker; it fail-closes."""
    _patch_gates(monkeypatch, build_df=_plain_frame(["INTC", "NVDA"]))
    result = run_ig_experiment(["INTC", "NVDA"])
    assert result["status"] in ("dry", "gated")
    for c in result["candidates"]:
        assert c.ticker in ("INTC", "NVDA")
        # qual/quant are exercised; reasons are honest (avoid/no_alpha_data or pass)
        assert c.reason_chain != "" or c.passed


def test_passed_requires_all_gates():
    candidate = IgCandidate(
        ticker="X", validated=True, qual_pass=True, quant_pass=True
    )
    assert candidate.passed is True
    candidate.liquidity_pass = False
    assert candidate.passed is False
    assert "liq:fail" in candidate.reason_chain


def test_current_scraper_cohort_loads_or_closes():
    """The baseline cohort either loads distinct tickers or returns []."""
    cohort = current_scraper_cohort(limit=5)
    assert isinstance(cohort, list)
    assert all(isinstance(t, str) and t for t in cohort)
    assert len(cohort) <= 5


def test_batch_quant_gate_on_multiple_names(monkeypatch):
    """Batch quant gate on >=2 real names produces non-no_alpha_data reason when data is patched."""
    df = pd.DataFrame(
        {
            "ticker": ["AAPL", "MSFT"],
            "alpha_3y_ann": [0.05, 0.08],
            "cash_burn_months_pct": [20.0, 25.0],
            "interest_coverage_ratio_pct": [5.0, 6.0],
            "mahalanobis": [0.2, 0.3],
        }
    )
    monkeypatch.setattr(gd, "build_names_frame", lambda tickers: df)
    monkeypatch.setattr(gd, "normalize_mahalanobis", lambda df: df)
    from discovery.census import _quant_baseline_gate

    fails = _quant_baseline_gate(["AAPL", "MSFT"])
    for t, reason in fails.items():
        assert reason != "no_alpha_data"


def test_qualitative_gate_default_neutral(monkeypatch):
    """Qualitative gate with all-default-neutral signals yields recommendation 'hold' (not 'avoid')."""
    monkeypatch.setattr(gd, "qualitative_signals", _neutral_signals)
    result = run_ig_experiment(["AAPL"])
    candidate = result["candidates"][0]
    assert not candidate.qual_pass
    assert "qual:hold" in candidate.reason_chain
    assert "avoid" not in candidate.reason_chain


def test_qualitative_gate_bull_passes_to_quant(monkeypatch):
    """Strong moat signals pass the qual gate so the candidate reaches the quant leg."""
    monkeypatch.setattr(gd, "qualitative_signals", _bull_signals)
    monkeypatch.setattr(gd, "build_names_frame", lambda tickers: _plain_frame(tickers))
    monkeypatch.setattr(gd, "normalize_mahalanobis", lambda df: df)
    result = run_ig_experiment(["AAPL", "MSFT"])
    for c in result["candidates"]:
        assert c.qual_pass
        assert c.reason_chain == ""


def test_run_ig_experiment_provenance(monkeypatch):
    """run_ig_experiment output includes provenance counts when live=True."""
    df = _plain_frame(["AAPL", "MSFT"])
    df.attrs["provenance"] = {
        "AAPL": {
            "alpha_3y_ann": "live_ff5",
            "cash_burn_months_pct": "NaN",
            "interest_coverage_ratio_pct": "NaN",
            "mahalanobis": "NaN",
        },
        "MSFT": {
            "alpha_3y_ann": "results_runa_cached",
            "cash_burn_months_pct": "live_sec",
            "interest_coverage_ratio_pct": "live_sec",
            "mahalanobis": "results_runa_cached",
        },
    }
    monkeypatch.setattr(gd, "build_names_frame", lambda tickers: df)
    monkeypatch.setattr(gd, "normalize_mahalanobis", lambda df: df)
    monkeypatch.setattr(gd, "qualitative_signals", _neutral_signals)
    result = run_ig_experiment(["AAPL", "MSFT"], live=True)
    assert result["provenance"] == {
        "live_ff5": 1,
        "results_runa_cached": 1,
        "live_sec": 0,
        "default_neutral": 0,
    }
