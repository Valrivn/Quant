"""
ingestion/harness/feature_gate.py — Pre-Registration-Gated Feature Admission

The harness is the enforcement point between "the FRED cache produced
something" and "a model is allowed to train on it". This module wires the
zero-synthetic contract into that boundary so the pre-registration's
``optimization.synthetic_fallback`` flag is actually consulted at runtime.

Why this exists
---------------
The pre-registration encodes the flag as the STRING ``"disabled"``::

    optimization:
      synthetic_fallback: "disabled"

The obvious guard::

    if config.get("synthetic_fallback", False) is False:
        if df[col].isna().any() or (df[col] == 0.0).all():
            raise RuntimeError(...)

never fires for that value, because ``"disabled" is False`` evaluates to
``False`` and the whole body is skipped. The guard silently degrades to a
no-op precisely on the pre-registration that disables synthetic data — the
worst possible failure direction. :func:`SyntheticFallbackPolicy` parses the
flag by value and fails closed.

The gate also enforces the NAME layer: CESI and its aliases are on a permanent
denylist because no authentic public series exists for them and every previous
attempt to populate them was synthetic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, List, Mapping, Optional, Sequence

import pandas as pd

from Quantitative.contracts.feature_contract import (
    CANONICAL_FEATURES,
    FORBIDDEN_SERIES,
    assert_canonical_names,
    assert_no_forbidden_series,
    assert_no_synthetic_data,
)
from Quantitative.data.fred_feed import (
    SyntheticDataViolation,
    assert_authentic_pit,
    synthetic_fallback_enabled,
)

logger = logging.getLogger(__name__)

__all__ = [
    "FeatureGateResult",
    "SyntheticFallbackPolicy",
    "assert_features_admitted",
]


@dataclass(frozen=True)
class FeatureGateResult:
    """Outcome of a feature-admission check."""

    admitted: bool
    features: List[str]
    reason: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return self.admitted


class SyntheticFallbackPolicy:
    """Typed view over a pre-registration's ``synthetic_fallback`` flag."""

    __slots__ = ("_config", "enabled")

    def __init__(self, config: Optional[Mapping[str, Any]] = None):
        self._config = config or {}
        self.enabled = synthetic_fallback_enabled(self._config)

    @classmethod
    def from_prereg(cls, prereg: Any) -> "SyntheticFallbackPolicy":
        """Build from a ``Prereg`` object or a raw mapping.

        ``Prereg`` exposes ``optimization.synthetic_fallback`` via
        ``Prereg.synthetic_fallback``; a plain mapping is read directly.
        """
        getter = getattr(prereg, "synthetic_fallback", None)
        if callable(getter):
            return cls({"optimization": {"synthetic_fallback": getter()}})
        if isinstance(prereg, Mapping):
            return cls(prereg)
        raw = getattr(prereg, "synthetic_fallback", None)
        return cls({"optimization": {"synthetic_fallback": raw}} if raw is not None else {})

    def describe(self) -> str:
        return (
            f"synthetic_fallback={'ENABLED' if self.enabled else 'disabled'} "
            f"(zero-synthetic policy {'WAIVED' if self.enabled else 'ENFORCED'})"
        )


def assert_features_admitted(
    df: pd.DataFrame,
    *,
    prereg: Any = None,
    config: Optional[Mapping[str, Any]] = None,
    expected: Optional[Sequence[str]] = None,
    context: str = "harness",
) -> FeatureGateResult:
    """Admit a feature frame for training, or raise.

    Checks, in order:
      1. The pre-registration's ``synthetic_fallback`` policy (fails closed).
      2. No forbidden series names (CESI & aliases) anywhere in the frame.
      3. Column names match the canonical contract, if ``expected`` is given.
      4. No NaN, no all-zero, no constant columns.

    Returns a :class:`FeatureGateResult` on success; raises
    :class:`~Quantitative.data.fred_feed.SyntheticDataViolation` (a
    ``RuntimeError``) on any violation.

    Example::

        gate = assert_features_admitted(frame, prereg=pre, context="h3_run")
        logger.info("admitted %d features", len(gate.features))
    """
    ctx = f"{context}"

    policy = (
        SyntheticFallbackPolicy.from_prereg(prereg) if prereg is not None
        else SyntheticFallbackPolicy(config)
    )
    if policy.enabled:
        raise SyntheticDataViolation(
            f"FATAL: {ctx} — pre-registration ENABLES synthetic_fallback "
            f"({policy.describe()}). The zero-synthetic policy forbids this. "
            f"Remove the flag from the pre-registration; the canonical feature "
            f"path never honours it."
        )
    logger.debug("%s: %s", ctx, policy.describe())

    cols = [str(c) for c in df.columns]
    assert_no_forbidden_series(cols, ctx)
    if expected is not None:
        assert_canonical_names([str(c) for c in expected], ctx)
    else:
        # No explicit expectation: if this is a canonical feature frame, hold
        # it to the canonical contract rather than accepting a partial set.
        if set(CANONICAL_FEATURES).issubset(set(cols)):
            assert_canonical_names(list(CANONICAL_FEATURES), ctx)

    assert_authentic_pit(df, cols, None, context=ctx)
    assert_no_synthetic_data(df, cols)

    logger.info(
        "Feature gate ADMITTED %d features (%s): %s",
        len(cols), ctx, ", ".join(cols),
    )
    return FeatureGateResult(admitted=True, features=cols)
