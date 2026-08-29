"""Anti-bias alt-data consensus gate (D-20260816-001, P1, research-only).

A LEAF module (mirrors ``discovery/``): additive-only, never imported by
production code. Implements the CEO-approved rev-3 design:

- Review usability ladder + SET-ASIDE rule (<50 reviews across platforms).
- Attack flags (BRIBE-ATTACK, COMPANY-PUNISHING-ATTACK) that quarantine
  evidence so it can never cross the pass line.
- POLARIZED / NO-CONVERGENCE flags.
- Frozen composite (50/30/20) with within-block factor weights.
- Sector-relative two-extreme normalization with directional fallback.
- Anti-bot collectors (NodeDriver CDP stealth for Cloudflare-heavy sites).
- Talent scout (Type-B, 10% block): talked-about people joining the company.

Kill-switch: ``consensus.enabled: false`` in ``config/weights_consensus.yaml``.
"""

from .config import load_consensus_config, ConsensusConfigError
from .engine import (
    INSUFFICIENT, DIRECTIONAL, DISTRIBUTIONAL, USABLE,
    F_BRIBE, F_PUNISHING, F_POLARIZED, F_NO_CONVERGENCE, F_SET_ASIDE,
    BLOCKS, TIER_A, TIER_B, TIER_C,
    ReviewRecord, ReviewEvidence, EvidenceRow, CompanyVerdict,
    usability_for_n, total_review_n, is_set_aside,
    detect_bribe_attack, detect_punishing_attack, detect_polarized,
    flag_review_sources, convergence_check, block_score,
    normalize_two_extreme, build_company_verdict, normalize_sector_verdicts,
)
from .collectors import (
    build_site_plan, fetch_html, live_enabled, LiveFetchDisabled,
    make_review_collector, make_talent_collector, make_quantifiable_collector,
    CollectorSite,
)
from .pipeline import (
    run_consensus_pass, collect_evidence_for_ticker, SortedCompanyRow,
)

__all__ = [
    "load_consensus_config", "ConsensusConfigError",
    "INSUFFICIENT", "DIRECTIONAL", "DISTRIBUTIONAL", "USABLE",
    "F_BRIBE", "F_PUNISHING", "F_POLARIZED", "F_NO_CONVERGENCE", "F_SET_ASIDE",
    "BLOCKS", "TIER_A", "TIER_B", "TIER_C",
    "ReviewRecord", "ReviewEvidence", "EvidenceRow", "CompanyVerdict",
    "usability_for_n", "total_review_n", "is_set_aside",
    "detect_bribe_attack", "detect_punishing_attack", "detect_polarized",
    "flag_review_sources", "convergence_check", "block_score",
    "normalize_two_extreme", "build_company_verdict", "normalize_sector_verdicts",
    "build_site_plan", "fetch_html", "live_enabled", "LiveFetchDisabled",
    "make_review_collector", "make_talent_collector", "make_quantifiable_collector",
    "CollectorSite",
    "run_consensus_pass", "collect_evidence_for_ticker", "SortedCompanyRow",
]