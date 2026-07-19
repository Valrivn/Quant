#!/usr/bin/env python3
"""
gold_etf_screener.py — Step 2: Gold ETF Selection Engine

Implements the quantitative screening pipeline for gold ETFs (IAU, GLDM)
using tracking error analysis and macro valuation gates.

Three screening dimensions:

1. Tracking Error Protocol:
   TE = sqrt(sum((R_ETF - R_Spot)^2) / (n-1))
   Maximum tracking error threshold: 1.5% (configurable)

2. Correlation Gate:
   Pearson r between ETF daily returns and spot gold daily returns.
   Must satisfy: r >= 0.99 (structural tracking integrity)

3. Annualized Tracking Difference (Directional):
   TD_annual = R_ETF_annual - R_Spot_annual
   If |TD_annual| > expense_ratio: FLAG for derivative counterparty risk
   If TD_annual > 0 (ETF outperforms): FLAG as suspicious

4. Macro Valuation Gate:
   Gold Macro Valuation signal must not be OVERVALUED.

Damodaran Principle: Physical gold ETFs should track spot gold with near-perfect
correlation. Deviations indicate synthetic derivative exposure, securities lending
leverage, or counterparty risk that defeats gold's role as a systemic hedge.
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Tuple

import numpy as np

from Quantitative.shared.etf_data_fetcher import ETFDataFetcher, ETFMetrics
from Quantitative.gold_etf.spot_gold_tracker import SpotGoldTracker, SpotGoldResult
from Quantitative.gold_etf.gold_macro_valuation import GoldMacroValuation, GoldMacroSignal

logger = logging.getLogger(__name__)


class GoldVerdict(Enum):
    """Screening verdict for a gold ETF."""
    SELECTED = "SELECTED"
    REJECTED_CORRELATION = "REJECTED_CORRELATION"
    REJECTED_TRACKING_ERROR = "REJECTED_TRACKING_ERROR"
    REJECTED_TRACKING_DIFF = "REJECTED_TRACKING_DIFF"
    REJECTED_MACRO = "REJECTED_MACRO"
    REJECTED_NO_DATA = "REJECTED_NO_DATA"


@dataclass
class TrackingMetrics:
    """Computed tracking metrics for a gold ETF vs spot gold."""
    tracking_error: Optional[float] = None
    correlation: Optional[float] = None
    tracking_difference_annual: Optional[float] = None
    expense_ratio: Optional[float] = None
    aligned_observations: int = 0
    tracking_diff_flag: str = ""


@dataclass
class GoldScreenerResult:
    """Complete screening result for a single gold ETF."""
    ticker: str
    verdict: GoldVerdict
    tracking_metrics: Optional[TrackingMetrics] = None
    metrics: Optional[ETFMetrics] = None
    macro_signal: Optional[GoldMacroSignal] = None
    macro_composite: Optional[float] = None
    explanation: str = ""
    evaluated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict:
        return {
            "ticker": self.ticker,
            "verdict": self.verdict.value,
            "tracking_error": self.tracking_metrics.tracking_error if self.tracking_metrics else None,
            "correlation": self.tracking_metrics.correlation if self.tracking_metrics else None,
            "tracking_difference": self.tracking_metrics.tracking_difference_annual if self.tracking_metrics else None,
            "macro_signal": self.macro_signal.value if self.macro_signal else None,
            "expense_ratio": self.metrics.expense_ratio if self.metrics else None,
            "explanation": self.explanation,
        }


class GoldETFScreener:
    """
    Peer-group screening engine for gold ETF candidates.

    Pipeline:
      1. Fetch spot gold returns from FRED
      2. For each gold ETF, compute tracking error + correlation
      3. Apply macro valuation gate
      4. SELECTED = passes all gates, REJECTED = fails any gate
    """

    # Configurable thresholds (from gold_blueprint.md)
    MAX_TRACKING_ERROR = 0.015     # 1.5% annualized
    MIN_CORRELATION = 0.99         # Pearson r >= 0.99
    TRADING_DAYS_PER_YEAR = 252

    def __init__(
        self,
        etf_fetcher: Optional[ETFDataFetcher] = None,
        spot_tracker: Optional[SpotGoldTracker] = None,
        macro_valuation: Optional[GoldMacroValuation] = None,
        max_tracking_error: float = 0.015,
        min_correlation: float = 0.99,
    ):
        self._fetcher = etf_fetcher or ETFDataFetcher()
        self._spot_tracker = spot_tracker or SpotGoldTracker()
        self._macro_valuation = macro_valuation or GoldMacroValuation()
        self._max_te = max_tracking_error
        self._min_corr = min_correlation

    def _compute_tracking_metrics(
        self,
        etf_returns: List[float],
        spot_returns: List[float],
        etf_annual_return: Optional[float],
        spot_annual_return: Optional[float],
        expense_ratio: Optional[float],
    ) -> TrackingMetrics:
        """
        Compute tracking error, correlation, and tracking difference.

        Tracking Error (TE):
          TE = std(R_ETF - R_Spot) * sqrt(252)

        Pearson Correlation (r):
          r = cov(R_ETF, R_Spot) / (std(R_ETF) * std(R_Spot))

        Tracking Difference (TD):
          TD_annual = R_ETF_annual - R_Spot_annual
        """
        n = min(len(etf_returns), len(spot_returns))
        if n < 30:
            return TrackingMetrics(aligned_observations=n)

        etf_arr = np.array(etf_returns[:n])
        spot_arr = np.array(spot_returns[:n])

        # Tracking Error
        diff = etf_arr - spot_arr
        daily_te = float(np.std(diff, ddof=1))
        tracking_error = daily_te * math.sqrt(self.TRADING_DAYS_PER_YEAR)

        # Pearson Correlation
        if np.std(etf_arr) > 0 and np.std(spot_arr) > 0:
            correlation = float(np.corrcoef(etf_arr, spot_arr)[0, 1])
        else:
            correlation = None

        # Tracking Difference
        tracking_diff_annual = None
        tracking_diff_flag = ""
        if etf_annual_return is not None and spot_annual_return is not None:
            tracking_diff_annual = etf_annual_return - spot_annual_return

            if expense_ratio is not None:
                if abs(tracking_diff_annual) > expense_ratio:
                    tracking_diff_flag = (
                        f"|TD|={abs(tracking_diff_annual):.4%} > ER={expense_ratio:.4%} — "
                        f"FLAG: Possible derivative counterparty risk"
                    )
                if tracking_diff_annual > 0:
                    tracking_diff_flag += " | ETF outperforms spot — FLAG: Suspicious"

        return TrackingMetrics(
            tracking_error=round(tracking_error, 6),
            correlation=round(correlation, 6) if correlation is not None else None,
            tracking_difference_annual=round(tracking_diff_annual, 6) if tracking_diff_annual is not None else None,
            expense_ratio=expense_ratio,
            aligned_observations=n,
            tracking_diff_flag=tracking_diff_flag,
        )

    def screen_universe(
        self,
        tickers: List[str],
        metrics_map: Optional[Dict[str, ETFMetrics]] = None,
        force_refresh: bool = False,
    ) -> Dict[str, GoldScreenerResult]:
        """
        Screen an entire universe of gold ETFs.

        Args:
            tickers: List of gold ETF tickers (e.g., IAU, GLDM)
            metrics_map: Pre-fetched ETF metrics
            force_refresh: Skip all caches if True

        Returns:
            Dict mapping ticker -> GoldScreenerResult
        """
        if metrics_map is None:
            metrics_map = self._fetcher.fetch_multiple(tickers)

        # Fetch macro valuation
        macro_result = self._macro_valuation.fetch_and_valuate(force_refresh=force_refresh)
        macro_signal = macro_result.signal

        results: Dict[str, GoldScreenerResult] = {}

        for ticker in tickers:
            metrics = metrics_map.get(ticker)
            if metrics is None or metrics.daily_returns is None:
                results[ticker] = GoldScreenerResult(
                    ticker=ticker,
                    verdict=GoldVerdict.REJECTED_NO_DATA,
                    metrics=metrics,
                    macro_signal=macro_signal,
                    macro_composite=macro_result.composite_score,
                    explanation=f"REJECTED: No market data available for {ticker}.",
                )
                continue

            # Align ETF returns with spot gold returns
            etf_dates = metrics.daily_dates or []
            etf_returns = metrics.daily_returns

            spot_returns_aligned, aligned_dates = self._spot_tracker.get_returns_aligned(
                etf_dates, force_refresh=force_refresh
            )

            if len(spot_returns_aligned) < 30:
                results[ticker] = GoldScreenerResult(
                    ticker=ticker,
                    verdict=GoldVerdict.REJECTED_NO_DATA,
                    metrics=metrics,
                    macro_signal=macro_signal,
                    macro_composite=macro_result.composite_score,
                    explanation=(
                        f"REJECTED: Insufficient aligned data "
                        f"({len(spot_returns_aligned)} points, need >= 30)."
                    ),
                )
                continue

            # Build aligned ETF returns (only dates present in both series)
            etf_by_date = {
                d: r for d, r in zip(etf_dates, etf_returns) if r is not None
            }
            common_dates = [d for d in aligned_dates if d in etf_by_date]
            etf_aligned = [etf_by_date[d] for d in common_dates]
            spot_returns_aligned = [
                spot_returns_aligned[aligned_dates.index(d)]
                for d in common_dates
            ]

            # Compute tracking metrics
            tracking_metrics = self._compute_tracking_metrics(
                etf_aligned,
                spot_returns_aligned,
                metrics.annualized_return if hasattr(metrics, 'annualized_return') else None,
                None,
                metrics.expense_ratio,
            )

            # Gate 1: Correlation
            if tracking_metrics.correlation is not None and tracking_metrics.correlation < self._min_corr:
                results[ticker] = GoldScreenerResult(
                    ticker=ticker,
                    verdict=GoldVerdict.REJECTED_CORRELATION,
                    tracking_metrics=tracking_metrics,
                    metrics=metrics,
                    macro_signal=macro_signal,
                    macro_composite=macro_result.composite_score,
                    explanation=(
                        f"REJECTED: Correlation r={tracking_metrics.correlation:.4f} < {self._min_corr}. "
                        f"Structural drift from physical gold detected."
                    ),
                )
                logger.info(f"  {ticker}: REJECTED_CORRELATION (r={tracking_metrics.correlation:.4f})")
                continue

            # Gate 2: Tracking Error
            if tracking_metrics.tracking_error is not None and tracking_metrics.tracking_error > self._max_te:
                results[ticker] = GoldScreenerResult(
                    ticker=ticker,
                    verdict=GoldVerdict.REJECTED_TRACKING_ERROR,
                    tracking_metrics=tracking_metrics,
                    metrics=metrics,
                    macro_signal=macro_signal,
                    macro_composite=macro_result.composite_score,
                    explanation=(
                        f"REJECTED: Tracking Error={tracking_metrics.tracking_error:.4%} > {self._max_te:.4%}. "
                        f"ETF does not faithfully replicate spot gold."
                    ),
                )
                logger.info(f"  {ticker}: REJECTED_TRACKING_ERROR (TE={tracking_metrics.tracking_error:.4%})")
                continue

            # Gate 3: Tracking Difference
            if tracking_metrics.tracking_diff_flag:
                results[ticker] = GoldScreenerResult(
                    ticker=ticker,
                    verdict=GoldVerdict.REJECTED_TRACKING_DIFF,
                    tracking_metrics=tracking_metrics,
                    metrics=metrics,
                    macro_signal=macro_signal,
                    macro_composite=macro_result.composite_score,
                    explanation=f"REJECTED: {tracking_metrics.tracking_diff_flag}",
                )
                logger.info(f"  {ticker}: REJECTED_TRACKING_DIFF")
                continue

            # Gate 4: Macro Valuation
            if macro_signal == GoldMacroSignal.OVERVALUED:
                results[ticker] = GoldScreenerResult(
                    ticker=ticker,
                    verdict=GoldVerdict.REJECTED_MACRO,
                    tracking_metrics=tracking_metrics,
                    metrics=metrics,
                    macro_signal=macro_signal,
                    macro_composite=macro_result.composite_score,
                    explanation=(
                        f"REJECTED: Macro valuation = OVERVALUED "
                        f"(composite: {macro_result.composite_score:.2f}). "
                        f"Gold is expensive relative to real rates and M2 expansion."
                    ),
                )
                logger.info(f"  {ticker}: REJECTED_MACRO")
                continue

            # All gates passed
            results[ticker] = GoldScreenerResult(
                ticker=ticker,
                verdict=GoldVerdict.SELECTED,
                tracking_metrics=tracking_metrics,
                metrics=metrics,
                macro_signal=macro_signal,
                macro_composite=macro_result.composite_score,
                explanation=(
                    f"SELECTED: r={tracking_metrics.correlation:.4f} (>= {self._min_corr}), "
                    f"TE={tracking_metrics.tracking_error:.4%} (<= {self._max_te:.4%}), "
                    f"macro={macro_signal.value}"
                ),
            )
            logger.info(
                f"  {ticker}: SELECTED — r={tracking_metrics.correlation:.4f}, "
                f"TE={tracking_metrics.tracking_error:.4%}, macro={macro_signal.value}"
            )

        selected = [t for t, r in results.items() if r.verdict == GoldVerdict.SELECTED]
        logger.info(
            f"GoldETFScreener: {len(selected)} selected out of {len(tickers)} candidates"
        )

        return results

    def get_selected_tickers(self, results: Dict[str, GoldScreenerResult]) -> List[str]:
        """Extract only the SELECTED tickers from screening results."""
        return [t for t, r in results.items() if r.verdict == GoldVerdict.SELECTED]


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(name)s %(message)s")

    from config.constants import ETF_UNIVERSE_GOLD

    screener = GoldETFScreener()
    results = screener.screen_universe(ETF_UNIVERSE_GOLD)

    print(f"\n{'='*60}")
    print("GOLD ETF SCREENER REPORT")
    print(f"{'='*60}")
    for ticker, result in sorted(results.items()):
        print(f"\n  {ticker}")
        print(f"    Verdict: {result.verdict.value}")
        print(f"    {result.explanation}")
        if result.tracking_metrics:
            tm = result.tracking_metrics
            if tm.correlation is not None:
                print(f"    Correlation: r={tm.correlation:.4f}")
            if tm.tracking_error is not None:
                print(f"    Tracking Error: {tm.tracking_error:.4%}")
            if tm.tracking_difference_annual is not None:
                print(f"    Tracking Difference: {tm.tracking_difference_annual:.4%}")
            print(f"    Aligned Observations: {tm.aligned_observations}")
        if result.macro_signal:
            print(f"    Macro Signal: {result.macro_signal.value}")

    selected = screener.get_selected_tickers(results)
    print(f"\n  Selected Gold ETFs: {', '.join(selected) if selected else 'NONE'}")
