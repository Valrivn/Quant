#!/usr/bin/env python3
"""
bond_etf_screener.py — Step 2: Z-Score Peer-Group Filtering

Implements the quantitative screening pipeline for corporate bond ETFs (VCSH, VCIT)
using peer-group Z-score analysis on two dimensions:

1. ICR Z-Score (Credit Quality Gate):
   Z_ICR = (ICR_fund - mu_peer) / sigma_peer >= +1.0
   Requires the fund's weighted ICR to be significantly ABOVE average (safer).

2. Expense Ratio Z-Score (Cost Gate):
   Z_ER = (ER_fund - mu_peer) / sigma_peer <= -1.0
   Requires the fund's expense ratio to be significantly BELOW average (cheaper).

Both gates must pass for a corporate bond ETF to be selected.
If no corporate ETF passes, the treasury_anchor auto-defaults to SHY/BIL.

Damodaran Principle: We use Z-scores to mathematically isolate top-tier funds,
filtering out junk-distorted averages that mask default clustering risk.
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from Quantitative.shared.etf_data_fetcher import ETFDataFetcher, ETFMetrics
from Quantitative.bonds.corporate_look_through import CorporateLookThrough, LookThroughResult
from Quantitative.bonds.credit_spread_monitor import CreditSpreadMonitor, SpreadRegime

logger = logging.getLogger(__name__)


class ScreenerVerdict(Enum):
    """Screening verdict for a bond ETF."""
    SELECTED = "SELECTED"
    REJECTED_ICR = "REJECTED_ICR"
    REJECTED_ER = "REJECTED_ER"
    REJECTED_CREDIT_SPREAD = "REJECTED_CREDIT_SPREAD"
    REJECTED_NO_DATA = "REJECTED_NO_DATA"
    TREASURY_PASS = "TREASURY_PASS"


@dataclass
class ZScoreResult:
    """Z-score computation result for a single metric."""
    metric_name: str
    value: float
    peer_mean: float
    peer_std: float
    z_score: float
    gate_threshold: float
    passed: bool


@dataclass
class BondScreenerResult:
    """Complete screening result for a single bond ETF."""
    ticker: str
    category: str
    verdict: ScreenerVerdict
    z_score_icr: Optional[ZScoreResult] = None
    z_score_er: Optional[ZScoreResult] = None
    look_through: Optional[LookThroughResult] = None
    metrics: Optional[ETFMetrics] = None
    credit_spread_bps: Optional[float] = None
    explanation: str = ""
    evaluated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict:
        return {
            "ticker": self.ticker,
            "category": self.category,
            "verdict": self.verdict.value,
            "z_score_icr": self.z_score_icr.z_score if self.z_score_icr else None,
            "z_score_er": self.z_score_er.z_score if self.z_score_er else None,
            "weighted_icr": self.look_through.weighted_icr if self.look_through else None,
            "expense_ratio": self.metrics.expense_ratio if self.metrics else None,
            "explanation": self.explanation,
        }


# ETF category classification (dynamically determined from etf_config)
TREASURY_TICKERS = {"BIL", "SHY", "IEF"}
CORPORATE_TICKERS = {"VCSH", "VCIT"}


class BondETFScreener:
    """
    Peer-group Z-score screening engine for bond ETF candidates.

    Pipeline:
      1. Classify each ETF as Treasury or Corporate
      2. Treasuries pass automatically (sovereign credit = no ICR needed)
      3. Corporates undergo Z-score peer-group analysis:
         - Weighted ICR look-through via SEC EDGAR
         - Expense ratio comparison
         - Credit spread macro overlay
      4. If no corporate ETF passes both Z-score gates, return empty
         (treasury_anchor handles defensive fallback to SHY/BIL)
    """

    # Z-score thresholds (configurable for pivoting)
    ICR_Z_THRESHOLD = 1.0    # Fund ICR must be +1 sigma above peer mean
    ER_Z_THRESHOLD = -1.0    # Fund ER must be -1 sigma below peer mean (cheaper)

    # Minimum data completeness required for look-through
    MIN_DATA_COMPLETENESS = 0.3

    def __init__(
        self,
        etf_fetcher: Optional[ETFDataFetcher] = None,
        look_through: Optional[CorporateLookThrough] = None,
        credit_monitor: Optional[CreditSpreadMonitor] = None,
        icr_z_threshold: float = 1.0,
        er_z_threshold: float = -1.0,
    ):
        self._fetcher = etf_fetcher or ETFDataFetcher()
        self._look_through = look_through or CorporateLookThrough()
        self._credit_monitor = credit_monitor or CreditSpreadMonitor()
        self._icr_z_threshold = icr_z_threshold
        self._er_z_threshold = er_z_threshold

    def _compute_peer_z_scores(
        self,
        corporate_tickers: List[str],
        metrics_map: Dict[str, ETFMetrics],
        look_throughs: Dict[str, LookThroughResult],
    ) -> Dict[str, Dict[str, ZScoreResult]]:
        """
        Compute Z-scores for all corporate ETFs against the peer group.

        Returns nested dict: ticker -> metric_name -> ZScoreResult
        """
        # Collect raw values across peer group
        icr_values = {}
        er_values = {}

        for ticker in corporate_tickers:
            lt = look_throughs.get(ticker)
            metrics = metrics_map.get(ticker)

            if lt and lt.weighted_icr is not None and lt.data_completeness >= self.MIN_DATA_COMPLETENESS:
                icr_values[ticker] = lt.weighted_icr

            if metrics and metrics.expense_ratio is not None:
                er_values[ticker] = metrics.expense_ratio

        # Compute peer statistics
        z_scores: Dict[str, Dict[str, ZScoreResult]] = {}

        # ICR Z-scores
        if len(icr_values) >= 2:
            icr_list = list(icr_values.values())
            icr_mean = sum(icr_list) / len(icr_list)
            icr_std = math.sqrt(sum((v - icr_mean) ** 2 for v in icr_list) / (len(icr_list) - 1))
            if icr_std == 0:
                icr_std = 1e-6

            for ticker in corporate_tickers:
                if ticker not in z_scores:
                    z_scores[ticker] = {}

                if ticker in icr_values:
                    z = (icr_values[ticker] - icr_mean) / icr_std
                    z_scores[ticker]["icr"] = ZScoreResult(
                        metric_name="Weighted ICR",
                        value=icr_values[ticker],
                        peer_mean=icr_mean,
                        peer_std=icr_std,
                        z_score=round(z, 4),
                        gate_threshold=self._icr_z_threshold,
                        passed=z >= self._icr_z_threshold,
                    )

        # Expense Ratio Z-scores (inverted: cheaper = higher z-score magnitude in negative direction)
        if len(er_values) >= 2:
            er_list = list(er_values.values())
            er_mean = sum(er_list) / len(er_list)
            er_std = math.sqrt(sum((v - er_mean) ** 2 for v in er_list) / (len(er_list) - 1))
            if er_std == 0:
                er_std = 1e-6

            for ticker in corporate_tickers:
                if ticker not in z_scores:
                    z_scores[ticker] = {}

                if ticker in er_values:
                    z = (er_values[ticker] - er_mean) / er_std
                    z_scores[ticker]["er"] = ZScoreResult(
                        metric_name="Expense Ratio",
                        value=er_values[ticker],
                        peer_mean=er_mean,
                        peer_std=er_std,
                        z_score=round(z, 4),
                        gate_threshold=self._er_z_threshold,
                        passed=z <= self._er_z_threshold,
                    )

        return z_scores

    def screen_universe(
        self,
        tickers: List[str],
        metrics_map: Optional[Dict[str, ETFMetrics]] = None,
        force_refresh: bool = False,
    ) -> Dict[str, BondScreenerResult]:
        """
        Screen an entire universe of bond ETFs.

        Args:
            tickers: List of bond ETF tickers (e.g., BIL, SHY, IEF, VCSH, VCIT)
            metrics_map: Pre-fetched ETF metrics (fetched automatically if None)
            force_refresh: Skip all caches if True

        Returns:
            Dict mapping ticker -> BondScreenerResult
        """
        if metrics_map is None:
            metrics_map = self._fetcher.fetch_multiple(tickers)

        # Classify tickers
        corporate_tickers = [t for t in tickers if t in CORPORATE_TICKERS]
        treasury_tickers = [t for t in tickers if t in TREASURY_TICKERS]
        other_tickers = [t for t in tickers if t not in CORPORATE_TICKERS and t not in TREASURY_TICKERS]

        logger.info(
            f"BondETFScreener: {len(tickers)} tickers "
            f"({len(treasury_tickers)} treasury, {len(corporate_tickers)} corporate, "
            f"{len(other_tickers)} other)"
        )

        results: Dict[str, BondScreenerResult] = {}

        # Treasuries pass automatically
        for ticker in treasury_tickers:
            results[ticker] = BondScreenerResult(
                ticker=ticker,
                category="treasury",
                verdict=ScreenerVerdict.TREASURY_PASS,
                metrics=metrics_map.get(ticker),
                explanation=f"{ticker} is a U.S. Treasury ETF — sovereign credit, no ICR required.",
            )
            logger.info(f"  {ticker}: TREASURY_PASS")

        # Corporate look-through
        look_throughs: Dict[str, LookThroughResult] = {}
        for ticker in corporate_tickers:
            lt = self._look_through.look_through(ticker, force_refresh=force_refresh)
            look_throughs[ticker] = lt

        # Compute peer Z-scores
        z_score_map = self._compute_peer_z_scores(corporate_tickers, metrics_map, look_throughs)

        # Credit spread regime
        spread_result = self._credit_monitor.fetch_and_classify(force_refresh=force_refresh)
        credit_spread_bps = spread_result.current_spread_bps

        # Screen corporates
        for ticker in corporate_tickers:
            z_scores = z_score_map.get(ticker, {})
            icr_z = z_scores.get("icr")
            er_z = z_scores.get("er")
            lt = look_throughs.get(ticker)
            metrics = metrics_map.get(ticker)

            # Check credit spread regime
            if spread_result.regime == SpreadRegime.CRISIS:
                results[ticker] = BondScreenerResult(
                    ticker=ticker,
                    category="corporate",
                    verdict=ScreenerVerdict.REJECTED_CREDIT_SPREAD,
                    z_score_icr=icr_z,
                    z_score_er=er_z,
                    look_through=lt,
                    metrics=metrics,
                    credit_spread_bps=credit_spread_bps,
                    explanation=(
                        f"REJECTED: Credit spread regime = CRISIS ({credit_spread_bps:.0f} bps). "
                        f"Corporate bonds carry elevated default risk."
                    ),
                )
                logger.info(f"  {ticker}: REJECTED_CREDIT_SPREAD ({credit_spread_bps:.0f} bps)")
                continue

            # Check data completeness
            if lt and lt.data_completeness < self.MIN_DATA_COMPLETENESS:
                results[ticker] = BondScreenerResult(
                    ticker=ticker,
                    category="corporate",
                    verdict=ScreenerVerdict.REJECTED_NO_DATA,
                    z_score_icr=icr_z,
                    z_score_er=er_z,
                    look_through=lt,
                    metrics=metrics,
                    credit_spread_bps=credit_spread_bps,
                    explanation=(
                        f"REJECTED: Insufficient EDGAR data ({lt.data_completeness:.0%} completeness). "
                        f"Cannot verify credit quality."
                    ),
                )
                logger.info(f"  {ticker}: REJECTED_NO_DATA")
                continue

            # Z-score gate checks
            icr_passed = icr_z.passed if icr_z else False
            er_passed = er_z.passed if er_z else False

            if not icr_passed:
                icr_detail = f"Z_ICR={icr_z.z_score:.2f} < {self._icr_z_threshold}" if icr_z else "ICR data unavailable"
                results[ticker] = BondScreenerResult(
                    ticker=ticker,
                    category="corporate",
                    verdict=ScreenerVerdict.REJECTED_ICR,
                    z_score_icr=icr_z,
                    z_score_er=er_z,
                    look_through=lt,
                    metrics=metrics,
                    credit_spread_bps=credit_spread_bps,
                    explanation=f"REJECTED: ICR Z-score gate failed — {icr_detail}",
                )
                logger.info(f"  {ticker}: REJECTED_ICR ({icr_detail})")
                continue

            if not er_passed:
                er_detail = f"Z_ER={er_z.z_score:.2f} > {self._er_z_threshold}" if er_z else "ER data unavailable"
                results[ticker] = BondScreenerResult(
                    ticker=ticker,
                    category="corporate",
                    verdict=ScreenerVerdict.REJECTED_ER,
                    z_score_icr=icr_z,
                    z_score_er=er_z,
                    look_through=lt,
                    metrics=metrics,
                    credit_spread_bps=credit_spread_bps,
                    explanation=f"REJECTED: Expense Ratio Z-score gate failed — {er_detail}",
                )
                logger.info(f"  {ticker}: REJECTED_ER ({er_detail})")
                continue

            # Both gates passed
            results[ticker] = BondScreenerResult(
                ticker=ticker,
                category="corporate",
                verdict=ScreenerVerdict.SELECTED,
                z_score_icr=icr_z,
                z_score_er=er_z,
                look_through=lt,
                metrics=metrics,
                credit_spread_bps=credit_spread_bps,
                explanation=(
                    f"SELECTED: Z_ICR={icr_z.z_score:.2f} (>= {self._icr_z_threshold}), "
                    f"Z_ER={er_z.z_score:.2f} (<= {self._er_z_threshold}), "
                    f"weighted_ICR={lt.weighted_icr:.2f}"
                ),
            )
            logger.info(
                f"  {ticker}: SELECTED — Z_ICR={icr_z.z_score:.2f}, Z_ER={er_z.z_score:.2f}"
            )

        # Screen other tickers (non-treasury, non-corporate)
        for ticker in other_tickers:
            results[ticker] = BondScreenerResult(
                ticker=ticker,
                category="unknown",
                verdict=ScreenerVerdict.REJECTED_NO_DATA,
                metrics=metrics_map.get(ticker),
                explanation=f"Ticker {ticker} is not in the configured bond universe.",
            )

        selected = [t for t, r in results.items() if r.verdict == ScreenerVerdict.SELECTED]
        treasury = [t for t, r in results.items() if r.verdict == ScreenerVerdict.TREASURY_PASS]
        logger.info(
            f"BondETFScreener: {len(selected)} corporate selected, "
            f"{len(treasury)} treasury passed, "
            f"{len(tickers) - len(selected) - len(treasury)} rejected"
        )

        return results

    def get_selected_corporate_tickers(
        self,
        results: Dict[str, BondScreenerResult],
    ) -> List[str]:
        """Extract only the SELECTED corporate tickers from screening results."""
        return [
            ticker for ticker, result in results.items()
            if result.verdict == ScreenerVerdict.SELECTED
        ]


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(name)s %(message)s")

    from config.constants import ETF_UNIVERSE_BONDS

    screener = BondETFScreener()
    results = screener.screen_universe(ETF_UNIVERSE_BONDS)

    print(f"\n{'='*60}")
    print("BOND ETF SCREENER REPORT")
    print(f"{'='*60}")
    for ticker, result in sorted(results.items()):
        print(f"\n  {ticker} [{result.category.upper()}]")
        print(f"    Verdict: {result.verdict.value}")
        print(f"    {result.explanation}")
        if result.z_score_icr:
            print(f"    ICR Z-Score: {result.z_score_icr.z_score:.2f} "
                  f"(threshold: >= {result.z_score_icr.gate_threshold})")
        if result.z_score_er:
            print(f"    ER Z-Score: {result.z_score_er.z_score:.2f} "
                  f"(threshold: <= {result.z_score_er.gate_threshold})")
        if result.credit_spread_bps is not None:
            print(f"    Credit Spread: {result.credit_spread_bps:.0f} bps")

    selected = screener.get_selected_corporate_tickers(results)
    print(f"\n  Selected Corporate ETFs: {', '.join(selected) if selected else 'NONE (treasury_anchor defaults to SHY/BIL)'}")
