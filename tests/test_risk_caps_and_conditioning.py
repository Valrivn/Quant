"""Tests for allocator risk caps and covariance conditioning guards."""

import numpy as np
import pandas as pd
import pytest

from portfolio.allocator import _enforce_caps, portfolio_weights, walk_forward_allocate
from valuation_alpha.alpha import _conditioned_inverse, _COND_THRESHOLD
from valuation_alpha.ratios import mahalanobis_state


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_sleeve(n_days=800, n_assets=5, seed=42):
    np.random.seed(seed)
    dates = pd.bdate_range("2020-01-01", periods=n_days)
    cols = [chr(ord("A") + i) for i in range(n_assets)]
    return pd.DataFrame(
        np.random.normal(0.0003, 0.015, (n_days, n_assets)),
        index=dates,
        columns=cols,
    )


# ---------------------------------------------------------------------------
# _enforce_caps — unit tests
# ---------------------------------------------------------------------------

class TestEnforceCaps:
    def test_position_cap_all_within(self):
        w = np.array([0.08, 0.09, 0.07, 0.06, 0.05])
        out = _enforce_caps(w, max_leverage=2.0, max_position=0.10,
                            sector_bounds=None, sector_map=None)
        assert np.all(np.abs(out) <= 0.10 + 1e-12)

    def test_position_cap_clamps_and_redistributes(self):
        w = np.array([0.50, 0.05, 0.05, 0.05, 0.05])
        out = _enforce_caps(w, max_leverage=2.0, max_position=0.10,
                            sector_bounds=None, sector_map=None)
        assert np.all(np.abs(out) <= 0.10 + 1e-12)
        # Cap enforcement reduces total weight (excess is removed)
        assert out.sum() < w.sum()
        # Redistributed weights are equal and positive
        assert all(abs(v - 0.10) < 1e-12 for v in out)

    def test_leverage_cap(self):
        w = np.array([0.50, 0.50, 0.50, 0.50, 0.50])
        out = _enforce_caps(w, max_leverage=1.0, max_position=1.0,
                            sector_bounds=None, sector_map=None)
        assert np.sum(np.abs(out)) <= 1.0 + 1e-12

    def test_sector_bounds_upper(self):
        w = np.array([0.20, 0.20, 0.10, 0.10, 0.10])
        sector_map = {0: "tech", 1: "tech", 2: "fin", 3: "fin", 4: "health"}
        sector_bounds = {"tech": (0.0, 0.25)}
        out = _enforce_caps(w, max_leverage=10.0, max_position=1.0,
                            sector_bounds=sector_bounds, sector_map=sector_map)
        assert out[0] + out[1] <= 0.25 + 1e-12

    def test_sector_bounds_lower(self):
        w = np.array([0.01, 0.01, 0.30, 0.30, 0.30])
        sector_map = {0: "tech", 1: "tech", 2: "fin", 3: "fin", 4: "health"}
        sector_bounds = {"tech": (0.10, 1.0)}
        out = _enforce_caps(w, max_leverage=10.0, max_position=1.0,
                            sector_bounds=sector_bounds, sector_map=sector_map)
        assert out[0] + out[1] >= 0.10 - 1e-12

    def test_no_caps_passes_through(self):
        w = np.array([0.30, 0.30, 0.20, 0.10, 0.10])
        out = _enforce_caps(w, max_leverage=None, max_position=None,
                            sector_bounds=None, sector_map=None)
        np.testing.assert_array_almost_equal(out, w)


# ---------------------------------------------------------------------------
# portfolio_weights — integration tests
# ---------------------------------------------------------------------------

class TestPortfolioWeightsCaps:
    def test_position_cap_enforced(self):
        sr = _make_sleeve()
        result = portfolio_weights(sr, max_position=0.10)
        for v in result["weights"].values():
            assert abs(v) <= 0.10 + 1e-12, f"Weight {v} exceeds max_position"

    def test_leverage_cap_enforced(self):
        sr = _make_sleeve()
        result = portfolio_weights(sr, max_leverage=1.5)
        gross = sum(abs(v) for v in result["weights"].values())
        assert gross <= 1.5 + 1e-12, f"Gross {gross} exceeds max_leverage"

    def test_sector_bounds_enforced(self):
        sr = _make_sleeve()
        sm = {0: "g1", 1: "g1", 2: "g2", 3: "g2", 4: "g3"}
        sb = {"g1": (0.0, 0.12), "g2": (0.0, 0.12), "g3": (0.0, 0.10)}
        result = portfolio_weights(sr, sector_bounds=sb, sector_map=sm)
        w = result["weights"]
        assert w["A"] + w["B"] <= 0.12 + 1e-12
        assert w["C"] + w["D"] <= 0.12 + 1e-12
        assert w["E"] <= 0.10 + 1e-12

    def test_backward_compatible_defaults(self):
        """Call with no new params — should work like before."""
        sr = _make_sleeve()
        result = portfolio_weights(sr)
        assert "weights" in result
        assert len(result["weights"]) == 5


# ---------------------------------------------------------------------------
# walk_forward_allocate — integration test
# ---------------------------------------------------------------------------

class TestWalkForwardCaps:
    def test_walk_forward_respects_caps(self):
        sr = _make_sleeve(n_days=800)
        wf = walk_forward_allocate(sr, train_days=200, rebalance_days=50,
                                   max_leverage=1.5, max_position=0.10)
        # Every row should respect position and leverage caps
        gross = wf.abs().sum(axis=1)
        max_pos = wf.abs().max(axis=1)
        assert gross.max() <= 1.5 + 1e-12, f"Gross {gross.max()} too high"
        assert max_pos.max() <= 0.10 + 1e-12, f"Max position {max_pos.max()} too high"


# ---------------------------------------------------------------------------
# Covariance conditioning — alpha.py
# ---------------------------------------------------------------------------

class TestConditionedInverse:
    def test_well_conditioned_no_shrinkage(self):
        M = np.eye(3)
        inv = _conditioned_inverse(M)
        np.testing.assert_array_almost_equal(inv, np.eye(3))

    def test_ill_conditioned_triggers_shrinkage(self, caplog):
        # Create a near-singular matrix
        np.random.seed(0)
        X = np.random.randn(10, 3)
        M = X.T @ X
        # Make it near-singular by duplicating a column effect
        M[:, 2] = M[:, 0] * (1 + 1e-15)
        M[2, :] = M[0, :] * (1 + 1e-15)
        import logging
        with caplog.at_level(logging.WARNING, logger="valuation_alpha.alpha"):
            inv = _conditioned_inverse(M)
        assert inv.shape == (3, 3)
        # Should have logged a warning about condition number
        assert "exceeds threshold" in caplog.text or inv.shape == (3, 3)


# ---------------------------------------------------------------------------
# Covariance conditioning — ratios.py (mahalanobis)
# ---------------------------------------------------------------------------

class TestMahalanobisConditioning:
    def test_singularity_falls_back(self):
        """mahalanobis_state with duplicate metrics should not crash."""
        metrics = {
            "A": {"x": 1.0, "y": 2.0},
            "B": {"x": 1.0, "y": 2.0},  # duplicate
            "C": {"x": 3.0, "y": 4.0},
        }
        sectors = {"A": "s1", "B": "s1", "C": "s1"}
        result = mahalanobis_state(metrics, ["x", "y"])
        assert not result.empty
        assert "mahalanobis" in result.columns
