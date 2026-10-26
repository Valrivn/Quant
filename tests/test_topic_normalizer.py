"""
Unit tests for TopicNormalizer.
"""

import numpy as np
import pandas as pd
import pytest

from Qualitative.normalization.topic_normalizer import (
    TopicNormalizer,
    DEFAULT_POLARITY_DICT,
    DEFAULT_THEMATIC_PILLARS,
)


def test_robust_z_scoring():
    normalizer = TopicNormalizer()
    # Simple symmetric array: [10, 20, 30, 40, 50]
    # Median = 30. Absolute diffs: [20, 10, 0, 10, 20] -> Median Absolute Deviation = 10.
    # Scaled MAD = 10 * 1.4826 = 14.826.
    s = pd.Series([10.0, 20.0, 30.0, 40.0, 50.0])
    z = normalizer.compute_robust_z(s)

    assert pytest.approx(z.iloc[2], abs=1e-5) == 0.0
    expected_z4 = (50.0 - 30.0) / (10.0 * 1.4826)
    assert pytest.approx(z.iloc[4], rel=1e-3) == expected_z4
    expected_z0 = (10.0 - 30.0) / (10.0 * 1.4826)
    assert pytest.approx(z.iloc[0], rel=1e-3) == expected_z0


def test_tanh_bounding_and_polarity():
    normalizer = TopicNormalizer()
    
    # Positive feature: patents (polarity = +1.0)
    # Negative feature: cfpb_complaints (polarity = -1.0)
    data = pd.DataFrame({
        "patents": [1.0, 5.0, 10.0, 50.0, 1000.0],
        "cfpb_complaints": [1.0, 5.0, 10.0, 50.0, 1000.0],
    })

    norm_df = normalizer.normalize_dataframe(data)

    # All values must strictly reside in [-1.0, 1.0]
    assert (norm_df["patents"] >= -1.0).all() and (norm_df["patents"] <= 1.0).all()
    assert (norm_df["cfpb_complaints"] >= -1.0).all() and (norm_df["cfpb_complaints"] <= 1.0).all()

    # High patents should have positive score
    assert norm_df["patents"].iloc[-1] > 0.5
    # Low patents should have negative score (median is 10.0, 1.0 is below median)
    assert norm_df["patents"].iloc[0] < -0.2

    # Reverse polarity for complaints: High complaints should be strongly negative
    assert norm_df["cfpb_complaints"].iloc[-1] < -0.5
    # Low complaints should be positive (clean track record)
    assert norm_df["cfpb_complaints"].iloc[0] > 0.2


def test_polarity_dictionary_mappings():
    normalizer = TopicNormalizer()

    # Test key positive signals
    positives = ["patents", "github_commits", "hf_downloads", "wiki_views", "insider_buys", "sec_13f_inflows", "contract_backlog"]
    for p in positives:
        assert normalizer.polarity_dict.get(p) == 1.0, f"Expected positive polarity for {p}"

    # Test key negative signals
    negatives = ["cfpb_complaints", "nhtsa_recalls", "fda_adverse_events", "osha_violations", "mda_litigious", "mda_uncertainty"]
    for n in negatives:
        assert normalizer.polarity_dict.get(n) == -1.0, f"Expected negative polarity for {n}"


def test_aggregate_pillars():
    normalizer = TopicNormalizer()

    # Construct synthetic data across sectors
    raw_df = pd.DataFrame({
        "patents": [10.0, 50.0, 200.0, 1000.0],
        "github_commits": [100.0, 200.0, 500.0, 10000.0],
        "contract_backlog": [5.0, 20.0, 10.0, 50.0],
        "wiki_views": [1000.0, 5000.0, 20000.0, 100000.0],
        "cfpb_complaints": [0.0, 2.0, 10.0, 100.0],
        "insider_buys": [0.0, 1.0, 5.0, 10.0],
    })

    pillars = normalizer.transform(raw_df)

    expected_pillars = [
        "rnd_innovation",
        "operational_health",
        "consumer_mindshare",
        "safety_regulatory",
        "governance_conviction",
    ]
    for ep in expected_pillars:
        assert ep in pillars.columns, f"Missing pillar {ep}"

    # Verify that all pillar values lie in [-1, 1]
    assert (pillars >= -1.0).all().all()
    assert (pillars <= 1.0).all().all()

    # Stock 3 (index 3) has huge patents, huge commits -> rnd_innovation should be strongly positive
    assert pillars.loc[3, "rnd_innovation"] > 0.4
    # Stock 3 has highest complaints -> safety_regulatory should be negative
    assert pillars.loc[3, "safety_regulatory"] < -0.4
