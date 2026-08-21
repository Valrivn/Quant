"""Tests for the Wiki-Frontier Expansion Layer (B-20260820-001 ruling)."""

from discovery.wiki_frontier import expand_wiki_frontier, WikiNode, WikiEdgeRel


def test_wiki_frontier_basic():
    # Setup a small mock graph
    # Seeds: Q1, Q2
    # Companies:
    # Q1 -> T1, Q2 -> T2, Q3 -> T3, Q4 -> T4
    # Q5 is a topic node (not in companies)
    companies = {"Q1": "T1", "Q2": "T2", "Q3": "T3", "Q4": "T4"}
    major_tickers = {"T1", "T2"}
    relevance = {"T1": 10.0, "T2": 5.0}

    # Edges:
    # Q1 (company, major) -subsidiary-> Q3
    # Q2 (company, major) -owner-> Q3
    # Q3 is adjacent to Q1 and Q2. Its grade should be 10.0 + 5.0 = 15.0.
    # Q4 is adjacent to Q2 only. Its grade should be 5.0.
    edges = [
        {"source_qid": "Q1", "target_qid": "Q3", "relation": "subsidiary"},
        {"source_qid": "Q2", "target_qid": "Q3", "relation": "owner"},
        {"source_qid": "Q2", "target_qid": "Q4", "relation": "parent"},
        {"source_qid": "Q2", "target_qid": "Q1", "relation": "industry"},  # ignored
    ]

    res = expand_wiki_frontier(
        seed_qids=["Q1", "Q2"],
        companies=companies,
        edges=edges,
        major_tickers=major_tickers,
        relevance=relevance,
        max_depth=3,
        max_nodes_per_seed=200,
    )

    # (a) overlap grading matches expected sums
    # Q3's grade should be 15.0. Q4's grade should be 5.0.
    grades = {n.qid: n.grade for n in res.nodes}
    assert grades["Q3"] == 15.0
    assert grades["Q4"] == 5.0

    # (b) priority order deterministic: highest grade first
    # Q3 (15.0 grade) must appear before Q4 (5.0 grade)
    qids = [n.qid for n in res.nodes]
    assert qids.index("Q3") < qids.index("Q4")


def test_wiki_frontier_hub_guard():
    # (c) hub with out-degree > cap propagates zero grade
    # Q1 is adjacent to Q3.
    # But Q1 has 3 outgoing edges. If hub_cap = 2, Q1 is a hub and propagates 0.
    companies = {"Q1": "T1", "Q3": "T3"}
    major_tickers = {"T1"}
    relevance = {"T1": 10.0}

    edges = [
        {"source_qid": "Q1", "target_qid": "Q3", "relation": "subsidiary"},
        {"source_qid": "Q1", "target_qid": "Q4", "relation": "subsidiary"},
        {"source_qid": "Q1", "target_qid": "Q5", "relation": "subsidiary"},
    ]

    res = expand_wiki_frontier(
        seed_qids=["Q1"],
        companies=companies,
        edges=edges,
        major_tickers=major_tickers,
        relevance=relevance,
        hub_cap=2,
    )

    grades = {n.qid: n.grade for n in res.nodes}
    assert grades["Q3"] == 0.0
    assert res.summary["hubs_neutered"] == 1


def test_wiki_frontier_topic_trigger_dfs():
    # (d) topic trigger fires at >= k adjacencies and DFS descent collects company neighbors within dfs_depth
    # Topic node is T99 (not in companies).
    # T99 is adjacent to Q1, Q2, Q3 (all in companies, so 3 distinct companies).
    # If topic_trigger_k = 3, T99 triggers!
    # T99's adjacent companies = Q1, Q2, Q3. So trigger strength = 3.
    # DFS from T99:
    # Level 1 neighbor: T98 (topic node)
    # Level 2 neighbor: Q4 (company) -> should inherit grade = 3.0.
    companies = {"Q1": "T1", "Q2": "T2", "Q3": "T3", "Q4": "T4"}
    major_tickers = set()

    edges = [
        {"source_qid": "T99", "target_qid": "Q1", "relation": "subsidiary"},
        {"source_qid": "T99", "target_qid": "Q2", "relation": "owner"},
        {"source_qid": "T99", "target_qid": "Q3", "relation": "parent"},
        {"source_qid": "T99", "target_qid": "T98", "relation": "subsidiary"},
        {"source_qid": "T98", "target_qid": "Q4", "relation": "owner"},
    ]

    res = expand_wiki_frontier(
        seed_qids=["Q1"],
        companies=companies,
        edges=edges,
        major_tickers=major_tickers,
        topic_trigger_k=3,
        dfs_depth=2,
    )

    grades = {n.qid: n.grade for n in res.nodes}
    # Q4 should have been discovered via DFS and have a grade of 3.0
    assert "Q4" in grades
    assert grades["Q4"] == 3.0
    assert res.summary["topics_triggered"] == 1
    assert res.summary["descents_run"] == 1


def test_wiki_frontier_caps():
    # (e) caps respected: max_nodes_per_seed and max_edges_per_node
    companies = {"Q1": "T1", "Q2": "T2", "Q3": "T3", "Q4": "T4"}
    major_tickers = {"T1"}
    edges = [
        {"source_qid": "Q1", "target_qid": "Q2", "relation": "subsidiary"},
        {"source_qid": "Q1", "target_qid": "Q3", "relation": "owner"},
        {"source_qid": "Q1", "target_qid": "Q4", "relation": "parent"},
    ]

    # max_nodes_per_seed = 2 (so only Q1 and one neighbor can be company nodes)
    # max_edges_per_node = 1
    res = expand_wiki_frontier(
        seed_qids=["Q1"],
        companies=companies,
        edges=edges,
        major_tickers=major_tickers,
        max_nodes_per_seed=2,
        max_edges_per_node=1,
    )

    assert len(res.nodes) <= 2
    # Output edges from Q1 should be capped at 1
    q1_edges = [e for e in res.edges if e.source_qid == "Q1"]
    assert len(q1_edges) <= 1
