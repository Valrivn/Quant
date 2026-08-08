"""Instagram independent discovery experiment (D-20260707-001).

PURPOSE
    Answer the CEO's question: can Instagram/TikTok -derived candidates generate
    alpha after passing the SAME standard stock screen (qualitative engine +
    quantitative baseline) that every pipeline candidate must cross?

DESIGN (research-only, isolated)
    - ``run_ig_experiment`` accepts an IG-derived candidate ticker list
      (supplied by the experiment runner; feed is provided by the caller, never
      fabricated).
    - Candidates are run through the EXACT gates the census uses (read-only):
        1. ticker hygiene + validation
        2. qualitative gate (AlternativeStrategyPipeline -> buy/strong_buy)
        3. quantitative baseline (quant_baseline_flags)
    - The PASS cohort is compared to a baseline cohort of companies the existing
      scrapers already surface (daily_aggregations tickers) so alpha is
      like-for-like. Alpha math lives in ``valuation_alpha.alpha``.
    - Nothing here touches portfolio/diversification/stochastic/backtesting
      cores. This module is a LEAF: only tests import it. It writes nothing to
      production tables.

Determinism / honesty
    - IG candidates are passed in (never invented). When the live IG feed is
      unavailable the experiment reports ``unfed`` rather than inventing data
      (no- fabrication invariant).
"""

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .census import _qualitative_gate, _quant_baseline_gate

_TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,10}$")


@dataclass
class IgCandidate:
    """Single IG-derived candidate after the standard screen."""

    ticker: str
    validated: bool = False
    qual_pass: bool = False
    qual_reason: str = ""
    quant_pass: bool = False
    quant_reason: str = ""
    liquidity_pass: bool = True
    liquidity_reason: str = ""

    @property
    def passed(self) -> bool:
        return self.validated and self.qual_pass and self.quant_pass and self.liquidity_pass

    @property
    def reason_chain(self) -> str:
        parts = []
        if not self.validated:
            parts.append("validation:fail")
        if not self.qual_pass:
            parts.append(self.qual_reason or "qual:not_buy")
        if not self.quant_pass:
            parts.append(self.quant_reason or "quant:fail")
        if not self.liquidity_pass:
            parts.append(self.liquidity_reason or "liq:fail")
        return ";".join(parts)


def _plausible_ticker(entity: str) -> bool:
    """Deterministic punctual: 1-10 uppercase letters/digits/dash/dot only."""
    return bool(_TICKER_RE.fullmatch((entity or "").strip().upper()))


def _validate_ticker(t: str) -> bool:
    """Consistent with census CIK resolution: pass only resolvable tickers.

    We reuse the CIK resolver when available; when the resolver is
    unavailable/fails the ticker is still accepted if it is a plausible symbol
    (the resolver only throws off-the-isy lookup, not a hard veto here). The
    qualitative + quant gates remain the real screen.
    """
    if not _plausible_ticker(t):
        return False
    try:
        from valuation_alpha.universe.cik_resolver import resolve_ciks

        resolved = resolve_ciks([t])
        return bool(resolved.get(t))
    except Exception:
        # Resolver unavailable (offline) -> defer to plausible-symbol rule.
        return True


def _screen_qual(t: str) -> Tuple[bool, str]:
    try:
        return _qualitative_gate(t)
    except Exception as exc:  # noqa: BLE001 - skip-with-reason
        return False, f"qual_error:{type(exc).__name__}"


def _screen_quant(t: str) -> Tuple[bool, str]:
    try:
        fails = _quant_baseline_gate([t])
        if t in fails:
            return False, f"quant:{fails[t]}"
        return True, ""
    except Exception as exc:  # noqa: BLE001 - skip-with-reason
        return False, f"quant_error:{type(exc).__name__}"


def run_ig_experiment(
    ig_tickers: List[str],
    live: bool = False,
) -> dict:
    """Screen IG candidates through the full standard stock screen.

    ``ig_tickers`` must come from IG-derived evidence (never fabricated). When
    ``ig_tickers`` is empty the experiment returns ``status: unfed`` instead of
    inventing a cohort (no- fabrication invariant).
    """
    if not ig_tickers:
        return {"status": "unfed", "candidates": [], "pass_cohort": [], "reasons": {}}

    candidates = []
    plausible_tickers = []
    for raw in ig_tickers:
        t = (str(raw or "")).strip().upper()
        if not _plausible_ticker(t):
            candidates.append(IgCandidate(ticker=t, validated=False))
            continue
        plausible_tickers.append(t)
        validated = _validate_ticker(t)
        qual_pass, qual_reason = _screen_qual(t)
        candidates.append(
            IgCandidate(
                ticker=t,
                validated=validated,
                qual_pass=qual_pass,
                qual_reason=qual_reason,
                quant_pass=False,
            )
        )

    qual_passers = [c for c in candidates if c.validated and c.qual_pass]
    qual_passing_tickers = [c.ticker for c in qual_passers]

    if qual_passing_tickers:
        try:
            qfails = _quant_baseline_gate(qual_passing_tickers)
            for c in qual_passers:
                if c.ticker in qfails:
                    c.quant_pass = False
                    c.quant_reason = f"quant:{qfails[c.ticker]}"
                else:
                    c.quant_pass = True
        except Exception as exc:
            for c in qual_passers:
                c.quant_pass = False
                c.quant_reason = f"quant_error:{type(exc).__name__}"

    pass_cohort = [c.ticker for c in candidates if c.passed]
    reasons = {c.ticker: c.reason_chain for c in candidates if not c.passed}
    
    res = {
        "status": "gated" if live else "dry",
        "candidates": candidates,
        "pass_cohort": pass_cohort,
        "reasons": reasons,
    }
    if live:
        try:
            from . import gate_data
            res["provenance"] = gate_data.coverage_summary(plausible_tickers)
        except Exception:
            res["provenance"] = {}
    return res


def current_scraper_cohort(limit: int = 50) -> List[str]:
    """Companies the existing scrapers/knowledge base already surface.

    Reads distinct tickers from the repo DB's ``daily_aggregations`` (whatever
    exists on disk; returns [] if the DB is absent). These are the "companies
    from our scraper already" the IG alpha is compared against.
    """
    db = Path(__file__).resolve().parents[1] / "reddit_quant.db"
    if not db.exists():
        return []
    try:
        con = sqlite3.connect(str(db))
        rows = con.execute(
            "SELECT DISTINCT ticker FROM daily_aggregations ORDER BY ticker LIMIT ?",
            (limit,),
        )
        tickers = [r[0] for r in rows.fetchall() if r[0]]
        con.close()
        return list(dict.fromkeys(tickers))
    except Exception:
        return []