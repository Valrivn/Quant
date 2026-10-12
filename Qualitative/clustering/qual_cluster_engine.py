"""
Qualitative Clustering Engine for Multi-Sector Pipeline.

Implements cross-sectional K-Means behavioral clustering with fixed K=4 archetypes:
    - Innovator: High R&D/tech (>0.2), clean safety (>=0.0) -> Multiplier M_k = 1.20
    - Compounder: Moderate R&D, positive governance/safety -> Multiplier M_k = 1.00
    - Hype_Trap: High retail mindshare (>0.3), low R&D (<-0.1) -> Multiplier M_k = 0.70
    - High_Friction_Distress: Depressed safety (<-0.2), high complaints -> Multiplier M_k = 0.50

Computes composite qualitative score:
    Q_i = M_k * raw_composite
"""

from typing import Dict, List, Optional, Tuple, Union
import numpy as np
import pandas as pd
from sklearn.cluster import KMeans


# Empirical archetype definitions and their multipliers M_k
ARCHETYPE_MULTIPLIERS: Dict[str, float] = {
    "Innovator": 1.20,
    "Compounder": 1.00,
    "Hype_Trap": 0.70,
    "High_Friction_Distress": 0.50,
}

# Archetype target profiles in feature space (rnd_innovation, operational_health, consumer_mindshare, safety_regulatory, governance_conviction)
# Used for minimum distance centroid assignment when labeling K-means clusters
ARCHETYPE_IDEALS: Dict[str, Dict[str, float]] = {
    "Innovator": {
        "rnd_innovation": 0.60,
        "operational_health": 0.30,
        "consumer_mindshare": 0.30,
        "safety_regulatory": 0.30,
        "governance_conviction": 0.30,
    },
    "Compounder": {
        "rnd_innovation": 0.15,
        "operational_health": 0.50,
        "consumer_mindshare": 0.10,
        "safety_regulatory": 0.40,
        "governance_conviction": 0.40,
    },
    "Hype_Trap": {
        "rnd_innovation": -0.40,
        "operational_health": -0.20,
        "consumer_mindshare": 0.60,
        "safety_regulatory": 0.00,
        "governance_conviction": -0.20,
    },
    "High_Friction_Distress": {
        "rnd_innovation": -0.20,
        "operational_health": -0.50,
        "consumer_mindshare": -0.10,
        "safety_regulatory": -0.60,
        "governance_conviction": -0.40,
    },
}

REQUIRED_PILLARS = [
    "rnd_innovation",
    "operational_health",
    "consumer_mindshare",
    "safety_regulatory",
    "governance_conviction",
]


class QualClusterEngine:
    """
    Cross-Sectional K-Means Behavioral Clustering Engine.
    Segments stocks into K=4 economic archetypes and scales raw composite scores.
    """

    def __init__(
        self,
        n_clusters: int = 4,
        random_state: int = 42,
        pillar_weights: Optional[Dict[str, float]] = None,
    ):
        """
        Initialize the clustering engine.

        Args:
            n_clusters: Number of clusters (fixed to 4 per specification).
            random_state: Random state for deterministic K-Means fitting.
            pillar_weights: Optional weighting for pillars in raw_composite computation.
        """
        self.n_clusters = n_clusters
        self.random_state = random_state
        self.kmeans: Optional[KMeans] = None
        self.cluster_to_archetype: Dict[int, str] = {}
        self.fitted_centroids_: Optional[pd.DataFrame] = None

        if pillar_weights is None:
            # Equal weighting by default across the 5 pillars
            self.pillar_weights = {p: 0.20 for p in REQUIRED_PILLARS}
        else:
            self.pillar_weights = pillar_weights

    def _rule_score_archetype(
        self,
        c_row: pd.Series,
        archetype: str,
    ) -> float:
        """
        Calculates rule-alignment bonus / score for an archetype.
        Higher score means centroid c_row aligns better with the archetype specification:
          - Innovator: High R&D/tech (>0.2), clean safety (>=0.0)
          - Compounder: Moderate R&D, positive governance/safety
          - Hype_Trap: High retail mindshare (>0.3), low R&D (<-0.1)
          - High_Friction_Distress: Depressed safety (<-0.2), high complaints
        """
        rnd = c_row.get("rnd_innovation", 0.0)
        safety = c_row.get("safety_regulatory", 0.0)
        mindshare = c_row.get("consumer_mindshare", 0.0)
        gov = c_row.get("governance_conviction", 0.0)
        ops = c_row.get("operational_health", 0.0)

        if archetype == "Innovator":
            score = 2.0 * rnd + 1.0 * safety + 0.5 * ops
            if rnd > 0.2:
                score += 1.0
            if safety >= 0.0:
                score += 0.5
            return score

        elif archetype == "Compounder":
            score = 1.0 * gov + 1.0 * safety + 1.0 * ops + 0.2 * rnd
            if safety >= 0.0:
                score += 0.5
            if gov >= 0.0:
                score += 0.5
            return score

        elif archetype == "Hype_Trap":
            score = 2.0 * mindshare - 2.0 * rnd - 0.5 * ops
            if mindshare > 0.3:
                score += 1.0
            if rnd < -0.1:
                score += 1.0
            return score

        elif archetype == "High_Friction_Distress":
            score = -2.5 * safety - 1.5 * ops - 1.0 * gov
            if safety < -0.2:
                score += 1.5
            return score

        return 0.0

    def _match_centroids_to_archetypes(
        self,
        centroids: pd.DataFrame,
    ) -> Dict[int, str]:
        """
        Bipartite matching of K=4 cluster centroids to the 4 empirical archetypes.
        Combines rule criteria matching and ideal profile euclidean distance.
        """
        archetypes = list(ARCHETYPE_MULTIPLIERS.keys())
        k_count = len(centroids)

        if k_count != len(archetypes):
            # Fallback if n_clusters != 4
            return {i: archetypes[i % len(archetypes)] for i in range(k_count)}

        # Cost matrix: lower is better
        # cost = Euclidean distance to ideal - rule alignment score
        cost_matrix = np.zeros((k_count, len(archetypes)))
        for i in range(k_count):
            c_vec = centroids.iloc[i]
            for j, arch in enumerate(archetypes):
                ideal_vec = pd.Series(ARCHETYPE_IDEALS[arch])
                # Ensure align on pillars
                diff = [c_vec.get(p, 0.0) - ideal_vec.get(p, 0.0) for p in REQUIRED_PILLARS]
                dist = np.sqrt(np.sum(np.square(diff)))
                rule_bonus = self._rule_score_archetype(c_vec, arch)
                cost_matrix[i, j] = dist - 0.5 * rule_bonus

        # Greedy or Hungarian assignment for 4x4
        # Since 4! = 24 permutations, find exact optimal permutation minimizing total cost
        from itertools import permutations

        best_perm = None
        min_total_cost = float("inf")

        for perm in permutations(range(len(archetypes))):
            total_cost = sum(cost_matrix[i, perm[i]] for i in range(k_count))
            if total_cost < min_total_cost:
                min_total_cost = total_cost
                best_perm = perm

        mapping = {}
        for i in range(k_count):
            arch_idx = best_perm[i]
            mapping[i] = archetypes[arch_idx]

        return mapping

    def fit(self, pillars_df: pd.DataFrame) -> "QualClusterEngine":
        """
        Fit K-Means clustering on the cross-sectional pillars.

        Args:
            pillars_df: DataFrame containing at least the 5 thematic pillars.
        """
        features = [col for col in REQUIRED_PILLARS if col in pillars_df.columns]
        if len(features) < len(REQUIRED_PILLARS):
            # Missing some pillars, create them with 0.0
            for p in REQUIRED_PILLARS:
                if p not in pillars_df.columns:
                    pillars_df = pillars_df.copy()
                    pillars_df[p] = 0.0
            features = REQUIRED_PILLARS

        X = pillars_df[features].fillna(0.0).values
        n_samples = len(X)
        actual_k = min(self.n_clusters, n_samples)

        self.kmeans = KMeans(
            n_clusters=actual_k,
            random_state=self.random_state,
            n_init=10,
        )
        self.kmeans.fit(X)

        self.fitted_centroids_ = pd.DataFrame(
            self.kmeans.cluster_centers_,
            columns=features,
            index=range(actual_k),
        )

        self.cluster_to_archetype = self._match_centroids_to_archetypes(
            self.fitted_centroids_
        )
        return self

    def predict_archetypes(
        self,
        pillars_df: pd.DataFrame,
    ) -> pd.DataFrame:
        """
        Predict cluster labels and map to archetypes and multipliers for each stock.

        Returns DataFrame with columns:
          - cluster: int
          - archetype: str
          - multiplier: float (M_k)
        """
        if self.kmeans is None:
            raise RuntimeError("QualClusterEngine must be fitted before predict.")

        features = [p for p in REQUIRED_PILLARS if p in pillars_df.columns]
        for p in REQUIRED_PILLARS:
            if p not in pillars_df.columns:
                pillars_df = pillars_df.copy()
                pillars_df[p] = 0.0
        features = REQUIRED_PILLARS

        X = pillars_df[features].fillna(0.0).values
        cluster_labels = self.kmeans.predict(X)

        archetype_labels = [self.cluster_to_archetype.get(c, "Compounder") for c in cluster_labels]
        multipliers = [ARCHETYPE_MULTIPLIERS.get(a, 1.0) for a in archetype_labels]

        return pd.DataFrame(
            {
                "cluster": cluster_labels,
                "archetype": archetype_labels,
                "multiplier": multipliers,
            },
            index=pillars_df.index,
        )

    def compute_composite_scores(
        self,
        pillars_df: pd.DataFrame,
        archetypes_df: Optional[pd.DataFrame] = None,
    ) -> pd.DataFrame:
        """
        Computes raw composite and final composite qualitative score:
            raw_composite = sum(w_p * pillar_p)
            Q_i = M_k * raw_composite

        Returns DataFrame with:
          - raw_composite: float
          - multiplier: float (M_k)
          - archetype: str
          - qual_score: float (Q_i)
        """
        if archetypes_df is None:
            archetypes_df = self.predict_archetypes(pillars_df)

        # Weighted raw composite
        raw_composite = pd.Series(0.0, index=pillars_df.index)
        for pillar, weight in self.pillar_weights.items():
            if pillar in pillars_df.columns:
                raw_composite += weight * pillars_df[pillar].fillna(0.0)

        multiplier = archetypes_df["multiplier"]
        qual_score = multiplier * raw_composite

        result = pd.DataFrame(
            {
                "raw_composite": raw_composite,
                "multiplier": multiplier,
                "archetype": archetypes_df["archetype"],
                "qual_score": qual_score,
            },
            index=pillars_df.index,
        )
        return result

    def fit_transform(
        self,
        pillars_df: pd.DataFrame,
    ) -> pd.DataFrame:
        """
        Convenience method to fit, predict, and compute composite scores in one pass.
        Returns pillars_df joined with archetypes, multiplier, raw_composite, and qual_score.
        """
        self.fit(pillars_df)
        arch_df = self.predict_archetypes(pillars_df)
        scores_df = self.compute_composite_scores(pillars_df, arch_df)
        return pd.concat([pillars_df, arch_df[["cluster"]], scores_df], axis=1)
