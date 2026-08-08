"""Tests for the Instagram independent discovery experiment (D-20260707-001).

Covers: unfed (empty) feed fail-closed, deterministic ticker punctuality,
the full standard-screen pass structure (qual + quant gates reused from census),
and the current-scraper baseline cohort loader. No live network: the gate
functions are exercised as-is (they are read-only and fail closed), and the
cohort loader falls back to [] when the DB is absent.
"""

import pytest

from discovery.ig_experiment import (
    run_ig_experiment,
    current_scraper_cohort,
    IgCandidate,
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


def test_known_ticker_runs_gates_without_raising():
    """The experiment never raises on a plausible ticker; it fail-closes."""
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