"""
Unit tests for QualClusterEngine.
"""

import numpy as np
import pandas as pd
import pytest

from Qualitative.clustering.qual_cluster_engine import (
    QualClusterEngine,
    ARCHETYPE_MULTIPLIERS,
    ARCHETYPE_IDEALS,
    REQUIRED_PILLARS,
)


def _generate_synthetic_universe():
    """
    Generate synthetic companies distinctly representing the 4 archetypes:
    - Innovators (high R&D, clean safety)
    - Compounders (moderate R&D, positive governance/safety/ops)
    - Hype_Traps (high mindshare, low R&D)
    - High_Friction_Distress (depressed safety, high friction)
    """
    data = []

    # Innovators
    for _ in range(8):
        data.append({
            "ticker": f"INO_{_}",
            "rnd_innovation": np.random.uniform(0.4, 0.8),
            "operational_health": np.random.uniform(0.1, 0.4),
            "consumer_mindshare": np.random.uniform(0.2, 0.5),
            "safety_regulatory": np.random.uniform(0.1, 0.4),
            "governance_conviction": np.random.uniform(0.1, 0.4),
        })

    # Compounders
    for _ in range(8):
        data.append({
            "ticker": f"CMP_{_}",
            "rnd_innovation": np.random.uniform(0.05, 0.20),
            "operational_health": np.random.uniform(0.4, 0.7),
            "consumer_mindshare": np.random.uniform(0.0, 0.2),
            "safety_regulatory": np.random.uniform(0.3, 0.6),
            "governance_conviction": np.random.uniform(0.3, 0.6),
        })

    # Hype Traps
    for _ in range(8):
        data.append({
            "ticker": f"HYP_{_}",
            "rnd_innovation": np.random.uniform(-0.5, -0.2),
            "operational_health": np.random.uniform(-0.3, 0.0),
            "consumer_mindshare": np.random.uniform(0.5, 0.9),
            "safety_regulatory": np.random.uniform(-0.1, 0.2),
            "governance_conviction": np.random.uniform(-0.3, 0.0),
        })

    # High Friction Distress
    for _ in range(8):
        data.append({
            "ticker": f"DIS_{_}",
            "rnd_innovation": np.random.uniform(-0.4, 0.0),
            "operational_health": np.random.uniform(-0.7, -0.3),
            "consumer_mindshare": np.random.uniform(-0.3, 0.1),
            "safety_regulatory": np.random.uniform(-0.8, -0.3),
            "governance_conviction": np.random.uniform(-0.6, -0.2),
        })

    df = pd.DataFrame(data).set_index("ticker")
    return df


def test_qual_cluster_engine_archetypes():
    df = _generate_synthetic_universe()
    engine = QualClusterEngine(n_clusters=4, random_state=42)
    engine.fit(df)

    # Check fitted centroids
    assert engine.fitted_centroids_ is not None
    assert len(engine.fitted_centroids_) == 4

    # Check mapping contains all 4 archetypes
    assigned_archetypes = set(engine.cluster_to_archetype.values())
    expected_archetypes = {"Innovator", "Compounder", "Hype_Trap", "High_Friction_Distress"}
    assert assigned_archetypes == expected_archetypes

    # Predict archetypes
    arch_df = engine.predict_archetypes(df)
    assert len(arch_df) == len(df)
    assert set(arch_df.columns) == {"cluster", "archetype", "multiplier"}

    # Verify multipliers
    for idx, row in arch_df.iterrows():
        expected_mult = ARCHETYPE_MULTIPLIERS[row["archetype"]]
        assert row["multiplier"] == expected_mult

    # Check that INO tickers get Innovator archetype with 1.20 multiplier
    ino_archetypes = arch_df.loc[[f"INO_{i}" for i in range(8)], "archetype"].values
    assert (ino_archetypes == "Innovator").all()

    # Check that HYP tickers get Hype_Trap archetype with 0.70 multiplier
    hyp_archetypes = arch_df.loc[[f"HYP_{i}" for i in range(8)], "archetype"].values
    assert (hyp_archetypes == "Hype_Trap").all()

    # Check that DIS tickers get High_Friction_Distress archetype with 0.50 multiplier
    dis_archetypes = arch_df.loc[[f"DIS_{i}" for i in range(8)], "archetype"].values
    assert (dis_archetypes == "High_Friction_Distress").all()


def test_composite_score_computation():
    df = _generate_synthetic_universe()
    engine = QualClusterEngine(n_clusters=4, random_state=42)
    result = engine.fit_transform(df)

    assert "raw_composite" in result.columns
    assert "multiplier" in result.columns
    assert "qual_score" in result.columns
    assert "archetype" in result.columns

    # Verify Q_i = M_k * raw_composite
    expected_qual_score = result["multiplier"] * result["raw_composite"]
    np.testing.assert_allclose(result["qual_score"].values, expected_qual_score.values, rtol=1e-5)

    # Innovator multiplier = 1.20
    ino_rows = result[result["archetype"] == "Innovator"]
    assert (ino_rows["multiplier"] == 1.20).all()

    # Compounder multiplier = 1.00
    cmp_rows = result[result["archetype"] == "Compounder"]
    assert (cmp_rows["multiplier"] == 1.00).all()

    # Hype Trap multiplier = 0.70
    hyp_rows = result[result["archetype"] == "Hype_Trap"]
    assert (hyp_rows["multiplier"] == 0.70).all()

    # High Friction Distress multiplier = 0.50
    dis_rows = result[result["archetype"] == "High_Friction_Distress"]
    assert (dis_rows["multiplier"] == 0.50).all()
