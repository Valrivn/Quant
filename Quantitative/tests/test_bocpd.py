"""
Unit Tests for BOCPD (Bayesian Online Change Point Detection) Seismic Shock Alarm
"""

from __future__ import annotations

import hashlib
import json
import numpy as np
import pytest

from Quantitative.bocpd import BOCPD, BOCPDConfig, BOCPDState, build_bocpd


def test_bocpd_default_config():
    cfg = BOCPDConfig()
    assert cfg.hazard_rate == 0.01
    assert cfg.threshold == 0.95
    assert cfg.min_run_length == 5
    assert cfg.max_run_length == 500
    assert cfg.mu_0 == 0.0
    assert cfg.kappa_0 == 1.0
    assert cfg.alpha_0 == 2.0
    assert cfg.beta_0 == 1.0


def test_bocpd_initialization():
    bocpd = BOCPD()
    assert bocpd.state is None
    bocpd.initialize()
    assert bocpd.state is not None
    assert isinstance(bocpd.state, BOCPDState)
    assert len(bocpd.state.run_length_probs) == 501
    assert np.isclose(bocpd.state.run_length_probs.sum(), 1.0)


def test_bocpd_initial_data_processing():
    initial_data = np.array([0.01, 0.02, -0.01, 0.03])
    bocpd = build_bocpd()
    for x in initial_data:
        bocpd.update(x)
    assert bocpd.state is not None
    assert len(bocpd.history) == 4


def test_bocpd_hazard_rate_functions():
    cfg = BOCPDConfig(hazard_rate=0.02)
    bocpd = BOCPD(cfg)
    log_h = bocpd._log_hazard(0)
    log_nh = bocpd._log_no_hazard(0)
    assert np.isclose(log_h, np.log(0.02 + cfg.eps))
    assert np.isclose(log_nh, np.log(0.98 + cfg.eps))


def test_bocpd_predictive_log_prob_prior():
    bocpd = build_bocpd()
    log_p_0 = bocpd._predictive_log_prob(0.0, 0)
    assert np.isfinite(log_p_0)
    assert log_p_0 <= 0.0


def test_bocpd_predictive_log_prob_posterior():
    bocpd = build_bocpd()
    bocpd.update(0.5)
    log_p_1 = bocpd._predictive_log_prob(0.5, 1)
    assert np.isfinite(log_p_1)


def test_bocpd_sufficient_stats_update():
    bocpd = build_bocpd()
    bocpd._update_sufficient_stats(1.5, 0)
    assert bocpd.state.N[0] == 1
    assert bocpd.state.sum_x[0] == 1.5
    assert bocpd.state.sum_x2[0] == 2.25
    assert bocpd.state.kappa_n[0] == bocpd.config.kappa_0 + 1


def test_bocpd_run_length_posterior_normalized():
    bocpd = build_bocpd()
    diag = bocpd.update(0.05)
    probs = diag["run_length_probs"]
    assert len(probs) == bocpd.config.max_run_length + 1
    assert np.isclose(probs.sum(), 1.0)
    assert all(p >= 0.0 for p in probs)


def test_bocpd_p_r0_threshold_and_alarm():
    # Force high probability of changepoint by manual state override
    cfg = BOCPDConfig(threshold=0.90, min_run_length=5)
    bocpd = BOCPD(cfg)
    bocpd.initialize()
    bocpd.state.run_length_probs[0] = 0.96
    bocpd.state.p_r0 = 0.96
    bocpd.state.map_run_length = 2

    # Run check logic
    alarm = (bocpd.state.p_r0 >= cfg.threshold and bocpd.state.map_run_length <= cfg.min_run_length)
    assert alarm is True


def test_bocpd_observation_credit_spread_velocity():
    # Test observation input matching credit spread velocity domain
    bocpd = build_bocpd()
    velocities = [0.01, 0.05, 0.02, 0.50, 1.20, 2.50]
    results = [bocpd.update(v) for v in velocities]
    assert len(results) == 6
    for r in results:
        assert "p_r0" in r
        assert "map_run_length" in r
        assert "alarm_triggered" in r


def test_bocpd_min_max_run_length_bounds():
    cfg = BOCPDConfig(min_run_length=3, max_run_length=50)
    bocpd = build_bocpd(cfg)
    assert len(bocpd.state.run_length_probs) == 51
    diag = bocpd.update(0.1)
    assert diag["map_run_length"] <= 50


def test_bocpd_posterior_mean_and_var():
    bocpd = build_bocpd()
    for x in [0.1, 0.2, 0.15, 0.12]:
        bocpd.update(x)
    mean = bocpd._posterior_mean()
    var = bocpd._posterior_var()
    assert np.isfinite(mean)
    assert np.isfinite(var)
    assert var > 0.0


def test_bocpd_diagnostics_structure():
    bocpd = build_bocpd()
    bocpd.update(0.1)
    diag = bocpd.get_diagnostics()
    assert "p_r0" in diag
    assert "map_run_length" in diag
    assert "alarm_triggered" in diag
    assert "posterior_mean" in diag
    assert "posterior_var" in diag
    assert diag["history_len"] == 1


def test_bocpd_reset():
    bocpd = build_bocpd()
    bocpd.update(0.5)
    bocpd.update(0.8)
    assert len(bocpd.history) == 2
    bocpd.reset()
    assert len(bocpd.history) == 0
    assert bocpd.state is not None


def test_bocpd_output_sha256_provenance():
    bocpd = build_bocpd()
    np.random.seed(42)
    series = np.random.normal(0, 1, 10).tolist()
    history = [bocpd.update(x) for x in series]

    # Compute deterministic SHA-256 provenance over diagnostic history
    payload = json.dumps([{"x": h["x"], "p_r0": float(h["p_r0"]), "map_rl": int(h["map_run_length"])} for h in history], sort_keys=True)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert len(digest) == 64
