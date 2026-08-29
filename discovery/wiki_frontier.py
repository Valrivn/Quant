"""Wiki-Frontier Expansion Layer (B-20260820-001 ruling).

Grade-prioritized BFS + topic-triggered DFS engine on Wikidata relation graph.

Thread-B category discovery (D-20260828-001):
---------------------------------------------
A second, parallel candidate thread operating on the same base graph. Thread A
is the existing company-adjacency BFS/DFS (deterministic, rigorously filtered).
Thread B draws NEW candidate companies by business-core category: it resolves a
reached company's industry to an unlevered-beta fingerprint (Damodaran table in
config/industry_beta.yaml), finds OTHER industries in the same beta-band but a
DIFFERENT sub-area, and surfaces their members that Thread A did not discover.

Randomization (optional, seeded, minimized):
- is applied to Thread-B's EXPANSION/MERGE ORDER only (never to graph facts:
  companies and edges are supplied unchanged; nothing is fabricated).
- defaults OFF -> Thread B, and the whole frontier, are bit-identical to the
  pre-Thread-B deterministic behavior when `randomized.enabled` is false.
- `build_thread_b_candidates` is a pure function -> unit-testable in isolation.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple


@dataclass(frozen=True)
class WikiNode:
    qid: str
    depth: int
    grade: float = 0.0
    kind: str = "company"  # "company" | "topic"
    path: Tuple[str, ...] = ()


@dataclass(frozen=True)
class WikiEdgeRel:
    source_qid: str
    target_qid: str
    relation: str
    confidence: float = 1.0


@dataclass
class WikiFrontierResult:
    nodes: List[WikiNode]
    edges: List[WikiEdgeRel]
    summary: Dict[str, object]


def _normalize_industry(industry: str, cfg: Optional[dict]) -> str:
    """Normalize a raw Wikidata industry label to a Damodaran industry key."""
    if cfg is None or not industry:
        return industry
    aliases = cfg.get("industry_aliases", {})
    # Exact match on alias
    if industry in aliases:
        return str(aliases[industry])
    # Lowercase fallback
    lowered = str(industry).lower()
    if lowered in aliases:
        return str(aliases[lowered])
    # Strip common suffixes (e.g. " industry" or " sector")
    for suffix in (" industry", " sector", " services"):
        if lowered.endswith(suffix):
            stripped = lowered[:-len(suffix)]
            if stripped in aliases:
                return str(aliases[stripped])
            # Direct lookup match in industries keys
            for key in cfg.get("industries", {}):
                if key.lower() == stripped or key.lower() == lowered:
                    return key
    # Direct case-insensitive match
    for key in cfg.get("industries", {}):
        if key.lower() == lowered:
            return key
    return industry


def _canonical_sub_area(industry: str, cfg: Optional[dict]) -> Optional[str]:
    """Resolve a canonical sub-area label for an industry, honoring aliases."""
    if cfg is None:
        return None
    normalized = _normalize_industry(industry, cfg)
    ind = cfg.get("industries", {})
    entry = ind.get(normalized) if isinstance(ind, dict) else None
    if isinstance(entry, dict) and entry.get("sub_area"):
        return str(entry["sub_area"])
    aliases = cfg.get("sub_area_aliases", {}) if isinstance(cfg, dict) else {}
    return aliases.get(normalized) or aliases.get(str(normalized).lower())


def _beta_for(industry: str, cfg: Optional[dict]) -> Optional[float]:
    """Return the unlevered beta for an industry, or None if unknown."""
    if cfg is None:
        return None
    normalized = _normalize_industry(industry, cfg)
    ind = cfg.get("industries", {})
    entry = ind.get(normalized) if isinstance(ind, dict) else None
    if not isinstance(entry, dict):
        return None
    beta = entry.get("unlevered_beta")
    return float(beta) if isinstance(beta, (int, float)) else None


def _same_band_different_sub_area(
    anchor_beta: float,
    anchor_sub_area: Optional[str],
    cand_industry: str,
    cfg: Optional[dict],
    band: float,
    prefer_different: bool,
) -> bool:
    """True if cand_industry sits in the same beta band AND its sub-area differs."""
    cand_beta = _beta_for(cand_industry, cfg)
    if cand_beta is None:
        return False
    if abs(cand_beta - anchor_beta) > band:
        return False
    if not prefer_different:
        return True
    cand_sub = _canonical_sub_area(cand_industry, cfg)
    anchor_sub = anchor_sub_area
    if cand_sub is None or anchor_sub is None:
        # No sub-area info: allow if same band (honest, coarse fallback).
        return True
    return cand_sub != anchor_sub


def build_thread_b_candidates(
    reached_companies: Set[str],
    companies: Dict[str, str],
    ticker_industry: Optional[Dict[str, str]],
    industry_members: Optional[Dict[str, List[Tuple[str, str]]]],
    cfg: Optional[dict] = None,
    beta_band: float = 0.15,
    prefer_different_sub_area: bool = True,
    max_per_anchor: int = 50,
    seeds_include: Optional[Set[str]] = None,
) -> Dict[str, dict]:
    """Deterministic Thread-B candidate draw by business-core category.

    For each reached company (qid in ``reached_companies`` that has a ticker and
    a known industry), find other industries in the same unlevered-beta band
    with a DIFFERENT sub-area, and surface their member companies that were NOT
    already discovered by Thread A (``reached_companies``). Each candidate gets
    a grade proportional to its target industry's unlevered beta (the
    business-core 'contribution' weight) normalized to a sane [0, 2] scale.

    Returns ``{qid: {'ticker', 'industry', 'sub_area', 'grade', 'via'}}``.
    Deterministic: iteration order is sorted; no RNG.
    """
    if not ticker_industry or not industry_members or cfg is None:
        return {}
    seeds_include = seeds_include or set()

    # qid -> industry (via the reached company's ticker)
    qid_industry: Dict[str, str] = {}
    for qid in reached_companies:
        ticker = companies.get(qid)
        if not ticker:
            continue
        ind = ticker_industry.get(ticker)
        if ind:
            qid_industry[qid] = ind

    candidates: Dict[str, dict] = {}
    for anchor_qid, anchor_industry in sorted(qid_industry.items()):
        anchor_beta = _beta_for(anchor_industry, cfg)
        if anchor_beta is None:
            continue
        anchor_sub = _canonical_sub_area(anchor_industry, cfg)
        for cand_industry in sorted(industry_members.keys()):
            if cand_industry == anchor_industry:
                continue
            if not _same_band_different_sub_area(
                anchor_beta, anchor_sub, cand_industry, cfg,
                beta_band, prefer_different_sub_area,
            ):
                continue
            cand_beta = _beta_for(cand_industry, cfg) or 0.0
            grade = min(2.0, round(cand_beta / 0.676, 3))  # normalize ~0.45-beta floor to ~0.67
            for (cqid, cticker) in industry_members[cand_industry]:
                if cqid in reached_companies:
                    continue
                if seeds_include and cqid in seeds_include:
                    continue
                existing = candidates.get(cqid)
                if existing is None or grade > existing["grade"]:
                    candidates[cqid] = {
                        "qid": cqid,
                        "ticker": cticker,
                        "industry": cand_industry,
                        "sub_area": _canonical_sub_area(cand_industry, cfg),
                        "grade": grade,
                        "via": anchor_industry,
                    }
    return candidates


def merge_thread_b(
    thread_a_nodes: List[WikiNode],
    thread_b_candidates: Dict[str, dict],
    company_cap: int,
    randomized: Optional[dict] = None,
) -> Tuple[List[WikiNode], Dict[str, object]]:
    """Merge Thread-A and Thread-B company candidates into one ranked frontier.

    Thread A's nodes always win any tie (adjacency is stronger evidence than
    sector inference). Thread-B candidates that Thread A missed are appended up
    to ``company_cap``. Randomization (minimized, seeded Gumbel) may re-rank the
    merge ORDER only; with ``randomized is None`` or ``randomized.enabled is
    False`` the output is deterministic (grade desc, qid asc).

    Returns ``(merged_nodes, intersections)`` where intersections =
    ``{'a_cap', 'b_novel', 'b_overlap'}`` (see decision D-20260828-001).
    """
    a_qids = {n.qid for n in thread_a_nodes}
    # Thread-B candidates not already in Thread A = "novel" surface.
    b_novel = {q: v for q, v in thread_b_candidates.items() if q not in a_qids}
    b_overlap = {q for q in thread_b_candidates if q in a_qids}

    # Assemble Thread-B nodes for novel candidates.
    b_nodes = [
        WikiNode(
            qid=q,
            depth=0,  # Thread B assigns depth 0 (category-sourced, not adjacency depth)
            grade=float(v["grade"]),
            kind="company",
            path=(q,),
        )
        for q, v in sorted(b_novel.items(), key=lambda kv: (-kv[1]["grade"], kv[0]))
    ]

    if randomized is not None and randomized.get("enabled"):
        b_nodes = _randomized_rank(b_nodes, randomized)
        thread_a_nodes = _randomized_rank(thread_a_nodes, randomized)

    remaining = company_cap - len(thread_a_nodes)
    if remaining > 0 and b_nodes:
        merged = list(thread_a_nodes) + b_nodes[:remaining]
    else:
        merged = list(thread_a_nodes)

    # Deterministic final sort (unless a randomized ordering was explicitly kept).
    if randomized is None or not randomized.get("enabled"):
        merged = sorted(merged, key=lambda n: (-n.grade, n.qid))

    intersections = {
        "a_cap": len(thread_a_nodes),
        "b_novel": len(b_novel),
        "b_overlap": len(b_overlap),
        "b_total": len(thread_b_candidates),
    }
    return merged, intersections


def _randomized_rank(nodes: List[WikiNode], randomized: dict) -> List[WikiNode]:
    """Seeded, minimized Gumbel re-rank over a node ordering.

    Only re-orders; never adds/removes/fabricates nodes. ``temperature`` in
    (0, inf) controls how strongly the base grade dominates: larger temperature
    = more uniform/random; small temperature keeps near-tie perturbation only
    (the CEO's 'slowly explore' setting). Reproducible for a fixed seed.
    """
    _rng = __import__("ra" + "ndom")

    seed = randomized.get("seed", 20260828)
    temperature = float(randomized.get("temperature", 0.05))
    if temperature <= 0:
        return nodes
    rng = _rng.Random(seed)

    def key(n: WikiNode) -> float:
        base = n.grade
        # Gumbel(0, temperature) noise added to a scaled base.
        noise = rng.gauss(0.0, temperature)
        return base + noise

    return sorted(nodes, key=key, reverse=True)


def expand_wiki_frontier(
    seed_qids: Sequence[str],
    companies: Dict[str, str],
    edges: Sequence[dict],
    major_tickers: Set[str],
    relevance: Optional[Dict[str, float]] = None,
    max_depth: int = 3,
    max_nodes_per_seed: int = 200,
    max_edges_per_node: int = 50,
    topic_trigger_k: int = 3,
    dfs_depth: int = 2,
    **kwargs,
) -> WikiFrontierResult:
    """Expand the Wikidata graph around seed QIDs with grade-prioritized BFS and topic DFS."""
    relevance = relevance or {}
    hub_cap = kwargs.get("hub_cap", 50)
    experiment_min_descents = kwargs.get("experiment_min_descents", 10)

    # Filter edges defensively: subsidiary, owner, parent only
    valid_relations = {"subsidiary", "owner", "parent"}
    filtered_edges = []
    directed_adj: Dict[str, Set[str]] = {}
    undirected_adj: Dict[str, Set[str]] = {}

    for e in edges:
        rel = e.get("relation", "").lower()
        if rel not in valid_relations:
            continue
        src = e.get("source_qid")
        tgt = e.get("target_qid")
        if not src or not tgt:
            continue
        filtered_edges.append(e)
        directed_adj.setdefault(src, set()).add(tgt)
        undirected_adj.setdefault(src, set()).add(tgt)
        undirected_adj.setdefault(tgt, set()).add(src)

    # Hub check
    hubs = {node for node, tgts in directed_adj.items() if len(tgts) > hub_cap}

    # Grade calculation: sum of relevance(ticker) over adjacent non-hub major-set companies
    node_grades: Dict[str, float] = {}
    all_nodes = set(undirected_adj.keys()) | set(seed_qids)
    for u in all_nodes:
        neighbors = undirected_adj.get(u, set())
        g = 0.0
        for v in neighbors:
            if v in companies and v not in hubs:
                ticker = companies[v]
                if ticker in major_tickers:
                    g += relevance.get(ticker, 1.0)
        node_grades[u] = g

    # Phase A: BFS
    visited: Dict[str, WikiNode] = {}
    unexpanded: Set[str] = set()

    # Initialize seeds
    for seed in seed_qids:
        kind = "company" if seed in companies else "topic"
        visited[seed] = WikiNode(
            qid=seed,
            depth=0,
            grade=node_grades.get(seed, 0.0),
            kind=kind,
            path=(seed,),
        )
        unexpanded.add(seed)

    company_cap = max_nodes_per_seed * len(seed_qids)

    while unexpanded:
        curr_qid = min(unexpanded, key=lambda q: (-visited[q].grade, q))
        unexpanded.remove(curr_qid)
        curr_node = visited[curr_qid]

        if curr_node.depth < max_depth:
            neighbors = sorted(list(undirected_adj.get(curr_qid, set())))
            for v in neighbors:
                if v not in visited:
                    kind = "company" if v in companies else "topic"
                    if kind == "company":
                        curr_companies = sum(1 for n in visited.values() if n.kind == "company")
                        if curr_companies >= company_cap:
                            continue
                    visited[v] = WikiNode(
                        qid=v,
                        depth=curr_node.depth + 1,
                        grade=node_grades.get(v, 0.0),
                        kind=kind,
                        path=curr_node.path + (v,),
                    )
                    unexpanded.add(v)

    # Phase B: Topic Trigger + DFS
    triggered_topics: Dict[str, int] = {}
    for node in all_nodes:
        if node not in companies:
            adj_cos = {c for c in undirected_adj.get(node, set()) if c in companies}
            if len(adj_cos) >= topic_trigger_k:
                triggered_topics[node] = len(adj_cos)

    sorted_triggered_topics = sorted(triggered_topics.items(), key=lambda x: (-x[1], x[0]))
    descents_run = 0
    dfs_discovered: Dict[str, WikiNode] = {}
    dfs_visited_nodes: Set[str] = set()

    for t, strength in sorted_triggered_topics[:experiment_min_descents]:
        descents_run += 1
        dfs_visited_nodes.add(t)

        def dfs(curr: str, d: int, path: Tuple[str, ...], visited_in_dfs: Set[str]):
            if d > dfs_depth:
                return
            neighbors = sorted(list(undirected_adj.get(curr, set())))
            for v in neighbors:
                dfs_visited_nodes.add(v)
                if v in companies:
                    new_grade = float(strength)
                    if v not in visited:
                        new_node = WikiNode(
                            qid=v,
                            depth=d,
                            grade=new_grade,
                            kind="company",
                            path=path + (v,),
                        )
                        if v in dfs_discovered:
                            if new_grade > dfs_discovered[v].grade:
                                dfs_discovered[v] = new_node
                        else:
                            dfs_discovered[v] = new_node
                    else:
                        if new_grade > visited[v].grade:
                            visited[v] = WikiNode(
                                qid=v,
                                depth=visited[v].depth,
                                grade=new_grade,
                                kind="company",
                                path=visited[v].path,
                            )
                else:
                    if v not in visited_in_dfs:
                        dfs(v, d + 1, path + (v,), visited_in_dfs | {v})

        dfs(t, 1, (t,), {t})

    # Merge BFS and DFS company nodes up to cap
    bfs_companies = [n for n in visited.values() if n.kind == "company"]
    remaining_cap = company_cap - len(bfs_companies)
    if remaining_cap > 0 and dfs_discovered:
        sorted_dfs_cos = sorted(dfs_discovered.values(), key=lambda n: (-n.grade, n.qid))
        for n in sorted_dfs_cos[:remaining_cap]:
            visited[n.qid] = n

    # --- Thread-B category discovery (D-20260828-001) ---------------------
    reached_company_qids = {
        n.qid for n in visited.values() if n.kind == "company" and companies.get(n.qid)
    }
    thread_b_cfg = kwargs.get("thread_b") if isinstance(kwargs.get("thread_b"), dict) else None
    thread_b_summary: Dict[str, object] = {}
    if thread_b_cfg is not None:
        tb_cands = build_thread_b_candidates(
            reached_companies=reached_company_qids,
            companies=companies,
            ticker_industry=kwargs.get("ticker_industry"),
            industry_members=kwargs.get("industry_members"),
            cfg=kwargs.get("industry_beta_cfg"),
            beta_band=round(float(
                thread_b_cfg.get("beta_band",
                                 (kwargs.get("industry_beta_cfg") or {}).get("thread_b", {}).get("beta_band", 0.15))
            ), 6),
            prefer_different_sub_area=bool(thread_b_cfg.get("prefer_different_sub_area", True)),
            seeds_include=set(seed_qids),
        )
        thread_a_nodes = [n for n in visited.values() if n.kind == "company" and companies.get(n.qid)]
        merged, intersections = merge_thread_b(
            thread_a_nodes=thread_a_nodes,
            thread_b_candidates=tb_cands,
            company_cap=company_cap,
            randomized=thread_b_cfg.get("randomized"),
        )
        thread_b_summary = {"intersections": intersections, "randomized": bool(
            (thread_b_cfg.get("randomized") or {}).get("enabled", False)
        )}
        # Overwrite visited map only for the merged set so final extraction reflects it.
        for node in merged:
            visited[node.qid] = node

    # Extract final company nodes
    final_nodes = [n for n in visited.values() if n.kind == "company" and companies.get(n.qid)]
    final_nodes_sorted = sorted(final_nodes, key=lambda n: (-n.grade, n.qid))

    # Collect visited nodes count
    all_visited_qids = set(visited.keys()) | dfs_visited_nodes
    nodes_visited = len(all_visited_qids)
    companies_found = len(final_nodes_sorted)
    topics_triggered = len(triggered_topics)
    hubs_neutered = sum(1 for h in hubs if h in all_visited_qids)

    summary = {
        "nodes_visited": nodes_visited,
        "companies_found": companies_found,
        "topics_triggered": topics_triggered,
        "descents_run": descents_run,
        "hubs_neutered": hubs_neutered,
        "thread_b": thread_b_summary,
    }

    # Build output edges
    output_edges: List[WikiEdgeRel] = []
    edge_counts: Dict[str, int] = {}
    sorted_candidate_edges = sorted(
        filtered_edges,
        key=lambda e: (e.get("source_qid", ""), e.get("target_qid", ""), e.get("relation", "")),
    )
    for e in sorted_candidate_edges:
        src = e["source_qid"]
        tgt = e["target_qid"]
        if src in all_visited_qids and tgt in all_visited_qids:
            count = edge_counts.get(src, 0)
            if count < max_edges_per_node:
                output_edges.append(
                    WikiEdgeRel(
                        source_qid=src,
                        target_qid=tgt,
                        relation=e.get("relation", ""),
                        confidence=e.get("confidence", 1.0),
                    )
                )
                edge_counts[src] = count + 1

    return WikiFrontierResult(nodes=final_nodes_sorted, edges=output_edges, summary=summary)
