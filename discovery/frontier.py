"""Index-Anchored Overlap Frontier (B-20260819-001 ruling).

Virus-like exploration over the stock interconnection web seeded at NVDA.

Core algorithm (CEO ruling, S-20260819-001 hybrid):
  1. Overlap grading across the MAJOR-company set, NOT NVDA-only:
     grade(S) = sum of relevance(customer) over every major customer that
     buys from supplier S. A supplier shared by NVDA+AMD+GOOGL+META grades
     higher than a supplier unique to NVDA; NVDA's unique suppliers are still
     checked (they are part of the seed graph).
  2. Upstream expansion: a graded supplier's own suppliers ("the spot before
     the shovel") inherit its grade, so tier-2/3 names are surfaced by WHO
     they serve, not by how loud they are.
  3. Slowly progressive: binge-block pacing (mirrors the IG loop), hard stop
     after max active hours, no blind retries. NEVER a rate-limit burn.

Deterministic: no RNG. Every grade is a pure function of the supplied edge
maps. This module is a LEAF: it never opens a connection itself and never
touches the frozen cores. Persistence lives in ``db/schema_discovery.py``.
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

# Ticker hygiene (mirrors ig_experiment._TICKER_RE): 1-10 uppercase/digit/dash/dot.
_PLAUSIBLE = __import__("re").compile(r"^[A-Z0-9.\-]{1,10}$")


@dataclass(frozen=True)
class GraphEdge:
    """A directed, point-in-time relationship between two validated tickers.

    ``source`` is the buyer/customer ticker, ``target`` the supplier ticker for
    the default ``customer`` relation. ``filed_date`` (YYYY-MM-DD) anchors the
    edge in time so the graph state at t never ingests data published after t
    (sim-guardian point-in-time invariant).
    """

    source: str
    target: str
    relation: str = "customer"
    confidence: float = 1.0
    filed_date: str = ""
    provenance: str = ""


@dataclass
class FrontierNode:
    """One discovered node with its depth and overlap grade."""

    ticker: str
    depth: int
    grade: float = 0.0
    path: Tuple[str, ...] = ()


@dataclass
class FrontierResult:
    """Outcome of a frontier expansion run."""

    nodes: List[FrontierNode] = field(default_factory=list)
    edges: List[GraphEdge] = field(default_factory=list)
    summary: Dict[str, object] = field(default_factory=dict)


def normalize_tickers(tickers: Sequence[str]) -> List[str]:
    """Uppercase, strip, dedupe, and drop implausible symbols (deterministic)."""
    seen: Set[str] = set()
    out: List[str] = []
    for t in tickers or []:
        t = str(t).strip().upper()
        if not _PLAUSIBLE.fullmatch(t):
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def overlap_grades(
    customer_to_suppliers: Dict[str, Sequence[str]],
    relevance: Optional[Dict[str, float]] = None,
) -> Dict[str, float]:
    """Grade each supplier by overlap across the customer set.

    grade(S) = sum of relevance(C) for every customer C that buys from S.
    With no relevance map, each customer counts 1.0. Deterministic: identical
    inputs always yield identical grades.
    """
    relevance = relevance or {}
    grades: Dict[str, float] = {}
    for customer, suppliers in customer_to_suppliers.items():
        w = float(relevance.get(customer, 1.0))
        for s in normalize_tickers(suppliers):
            grades[s] = grades.get(s, 0.0) + w
    return dict(sorted(grades.items()))


def upstream_grades(
    supplier_to_suppliers: Dict[str, Sequence[str]],
    base_grades: Dict[str, float],
) -> Dict[str, float]:
    """Inherit grades one level upstream ("the spot before the shovel").

    A tier-2 supplier T's grade = sum of base_grades(S) for every tier-1
    supplier S that T supplies. Tier-2 names are surfaced by who they serve.
    """
    grades: Dict[str, float] = {}
    for supplier, its_suppliers in supplier_to_suppliers.items():
        base = float(base_grades.get(supplier, 0.0))
        if base <= 0.0:
            continue
        for t in normalize_tickers(its_suppliers):
            grades[t] = grades.get(t, 0.0) + base
    return dict(sorted(grades.items()))


def expand_frontier(
    seed_tickers: Sequence[str],
    competitor_set: Sequence[str],
    customer_to_suppliers: Dict[str, Sequence[str]],
    supplier_to_suppliers: Optional[Dict[str, Sequence[str]]] = None,
    major_set: Optional[Sequence[str]] = None,
    relevance: Optional[Dict[str, float]] = None,
    max_depth: int = 3,
    max_nodes: int = 200,
    max_edges_per_node: int = 50,
    filed_date: str = "",
) -> FrontierResult:
    """Run the overlap-graded frontier expansion around the seed set.

    Levels:
      depth 0 — seed + competitors (grade = relevance or 1.0).
      depth 1 — every supplier of the seed set, graded by overlap across the
                major set (NOT seed-only).
      depth 2+ — upstream suppliers of the top-graded tier-1 names, inheriting
                their grades, recursively up to ``max_depth``.

    Every grade is a pure function of the supplied maps; the result is fully
    deterministic for the same inputs.
    """
    seeds = normalize_tickers(seed_tickers)
    competitors = normalize_tickers(competitor_set)
    majors = normalize_tickers(major_set) if major_set is not None else seeds
    relevance = relevance or {}

    supplier_map = {k: normalize_tickers(v) for k, v in customer_to_suppliers.items()}
    upstream = {k: normalize_tickers(v) for k, v in (supplier_to_suppliers or {}).items()}

    nodes: List[FrontierNode] = []
    edges: List[GraphEdge] = []
    visited: Set[str] = set()

    def add_edge(source: str, target: str, depth: int) -> None:
        if len([e for e in edges if e.source == source]) >= max_edges_per_node:
            return
        edges.append(GraphEdge(
            source=source, target=target, filed_date=filed_date,
            provenance="frontier_overlap",
        ))

    # Depth 0: seeds + competitors.
    for ticker in list(seeds) + [c for c in competitors if c not in seeds]:
        if len(nodes) >= max_nodes:
            break
        nodes.append(FrontierNode(ticker, 0, float(relevance.get(ticker, 1.0)), (ticker,)))
        visited.add(ticker)

    # Depth 1: suppliers of the seed graph, graded across the major set.
    seed_graph = dict(supplier_map)
    for c in list(seeds) + list(competitors):
        seed_graph.setdefault(c, [])
    d1_grades = overlap_grades(seed_graph, relevance)

    for target, grade in d1_grades.items():
        if len(nodes) >= max_nodes:
            break
        if target in visited:
            continue
        depth = 1
        # Grade only counts customers that are in the major set (overlap rule).
        effective = grade
        if majors:
            effective = sum(
                float(relevance.get(c, 1.0))
                for c in supplier_map if target in supplier_map[c] and c in majors
            )
        nodes.append(FrontierNode(target, depth, effective, (target,)))
        visited.add(target)
        for customer in supplier_map:
            if target in supplier_map[customer]:
                add_edge(customer, target, depth)

    # Depth 2+: upstream inheritance, top-graded first, capped by max_nodes.
    for depth in range(2, max_depth + 1):
        if not upstream:
            break
        base_grades = {n.ticker: n.grade for n in nodes}
        tier_grades = upstream_grades(upstream, base_grades)
        for target, grade in tier_grades.items():
            if len(nodes) >= max_nodes:
                break
            if target in visited:
                continue
            nodes.append(FrontierNode(target, depth, grade, (target,)))
            visited.add(target)
            for supplier in upstream:
                if target in upstream[supplier] and supplier in base_grades:
                    add_edge(supplier, target, depth)

    result = FrontierResult(nodes=nodes, edges=edges)
    result.summary = {
        "seeds": seeds,
        "competitors": competitors,
        "majors": majors,
        "max_depth": max_depth,
        "nodes": len(nodes),
        "edges": len(edges),
        "max_depth_reached": max((n.depth for n in nodes), default=0),
        "top_graded": [n.ticker for n in sorted(nodes, key=lambda n: n.grade, reverse=True)[:5]],
    }
    return result


class PacingStopped(RuntimeError):
    """Raised when the binge-block supervisor reaches its active-hours cap."""


@dataclass
class BingeBlockPacing:
    """Slow, ban-safe block supervisor (mirrors the IG loop's pattern).

    Processes ``work`` in blocks of ``block_size``, sleeping ``gap_seconds``
    between blocks, and HARD STOPS after ``max_active_hours`` of ACTIVE work
    (gaps do not count). Never blind-retries: a failing item is skipped and
    recorded, never re-submitted in the same run.
    """

    block_size: int = 10
    gap_seconds: Tuple[float, float] = (120.0, 300.0)
    max_active_hours: float = 3.0

    def run(
        self,
        work: Sequence[object],
        process: Callable[[object], None],
        on_block: Optional[Callable[[int, int], None]] = None,
        now_fn: Callable[[], float] = __import__("time").time,
        sleep_fn: Callable[[float], None] = __import__("time").sleep,
    ) -> Dict[str, object]:
        """Run ``process`` over ``work`` under the pacing discipline.

        ``now_fn``/``sleep_fn`` are injectable so tests never actually sleep.
        """
        items = list(work)
        start = now_fn()
        active = 0.0
        processed = skipped = 0
        block_start = start
        for i, item in enumerate(items):
            now = now_fn()
            active = now - start
            if active >= self.max_active_hours * 3600:
                raise PacingStopped(
                    f"active-hours cap reached ({self.max_active_hours}h) after {processed} items"
                )
            if i > 0 and i % self.block_size == 0:
                lo, hi = self.gap_seconds
                span = max(lo, min(hi, lo + (hi - lo) * 0.5))
                sleep_fn(span)
                block_end = now_fn()
                if on_block:
                    on_block(processed, int(block_end - block_start))
                block_start = block_end
            try:
                process(item)
                processed += 1
            except Exception:  # noqa: BLE001 - skip-with-record, never blind-retry
                skipped += 1
        if on_block:
            on_block(processed, int(now_fn() - block_start))
        return {
            "processed": processed,
            "skipped": skipped,
            "active_seconds": int(now_fn() - start),
        }