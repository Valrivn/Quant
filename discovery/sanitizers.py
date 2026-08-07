"""Video-signal hygiene sanitizers (D-20260806-001, SEC 4).

Each sanitizer returns a deterministic verdict (KEEP / EXCLUDE) with reason
codes. All thresholds come from config/weights_discovery.yaml (invariant 4).
Fully deterministic: no stochastic draws of any kind.
"""

from dataclasses import dataclass, field
from typing import List, Optional

from .config_loader import load_discovery_config

KEEP = "KEEP"
EXCLUDE = "EXCLUDE"


@dataclass
class SanitizerVerdict:
    """Deterministic verdict from a sanitizer."""

    action: str  # KEEP | EXCLUDE
    reason_codes: List[str] = field(default_factory=list)

    @property
    def excluded(self) -> bool:
        return self.action == EXCLUDE


class CloutChaserSanitizer:
    """SEC 4.1: exclude videos riding an already-exploded stock.

    Exclusion when runup_ratio AND mention_velocity floors hold AND the mention
    spike LAGS the run-up start (explosion_lag > min).
    """

    def __init__(self, config: Optional[dict] = None):
        cfg = config or load_discovery_config()
        cc = cfg["sanitizers"]["clout_chaser"]
        self.runup_ratio_floor = float(cc["runup_ratio_floor"])
        self.mention_velocity_floor = float(cc["mention_velocity_floor"])
        self.explosion_lag_min_days = float(cc["explosion_lag_min_days"])

    def evaluate(
        self,
        price: float,
        week_52_high: float,
        mentions_7d: float,
        mentions_28d: float,
        explosion_lag_days: float,
    ) -> SanitizerVerdict:
        runup_ratio = price / week_52_high if week_52_high > 0 else 0.0
        mention_velocity = mentions_7d / mentions_28d if mentions_28d > 0 else 0.0
        reasons: List[str] = []
        if runup_ratio >= self.runup_ratio_floor:
            reasons.append("runup_ratio>=floor")
        if mention_velocity >= self.mention_velocity_floor:
            reasons.append("mention_velocity>=floor")
        if explosion_lag_days > self.explosion_lag_min_days:
            reasons.append("spike_lags_runup")

        # Exclusion requires ALL three conditions.
        if len(reasons) == 3:
            return SanitizerVerdict(EXCLUDE, reasons)
        return SanitizerVerdict(KEEP, reasons)


class NicheSanitizer:
    """SEC-4.2: prefer low absolute popularity + healthy non-viral engagement."""

    def __init__(self, config: Optional[dict] = None):
        cfg = config or load_discovery_config()
        niche = cfg["sanitizers"]["niche"]
        self.views_ceiling = float(niche["views_ceiling"])
        self.comment_to_view_band = tuple(niche["comment_to_view_band"])
        self.view_to_follower_band = tuple(niche["view_to_follower_band"])

    def evaluate(
        self,
        views: float,
        comments: float,
        followers: float,
    ) -> SanitizerVerdict:
        reasons: List[str] = []
        if views > self.views_ceiling:
            reasons.append(f"views>{self.views_ceiling:g}")
        ctv = comments / views if views > 0 else 0.0
        if not (self.comment_to_view_band[0] <= ctv <= self.comment_to_view_band[1]):
            reasons.append("comment_to_view_out_of_band")
        vtf = views / followers if followers > 0 else 0.0
        if not (self.view_to_follower_band[0] <= vtf <= self.view_to_follower_band[1]):
            reasons.append("view_to_follower_out_of_band")
        if reasons:
            return SanitizerVerdict(EXCLUDE, reasons)
        return SanitizerVerdict(KEEP, [])


class AdSanitizer:
    """SEC-4.3: ad/sponsored detection — OR-rule, any hit => EXCLUDE."""

    def __init__(self, config: Optional[dict] = None):
        cfg = config or load_discovery_config()
        ad = cfg["sanitizers"]["ad"]
        self.hashtags = [h.lower() for h in ad["hashtags"]]
        self.affiliate_patterns = ad["affiliate_patterns"]
        self.brand_account_signals = ad["brand_account_signals"]
        eab = ad["engagement_anomaly_bands"]
        self.comment_to_view_band = tuple(eab["comment_to_view"])
        self.view_to_follower_band = tuple(eab["view_to_follower"])

    def evaluate(
        self,
        caption: str = "",
        bio: str = "",
        hashtags: Optional[List[str]] = None,
        brand_account: bool = False,
        views: float = 0.0,
        comments: float = 0.0,
        followers: float = 0.0,
    ) -> SanitizerVerdict:
        reasons: List[str] = []
        text = f"{caption or ''} {bio or ''}".lower()

        # Hashtag OR-rule (case-insensitive).
        for tag in hashtags or []:
            if tag.lower() in self.hashtags:
                reasons.append(f"hashtag:{tag.lower()}")
                break

        # Affiliate / tracked-link patterns.
        for pat in self.affiliate_patterns:
            if pat.lower() in text:
                reasons.append(f"affiliate:{pat}")
                break

        # Brand-account signals.
        for sig in self.brand_account_signals:
            if sig.lower() in text or (brand_account and sig == "verified_brand"):
                reasons.append(f"brand:{sig}")
                break

        # Engagement anomalies (OR-rule).
        ctv = comments / views if views > 0 else 0.0
        if not (self.comment_to_view_band[0] <= ctv <= self.comment_to_view_band[1]):
            reasons.append("engagement:comment_to_view")
        vtf = views / followers if followers > 0 else 0.0
        if not (self.view_to_follower_band[0] <= vtf <= self.view_to_follower_band[1]):
            reasons.append("engagement:view_to_follower")

        if reasons:
            return SanitizerVerdict(EXCLUDE, reasons)
        return SanitizerVerdict(KEEP, [])