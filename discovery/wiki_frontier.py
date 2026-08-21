"""Wiki-Frontier Expansion Layer (B-20260820-001 ruling).

Grade-prioritized BFS + topic-triggered DFS engine on Wikidata relation graph.
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
