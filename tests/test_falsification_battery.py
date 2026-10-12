"""
Unit tests for The 4-Gate Statistical Falsification Suite
"""

import numpy as np
import pandas as pd
import pytest
from validation.falsification_battery import (
    run_deflated_sharpe_ratio,
    run_regime_contingency_gate,
    run_moving_block_bootstrap,
)

def test_deflated_sharpe_ratio():
    # Strong positive return series
    rng = np.random.default_rng(42)
    rets = rng.normal(0.015, 0.03, size=84) # positive mean
    res = run_deflated_sharpe_ratio(rets, n_trials=10, rf=0.02)
    assert 'deflated_sharpe_ratio' in res
    assert 0.0 <= res['deflated_sharpe_ratio'] <= 1.0

def test_regime_contingency_gate():
    strat_rets = [0.03, -0.01, 0.04, -0.02, 0.05, 0.01]
    bench_rets = [0.02, -0.03, 0.01, -0.04, 0.02, -0.01]
    res = run_regime_contingency_gate(strat_rets, bench_rets)
    assert 'contingency_table' in res
    assert 'bull_win_rate' in res
    assert 'bear_win_rate' in res

def test_bootstrap_null():
    df_reg = pd.DataFrame({
        'strat_excess': np.random.normal(0.005, 0.02, 60),
        'Mkt-RF': np.random.normal(0.008, 0.04, 60),
        'SMB': np.random.normal(0.0, 0.02, 60),
        'HML': np.random.normal(0.0, 0.02, 60),
        'RMW': np.random.normal(0.0, 0.02, 60),
        'CMA': np.random.normal(0.0, 0.02, 60),
    })
    res = run_moving_block_bootstrap(df_reg, block_size=6, B=50)
    assert 'empirical_p_value' in res
    assert 0.0 <= res['empirical_p_value'] <= 1.0
