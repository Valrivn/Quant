"""Tests for valuation_alpha.moat_gate (D-20260802-002 moat/uniqueness signal)."""

import numpy as np
import pytest

from valuation_alpha.moat_gate import (
    _norm_rating,
    moat_score_from_parts,
    build_moat_gate,
    moat_compromise_flag,
)


class TestNormRating:
    def test_rating_bounds(self):
        assert _norm_rating(5.0, 0.0, 5.0) == 1.0
        assert _norm_rating(0.0, 0.0, 5.0) == 0.0
        assert _norm_rating(2.5, 0.0, 5.0) == 0.5

    def test_sentiment_bounds(self):
        assert _norm_rating(1.0, -1.0, 1.0) == 1.0
        assert _norm_rating(-1.0, -1.0, 1.0) == 0.0

    def test_none_and_nan(self):
        assert _norm_rating(None, 0.0, 5.0) is None
        assert _norm_rating(np.nan, 0.0, 5.0) is None
        assert _norm_rating("garbage", 0.0, 5.0) is None


class TestMoatScoreFromParts:
    def test_no_signals(self):
        assert moat_score_from_parts() is None
        assert moat_score_from_parts(avg_rating=None, sentiment=None, n_products=None) is None

    def test_strong_reviews_only(self):
        s = moat_score_from_parts(avg_rating=5.0)
        assert s == 1.0

    def test_single_signal_is_that_signal(self):
        assert moat_score_from_parts(avg_rating=2.5) == 0.5
        assert moat_score_from_parts(sentiment=1.0) == 1.0
        assert moat_score_from_parts(n_products=6) == 1.0
        assert moat_score_from_parts(n_products=3) == pytest.approx(0.5)

    def test_composite_weighted(self):
        # rating 1.0 (w 0.4), sentiment 1.0 (w 0.35), breadth 6/6=1.0 (w 0.25)
        s = moat_score_from_parts(avg_rating=5.0, sentiment=1.0, n_products=6)
        assert s == pytest.approx(1.0)

    def test_partial_composite_renormalizes(self):
        # rating 0 (w 0.4) + sentiment 1 (w 0.35); breadth None dropped.
        s = moat_score_from_parts(avg_rating=0.0, sentiment=1.0)
        assert s == pytest.approx(0.35 / 0.75, rel=1e-3)

    def test_breadth_saturates(self):
        assert moat_score_from_parts(n_products=50) == 1.0


class TestBuildMoatGate:
    def test_aggregates_tickers(self):
        out = build_moat_gate(
            product_intel_by_ticker={"A": {"avg_rating": 4.0}, "B": {"avg_rating": 3.0}},
            reddit_by_ticker={"A": 0.5},
            breadth_by_ticker={"B": 3},
        )
        assert set(out) == {"A", "B"}
        assert out["A"] is not None
        assert out["B"] is not None

    def test_missing_signal_ticker(self):
        out = build_moat_gate(
            product_intel_by_ticker={"A": {"avg_rating": 4.0}},
        )
        assert out["A"] == pytest.approx(0.8)

    def test_no_data(self):
        assert build_moat_gate() == {}
        assert build_moat_gate(None, None, None) == {}


class TestMoatCompromiseFlag:
    def test_no_compromise(self):
        assert moat_compromise_flag(0.8, 0.9) is False

    def test_compromise(self):
        assert moat_compromise_flag(0.4, 0.8) is True

    def test_unknown_values_no_sell(self):
        assert moat_compromise_flag(None, 0.8) is False
        assert moat_compromise_flag(0.8, None) is False
        assert moat_compromise_flag(None, None) is False

    def test_custom_threshold(self):
        assert moat_compromise_flag(0.55, 0.8, drop_threshold=0.30) is False
        assert moat_compromise_flag(0.49, 0.8, drop_threshold=0.30) is True
