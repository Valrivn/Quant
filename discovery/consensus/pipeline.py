"""Consensus gate pipeline (D-20260816-001, P1, research-only).

Drives the sorted-data output the CEO requested: per-company rows that combine
review evidence (with usability/attack flags), quantifiable signals, and the
talent scout into a frozen composite, then sector-normalized. The output feeds a
pre-registered house backtest vs the rm-final baseline before any integration.

All collectors are injectable so tests run offline. Live collection is gated
behind ``DISCOVERY_LIVE=1`` and the ``consensus.enabled`` kill-switch.
"""

import inspect
import logging
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .config import load_consensus_config
from .engine import (
    BLOCKS, TIER_A, TIER_B, TIER_C,
    ReviewEvidence, EvidenceRow, CompanyVerdict,
    usability_for_n, build_company_verdict, normalize_sector_verdicts,
)

logger = logging.getLogger(__name__)

# Review sites collected per company (Type-C tier).
DEFAULT_REVIEW_SITES = ["glassdoor", "indeed", "g2", "capterra", "trustpilot"]

# Factor -> site mapping used to build EvidenceRow entries.
FACTOR_SITES = {
    "exec_approval": "glassdoor",
    "culture_values": "glassdoor",
    "comp_equity": "levels_fyi",
    "work_life": "glassdoor",
    "talent_capture": "linkedin",
    "review_skewness": "computed",
    "sentiment_breadth": "computed",
    "transaction_volume": "edgar",
    "sec_attrition_velocity": "edgar",
    "hiring_velocity": "adzuna",
    "review_volume": "computed",
}


@dataclass
class SortedCompanyRow:
    """One sorted row ready for the backtest (CEO's "sort the data" ask)."""

    ticker: str
    sector: str
    composite_score: float
    block_scores: Dict[str, float] = field(default_factory=dict)
    factor_scores: Dict[str, float] = field(default_factory=dict)
    flags: List[str] = field(default_factory=list)
    converged: bool = True
    normalized: bool = False
    usable_sources: int = 0
    total_reviews: int = 0


def _usable_sources(reviews: List[ReviewEvidence], cfg: Dict) -> int:
    return sum(1 for r in reviews if usability_for_n(r.n, cfg) == "USABLE")


async def _call(callable_, *args):
    """Call a collector, awaiting it if it returns a coroutine.

    Tests inject sync lambdas (they return dicts directly); the live
    collectors are async. This single helper lets both work against the same
    pass code.
    """
    result = callable_(*args)
    if inspect.isawaitable(result):
        return await result
    return result


async def collect_evidence_for_ticker(
    ticker: str,
    sector: str,
    cfg: Dict,
    review_collectors: Optional[Dict[str, Callable]] = None,
    talent_collector: Optional[Callable] = None,
    quantifiable_collector: Optional[Callable] = None,
    review_sites: Optional[List[str]] = None,
) -> tuple:
    """Collect + assemble evidence rows and ReviewEvidence for one ticker.

    Returns (rows, reviews). All collectors are injectable; defaults are the
    live ones from ``collectors`` (gated behind DISCOVERY_LIVE=1).
    """
    from .collectors import (
        make_review_collector, make_talent_collector, make_quantifiable_collector,
    )

    sites = review_sites or DEFAULT_REVIEW_SITES
    rc = review_collectors or {s: make_review_collector(s) for s in sites}
    tc = talent_collector or make_talent_collector()
    qc = quantifiable_collector or make_quantifiable_collector()

    company = ticker
    rows: List[EvidenceRow] = []
    reviews: List[ReviewEvidence] = []

    # --- Type-C review evidence (per site). ---
    for site in sites:
        try:
            data = await _call(rc[site], company)
            if data is None:
                continue
        except Exception as exc:  # noqa: BLE001 - fail-closed, never a hard stop
            logger.warning("collector %s failed for %s: %s", site, ticker, exc)
            continue
        n = int(data.get("n", 0))
        star = data.get("star_level")
        skew = data.get("skewness")
        iqr = data.get("iqr")
        reviews.append(ReviewEvidence(
            source=site, n=n, star_level=star, skewness=skew, iqr=iqr,
            records=data.get("records", []),
            recent_weekly_volume=data.get("recent_weekly_volume"),
            normal_weekly_volume=data.get("normal_weekly_volume"),
        ))
        if n > 0:
            rows.append(EvidenceRow(
                factor=_factor_for_site(site),
                value=_level_to_score(star) if star is not None else 0.5,
                tier=TIER_C,
                source=site,
                n=n,
                usability=usability_for_n(n, cfg),
            ))

    # --- Type-A / Type-B quantifiable. ---
    try:
        qdata = await _call(qc, company)
        if qdata:
            for f, val in qdata.items():
                if f == "cik":
                    continue
                if val is not None:
                    rows.append(EvidenceRow(
                        factor=f, value=float(val), tier=TIER_A, source="edgar",
                        usability="USABLE",
                    ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("quantifiable collector failed for %s: %s", ticker, exc)

    # --- Type-B talent scout. ---
    try:
        tdata = await _call(tc, company)
        if tdata:
            mentions = int(tdata.get("senior_mentions", 0) or 0)
            hv = tdata.get("hiring_velocity")
            min_senior = cfg["talent"]["min_senior_mentions"]
            talent_val = _talent_to_score(mentions, min_senior)
            if talent_val is not None:
                rows.append(EvidenceRow(
                    factor="talent_capture", value=talent_val, tier=TIER_B,
                    source="linkedin", usability="USABLE",
                ))
            if hv is not None:
                rows.append(EvidenceRow(
                    factor="hiring_velocity", value=_velocity_to_score(hv), tier=TIER_B,
                    source="adzuna", usability="USABLE",
                ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("talent collector failed for %s: %s", ticker, exc)

    return rows, reviews


async def run_consensus_pass(
    tickers: List[tuple],
    cfg: Dict = None,
    review_collectors: Optional[Dict[str, Callable]] = None,
    talent_collector: Optional[Callable] = None,
    quantifiable_collector: Optional[Callable] = None,
) -> List[SortedCompanyRow]:
    """Run the full pass over (ticker, sector) pairs, returning sorted rows.

    The output is deterministic: rows are sorted by composite_score descending,
    tie-broken by ticker. Sector two-extreme normalization applies when a sector
    has enough scored companies. Accepts sync (test fixture) or async (live)
    collectors interchangeably.
    """
    cfg = cfg or load_consensus_config()
    verdicts: List[CompanyVerdict] = []
    raw_reviews: Dict[str, List[ReviewEvidence]] = {}

    for ticker, sector in tickers:
        rows, reviews = await collect_evidence_for_ticker(
            ticker, sector, cfg,
            review_collectors=review_collectors,
            talent_collector=talent_collector,
            quantifiable_collector=quantifiable_collector,
        )
        verdicts.append(build_company_verdict(ticker, sector, rows, cfg, reviews))
        raw_reviews[ticker] = reviews

    normalize_sector_verdicts(verdicts, cfg)

    sorted_rows = sorted(
        (
            SortedCompanyRow(
                ticker=v.ticker,
                sector=v.sector,
                composite_score=v.composite_score,
                block_scores=v.block_scores,
                factor_scores=v.factor_scores,
                flags=v.flags,
                converged=v.converged,
                normalized=v.normalized,
                usable_sources=_usable_sources(raw_reviews.get(v.ticker, []), cfg),
                total_reviews=sum(r.n for r in raw_reviews.get(v.ticker, [])),
            )
            for v in verdicts
        ),
        key=lambda r: (-r.composite_score, r.ticker),
    )
    return sorted_rows


# --------------------------------------------------------------------------
# Deterministic factor value helpers (pre-registered mappings).
# --------------------------------------------------------------------------

def _factor_for_site(site: str) -> str:
    mapping = {
        "glassdoor": "exec_approval",
        "indeed": "culture_values",
        "g2": "review_volume",
        "capterra": "review_volume",
        "trustpilot": "review_volume",
        "comparably": "culture_values",
        "levels_fyi": "comp_equity",
        "blind": "work_life",
    }
    return mapping.get(site, "review_volume")


def _level_to_score(star: float) -> float:
    """Map a 1-5 star level to a 0..1 score (5 -> 1.0, 1 -> 0.0)."""
    if star is None:
        return 0.5
    return max(0.0, min(1.0, (float(star) - 1.0) / 4.0))


def _talent_to_score(mentions: int, min_senior: int) -> Optional[float]:
    """Talent-capture score: 0 until the minimum senior-mention count fires."""
    if mentions < min_senior:
        return None  # no signal yet — abstain, not neutral
    return min(1.0, mentions / (min_senior * 5.0))


def _velocity_to_score(velocity: float) -> float:
    """Hiring/attrition velocity to 0..1 via a monotone clamp."""
    return max(0.0, min(1.0, velocity / 100.0))