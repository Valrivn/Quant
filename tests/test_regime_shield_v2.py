"""
test_regime_shield_v2.py — Unit tests for Quantitative/regime_shield_v2.py (Phase 8 Redesign)
"""

import numpy as np
import pandas as pd
import pytest
from Quantitative.regime_shield_v2 import TDistributionHMM, load_config, RegimeState


def test_t_hmm_initialization():
    config_path = "config/pre_registration_phase8.yaml"
    cfg = load_config(config_path)
    hmm = TDistributionHMM(cfg)

    assert hmm.n_states == 3
    assert hmm.sticky_kappa == 50.0
    assert hmm.min_dwell_months == 3
    assert hmm.state_names == ["EXPANSION", "SHOCK", "CRISIS"]


def test_t_hmm_monthly_reestimate_and_nowcast():
    config_path = "config/pre_registration_phase8.yaml"
    cfg = load_config(config_path)
    hmm = TDistributionHMM(cfg)

    np.random.seed(42)
    n_days = 300
    dates = pd.date_range("2018-01-01", periods=n_days, freq="B")

    # Generate synthetic observations (BAA10Y, VIX, ERP)
    baa = np.concatenate([np.random.normal(1.8, 0.2, 150), np.random.normal(2.8, 0.3, 75), np.random.normal(4.5, 0.5, 75)])
    vix = np.concatenate([np.random.normal(14.0, 2.0, 150), np.random.normal(22.0, 3.0, 75), np.random.normal(35.0, 5.0, 75)])
    erp = np.concatenate([np.random.normal(0.045, 0.005, 150), np.random.normal(0.035, 0.005, 75), np.random.normal(0.015, 0.01, 75)])

    df = pd.DataFrame({"BAA10Y_spread": baa, "VIX": vix, "ERP": erp}, index=dates)

    hmm.monthly_reestimate(df)
    assert hmm.is_fitted
    assert hmm.transmat_.shape == (3, 3)
    assert hmm.means_.shape == (3, 3)

    # State label lock verification: VIX mean ascending (EXPANSION < SHOCK < CRISIS)
    vix_idx = hmm.observables.index("VIX")
    vix_means = hmm.means_[:, vix_idx]
    assert vix_means[0] < vix_means[1] < vix_means[2]

    # Daily nowcast
    sample_obs = df.iloc[-1]
    probs, state = hmm.daily_nowcast(sample_obs)
    assert len(probs) == 3
    assert np.isclose(np.sum(probs), 1.0)
    assert state in ["EXPANSION", "SHOCK", "CRISIS"]

    # RegimeState dataclass
    rs = hmm.get_regime_state(dates[-1], sample_obs)
    assert isinstance(rs, RegimeState)
    assert rs.detected_state == state
