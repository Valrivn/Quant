"""
Unit Tests for IO-HMM (Input-Output HMM) Tier 1 Macro States (Wind-Vane)
"""

from __future__ import annotations

import hashlib
import json
import numpy as np
import pandas as pd
import pytest

from Quantitative.io_hmm import IOHMM, IOHMMConfig, IOHMMState, build_io_hmm


def test_io_hmm_default_config():
    cfg = IOHMMConfig()
    assert cfg.n_states == 3
    assert cfg.state_names == ("EXPANSION", "CHOPPY", "CRISIS")
    assert cfg.n_inputs == 6
    assert len(cfg.input_features) == 6
    assert cfg.initial_prior == (0.6, 0.3, 0.1)
    assert cfg.sticky_prior == 10.0


def test_io_hmm_initialization():
    io_hmm = build_io_hmm()
    assert io_hmm.state is not None
    assert isinstance(io_hmm.state, IOHMMState)
    assert io_hmm.state.trans_base.shape == (3, 3)
    assert io_hmm.state.trans_weights.shape == (3, 3, 6)
    assert io_hmm.state.emit_mean.shape == (3, 6)
    assert io_hmm.state.emit_cov.shape == (3, 6, 6)


def test_io_hmm_initial_inputs_processing():
    initial_u = np.array([
        [0.0, 0.0, 0.15, 0.02, 0.01, 18.0],
        [0.1, 0.0, 0.16, 0.02, 0.01, 19.0],
    ])
    io_hmm = IOHMM()
    io_hmm.initialize(initial_inputs=initial_u)
    assert len(io_hmm.state.input_history) == 2


def test_io_hmm_transition_probs_computation():
    io_hmm = build_io_hmm()
    u_t = np.array([0.05, -0.01, 0.15, 0.02, 0.01, 18.0])
    trans_p = io_hmm._compute_transition_probs(u_t)
    assert trans_p.shape == (3, 3)
    assert np.allclose(trans_p.sum(axis=1), 1.0)
    assert np.all(trans_p >= 0.0)


def test_io_hmm_emission_log_prob():
    io_hmm = build_io_hmm()
    u_t = np.zeros(6)
    for s in range(3):
        log_prob = io_hmm._emission_log_prob(u_t, s)
        assert np.isfinite(log_prob)


def test_io_hmm_belief_softmax_normalization():
    io_hmm = build_io_hmm()
    u_t = np.array([0.0, 0.0, 0.12, 0.02, 0.01, 15.0])
    diag = io_hmm.update(u_t)
    belief = diag["belief"]
    assert len(belief) == 3
    assert np.isclose(belief.sum(), 1.0)
    assert all(b >= 0.0 for b in belief)


def test_io_hmm_map_state_and_name():
    io_hmm = build_io_hmm()
    u_t = np.array([0.0, 0.0, 0.12, 0.02, 0.01, 15.0])
    diag = io_hmm.update(u_t)
    assert diag["map_state"] in (0, 1, 2)
    assert diag["state_name"] in ("EXPANSION", "CHOPPY", "CRISIS")


def test_io_hmm_emission_confidence_max_posterior():
    io_hmm = build_io_hmm()
    u_t = np.array([0.0, 0.0, 0.12, 0.02, 0.01, 15.0])
    diag = io_hmm.update(u_t)
    max_post = float(np.max(diag["belief"]))
    assert 1.0 / 3.0 <= max_post <= 1.0


def test_io_hmm_state_sequence_generation():
    io_hmm = build_io_hmm()
    np.random.seed(42)
    sequence_inputs = np.random.normal(0, 1, (20, 6))
    state_names = []
    for u in sequence_inputs:
        diag = io_hmm.update(u)
        state_names.append(diag["state_name"])
    assert len(state_names) == 20
    assert all(s in ("EXPANSION", "CHOPPY", "CRISIS") for s in state_names)


def test_io_hmm_input_history_max_len():
    io_hmm = build_io_hmm()
    for i in range(120):
        io_hmm.update(np.ones(6) * (i * 0.01))
    assert len(io_hmm.state.input_history) == 100


def test_io_hmm_get_current_state():
    io_hmm = build_io_hmm()
    io_hmm.update(np.zeros(6))
    cur = io_hmm.get_current_state()
    assert "belief" in cur
    assert "map_state" in cur
    assert "state_name" in cur


def test_io_hmm_compute_velocity_features():
    io_hmm = build_io_hmm()
    dates = pd.date_range("2020-01-01", periods=50, freq="D")
    credit_spread = pd.Series(np.linspace(200, 300, 50), index=dates)
    yield_curve = pd.Series(np.linspace(50, -10, 50), index=dates)
    realized_vol = pd.Series(np.linspace(0.1, 0.3, 50), index=dates)
    vix = pd.Series(np.linspace(15, 30, 50), index=dates)

    df = io_hmm.compute_velocity_features(credit_spread, yield_curve, realized_vol, vix)
    assert not df.empty
    assert "credit_spread_velocity" in df.columns
    assert "yield_curve_momentum" in df.columns
    assert "realized_vol_21d" in df.columns
    assert "vix_level" in df.columns


def test_io_hmm_factory_function():
    cfg = IOHMMConfig(n_states=3)
    model = build_io_hmm(cfg)
    assert model.state is not None
    assert isinstance(model, IOHMM)


def test_io_hmm_state_counts_update():
    io_hmm = build_io_hmm()
    for _ in range(5):
        io_hmm.update(np.zeros(6))
    assert io_hmm.state.state_counts.sum() == 5


def test_io_hmm_output_provenance_sha256():
    io_hmm = build_io_hmm()
    np.random.seed(42)
    inputs = np.random.normal(0, 1, (10, 6))
    beliefs = [io_hmm.update(u)["belief"].tolist() for u in inputs]

    payload = json.dumps(beliefs, sort_keys=True)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert len(digest) == 64
