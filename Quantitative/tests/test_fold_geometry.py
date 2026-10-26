"""
Unit Tests for Walk-Forward Fold Geometry & Leakage Invariants
"""

from __future__ import annotations

import pandas as pd
import pytest

from Quantitative.validation.walk_forward import (
    make_walk_forward_splits,
    validate_walk_forward_folds,
    verify_embargo_purge,
)


def test_fold_execution_invariant():
    # Should pass when count matches
    validate_walk_forward_folds(n_requested=4, n_executed=4)

    # Should raise RuntimeError when folds dropped
    with pytest.raises(RuntimeError, match="Walk-Forward Geometry Violation"):
        validate_walk_forward_folds(n_requested=4, n_executed=3)


def test_make_walk_forward_splits():
    splits = make_walk_forward_splits(folds=4)
    assert len(splits) == 4
    
    for train_start, train_end, test_start, test_end in splits:
        assert train_start < train_end
        assert test_start < test_end
        assert train_end < test_start


def test_embargo_purge_verification():
    train_end = pd.Timestamp("2020-12-31")
    valid_test_start = pd.Timestamp("2021-01-01")
    assert verify_embargo_purge(train_end, valid_test_start)

    invalid_test_start = pd.Timestamp("2020-12-15")
    with pytest.raises(RuntimeError, match="Data leakage detected"):
        verify_embargo_purge(train_end, invalid_test_start)


def test_fold_1_geometry():
    splits = make_walk_forward_splits(folds=4)
    train_start, train_end, test_start, test_end = splits[0]
    assert train_start == pd.Timestamp("2010-01-01")
    assert train_end == pd.Timestamp("2017-12-31")
    assert test_start == pd.Timestamp("2018-01-01")
    assert test_end == pd.Timestamp("2019-12-31")


def test_fold_2_geometry():
    splits = make_walk_forward_splits(folds=4)
    train_start, train_end, test_start, test_end = splits[1]
    assert train_start == pd.Timestamp("2010-01-01")
    assert train_end == pd.Timestamp("2019-12-31")
    assert test_start == pd.Timestamp("2020-01-01")
    assert test_end == pd.Timestamp("2021-12-31")


def test_fold_3_geometry():
    splits = make_walk_forward_splits(folds=4)
    train_start, train_end, test_start, test_end = splits[2]
    assert train_start == pd.Timestamp("2010-01-01")
    assert train_end == pd.Timestamp("2021-12-31")
    assert test_start == pd.Timestamp("2022-01-01")
    assert test_end == pd.Timestamp("2023-12-31")


def test_fold_4_geometry():
    splits = make_walk_forward_splits(folds=4)
    train_start, train_end, test_start, test_end = splits[3]
    assert train_start == pd.Timestamp("2010-01-01")
    assert train_end == pd.Timestamp("2023-12-31")
    assert test_start == pd.Timestamp("2024-01-01")
    assert test_end == pd.Timestamp("2024-12-31")


def test_expanding_window_train_growth():
    splits = make_walk_forward_splits(folds=4)
    train_ends = [s[1] for s in splits]
    # Train end dates strictly increase
    for i in range(1, len(train_ends)):
        assert train_ends[i] > train_ends[i - 1]


def test_no_overlap_between_test_windows():
    splits = make_walk_forward_splits(folds=4)
    for i in range(len(splits) - 1):
        test_end_i = splits[i][3]
        test_start_next = splits[i + 1][2]
        assert test_start_next > test_end_i


def test_concatenated_oos_duration_84_months():
    splits = make_walk_forward_splits(folds=4)
    total_months = 0
    for _, _, test_start, test_end in splits:
        # Compute number of months in test window
        months = (test_end.year - test_start.year) * 12 + (test_end.month - test_start.month) + 1
        total_months += months
    # Fold 1: 2018-2019 (24m), Fold 2: 2020-2021 (24m), Fold 3: 2022-2023 (24m), Fold 4: 2024 (12m) = 84m
    assert total_months == 84


def test_insufficient_folds_raises():
    with pytest.raises(RuntimeError, match="Data range insufficient"):
        make_walk_forward_splits(folds=5)


def test_validate_walk_forward_folds_zero_executed():
    with pytest.raises(RuntimeError, match="Walk-Forward Geometry Violation"):
        validate_walk_forward_folds(n_requested=4, n_executed=0)


def test_embargo_purge_verification_same_date_fails():
    dt = pd.Timestamp("2020-12-31")
    with pytest.raises(RuntimeError, match="Data leakage detected"):
        verify_embargo_purge(train_end=dt, test_start=dt)


def test_embargo_purge_verification_test_before_train_fails():
    train_end = pd.Timestamp("2021-01-01")
    test_start = pd.Timestamp("2020-12-31")
    with pytest.raises(RuntimeError, match="Data leakage detected"):
        verify_embargo_purge(train_end, test_start)


def test_make_walk_forward_splits_subset():
    splits = make_walk_forward_splits(folds=2)
    assert len(splits) == 2
    assert splits[0][2] == pd.Timestamp("2018-01-01")
    assert splits[1][2] == pd.Timestamp("2020-01-01")

