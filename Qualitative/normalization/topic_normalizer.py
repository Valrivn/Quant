"""
Polarity-Aligned Topic Normalizer for Multi-Sector Qualitative Pipeline.

Implements robust cross-sectional normalization using median and MAD
(Median Absolute Deviation * 1.4826), maps features into [-1, 1] via
hyperbolic tangent projection:
    u = polarity * np.tanh(z / 2.0)
and aggregates normalized features into the 5 Thematic Pillars:
    1. rnd_innovation
    2. operational_health
    3. consumer_mindshare
    4. safety_regulatory
    5. governance_conviction
"""

from typing import Dict, List, Optional, Union
import numpy as np
import pandas as pd


# Default polarity dictionary: positive (+1.0) or negative (-1.0)
DEFAULT_POLARITY_DICT: Dict[str, float] = {
    # R&D / Innovation
    "patents": 1.0,
    "patent_citations": 1.0,
    "github_commits": 1.0,
    "github_prs": 1.0,
    "hf_downloads": 1.0,
    "huggingface_downloads": 1.0,
    "clinical_trials": 1.0,
    "fda_approvals": 1.0,
    "dev_traction": 1.0,
    # Operational Health
    "contract_backlog": 1.0,
    "usaspending_backlog": 1.0,
    "fdic_margins": 1.0,
    "rpo_backlog": 1.0,
    "operating_cash_flow": 1.0,
    # Consumer / Mindshare
    "wiki_views": 1.0,
    "wikimedia_views": 1.0,
    "mindshare": 1.0,
    "reddit_mentions": 1.0,
    "stocktwits_volume": 1.0,
    # Safety / Regulatory (Negative indicators -> -1.0)
    "cfpb_complaints": -1.0,
    "nhtsa_recalls": -1.0,
    "fda_adverse_events": -1.0,
    "osha_violations": -1.0,
    "mda_litigious": -1.0,
    "mda_uncertainty": -1.0,
    "antitrust_cases": -1.0,
    # Governance / Conviction
    "insider_buys": 1.0,
    "form4_insider_buys": 1.0,
    "sec_13f_inflows": 1.0,
    "institutional_inflows": 1.0,
    "governance_score": 1.0,
}

# Thematic Pillar definitions mapping to raw feature names
DEFAULT_THEMATIC_PILLARS: Dict[str, List[str]] = {
    "rnd_innovation": [
        "patents",
        "patent_citations",
        "github_commits",
        "github_prs",
        "hf_downloads",
        "huggingface_downloads",
        "clinical_trials",
        "fda_approvals",
        "dev_traction",
    ],
    "operational_health": [
        "contract_backlog",
        "usaspending_backlog",
        "fdic_margins",
        "rpo_backlog",
        "operating_cash_flow",
    ],
    "consumer_mindshare": [
        "wiki_views",
        "wikimedia_views",
        "mindshare",
        "reddit_mentions",
        "stocktwits_volume",
    ],
    "safety_regulatory": [
        "cfpb_complaints",
        "nhtsa_recalls",
        "fda_adverse_events",
        "osha_violations",
        "mda_litigious",
        "mda_uncertainty",
        "antitrust_cases",
    ],
    "governance_conviction": [
        "insider_buys",
        "form4_insider_buys",
        "sec_13f_inflows",
        "institutional_inflows",
        "governance_score",
    ],
}


class TopicNormalizer:
    """
    Robust Z-Score and Polarity-Aligned Bounded Projection Engine.
    """

    MAD_SCALE_FACTOR = 1.4826

    def __init__(
        self,
        polarity_dict: Optional[Dict[str, float]] = None,
        thematic_pillars: Optional[Dict[str, List[str]]] = None,
        eps: float = 1e-6,
    ):
        """
        Initialize TopicNormalizer.

        Args:
            polarity_dict: Dict mapping feature names to +1.0 (positive) or -1.0 (negative).
            thematic_pillars: Dict mapping 5 pillar names to list of component features.
            eps: Epsilon to prevent division by zero when MAD is 0.
        """
        self.polarity_dict = dict(DEFAULT_POLARITY_DICT)
        if polarity_dict:
            self.polarity_dict.update(polarity_dict)

        self.thematic_pillars = dict(DEFAULT_THEMATIC_PILLARS)
        if thematic_pillars:
            self.thematic_pillars.update(thematic_pillars)

        self.eps = eps

    def compute_robust_z(
        self,
        data: Union[pd.Series, np.ndarray, pd.DataFrame],
    ) -> Union[pd.Series, np.ndarray, pd.DataFrame]:
        """
        Compute cross-sectional robust Z-score using Median and MAD * 1.4826.
        z = (x - median) / (MAD * 1.4826)
        """
        if isinstance(data, pd.DataFrame):
            med = data.median(axis=0)
            mad = (data - med).abs().median(axis=0) * self.MAD_SCALE_FACTOR
            mad = mad.replace(0.0, self.eps)
            return (data - med) / (mad + self.eps)
        elif isinstance(data, pd.Series):
            med = data.median()
            mad = (data - med).abs().median() * self.MAD_SCALE_FACTOR
            if mad == 0.0 or np.isnan(mad):
                mad = self.eps
            return (data - med) / (mad + self.eps)
        else:
            arr = np.asarray(data, dtype=float)
            med = np.nanmedian(arr, axis=0)
            mad = np.nanmedian(np.abs(arr - med), axis=0) * self.MAD_SCALE_FACTOR
            mad = np.where(mad == 0.0, self.eps, mad)
            return (arr - med) / (mad + self.eps)

    def normalize_feature(
        self,
        values: Union[pd.Series, np.ndarray],
        feature_name: str,
        custom_polarity: Optional[float] = None,
    ) -> Union[pd.Series, np.ndarray]:
        """
        Compute polarity-aligned bounded score for a feature:
            z = robust_z(values)
            u = polarity * np.tanh(z / 2.0)
        """
        polarity = (
            custom_polarity
            if custom_polarity is not None
            else self.polarity_dict.get(feature_name, 1.0)
        )
        z = self.compute_robust_z(values)
        return polarity * np.tanh(z / 2.0)

    def normalize_dataframe(
        self,
        df: pd.DataFrame,
    ) -> pd.DataFrame:
        """
        Normalizes all columns in a dataframe according to their polarity.
        Non-numeric columns or columns with all NaNs are preserved or handled cleanly.
        """
        result = pd.DataFrame(index=df.index)
        for col in df.columns:
            if pd.api.types.is_numeric_dtype(df[col]):
                result[col] = self.normalize_feature(df[col], col)
            else:
                result[col] = df[col]
        return result

    def aggregate_pillars(
        self,
        normalized_df: pd.DataFrame,
    ) -> pd.DataFrame:
        """
        Aggregates normalized features into the 5 Thematic Pillars:
          - rnd_innovation
          - operational_health
          - consumer_mindshare
          - safety_regulatory
          - governance_conviction

        Each pillar score is the mean across available, non-NaN normalized component features.
        If none of the component features exist or all are NaN, returns 0.0 for that pillar.
        """
        pillars_df = pd.DataFrame(index=normalized_df.index)

        for pillar_name, features in self.thematic_pillars.items():
            matching_cols = [f for f in features if f in normalized_df.columns]
            if matching_cols:
                sub = normalized_df[matching_cols]
                # Mean across available columns ignoring NaN
                pillar_series = sub.mean(axis=1, skipna=True)
                pillars_df[pillar_name] = pillar_series.fillna(0.0)
            else:
                pillars_df[pillar_name] = 0.0

        return pillars_df

    def transform(
        self,
        raw_df: pd.DataFrame,
    ) -> pd.DataFrame:
        """
        Full pipeline: Raw features -> Robust Polarity Normalization -> Thematic Pillars DataFrame.
        """
        norm_df = self.normalize_dataframe(raw_df)
        return self.aggregate_pillars(norm_df)
