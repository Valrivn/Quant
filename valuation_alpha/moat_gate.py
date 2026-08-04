"""Qualitative moat gate (D-20260802-002) — product uniqueness for small caps.

The CEO's re-thesis: a small-cap holding's value depends on whether its product
is genuinely unique vs competitors ("what makes it different"), assessed from
external qualitative sources (Reddit, product intel / Amazon-style reviews,
brand mentions). This module produces a 0..1 moat/uniqueness composite that the
reinvestment screen consumes as a continuous tilt (never a hard floor).

Signals (each normalized 0..1, weighted, None-safe):
  1. product_intel: average review rating across G2/Capterra/App Store from the
     existing ProductIntelEngine datastore (0.4 weight) — the closest proxy we
     have to Amazon-style buyer reviews.
  2. reddit_signal: mean VADER sentiment of ticker mentions from the Reddit
     harvest (0.35 weight) — community "mindset"/mind-share proxy.
  3. coverage/breadth: the count of distinct moat nodes (products/brands)
     discovered for the ticker (0.25 weight) — product-family uniqueness proxy.

Any missing signal contributes 0 to its weighted slot; the composite is only
reported when at least one signal is present, else None (screen treats None as
tilt 0). A deterministic fallback path (``moat_score_from_parts``) is provided
so tests and offline runs never hit the network.
"""

from typing import Dict, Optional

import numpy as np

_PRODUCT_INTEL_W = 0.40
_REDDIT_W = 0.35
_BREADTH_W = 0.25
_BREADTH_MAX = 6  # saturate product-family breadth at 6 nodes

# Bounded helper mapping a 0..5 star rating / -1..1 sentiment into 0..1.
def _norm_rating(rating: float, lo: float, hi: float) -> Optional[float]:
    if rating is None or (isinstance(rating, float) and np.isnan(rating)):
        return None
    try:
        v = float(rating)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


def moat_score_from_parts(
    avg_rating: Optional[float] = None,      # e.g. 0..5 buyer review rating
    sentiment: Optional[float] = None,       # e.g. -1..1 VADER
    n_products: Optional[int] = None,        # product/brand breadth
    product_w: float = _PRODUCT_INTEL_W,
    reddit_w: float = _REDDIT_W,
    breadth_w: float = _BREADTH_W,
) -> Optional[float]:
    """Deterministic moat composite from pre-computed parts (no I/O).

    Returns None when every signal is missing, so callers can treat it as
    "no moat evidence -> no tilt".
    """
    r = _norm_rating(avg_rating, 0.0, 5.0) if avg_rating is not None else None
    s = _norm_rating(sentiment, -1.0, 1.0) if sentiment is not None else None
    if n_products is not None:
        b = max(0.0, min(1.0, int(n_products) / _BREADTH_MAX))
    else:
        b = None
    parts = [(r, product_w), (s, reddit_w), (b, breadth_w)]
    if all(p is None for p, _ in parts):
        return None
    total_w = sum(w for p, w in parts if p is not None)
    if total_w <= 0:
        return None
    return round(sum(p * w for p, w in parts if p is not None) / total_w, 4)


def build_moat_gate(
    product_intel_by_ticker: Optional[Dict[str, dict]] = None,
    reddit_by_ticker: Optional[Dict[str, float]] = None,
    breadth_by_ticker: Optional[Dict[str, int]] = None,
) -> Dict[str, Optional[float]]:
    """Aggregate moat scores for a ticker universe from external-signal dicts.

    Args:
        product_intel_by_ticker: ticker -> dict with key ``avg_rating`` (0..5),
            e.g. produced by ProductIntelEngine.compute_product_sentiment.
        reddit_by_ticker: ticker -> mean sentiment (-1..1) of Reddit mentions.
        breadth_by_ticker: ticker -> count of distinct products/brands.

    Returns {ticker: moat_score_or_None}. Pure; safe offline.
    """
    tickers = set()
    for d in (product_intel_by_ticker, reddit_by_ticker, breadth_by_ticker):
        if d:
            tickers |= set(d.keys())
    out: Dict[str, Optional[float]] = {}
    for t in tickers:
        pi = (product_intel_by_ticker or {}).get(t) or {}
        rating = pi.get("avg_rating") if isinstance(pi, dict) else None
        sent = (reddit_by_ticker or {}).get(t)
        breadth = (breadth_by_ticker or {}).get(t)
        out[t] = moat_score_from_parts(
            avg_rating=rating,
            sentiment=sent,
            n_products=breadth,
        )
    return out


def moat_compromise_flag(
    current_moat: Optional[float],
    prior_moat: Optional[float],
    drop_threshold: float = 0.30,
) -> bool:
    """Flag a moat compromise when the composite falls by >= drop_threshold.

    The CEO's exit rule: sell only when the qualitative moat is considered
    compromised — a material decline in the uniqueness composite, not a price
    move. Returns False when either value is unknown (no data -> no sell).
    """
    if current_moat is None or prior_moat is None:
        return False
    if current_moat == current_moat and prior_moat == prior_moat:
        return prior_moat - current_moat >= drop_threshold
    return False
