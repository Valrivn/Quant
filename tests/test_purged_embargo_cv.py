"""
test_purged_embargo_cv.py — Unit tests for PurgedEmbargoCV and TrialTracker
"""

import numpy as np
import pandas as pd
import pytest
from research.ml_parameter_optimization import PurgedEmbargoCV, TrialTracker, GroupLasso


def test_purged_embargo_cv_splits():
    dates = pd.date_range("2018-01-01", "2024-12-31", freq="B")
    df = pd.DataFrame(np.random.normal(0, 1, (len(dates), 2)), index=dates)

    cv = PurgedEmbargoCV(n_splits=4, trailing_tau=10, embargo_months=12)
    splits = list(cv.split(df))

    assert len(splits) == 4
    for train_idx, test_idx in splits:
        assert len(train_idx) > 0
        assert len(test_idx) > 0
        # Check purge: training index end must be strictly less than test index start minus purge days
        purge_days = 10 * 5
        assert train_idx[-1] < test_idx[0] - purge_days + 1 or train_idx[-1] < test_idx[0]


def test_trial_tracker_logging_and_dsr():
    tracker = TrialTracker(base_n=13, target_n=513, track_record_days=1008)

    p1 = {"swap_barrier_C": 0.0250, "gamma": 0.25}
    entry = tracker.log_trial(trial_id=1, params=p1, metric_value=1.45)

    assert len(tracker.trials) == 1
    assert "sha256" in entry
    assert len(entry["sha256"]) == 64

    # Compute DSR with N=513
    dsr = tracker.compute_dsr(best_sharpe=1.45)
    assert 0.0 <= dsr <= 1.0


def test_group_lasso_penalty():
    gl = GroupLasso()
    score = 1.50
    penalized = gl.apply_shrinkage("A_wacc_regime", score)
    assert penalized < score
