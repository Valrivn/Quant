#!/usr/bin/env python3
"""
liquidity_gatekeeper.py — Step 1: The Liquidity Gatekeeper (Pass/Fail)

Disqualifies any ETF that fails any of three hard statistical criteria:
1. Average Daily Volume (ADV) > 1,000,000 shares/day
2. Median Bid-Ask Spread ≤ 0.02%
3. Premium/Discount to NAV within ±0.10%

Critical Trigger: NAV discount below -0.50% → immediate flag for
underlying asset liquidity failure (OTC bond freezing or toxic asset decoupling).

This module is shared across both the Bond and Gold ETF pipelines.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from Quantitative.shared.etf_data_fetcher import ETFDataFetcher, ETFMetrics

logger = logging.getLogger(__name__)


class GateResult(Enum):
    """Result state for each individual gate."""
    PASS = "PASS"
    FAIL = "FAIL"
    NO_DATA = "NO_DATA"


class CriticalFlag(Enum):
    """Critical flags raised by the gatekeeper."""
    NONE = "NONE"
    NAV_LIQUIDITY_FAILURE = "NAV_LIQUIDITY_FAILURE"  # NAV discount below -0.50%


@dataclass
class GateDetail:
    """Result of a single gate evaluation."""
    gate_name: str
    metric_value: Optional[float]
    threshold: str
    result: GateResult
    explanation: str


@dataclass
class GatekeeperResult:
    """Complete gatekeeper evaluation for a single ETF."""
    ticker: str
    overall_pass: bool
    gates: List[GateDetail]
    critical_flag: CriticalFlag = CriticalFlag.NONE
    critical_message: Optional[str] = None
    evaluated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def summary(self) -> str:
        """Human-readable one-line summary."""
        status = "✅ PASS" if self.overall_pass else "❌ FAIL"
        flags = f" ⚠️ {self.critical_message}" if self.critical_flag != CriticalFlag.NONE else ""
        return f"{self.ticker}: {status}{flags}"


class LiquidityGatekeeper:
    """
    Three-gate liquidity filter for ETF candidates.

    All thresholds are configured as class constants matching the quantitative
    specification. An ETF must pass ALL three gates to proceed to Step 2.
    """

    # Gate thresholds (from the quantitative specification)
    ADV_THRESHOLD = 1_000_000           # shares per day
    MAX_BID_ASK_SPREAD = 0.0002         # 0.02% as decimal
    NAV_DEVIATION_LOWER = -0.0010       # -0.10%
    NAV_DEVIATION_UPPER = 0.0010        # +0.10%
    NAV_CRITICAL_TRIGGER = -0.0050      # -0.50% → OTC freeze flag

    def __init__(self, fetcher: Optional[ETFDataFetcher] = None):
        self._fetcher = fetcher or ETFDataFetcher()

    def _evaluate_adv_gate(self, metrics: ETFMetrics) -> GateDetail:
        """Gate 1: Average Daily Volume > 1,000,000 shares/day."""
        adv = metrics.avg_daily_volume
        if adv is None:
            return GateDetail(
                gate_name="ADV",
                metric_value=None,
                threshold=f"> {self.ADV_THRESHOLD:,} shares/day",
                result=GateResult.NO_DATA,
                explanation="Average Daily Volume data not available",
            )

        passed = adv > self.ADV_THRESHOLD
        return GateDetail(
            gate_name="ADV",
            metric_value=adv,
            threshold=f"> {self.ADV_THRESHOLD:,} shares/day",
            result=GateResult.PASS if passed else GateResult.FAIL,
            explanation=(
                f"ADV = {adv:,.0f} shares/day {'exceeds' if passed else 'below'} "
                f"minimum threshold of {self.ADV_THRESHOLD:,}"
            ),
        )

    def _evaluate_spread_gate(self, metrics: ETFMetrics) -> GateDetail:
        """Gate 2: Median Bid-Ask Spread ≤ 0.02%."""
        spread = metrics.median_bid_ask_spread
        if spread is None:
            return GateDetail(
                gate_name="Bid-Ask Spread",
                metric_value=None,
                threshold=f"≤ {self.MAX_BID_ASK_SPREAD * 100:.2f}%",
                result=GateResult.NO_DATA,
                explanation="Bid-ask spread data not available",
            )

        passed = spread <= self.MAX_BID_ASK_SPREAD
        return GateDetail(
            gate_name="Bid-Ask Spread",
            metric_value=spread,
            threshold=f"≤ {self.MAX_BID_ASK_SPREAD * 100:.2f}%",
            result=GateResult.PASS if passed else GateResult.FAIL,
            explanation=(
                f"Median spread = {spread * 100:.4f}% {'within' if passed else 'exceeds'} "
                f"maximum of {self.MAX_BID_ASK_SPREAD * 100:.2f}%"
            ),
        )

    def _evaluate_nav_gate(self, metrics: ETFMetrics) -> GateDetail:
        """Gate 3: NAV Premium/Discount within ±0.10%."""
        nav_dev = metrics.nav_premium_discount
        if nav_dev is None:
            return GateDetail(
                gate_name="NAV Deviation",
                metric_value=None,
                threshold=f"±{abs(self.NAV_DEVIATION_UPPER) * 100:.2f}%",
                result=GateResult.NO_DATA,
                explanation="NAV premium/discount data not available",
            )

        passed = self.NAV_DEVIATION_LOWER <= nav_dev <= self.NAV_DEVIATION_UPPER
        return GateDetail(
            gate_name="NAV Deviation",
            metric_value=nav_dev,
            threshold=f"±{abs(self.NAV_DEVIATION_UPPER) * 100:.2f}%",
            result=GateResult.PASS if passed else GateResult.FAIL,
            explanation=(
                f"NAV deviation = {nav_dev * 100:.4f}% {'within' if passed else 'outside'} "
                f"bounds of [{self.NAV_DEVIATION_LOWER * 100:.2f}%, {self.NAV_DEVIATION_UPPER * 100:.2f}%]"
            ),
        )

    def evaluate(self, ticker: str, metrics: Optional[ETFMetrics] = None) -> GatekeeperResult:
        """
        Run all three gates on a single ETF.

        Args:
            ticker: ETF ticker symbol
            metrics: Pre-fetched metrics (fetched automatically if None)

        Returns:
            GatekeeperResult with pass/fail state and gate details
        """
        if metrics is None:
            metrics = self._fetcher.fetch(ticker)

        # Run all three gates
        adv_gate = self._evaluate_adv_gate(metrics)
        spread_gate = self._evaluate_spread_gate(metrics)
        nav_gate = self._evaluate_nav_gate(metrics)
        gates = [adv_gate, spread_gate, nav_gate]

        # Overall pass requires ALL gates to pass (NO_DATA treated as fail)
        overall_pass = all(g.result == GateResult.PASS for g in gates)

        # Check critical trigger: NAV discount below -0.50%
        critical_flag = CriticalFlag.NONE
        critical_message = None
        if metrics.nav_premium_discount is not None and metrics.nav_premium_discount < self.NAV_CRITICAL_TRIGGER:
            critical_flag = CriticalFlag.NAV_LIQUIDITY_FAILURE
            critical_message = (
                f"⚠️ CRITICAL: {ticker} NAV discount = {metrics.nav_premium_discount * 100:.4f}% "
                f"breached -0.50% threshold. Flagged as underlying asset liquidity failure "
                f"(potential OTC bond freezing or toxic asset decoupling)."
            )
            logger.critical(critical_message)

        result = GatekeeperResult(
            ticker=ticker,
            overall_pass=overall_pass,
            gates=gates,
            critical_flag=critical_flag,
            critical_message=critical_message,
        )

        logger.info(f"Gatekeeper: {result.summary}")
        return result

    def evaluate_universe(
        self,
        tickers: List[str],
        metrics_map: Optional[Dict[str, ETFMetrics]] = None,
    ) -> Dict[str, GatekeeperResult]:
        """
        Run the gatekeeper on an entire ETF universe.

        Args:
            tickers: List of ETF ticker symbols
            metrics_map: Pre-fetched metrics keyed by ticker

        Returns:
            Dictionary mapping ticker → GatekeeperResult
        """
        if metrics_map is None:
            metrics_map = self._fetcher.fetch_multiple(tickers)

        results = {}
        for ticker in tickers:
            metrics = metrics_map.get(ticker)
            results[ticker] = self.evaluate(ticker, metrics=metrics)

        passed = [t for t, r in results.items() if r.overall_pass]
        failed = [t for t, r in results.items() if not r.overall_pass]
        logger.info(f"Gatekeeper Universe: {len(passed)} passed, {len(failed)} failed out of {len(tickers)}")
        logger.info(f"  Passed: {', '.join(passed) if passed else 'none'}")
        logger.info(f"  Failed: {', '.join(failed) if failed else 'none'}")

        return results


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(name)s %(message)s")

    ticker = sys.argv[1] if len(sys.argv) > 1 else "SHY"
    gatekeeper = LiquidityGatekeeper()
    result = gatekeeper.evaluate(ticker)

    print(f"\n{'='*60}")
    print(f"LIQUIDITY GATEKEEPER REPORT: {result.ticker}")
    print(f"{'='*60}")
    for gate in result.gates:
        status = "✅" if gate.result == GateResult.PASS else "❌" if gate.result == GateResult.FAIL else "⚠️"
        val_str = f"{gate.metric_value:.6f}" if gate.metric_value is not None else "N/A"
        print(f"  {status} {gate.gate_name}: {val_str} (threshold: {gate.threshold})")
        print(f"     {gate.explanation}")
    print(f"\n  Overall: {'✅ PASS' if result.overall_pass else '❌ FAIL'}")
    if result.critical_message:
        print(f"\n  {result.critical_message}")
