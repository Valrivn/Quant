"""
Reddit / WSB Sentiment Engine
Combines VADER compound + WSB slang boost + emoji sentiment + context window.
Returns a structured score dict with label, confidence, and hit details.
"""

import re
from typing import Dict, List

import nltk
from nltk.sentiment.vader import SentimentIntensityAnalyzer

nltk.download("vader_lexicon", quiet=True)


class RedditSentimentEngine:
    """Lightweight Reddit/WSB sentiment scorer — no external model downloads."""

    # ── WSB slang definitions ──────────────────────────────────────────
    # Each pattern maps to a sentiment shift applied on top of the VADER
    # compound score.  Multi-word patterns use \b...\b word boundaries.
    _BULLISH_PATTERNS: List[tuple] = [
        (re.compile(r"\bdiamond\s+hands?\b", re.I), "diamond hands"),
        (re.compile(r"\bmoon\b", re.I), "moon"),
        (re.compile(r"\btendies\b", re.I), "tendies"),
        (re.compile(r"\bbuy\s+the\s+dip\b", re.I), "buy the dip"),
        (re.compile(r"\bYOLO\b"), "YOLO"),
        (re.compile(r"\brocket\b", re.I), "rocket"),
        (re.compile(r"\bbull\s+gang\b", re.I), "bull gang"),
        (re.compile(r"\bcalls?\s+printing\b", re.I), "calls printing"),
        (re.compile(r"\bto\s+the\s+moon\b", re.I), "to the moon"),
        (re.compile(r"\bGME\b"), "GME"),
        (re.compile(r"\bAMC\b"), "AMC"),
        (re.compile(r"\bbullish\b", re.I), "bullish"),
        (re.compile(r"\bundervalued\b", re.I), "undervalued"),
    ]

    _BEARISH_PATTERNS: List[tuple] = [
        (re.compile(r"\bpaper\s+hands?\b", re.I), "paper hands"),
        (re.compile(r"\bbagholder\b", re.I), "bagholder"),
        (re.compile(r"\bGUH\b"), "GUH"),
        (re.compile(r"\bputs?\s+printing\b", re.I), "puts printing"),
        (re.compile(r"\bblood\s+red\b", re.I), "blood red"),
        (re.compile(r"\bdump\b", re.I), "dump"),
        (re.compile(r"\bbear\s+gang\b", re.I), "bear gang"),
        (re.compile(r"\bsell\s+everything\b", re.I), "sell everything"),
        (re.compile(r"\brekt\b", re.I), "rekt"),
        (re.compile(r"\bbearish\b", re.I), "bearish"),
        (re.compile(r"\bovervalued\b", re.I), "overvalued"),
        (re.compile(r"\bcrash\b", re.I), "crash"),
    ]

    # ── Emoji sentiment ────────────────────────────────────────────────
    _BULLISH_EMOJIS = list("🚀📈💰🐂🟢")
    _BEARISH_EMOJIS = list("📉💀🔴🐻")


    def __init__(self, slang_shift: float = 0.15, emoji_shift: float = 0.1):
        """
        Parameters
        ----------
        slang_shift : float
            Shift applied per WSB slang hit (±).
        emoji_shift : float
            Shift applied per emoji hit (±).
        """
        self._sia = SentimentIntensityAnalyzer()
        self._slang_shift = slang_shift
        self._emoji_shift = emoji_shift

    # ── public API ─────────────────────────────────────────────────────

    def score(self, text: str, upvotes: int = 0) -> Dict:
        """Score a single Reddit post.

        Parameters
        ----------
        text : str
            Post title + body (concatenated).
        upvotes : int, optional
            Post upvote count for context-window boost.

        Returns
        -------
        dict with keys: compound, label, confidence, slang_hits, emoji_hits
        """
        if not text or not text.strip():
            return self._empty_result()

        # 1) VADER base compound
        compound = self._sia.polarity_scores(text)["compound"]

        # 2) WSB slang boost/penalty
        slang_hits: List[str] = []
        for pat, name in self._BULLISH_PATTERNS:
            if pat.search(text):
                compound = _clamp(compound + self._slang_shift)
                slang_hits.append(name)
        for pat, name in self._BEARISH_PATTERNS:
            if pat.search(text):
                compound = _clamp(compound - self._slang_shift)
                slang_hits.append(name)

        # 3) Emoji sentiment boost
        emoji_hits: List[str] = []
        emoji_bull = 0
        emoji_bear = 0
        for ch in text:
            if ch in self._BULLISH_EMOJIS:
                emoji_hits.append(ch)
                emoji_bull += 1
            elif ch in self._BEARISH_EMOJIS:
                emoji_hits.append(ch)
                emoji_bear += 1

        # Bullish emojis: +0.1 each, capped at +0.3
        bull_bonus = min(emoji_bull * self._emoji_shift, 0.3)
        # Bearish emojis: -0.1 each, capped at -0.3
        bear_penalty = max(-emoji_bear * self._emoji_shift, -0.3)
        compound = _clamp(compound + bull_bonus + bear_penalty)

        # 4) Context window: high upvotes (>100) AND compound > 0.5 → +0.1
        if upvotes > 100 and compound > 0.5:
            compound = _clamp(compound + 0.1)

        # 5) Label mapping
        if compound > 0.2:
            label = 2  # bullish
        elif compound < -0.2:
            label = 0  # bearish
        else:
            label = 1  # neutral

        # 6) Confidence
        abs_c = abs(compound)
        if abs_c > 0.6:
            confidence = "high"
        elif abs_c > 0.3:
            confidence = "medium"
        else:
            confidence = "low"

        return {
            "compound": round(compound, 4),
            "label": label,
            "confidence": confidence,
            "slang_hits": slang_hits,
            "emoji_hits": emoji_hits,
        }

    def score_batch(self, texts: List[str], upvotes: List[int] = None) -> List[Dict]:
        """Score a batch of texts.  Optional per-text upvotes list."""
        if upvotes is None:
            upvotes = [0] * len(texts)
        return [self.score(t, u) for t, u in zip(texts, upvotes)]

    # ── helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _empty_result() -> Dict:
        return {
            "compound": 0.0,
            "label": 1,
            "confidence": "low",
            "slang_hits": [],
            "emoji_hits": [],
        }


def _clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))
