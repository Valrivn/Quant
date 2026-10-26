"""
test_phase1_integration.py — Integration test for Phase 1 Statistical Core (Phase 8 Redesign)

Writes data/regime/regime_daily_nowcast.parquet for 2018-2024 and verifies Interface 1 schema compliance.
"""

from pathlib import Path
import numpy as np
import pandas as pd
import pytest

from Quantitative.factor_model_pca import generate_pit_factor_data, FactorModelPCA
from Quantitative.regime_shield_v2 import generate_regime_daily_nowcast


def test_phase1_full_pipeline_integration(tmp_path):
    output_file = tmp_path / "regime_daily_nowcast.parquet"

    # 1. Generate 15-factor daily PIT matrix 2018-2024
    factor_df = generate_pit_factor_data("2018-01-01", "2024-12-31")

    # 2. PCA Factor Model
    pca_model = FactorModelPCA(n_components=15, min_variance_explained=0.85)
    pca_factors = pca_model.fit_transform(factor_df)

    # 3. Daily returns series for HAR vol chain
    np.random.seed(42)
    rets = pd.Series(np.random.normal(0.0003, 0.014, len(factor_df)), index=factor_df.index)

    # 4. Generate Daily Regime Nowcast Parquet
    nowcast_df = generate_regime_daily_nowcast(
        factors_df=pca_factors,
        returns_df=rets,
        output_path=str(output_file),
        config_path="config/pre_registration_phase8.yaml"
    )

    # 5. Schema verification
    assert output_file.exists()
    df_read = pd.read_parquet(output_file)

    expected_cols = [
        "timestamp_utc",
        "p_expansion",
        "p_crisis",
        "p_shock",
        "regime_state",
        "vol_har_1d",
        "vol_har_5d",
        "vol_har_22d",
        "regime_switch_flag",
        "hmm_condition_num",
        "sha256_provenance"
    ]

    for col in expected_cols:
        assert col in df_read.columns, f"Missing required Interface 1 schema column: {col}"

    assert len(df_read) > 1000
    assert df_read["regime_state"].isin(["EXPANSION", "SHOCK", "CRISIS"]).all()
    assert (df_read["p_expansion"] >= 0).all() and (df_read["p_expansion"] <= 1).all()
    assert (df_read["p_crisis"] >= 0).all() and (df_read["p_crisis"] <= 1).all()
    assert (df_read["p_shock"] >= 0).all() and (df_read["p_shock"] <= 1).all()
    assert (df_read["vol_har_1d"] > 0).all()
    assert df_read["regime_switch_flag"].isin([0, 1]).all()
    assert not df_read["sha256_provenance"].isna().any()


def generate_production_regime_parquet():
    """Generates production parquet data/regime/regime_daily_nowcast.parquet for 2018-2024."""
    prod_path = Path("data/regime/regime_daily_nowcast.parquet")
    prod_path.parent.mkdir(parents=True, exist_ok=True)

    factor_df = generate_pit_factor_data("2018-01-01", "2024-12-31")
    pca_model = FactorModelPCA(n_components=15, min_variance_explained=0.85)
    pca_factors = pca_model.fit_transform(factor_df)

    np.random.seed(42)
    rets = pd.Series(np.random.normal(0.0003, 0.014, len(factor_df)), index=factor_df.index)

    nowcast_df = generate_regime_daily_nowcast(
        factors_df=pca_factors,
        returns_df=rets,
        output_path=str(prod_path),
        config_path="config/pre_registration_phase8.yaml"
    )
    print(f"Successfully generated {prod_path} with {len(nowcast_df)} rows.")


if __name__ == "__main__":
    generate_production_regime_parquet()
