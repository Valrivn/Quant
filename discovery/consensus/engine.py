"""Anti-bias alt-data consensus engine (D-20260816-001, P1, research-only).

Implements the CEO-approved rev-3 design:
- Review usability ladder (INSUFFICIENT / DIRECTIONAL / DISTRIBUTIONAL / USABLE).
- SET-ASIDE rule: <``set_aside_threshold`` reviews across ALL platforms => the
  company is left aside and marked; review block contributes 0 (never neutral).
- Attack flags: BRIBE-ATTACK, COMPANY-PUNISHING-ATTACK (quarantine evidence so
  it can never cross the pass line).
- POLARIZED / NO-CONVERGENCE flag: |skew| > floor, bimodal clusters, or 2-of-3
  convergence failure at usable threshold.
- Frozen composite: quantifiable 50% / expression 30% / subjective 20% (capped),
  with within-block factor weights from ``config/weights_consensus.yaml``.
- Sector-relative two-extreme normalization (disabled when a sector has fewer
  than ``min_companies_for_two_extreme`` companies -> directional fallback).

Everything is deterministic and pre-registered; all thresholds come from the
frozen config. This module is a LEAF: no production code imports it.
"""

from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .config import load_consensus_config

# Review usability verdicts.
INSUFFICIENT = "INSUFFICIENT"       # <10: abstain, never neutral 0.5
DIRECTIONAL = "DIRECTIONAL"         # 10-49: moves weights, cannot pass/fail
DISTRIBUTIONAL = "DISTRIBUTIONAL"   # 50-99: skew/IQR usable, level directional
USABLE = "USABLE"                   # >=100: enters two-extreme + 2-of-3 convergence

# Attack/polarization flags.
F_BRIBE = "BRIBE-ATTACK"
F_PUNISHING = "COMPANY-PUNISHING-ATTACK"
F_POLARIZED = "POLARIZED"
F_NO_CONVERGENCE = "NO-CONVERGENCE"
F_SET_ASIDE = "SET-ASIDE"

# Blocks.
BLOCK_QUANTIFIABLE = "quantifiable"
BLOCK_EXPRESSION = "expression"
BLOCK_SUBJECTIVE = "subjective"
BLOCKS = (BLOCK_QUANTIFIABLE, BLOCK_EXPRESSION, BLOCK_SUBJECTIVE)

# Tiers.
TIER_A = "Type-A"   # official/transactional — pass-capable alone
TIER_B = "Type-B"   # modeled/estimated — directional only
TIER_C = "Type-C"   # reviews — 2-of-3 convergence required

# Profile buckets used by attack detection.
PROFILE_NEW = "new"
PROFILE_NOVICE = "novice"
PROFILE_STAR1 = "star_1"
PROFILE_STAR5 = "star_5"


@dataclass
class ReviewRecord:
    """One individual review (needed for burst/attack detection)."""

    star: int  # 1..5
    ts: int    # unix timestamp
    profile_bucket: str = "standard"  # new | novice | standard
    text: str = ""


@dataclass
class ReviewEvidence:
    """Review-platform evidence for one company, one source.

    ``n`` = usable review count. ``star_level`` in [0,5] or None when the
    platform does not expose a level (only volume/skewness count).
    """

    source: str
    n: int
    star_level: Optional[float] = None
    skewness: Optional[float] = None
    iqr: Optional[float] = None
    records: List[ReviewRecord] = field(default_factory=list)
    recent_weekly_volume: Optional[float] = None
    normal_weekly_volume: Optional[float] = None

    @property
    def profiles(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for r in self.records:
            counts[r.profile_bucket] = counts.get(r.profile_bucket, 0) + 1
            if r.star == 1:
                counts[PROFILE_STAR1] = counts.get(PROFILE_STAR1, 0) + 1
            if r.star == 5:
                counts[PROFILE_STAR5] = counts.get(PROFILE_STAR5, 0) + 1
        return counts


@dataclass
class EvidenceRow:
    """One factor's measured signal for a company (0..1 unless noted)."""

    factor: str
    value: float
    tier: str  # TIER_A | TIER_B | TIER_C
    source: str
    n: int = 0
    usability: Optional[str] = None
    is_set_aside: bool = False
    flagged: List[str] = field(default_factory=list)
    note: str = ""


@dataclass
class CompanyVerdict:
    """Final per-company consensus verdict (research-only output)."""

    ticker: str
    sector: str
    composite_score: float
    block_scores: Dict[str, float] = field(default_factory=dict)
    factor_scores: Dict[str, float] = field(default_factory=dict)
    flags: List[str] = field(default_factory=list)
    normalized: bool = False
    sector_n: int = 0
    converged: bool = True
    rows: List[EvidenceRow] = field(default_factory=list)


def usability_for_n(n: int, cfg: Dict) -> str:
    """Map a review count to its usability verdict (frozen ladder)."""
    us = cfg["usability"]
    if n < us["insufficient_threshold"]:
        return INSUFFICIENT
    if n < us["directional_threshold"]:
        return DIRECTIONAL
    if n < us["distributional_threshold"]:
        return DISTRIBUTIONAL
    return USABLE


def total_review_n(reviews: Sequence[ReviewEvidence]) -> int:
    """Total usable reviews across ALL platforms for a company."""
    return sum(r.n for r in reviews)


def is_set_aside(reviews: Sequence[ReviewEvidence], cfg: Dict) -> bool:
    """CEO SET-ASIDE rule: < threshold total reviews across all platforms."""
    return total_review_n(reviews) < cfg["usability"]["set_aside_threshold"]


def _same_star_burst(records: Sequence[ReviewRecord], window_hours: int, min_burst: int) -> bool:
    """True if >= min_burst same-star reviews land within window_hours."""
    if len(records) < min_burst:
        return False
    ts = sorted((r.ts, r.star) for r in records)
    window_secs = window_hours * 3600
    i = 0
    for j in range(len(ts)):
        while ts[j][0] - ts[i][0] > window_secs:
            i += 1
        burst = ts[i : j + 1]
        if len(burst) < min_burst:
            continue
        most_common_star, count = Counter(s for _, s in burst).most_common(1)[0]
        if count >= min_burst:
            return True
    return False


def _burst_records(records: Sequence[ReviewRecord], window_hours: int) -> List[ReviewRecord]:
    """Return the first maximal same-star burst within the window (or empty)."""
    if not records:
        return []
    ts = sorted((r.ts, r.star, r.profile_bucket) for r in records)
    window_secs = window_hours * 3600
    i = 0
    for j in range(len(ts)):
        while ts[j][0] - ts[i][0] > window_secs:
            i += 1
        burst = ts[i : j + 1]
        if len(burst) < 2:
            continue
        most_common_star, count = Counter(s for _, s, _ in burst).most_common(1)[0]
        if count >= 3:
            return [r for r in records if r.star == most_common_star]
    return []


def detect_bribe_attack(rev: ReviewEvidence, cfg: Dict) -> bool:
    """Obvious paid-review burst: same-star cluster in a short window.

    Requires >= ``min_same_star_burst`` reviews sharing a star level within
    ``burst_window_hours`` AND a suspicious profile ratio >= threshold measured
    WITHIN the burst (a burst of all-new/all-novice profiles is the obvious
    bribe signature, regardless of corpus size).
    """
    ba = cfg["flags"]["bribe_attack"]
    burst = _burst_records(rev.records, ba["burst_window_hours"])
    if len(burst) < ba["min_same_star_burst"]:
        return False
    suspicious = sum(1 for r in burst if r.profile_bucket in (PROFILE_NEW, PROFILE_NOVICE))
    return len(burst) > 0 and suspicious / len(burst) >= ba["suspicious_profile_ratio"]


def detect_punishing_attack(rev: ReviewEvidence, cfg: Dict) -> bool:
    """Organized negative barrage: >3x weekly volume spike OR dense novice 1-star."""
    pa = cfg["flags"]["punishing_attack"]
    if rev.recent_weekly_volume is not None and rev.normal_weekly_volume is not None:
        if rev.normal_weekly_volume > 0 and (
            rev.recent_weekly_volume / rev.normal_weekly_volume
            > pa["weekly_volume_spike_ratio"]
        ):
            return True
    prof = rev.profiles
    novice = prof.get(PROFILE_NOVICE, 0)
    ones = prof.get(PROFILE_STAR1, 0)
    if rev.n > 0 and novice / rev.n >= pa["novice_profile_ratio"] and ones / rev.n >= 0.5:
        return True
    return False


def detect_polarized(rev: ReviewEvidence, cfg: Dict) -> bool:
    """Bimodal/polarized: |skew| > floor OR two sizable clusters far apart."""
    pol = cfg["flags"]["polarized"]
    if rev.skewness is not None and abs(rev.skewness) > pol["skewness_abs_floor"]:
        return True
    prof = rev.profiles
    ones = prof.get(PROFILE_STAR1, 0)
    fives = prof.get(PROFILE_STAR5, 0)
    if ones >= pol["bimodal_min_cluster"] and fives >= pol["bimodal_min_cluster"]:
        return True
    return False


def flag_review_sources(reviews: Sequence[ReviewEvidence], cfg: Dict) -> Dict[str, List[str]]:
    """Return {source: [flags]} for review evidence."""
    out: Dict[str, List[str]] = {}
    for rev in reviews:
        flags: List[str] = []
        if detect_bribe_attack(rev, cfg):
            flags.append(F_BRIBE)
        if detect_punishing_attack(rev, cfg):
            flags.append(F_PUNISHING)
        if detect_polarized(rev, cfg):
            flags.append(F_POLARIZED)
        if flags:
            out[rev.source] = flags
    return out


def convergence_check(reviews: Sequence[ReviewEvidence], cfg: Dict) -> bool:
    """2-of-3 convergence for Type-C review evidence.

    Only USABLE sources count toward convergence. Fewer than 2 usable sources
    => NO-CONVERGENCE (cannot pass on reviews alone). When >=2 usable sources
    expose star levels, they must agree within a 1.0-star tolerance.
    """
    usable = [r for r in reviews if usability_for_n(r.n, cfg) == USABLE]
    if len(usable) < cfg["flags"]["polarized"]["min_converging_sources"]:
        return False
    levels = [r.star_level for r in usable if r.star_level is not None]
    if len(levels) < 2:
        return True  # convergence on volume/skew direction only
    return (max(levels) - min(levels)) <= 1.0


def block_score(factors: Dict[str, float], block: str, cfg: Dict) -> Optional[float]:
    """Weighted within-block factor score, or None if no factor has evidence."""
    factor_weights = cfg["weights"]["factors"][block]
    num = 0.0
    den = 0.0
    for name, w in factor_weights.items():
        v = factors.get(name)
        if v is not None:
            num += w * v
            den += w
    if den == 0:
        return None
    return num / den


def normalize_two_extreme(
    raw_scores: Dict[str, Optional[float]],
    cfg: Dict,
) -> Dict[str, Optional[float]]:
    """Sector-relative two-extreme normalization for one block.

    Maps each company's raw block score to [0,1] via (v - min)/(max - min).
    When fewer than ``min_companies_for_two_extreme`` companies have evidence,
    the block is NOT normalized (returns raw) — the caller marks it directional.
    """
    present = {t: v for t, v in raw_scores.items() if v is not None}
    if len(present) < cfg["sector"]["min_companies_for_two_extreme"]:
        return dict(raw_scores)
    lo = min(present.values())
    hi = max(present.values())
    if hi - lo < 1e-9:
        return {t: (0.5 if v is not None else None) for t, v in raw_scores.items()}
    return {
        t: ((v - lo) / (hi - lo) if v is not None else None)
        for t, v in raw_scores.items()
    }


def build_company_verdict(
    ticker: str,
    sector: str,
    rows: Sequence[EvidenceRow],
    cfg: Dict,
    reviews: Sequence[ReviewEvidence] = (),
) -> CompanyVerdict:
    """Compute the composite consensus verdict for one company.

    Applies SET-ASIDE, attack flags, polarization and convergence from the
    review evidence, then aggregates the frozen block/factor weights.
    """
    flags: List[str] = []
    block_factors: Dict[str, Dict[str, float]] = {b: {} for b in BLOCKS}

    if is_set_aside(reviews, cfg):
        flags.append(F_SET_ASIDE)
        rows = [r for r in rows if r.tier != TIER_C]

    source_flags = flag_review_sources(reviews, cfg)
    flagged_sources = set()
    for source, fl in source_flags.items():
        for f in fl:
            if f not in flags:
                flags.append(f)
        flagged_sources.add(source)

    usable_any = any(usability_for_n(r.n, cfg) == USABLE for r in reviews)
    if usable_any and not convergence_check(reviews, cfg):
        flags.append(F_NO_CONVERGENCE)

    for row in rows:
        if row.tier == TIER_C and row.source in flagged_sources:
            continue  # Quarantined evidence can never cross the pass line.
        if row.value is None:
            continue
        block = _block_for_factor(row.factor, cfg)
        if block is not None and row.factor not in block_factors[block]:
            block_factors[block][row.factor] = row.value

    block_scores: Dict[str, float] = {}
    for b in BLOCKS:
        bs = block_score(block_factors[b], b, cfg)
        if bs is not None:
            block_scores[b] = bs

    composite = 0.0
    for b, w in cfg["weights"]["blocks"].items():
        composite += w * block_scores.get(b, 0.0)

    factor_scores = {f: v for b in BLOCKS for f, v in block_factors[b].items()}

    return CompanyVerdict(
        ticker=ticker,
        sector=sector,
        composite_score=composite,
        block_scores=block_scores,
        factor_scores=factor_scores,
        flags=flags,
        sector_n=len({r.source for r in rows}),
        converged=F_NO_CONVERGENCE not in flags,
        rows=list(rows),
    )


def normalize_sector_verdicts(
    verdicts: Sequence[CompanyVerdict], cfg: Dict
) -> List[CompanyVerdict]:
    """Apply two-extreme normalization per sector on each block, in place.

    Companies whose block lacks evidence keep None. When a sector has fewer
    than ``min_companies_for_two_extreme`` scored companies, that block stays
    raw (directional fallback) and ``normalized`` is False.
    """
    sectors: Dict[str, List[CompanyVerdict]] = {}
    for v in verdicts:
        sectors.setdefault(v.sector, []).append(v)

    out: List[CompanyVerdict] = []
    for sector, members in sectors.items():
        for b in BLOCKS:
            raw = {v.ticker: v.block_scores.get(b) for v in members}
            normed = normalize_two_extreme(raw, cfg)
            for v in members:
                v.block_scores[b] = normed.get(v.ticker)
                if normed.get(v.ticker) is not None:
                    v.normalized = v.normalized or True
        for v in members:
            composite = 0.0
            for b, w in cfg["weights"]["blocks"].items():
                bs = v.block_scores.get(b)
                composite += w * (bs if bs is not None else 0.0)
            v.composite_score = composite
            out.append(v)
    return out


def _block_for_factor(factor: str, cfg: Dict) -> Optional[str]:
    for b in BLOCKS:
        if factor in cfg["weights"]["factors"][b]:
            return b
    return None