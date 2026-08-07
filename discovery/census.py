"""P1 sandbox pass-through census runner (D-20260806-001, SEC 2.1).

ZERO pipeline contact: this runner only reads public interfaces. It never calls
portfolio/diversification allocator code and never writes to production tables.
(Sandbox persistence of the census rows is handled separately by the P1 census
step via ``db/schema_discovery.py``.)

For each structured source it:
  1. Fetches a mention batch (up to N=500) via ``structured_sources.py``. Live
     fetch only when ``DISCOVERY_LIVE=1``; otherwise the source is DEGRADED-
     tagged via ``deg_registry`` and recorded as such (no fake data).
  2. Validates mentions to tickers (CIK resolver, read-only).
  3. Runs each validated ticker through a READ-ONLY qualitative gate
     (``AlternativeStrategyPipeline``) and the quant baseline
     (``valuation_alpha.discovery_screen.quant_baseline_flags``).
  4. Produces a per-source pass-through census + reject reason histogram.

This is a minimal P1 runner, not the P3 integration harness.
"""

import concurrent.futures
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .deg_registry import DegradedRegistry, LIVE, DEGRADED
from .structured_sources import (
    SecEdgarNewFilersSource,
    RedditSource,
    StockTwitsSource,
    ApeWisdomSource,
    live_enabled,
)
from .ledger import Mention

# Per-source mention batch cap (SEC 2.1: up to N=500 validated tickers/source).
MAX_MENTIONS_PER_SOURCE = 500

# Per-source network timeout (seconds): one dead source cannot stall the census.
SOURCE_FETCH_TIMEOUT_S = 15

# Cap on tickers run through the qualitative gate per source (bounds runtime).
MAX_GATE_TICKERS_PER_SOURCE = 20

# Qualitative gate: a ticker passes if the read-only qualitative pipeline
# recommends a buy-class action.
QUAL_PASS_RECS = {"strong_buy", "buy"}


def _ensure_qual_path() -> None:
    """Put ``Qualitative/`` on sys.path so ``psychological``/``scraper`` resolve.

    The repo's pytest conftest does this for tests; a standalone ``python -m
    discovery.census`` run needs it too.
    """
    qual = Path(__file__).resolve().parent.parent / "Qualitative"
    if str(qual) not in sys.path:
        sys.path.insert(0, str(qual))


@dataclass
class SourceCensusRow:
    """Pass-through census for one source."""

    source_id: str
    status: str = DEGRADED  # LIVE | DEGRADED
    reason: Optional[str] = None
    raw_mentions: int = 0
    validated: int = 0
    gated: int = 0
    reject_reasons: Dict[str, int] = field(default_factory=dict)

    @property
    def pass_pct(self) -> float:
        if self.validated == 0:
            return 0.0
        return round(100.0 * self.gated / self.validated, 2)


def _qualitative_gate(ticker: str) -> tuple:
    """Read-only qualitative gate via AlternativeStrategyPipeline.

    Returns (passed: bool, reason: str). With no live signals the pipeline
    returns a neutral 'hold' recommendation, which does not pass the gate.
    """
    _ensure_qual_path()
    from Qualitative.psychological.qualitative_scoring import (
        create_alternative_strategy_pipeline,
    )

    pipeline = create_alternative_strategy_pipeline()
    out = pipeline.run(ticker=ticker, moat_signals={}, financial_inputs=None, z_score=None)
    if out.recommendation in QUAL_PASS_RECS:
        return True, out.recommendation
    return False, f"qual:{out.recommendation}"


def _quant_baseline_gate(tickers: List[str]) -> Dict[str, str]:
    """Read-only quant baseline gate via discovery_screen.quant_baseline_flags.

    Returns {ticker: reason} for tickers that FAIL the quant baseline. Names
    missing fundamental data are flagged (no_alpha_data) rather than passed.
    """
    import pandas as pd

    from valuation_alpha.discovery_screen import quant_baseline_flags

    names = pd.DataFrame({"ticker": tickers})
    flagged = quant_baseline_flags(names)
    flagged = flagged.set_index("ticker")
    fails: Dict[str, str] = {}
    for t in tickers:
        if t in flagged.index and not bool(flagged.loc[t, "pass_quant"]):
            fails[t] = str(flagged.loc[t, "quant_reason"])
    return fails


def _validate_tickers(mentions: List[Mention]) -> List[str]:
    """Dedupe mention entities and validate via CIK resolution (read-only).

    Returns the sorted list of validated tickers. With no live data this is
    empty. Fails closed on any resolver error (no tickers validated).
    """
    entities = list(dict.fromkeys(m.entity for m in mentions))
    if not entities:
        return []
    try:
        from valuation_alpha.universe.cik_resolver import resolve_ciks

        resolved = resolve_ciks(entities)
        return sorted({t for t, cik in resolved.items() if cik})
    except Exception:  # noqa: BLE001 - fail closed
        return []


def _fetch_with_timeout(source, limit: int, timeout_s: int = SOURCE_FETCH_TIMEOUT_S):
    """Run ``source.fetch`` in a worker thread with a hard timeout.

    Returns the FetchResult, or a degraded FetchResult tagged ``timeout`` if the
    fetch exceeds ``timeout_s``. One dead source cannot stall the census.
    """
    from .structured_sources import FetchResult

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(source.fetch, limit=limit)
        try:
            return fut.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            return FetchResult(source.source_id, [], degraded=True, reason="timeout")
        except Exception as exc:  # noqa: BLE001 - fail closed
            return FetchResult(source.source_id, [], degraded=True, reason=str(exc))


def _run_source(source, registry: DegradedRegistry, limit: int = MAX_MENTIONS_PER_SOURCE) -> SourceCensusRow:
    """Fetch one source and run its validated tickers through the gates."""
    row = SourceCensusRow(source_id=source.source_id)
    result = _fetch_with_timeout(source, limit)
    if result.degraded:
        row.reason = result.reason or "degraded"
        return row

    row.status = LIVE
    row.raw_mentions = len(result.mentions)
    validated = _validate_tickers(result.mentions)
    row.validated = len(validated)

    for ticker in validated[:MAX_GATE_TICKERS_PER_SOURCE]:
        try:
            passed, reason = _qualitative_gate(ticker)
        except Exception as exc:  # noqa: BLE001 - skip-with-reason on failure
            row.reject_reasons[f"qual_error:{ticker}"] = row.reject_reasons.get(f"qual_error:{ticker}", 0) + 1
            continue
        if not passed:
            row.reject_reasons[reason] = row.reject_reasons.get(reason, 0) + 1
            continue
        # Quant baseline gate (read-only).
        try:
            qfails = _quant_baseline_gate([ticker])
        except Exception as exc:  # noqa: BLE001 - skip-with-reason on failure
            row.reject_reasons[f"quant_error:{ticker}"] = row.reject_reasons.get(f"quant_error:{ticker}", 0) + 1
            continue
        if ticker in qfails:
            qreason = f"quant:{qfails[ticker]}"
            row.reject_reasons[qreason] = row.reject_reasons.get(qreason, 0) + 1
            continue
        row.gated += 1
    return row


def run_census(limit: int = MAX_MENTIONS_PER_SOURCE) -> Dict:
    """Run the P1 sandbox census across all structured sources.

    Returns a dict with per-source rows, the registry, and a run timestamp.
    """
    registry = DegradedRegistry()
    sources = [
        SecEdgarNewFilersSource(registry),
        RedditSource(registry),
        StockTwitsSource(registry),
        ApeWisdomSource(registry),
    ]

    rows: List[SourceCensusRow] = []
    for src in sources:
        rows.append(_run_source(src, registry, limit=limit))

    return {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "live_enabled": live_enabled(),
        "registry": registry,
        "rows": rows,
    }


def _format_census(census: Dict) -> str:
    lines = [
        f"run_at: {census['run_at']}",
        f"live_enabled: {census['live_enabled']}",
        "",
        "source | mentions | validated | gated | pass% | status",
        "-" * 60,
    ]
    for row in census["rows"]:
        lines.append(
            f"{row.source_id} | {row.raw_mentions} | {row.validated} | "
            f"{row.gated} | {row.pass_pct}% | {row.status}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    _census = run_census()
    print(_format_census(_census))
    print("\nReject reason histogram:")
    for row in _census["rows"]:
        if row.reject_reasons:
            print(f"  {row.source_id}: {row.reject_reasons}")
    print("\nDEGRADED reasons:")
    for row in _census["rows"]:
        if row.status == DEGRADED:
            print(f"  {row.source_id}: {row.reason}")