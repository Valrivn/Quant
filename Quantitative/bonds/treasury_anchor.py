#!/usr/bin/env python3
"""
treasury_anchor.py — SHY/BIL Defensive Overlay

Implements the defensive fallback logic for bond ETF selection. When no
corporate bond ETF passes both Z-score gates in the bond_etf_screener,
this module auto-defaults the bond allocation to short-term U.S. Treasury
ETFs (BIL, SHY).

Damodaran Principle: In a credit crisis, the only truly safe haven is
sovereign U.S. government paper. When corporate credit quality cannot
be verified, capital flows to the shortest-duration government bonds.

The treasury_anchor also applies a dynamic weighting between BIL (1-3 month)
and SHY (1-3 year) based on the credit spread regime:
  - NORMAL: 60% SHY / 40% BIL (capture slight term premium)
  - WIDENING: 40% SHY / 60% BIL (shorten duration)
  - CRISIS: 20% SHY / 80% BIL (maximum safety)
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from Quantitative.bonds.credit_spread_monitor import CreditSpreadMonitor, SpreadRegime

logger = logging.getLogger(__name__)


class AnchorMode(Enum):
    """How the treasury anchor is deployed."""
    FULL_ANCHOR = "FULL_ANCHOR"      # No corporate ETFs passed — full defensive
    PARTIAL_ANCHOR = "PARTIAL_ANCHOR"  # Some corporate ETFs passed, partial defense
    NO_ANCHOR = "NO_ANCHOR"          # Corporate ETFs selected, no anchor needed


@dataclass
class AnchorAllocation:
    """Treasury allocation from the defensive overlay."""
    ticker: str
    weight: float
    rationale: str


@dataclass
class TreasuryAnchorResult:
    """Complete treasury anchor analysis and allocation."""
    mode: AnchorMode
    allocations: List[AnchorAllocation]
    spread_regime: SpreadRegime
    corporate_selected: List[str]
    corporate_rejected: List[str]
    explanation: str = ""
    evaluated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def allocation_dict(self) -> Dict[str, float]:
        """Convert to ticker -> weight dict for downstream consumers."""
        return {a.ticker: a.weight for a in self.allocations}

    def to_dict(self) -> Dict:
        return {
            "mode": self.mode.value,
            "allocations": {a.ticker: a.weight for a in self.allocations},
            "spread_regime": self.spread_regime.value,
            "corporate_selected": self.corporate_selected,
            "corporate_rejected": self.corporate_rejected,
            "explanation": self.explanation,
        }


# Treasury allocation profiles per credit regime
REGIME_PROFILES = {
    SpreadRegime.NORMAL: {"BIL": 0.40, "SHY": 0.60},
    SpreadRegime.WIDENING: {"BIL": 0.60, "SHY": 0.40},
    SpreadRegime.CRISIS: {"BIL": 0.80, "SHY": 0.20},
    SpreadRegime.UNKNOWN: {"BIL": 0.50, "SHY": 0.50},
}


class TreasuryAnchor:
    """
    Defensive overlay that defaults bond allocation to U.S. Treasuries
    when corporate credit quality cannot be verified.

    Usage:
        anchor = TreasuryAnchor()
        result = anchor.deploy(
            corporate_selected=["VCSH"],
            corporate_rejected=["VCIT"],
            bond_budget=0.30,
        )
        # result.allocation_dict -> {"BIL": 0.12, "SHY": 0.18}
    """

    def __init__(self, credit_monitor: Optional[CreditSpreadMonitor] = None):
        self._credit_monitor = credit_monitor or CreditSpreadMonitor()

    def deploy(
        self,
        corporate_selected: List[str],
        corporate_rejected: List[str],
        bond_budget: float = 0.30,
        force_refresh: bool = False,
    ) -> TreasuryAnchorResult:
        """
        Deploy the treasury anchor based on corporate screening results.

        Args:
            corporate_selected: Corporate ETFs that passed Z-score gates
            corporate_rejected: Corporate ETFs that failed Z-score gates
            bond_budget: Total bond allocation percentage (e.g., 0.30 = 30%)
            force_refresh: Skip FRED cache if True

        Returns:
            TreasuryAnchorResult with mode, allocations, and explanation.
        """
        spread_result = self._credit_monitor.fetch_and_classify(force_refresh=force_refresh)
        regime = spread_result.regime
        profile = REGIME_PROFILES[regime]

        if not corporate_selected:
            # FULL ANCHOR: No corporate ETFs passed
            mode = AnchorMode.FULL_ANCHOR
            allocations = [
                AnchorAllocation(
                    ticker=ticker,
                    weight=weight * bond_budget,
                    rationale=f"Full anchor: {regime.value} regime, no corporate ETFs passed Z-score gates",
                )
                for ticker, weight in profile.items()
            ]
            explanation = (
                f"FULL ANCHOR: No corporate bond ETFs passed both Z-score gates. "
                f"100% of bond budget ({bond_budget:.0%}) allocated to U.S. Treasuries. "
                f"Regime: {regime.value}, Spread: {spread_result.current_spread_bps:.0f} bps."
            )

        elif len(corporate_rejected) > 0:
            # PARTIAL ANCHOR: Some corporate, some treasury
            mode = AnchorMode.PARTIAL_ANCHOR
            corporate_weight = len(corporate_selected) / (len(corporate_selected) + len(corporate_rejected))
            treasury_budget = bond_budget * (1.0 - corporate_weight)

            allocations = [
                AnchorAllocation(
                    ticker=ticker,
                    weight=weight * treasury_budget,
                    rationale=f"Partial anchor: complementing corporate allocation with {regime.value} treasury mix",
                )
                for ticker, weight in profile.items()
            ]
            explanation = (
                f"PARTIAL ANCHOR: {len(corporate_selected)} corporate ETFs passed, "
                f"{len(corporate_rejected)} rejected. Treasury allocation: {treasury_budget:.1%} of bond budget. "
                f"Regime: {regime.value}."
            )

        else:
            # NO ANCHOR: All corporate ETFs passed
            mode = AnchorMode.NO_ANCHOR
            allocations = []
            explanation = (
                f"NO ANCHOR: All corporate ETFs passed Z-score gates. "
                f"Bond allocation directed entirely to corporate ETFs."
            )

        result = TreasuryAnchorResult(
            mode=mode,
            allocations=allocations,
            spread_regime=regime,
            corporate_selected=corporate_selected,
            corporate_rejected=corporate_rejected,
            explanation=explanation,
        )

        logger.info(f"TreasuryAnchor: {mode.value} — {explanation}")

        return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(name)s %(message)s")

    anchor = TreasuryAnchor()

    print(f"\n{'='*60}")
    print("TREASURY ANCHOR DEMO")
    print(f"{'='*60}")

    # Scenario 1: Full anchor
    result1 = anchor.deploy(
        corporate_selected=[],
        corporate_rejected=["VCSH", "VCIT"],
        bond_budget=0.30,
    )
    print(f"\n  Scenario 1: Full Anchor")
    print(f"  Mode: {result1.mode.value}")
    print(f"  Allocations: {result1.allocation_dict}")
    print(f"  {result1.explanation}")

    # Scenario 2: Partial anchor
    result2 = anchor.deploy(
        corporate_selected=["VCSH"],
        corporate_rejected=["VCIT"],
        bond_budget=0.30,
    )
    print(f"\n  Scenario 2: Partial Anchor")
    print(f"  Mode: {result2.mode.value}")
    print(f"  Allocations: {result2.allocation_dict}")
    print(f"  {result2.explanation}")

    # Scenario 3: No anchor
    result3 = anchor.deploy(
        corporate_selected=["VCSH", "VCIT"],
        corporate_rejected=[],
        bond_budget=0.30,
    )
    print(f"\n  Scenario 3: No Anchor")
    print(f"  Mode: {result3.mode.value}")
    print(f"  Allocations: {result3.allocation_dict}")
    print(f"  {result3.explanation}")
