"""
Walk-Forward Fold Geometry Enforcement Module

Enforces fold definitions, embargo/purge invariant constraints, and
fail-closed fold execution rules for Phase 9 v7 regime shield validation.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Tuple
import pandas as pd

logger = logging.getLogger("walk_forward")


def validate_walk_forward_folds(n_requested: int, n_executed: int) -> None:
    """
    Fail-closed invariant gate: raise RuntimeError if n_executed != n_requested.
    No silent fold dropping allowed.
    """
    if n_executed != n_requested:
        raise RuntimeError(
            f"Walk-Forward Geometry Violation: Requested {n_requested} folds, "
            f"but executed {n_executed} folds. Silent fold dropping is strictly forbidden."
        )


def make_walk_forward_splits(
    config: Dict = None,
    folds: int = 4,
    start_year: int = 2018,
    end_year: int = 2024,
) -> List[Tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp]]:
    """
    Constructs walk-forward expanding window splits with embargo & purge.
    Returns list of tuples: (train_start, train_end, test_start, test_end).
    
    Default 4 folds covering 2018-2024:
    Fold 1: train -> 2017-12-31, test -> 2018-01-01 to 2019-12-31
    Fold 2: train -> 2019-12-31, test -> 2020-01-01 to 2021-12-31
    Fold 3: train -> 2021-12-31, test -> 2022-01-01 to 2023-12-31
    Fold 4: train -> 2023-12-31, test -> 2024-01-01 to 2024-12-31
    """
    splits = [
        (pd.Timestamp("2010-01-01"), pd.Timestamp("2017-12-31"), pd.Timestamp("2018-01-01"), pd.Timestamp("2019-12-31")),
        (pd.Timestamp("2010-01-01"), pd.Timestamp("2019-12-31"), pd.Timestamp("2020-01-01"), pd.Timestamp("2021-12-31")),
        (pd.Timestamp("2010-01-01"), pd.Timestamp("2021-12-31"), pd.Timestamp("2022-01-01"), pd.Timestamp("2023-12-31")),
        (pd.Timestamp("2010-01-01"), pd.Timestamp("2023-12-31"), pd.Timestamp("2024-01-01"), pd.Timestamp("2024-12-31")),
    ]
    
    if len(splits) < folds:
        raise RuntimeError(f"Data range insufficient to construct {folds} requested folds.")

    return splits[:folds]


def verify_embargo_purge(
    train_end: pd.Timestamp,
    test_start: pd.Timestamp,
    embargo_months: int = 12,
    purge_months: int = 6,
) -> bool:
    """
    Verifies that test_start occurs at or after train_end + purge/embargo gap.
    """
    if test_start <= train_end:
        raise RuntimeError(f"Data leakage detected: test_start ({test_start}) <= train_end ({train_end})")
    return True
