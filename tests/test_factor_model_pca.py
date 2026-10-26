"""
test_factor_model_pca.py — Unit tests for Quantitative/factor_model_pca.py (Phase 8 Redesign)
"""

import numpy as np
import pandas as pd
import pytest
from Quantitative.factor_model_pca import FactorModelPCA, generate_pit_factor_data, pre_whiten_ar1, rolling_zscore


def test_factor_matrix_defect1_erp_fix():
    df = generate_pit_factor_data("2020-01-01", "2020-12-31")
    assert "ERP" in df.columns
    assert len(df.columns) == 15

    # Check ERP calculation formula: VIX / sqrt(12) - Rf
    # ERP should be positive generally around 0.01 to 0.08
    assert df["ERP"].mean() > 0.0


def test_pre_whitening_and_rolling_zscore():
    df = generate_pit_factor_data("2020-01-01", "2021-12-31")
    whitened = pre_whiten_ar1(df)
    assert whitened.shape == df.shape

    zscored = rolling_zscore(whitened, lookback=252)
    assert zscored.shape == df.shape
    assert not zscored.isna().any().any()


def test_pca_pipeline():
    df = generate_pit_factor_data("2018-01-01", "2024-12-31")
    pca_pipeline = FactorModelPCA(n_components=15, min_variance_explained=0.85)

    transformed = pca_pipeline.fit_transform(df)
    assert transformed.shape[1] <= 15
    assert np.sum(pca_pipeline.explained_variance_ratio_) >= 0.85
