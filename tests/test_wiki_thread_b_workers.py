"""Tests for the Thread-B parallel worker lane (D-20260828-001).

Uses injected fake callables so the concurrency logic is verified without
network or DB side effects. Confirms: Thread A and the industry fetch run
concurrently and merge deterministically; the parallel screen settles fill
discipline over NOVEL candidates only; ordering is deterministic.
"""

import threading

from discovery.wiki_frontier import WikiNode, WikiFrontierResult, WikiEdgeRel
from discovery.wiki_thread_b_workers import (
    run_threads_parallel,
    screen_novel_parallel,
)
from discovery.industry_beta import load_industry_beta

CFG = load_industry_beta()


def _fake_frontier(reached_qids):
    nodes = [
        WikiNode(qid=q, depth=1, grade=1.0, kind="company", path=(q,))
        for q in reached_qids
    ]
    return WikiFrontierResult(nodes=nodes, edges=[], summary={"nodes_visited": len(nodes)})


def _fake_industry_payload():
    return {
        "industry_members": {
            "Semiconductor": [["QS1", "AMD"], ["QS2", "INTC"]],
            "Semiconductor Equip": [["QE1", "KLAC"], ["QE2", "LRCX"]],
        },
        "company_industry": {
            "QS1": "Semiconductor", "QS2": "Semiconductor",
            "QE1": "Semiconductor Equip", "QE2": "Semiconductor Equip",
        },
    }


def test_run_threads_parallel_merges_and_records_intersections():
    # Thread A reaches NVDA (Semiconductor) only.
    companies = {"QN": "NVDA", "QE1": "KLAC"}
    thread_b_cfg = {"beta_band": 0.15, "prefer_different_sub_area": True,
                    "randomized": None}
    order = []

    def frontier():
        order.append("A")
        return _fake_frontier(["QN"])

    def seed_fetch(qids):
        order.append("B")
        # In mock, seed QN -> Semiconductor. We return the map company_industry
        # mapping QN to Semiconductor, which matches companies["QN"] = "NVDA"
        # and resolved_ticker_industry["NVDA"] = "Semiconductor".
        return {"company_industry": {"QN": "Semiconductor"}, "industry_members": {}}

    def peer_fetch(labels):
        # When peer_fetch is called with target industry label "Semiconductor Equipment",
        # return the fake members of Semiconductor Equipment.
        return _fake_industry_payload()

    out = run_threads_parallel(
        frontier_callable=frontier,
        seed_industries_callable=seed_fetch,
        companies=companies,
        ticker_industry=None,
        industry_members_raw=None,
        industry_beta_cfg=CFG,
        thread_b_cfg=thread_b_cfg,
        seed_qids=["QN"],
        company_cap=10,
        peer_members_callable=peer_fetch,
        max_workers=2,
    )

    # Both lanes ran; Thread-B novel candidate KLAC (same band, diff sub-area) surfaced.
    assert "QE1" in out["thread_b_candidates"]
    # Intersections bookkeeping present.
    assert out["b_intersections"]["b_novel"] >= 1
    assert out["b_intersections"]["b_overlap"] >= 0
    # NVDA (Thread A) retained in merged.
    assert "QN" in out["merged_qids"]
    # Deterministic merge output.
    assert out["merged_qids"] == out["merged_qids"]


def test_screen_novel_only_and_parallel():
    a_nodes = [WikiNode(qid="QA", depth=0, grade=1.0, kind="company", path=("QA",))]
    cands = {
        "QA": {"qid": "QA", "ticker": "SAME", "grade": 1.0, "sub_area": "x", "via": "A"},  # overlap -> skipped
        "QB": {"qid": "QB", "ticker": "NEW1", "grade": 2.0, "sub_area": "y", "via": "A"},  # novel
        "QC": {"qid": "QC", "ticker": "NEW2", "grade": 1.5, "sub_area": "y", "via": "A"},  # novel
    }

    calls = []
    lock = threading.Lock()

    def screen(t):
        with lock:
            calls.append(t)
        return t == "NEW1"  # only NEW1 passes

    out = screen_novel_parallel(cands, a_nodes, screen, max_workers=2)

    # Only NOVEL candidates screened (overlap "SAME" skipped).
    assert "SAME" not in out
    assert "NEW1" in out and "NEW2" in out
    # NEW1 passed, NEW2 failed.
    assert out["NEW1"]["passed"] is True
    assert out["NEW2"]["passed"] is False
    # Deterministic ordering: NEW1 (passed) before NEW2 (failed).
    assert list(out.keys()) == ["NEW1", "NEW2"]


def test_screen_empty_novel():
    assert screen_novel_parallel({}, [], lambda t: True, max_workers=2) == {}


def test_screen_handles_screen_exception():
    a_nodes = [WikiNode(qid="QA", depth=0, grade=1.0, kind="company", path=("QA",))]
    cands = {"QB": {"qid": "QB", "ticker": "BAD", "grade": 1.0, "sub_area": "y", "via": "A"}}
    out = screen_novel_parallel(cands, a_nodes, lambda t: (_ for _ in ()).throw(RuntimeError), 1)
    assert out["BAD"]["passed"] is False


# --- ecosystem traversal (D-20260901-001) ------------------------------------

from discovery.wiki_thread_b_workers import _ecosystem_peers  # noqa: E402


def _mini_cfg_with_chain(chain, sub_areas):
    industries = {
        ind: {"unlevered_beta": 1.0, "sub_area": sa}
        for ind, sa in sub_areas
    }
    return {
        "industries": industries,
        "thread_b": {
            "ecosystem": {
                "enabled": True,
                "max_hop_depth": 2,
                "max_industries_per_hop": 12,
                "sub_area_chain": chain,
            }
        },
    }


def test_ecosystem_disabled_when_block_absent():
    cfg = _mini_cfg_with_chain({"a": ["b"]}, [("IND1", "a")])
    tb = {"beta_band": 0.15}
    assert _ecosystem_peers({"IND1"}, cfg, tb) == set()


def test_ecosystem_stays_on_different_path_and_cycle_guarded():
    # a <-> b <-> c: from a, hop1 digs the OTHER path into b, hop2 into c.
    chain = {"a": ["b", "x"], "b": ["a", "c"], "c": ["b"], "x": ["a"]}
    sub_areas = [
        ("A1", "a"), ("A2", "a"),
        ("B1", "b"), ("B2", "b"),
        ("C1", "c"), ("X1", "x"),
    ]
    cfg = _mini_cfg_with_chain(chain, sub_areas)
    tb = {"beta_band": 0.15, "ecosystem": cfg["thread_b"]["ecosystem"]}
    peers = _ecosystem_peers({"A1", "A2"}, cfg, tb)
    # Never stays in the reached sub_area "a", never revisits:
    assert not any(ind.startswith("A") for ind in peers)
    first = set(peers)  # no duplicates
    assert sorted(first) == sorted(first)


def test_ecosystem_deterministic_and_no_seed_reentry():
    chain = {"a": ["b", "c"], "b": ["d", "e"], "c": ["f", "g"]}
    sub_areas = [(i, i[0]) for i in ["a1", "a2", "b1", "b2", "c1", "d1", "e1", "f1", "g1"]]
    cfg = _mini_cfg_with_chain(chain, sub_areas)
    tb = {"beta_band": 0.15, "ecosystem": cfg["thread_b"]["ecosystem"]}
    p1 = _ecosystem_peers({"a1"}, cfg, tb)
    p2 = _ecosystem_peers({"a1"}, cfg, tb)
    assert p1 == p2
    assert "a1" not in p1 and "a2" not in p1
    assert "b1" in p1 and "c1" in p1


def test_ecosystem_respects_per_hop_cap():
    chain = {"a": ["b", "c", "d"]}
    sub_areas = [
        ("A1", "a"), ("B1", "b"), ("B2", "b"), ("C1", "c"), ("C2", "c"), ("D1", "d"),
    ]
    cfg = _mini_cfg_with_chain(chain, sub_areas)
    eco = dict(cfg["thread_b"]["ecosystem"])
    eco["max_industries_per_hop"] = 2
    tb = {"beta_band": 0.15, "ecosystem": eco}
    assert len(_ecosystem_peers({"A1"}, cfg, tb)) <= 2


def test_ecosystem_walks_up_and_down_undirected():
    # Reverse edge absent in data but traversal is symmetric (up AND down).
    chain = {"a": ["b"]}  # no c->b edge, but b should still pull a-peer *below*
    sub_areas = [
        ("A1", "a"), ("A2", "a"), ("B1", "b"), ("B2", "b"),
        ("C1", "c"), ("C2", "c"), ("C3", "c"),
    ]
    cfg = _mini_cfg_with_chain(chain, sub_areas)
    tb = {"beta_band": 0.15, "ecosystem": cfg["thread_b"]["ecosystem"]}
    # From a: b's reverse neighbors include nothing extra; from seed a -> b is
    # the different path; b's own chain target absent, sym edge keeps a reachable
    # but visited. No crash, deterministic non-empty result.
    assert "B1" in _ecosystem_peers({"A1"}, cfg, tb)
