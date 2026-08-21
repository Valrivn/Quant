"""Wiki-Census Sandbox Runner (B-20260820-001 ruling).

Validates Wikidata discovery data against SEC requirements and point-in-time constraints.
"""

import os
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Sequence

from discovery.deg_registry import DEGRADED, LIVE


@dataclass
class WikiCensusRow:
    source_id: str = "wikidata"
    status: str = DEGRADED
    raw_companies: int = 0
    validated: int = 0
    gated: int = 0
    reject_reasons: Dict[str, int] = field(default_factory=dict)
    dated_edges: int = 0
    pct_dated: float = 0.0


def pit_unlock_check(pct_dated: float, bar_pct: float = 50.0) -> bool:
    """Implement CEO's >=50% dated edge check for point-in-time unlock."""
    return pct_dated >= bar_pct


def run_wiki_census(
    companies: Dict[str, str],
    edges: Sequence[dict],
    cik_resolver: Callable[[str], Optional[str]],
    gate_fn: Optional[Callable[[str], bool]] = None,
    live: Optional[bool] = None,
) -> WikiCensusRow:
    """Run sandboxed census validation on Wikidata companies and edges."""
    if live is None:
        live = os.environ.get("DISCOVERY_LIVE") == "1"

    if not live:
        return WikiCensusRow(status=DEGRADED)

    raw_companies = len(companies)
    validated = 0
    gated = 0
    reject_reasons: Dict[str, int] = {}

    for qid, ticker in companies.items():
        if not ticker:
            reject_reasons["no_cik"] = reject_reasons.get("no_cik", 0) + 1
            continue

        try:
            cik = cik_resolver(ticker)
        except Exception:
            cik = None

        if cik is None:
            reject_reasons["no_cik"] = reject_reasons.get("no_cik", 0) + 1
            continue

        validated += 1

        if gate_fn is not None:
            try:
                passed = gate_fn(ticker)
            except Exception:
                passed = False

            if passed:
                gated += 1
            else:
                reject_reasons["failed_gate"] = reject_reasons.get("failed_gate", 0) + 1
        else:
            gated += 1

    # Compute PIT dated edges
    total_edges = len(edges)
    dated_count = 0
    for e in edges:
        if e.get("valid_from") or e.get("valid_to"):
            dated_count += 1

    pct_dated = (dated_count / total_edges * 100.0) if total_edges > 0 else 0.0

    return WikiCensusRow(
        source_id="wikidata",
        status=LIVE,
        raw_companies=raw_companies,
        validated=validated,
        gated=gated,
        reject_reasons=reject_reasons,
        dated_edges=dated_count,
        pct_dated=pct_dated,
    )
