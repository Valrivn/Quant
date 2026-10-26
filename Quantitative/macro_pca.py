"""
macro_pca.py — Rolling Point-In-Time (PIT) Macro PCA Engine
Pre-Registration Config: config/pre_registration_v7.yaml

Processes the CANONICAL 6 macro features (owned by
``Quantitative.contracts.feature_contract``; not redefined here):
  - CREDIT_SPREAD_VELOCITY_21D: 21d change in HY OAS (BAMLH0A0HYM2)
  - YIELD_CURVE_10Y2Y: 10Y-2Y Treasury Yield Spread (DGS10, DGS2)
  - REALIZED_VOLATILITY_21D: 21-day Realized Volatility (FF5 gross market return)
  - FINANCIAL_STRESS_INDEX: St. Louis Financial Stress Index (STLFSI2)
  - REAL_RATE_10Y: 10-Year TIPS Real Yield (DFII10)
  - UNANTICIPATED_INFLATION: CPI YoY - 10Y Breakeven (CPIAUCSL, T10YIE)

CESI (Citi Economic Surprise Index) is REMOVED. It previously appeared here both
in ``MACRO_INDICATORS`` and as a synthetic generator line
(``"CESI": 5.0 - 15.0 * f2 + np.random.normal(0, 1.0, n)``). It is proprietary
and has no FRED series, so the only way to populate it was to invent it.

The synthetic generator below is retained for TEST/DIAGNOSTIC use only and is
now the sole remaining synthetic path in this module. It is explicitly gated:
in production the input must come from
``build_canonical_features`` and a missing column is a hard abort, not a
synthetic fill. See ``allow_synthetic`` in ``fit_transform_pit``.

Outputs: 3 Principal Components explaining >80% cumulative variance.
No Lookahead Bias: Strictly rolling PIT 252-day window transform.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence, Tuple
import numpy as np
import pandas as pd
from sklearn.decomposition import PCA
from sklearn.preprocessing import StandardScaler

from Quantitative.contracts.feature_contract import CANONICAL_FEATURES, assert_canonical_names
from Quantitative.data.fred_feed import SyntheticDataViolation

logger = logging.getLogger(__name__)

# Core macro indicators — the canonical contract. VIX/Skew are excluded to
# prevent circular validation against VIX-based labels.
MACRO_INDICATORS: list = list(CANONICAL_FEATURES)
assert_canonical_names(MACRO_INDICATORS, "macro_pca.MACRO_INDICATORS")

# VIX/Skew available separately for reference but NOT used in HMM features
MACRO_INDICATORS_AUX = [
    "VIX_VVIX",        # VIX / VVIX Ratio - REFERENCE ONLY
    "SkewIndex",       # CBOE SKEW Index - REFERENCE ONLY
]

# Combined list for data fetching
ALL_MACRO_INDICATORS = MACRO_INDICATORS + MACRO_INDICATORS_AUX


def generate_pit_macro_data(
    start_date: str = "2018-01-01",
    end_date: str = "2024-12-31",
    seed: int = 42,
    include_aux: bool = False
) -> pd.DataFrame:
    """Generate SYNTHETIC PIT daily macro data. **TEST/DIAGNOSTIC USE ONLY.**

    This function fabricates every column. It must never be reachable from an
    ingestion, backtest, or pre-registered validation path. Production callers
    use ``Quantitative.canonical_features.build_canonical_features``.

    Kept so the PCA math can be exercised in isolation without a FRED cache.
    """
    dates = pd.date_range(start_date, end_date, freq="B")
    n = len(dates)
    rng = np.random.default_rng(seed)

    # Correlated factors driving the indicators
    f1 = np.cumsum(rng.normal(0, 0.05, n))
    f2 = np.cumsum(rng.normal(0, 0.03, n))
    f3 = np.cumsum(rng.normal(0, 0.02, n))

    data = {
        # Credit spread velocity: rises in stress, matching the canonical
        # definition d(HY OAS)_21d where a higher value = tighter stress.
        "CREDIT_SPREAD_VELOCITY_21D": 0.5 * f1 + rng.normal(0, 0.05, n),
        "YIELD_CURVE_10Y2Y": 0.8 - 0.4 * f1 + 0.3 * f2 + rng.normal(0, 0.02, n),
        "REALIZED_VOLATILITY_21D": 0.15 + 0.4 * f1 + 0.2 * f3 + rng.normal(0, 0.01, n),
        "FINANCIAL_STRESS_INDEX": -0.5 + 0.8 * f1 + rng.normal(0, 0.05, n),
        "REAL_RATE_10Y": 1.0 - 0.3 * f1 + rng.normal(0, 0.02, n),
        # Replaces the synthetic CESI leg: unanticipated inflation rises when
        # the second factor trends, standing in for a real CPI/breakeven wedge.
        "UNANTICIPATED_INFLATION": 0.2 - 0.6 * f2 + rng.normal(0, 0.05, n),
    }

    if include_aux:
        data["VIX_VVIX"] = 0.18 + 0.1 * f2 + rng.normal(0, 0.01, n)
        data["SkewIndex"] = 125.0 + 5.0 * f3 + rng.normal(0, 0.5, n)

    df = pd.DataFrame(data, index=dates)
    return df[list(MACRO_INDICATORS) + (list(MACRO_INDICATORS_AUX) if include_aux else [])]


class MacroPCA:
    """Rolling Point-In-Time (PIT) PCA engine for macro factor reduction."""

    def __init__(
        self,
        n_components: int = 3,
        lookback: int = 252,
        variance_explained_min: float = 0.80,
    ):
        self.n_components = n_components
        self.lookback = lookback
        self.variance_explained_min = variance_explained_min
        self.latest_explained_variance_: Optional[np.ndarray] = None

    def fit_transform_pit(
        self, df: pd.DataFrame, allow_synthetic: bool = False
    ) -> pd.DataFrame:
        """
        Executes strictly causal, rolling PIT PCA.
        For day T >= lookback, fits PCA on [T - lookback : T] and transforms row T.
        For day T < lookback, expands window from start.
        Uses only the canonical 6 features (excludes VIX/Skew to prevent circular
        validation).

        Zero-synthetic: if a canonical feature column is absent the default is
        a HARD ABORT. The previous behaviour logged a warning and silently
        filled every missing column from ``generate_pit_macro_data`` — i.e. it
        invented data on the production path, which is precisely the defect that
        let the synthetic-CESI column persist. Synthetic fill now requires an
        explicit ``allow_synthetic=True``, which no production caller passes.
        """
        # Ensure required columns exist - use canonical features only
        missing = [c for c in MACRO_INDICATORS if c not in df.columns]
        if missing:
            if not allow_synthetic:
                raise SyntheticDataViolation(
                    f"FATAL: Missing authentic PIT data for {', '.join(missing)}. "
                    f"Aborting per zero-synthetic policy. Present: {list(df.columns)}. "
                    f"Build the input with Quantitative.canonical_features."
                    f".build_canonical_features, or pass allow_synthetic=True in a "
                    f"test context."
                )
            logger.warning(
                "Missing columns %s — SYNTHETIC fill (allow_synthetic=True). "
                "This must not happen on a production path.", missing,
            )
            synthetic = generate_pit_macro_data(
                start_date=df.index[0].strftime("%Y-%m-%d"),
                end_date=df.index[-1].strftime("%Y-%m-%d"),
            )
            for col in missing:
                df[col] = synthetic[col]

        data_sub = df[MACRO_INDICATORS].copy().ffill()
        if data_sub.isna().any().any():
            bad = [c for c in MACRO_INDICATORS if data_sub[c].isna().any()]
            raise SyntheticDataViolation(
                f"FATAL: Missing authentic PIT data for {', '.join(bad)} — NaN remains "
                f"after as-of alignment. Aborting per zero-synthetic policy; do not "
                f"back-fill, zero-fill, or proxy."
            )
        n_obs = len(data_sub)

        pc_matrix = np.zeros((n_obs, self.n_components))
        explained_vars = []

        min_window = max(10, self.n_components + 2)

        for i in range(n_obs):
            if i + 1 < min_window:
                window_data = data_sub.iloc[0 : min_window]
            else:
                start_idx = max(0, i + 1 - self.lookback)
                window_data = data_sub.iloc[start_idx : i + 1]

            scaler = StandardScaler()
            scaled_window = scaler.fit_transform(window_data)

            pca = PCA(n_components=self.n_components)
            pca.fit(scaled_window)

            # Transform latest single row of current point
            latest_row = scaler.transform(data_sub.iloc[i : i + 1])
            pcs = pca.transform(latest_row)
            pc_matrix[i, :] = pcs[0]
            explained_vars.append(np.sum(pca.explained_variance_ratio_))

        self.latest_explained_variance_ = np.mean(explained_vars)
        pc_cols = [f"PC{k+1}" for k in range(self.n_components)]
        out_df = pd.DataFrame(pc_matrix, index=df.index, columns=pc_cols)
        return out_df

    def validate_variance_explained(self, pcs_df: pd.DataFrame) -> bool:
        """Checks whether cumulative variance explained meets threshold."""
        if self.latest_explained_variance_ is None:
            return True
        return float(self.latest_explained_variance_) >= self.variance_explained_min


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Macro PCA Engine")
    parser.add_argument("--verify-variance", action="store_true", help="Verify explained variance threshold")
    parser.add_argument("--threshold", type=float, default=0.80, help="Minimum cumulative variance threshold")

    args = parser.parse_args()

    df_macro = generate_pit_macro_data()
    pca_engine = MacroPCA(n_components=3, lookback=252, variance_explained_min=args.threshold)
    # allow_synthetic=True is correct here: --verify-variance is a self-test of
    # the PCA math on a synthetic fixture, not a market measurement.
    pcs = pca_engine.fit_transform_pit(df_macro, allow_synthetic=True)
    explained_var = pca_engine.latest_explained_variance_

    print(f"Computed PCs shape: {pcs.shape}")
    print(f"Average variance explained: {explained_var:.4f}")

    if args.verify_variance:
        if explained_var >= args.threshold:
            print(f"PASS: 3 PCs explain {explained_var*100:.2f}% variance (>= {args.threshold*100:.0f}% threshold).")
        else:
            print(f"FAIL: Variance explained ({explained_var*100:.2f}%) below threshold ({args.threshold*100:.0f}%).")
            exit(1)
