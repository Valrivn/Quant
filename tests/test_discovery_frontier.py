"""Tests for the overlap-graded frontier engine (B-20260819-001).

Covers: overlap grading across the major set (not seed-only), upstream
inheritance ("the spot before the shovel"), determinism (no RNG), caps
(max_depth/max_nodes/max_edges_per_node), ticker hygiene, and the binge-block
pacing supervisor (injectable time so tests never sleep).
"""

import pytest

from discovery.frontier import (
    BingeBlockPacing,
    PacingStopped,
    expand_frontier,
    normalize_tickers,
    overlap_grades,
    upstream_grades,
)


class TestNormalize:
    def test_uppercase_strip_dedupe(self):
        assert normalize_tickers(["nvda", " TSLA ", "NVDA", "amd"]) == ["NVDA", "TSLA", "AMD"]

    def test_drops_implausible_symbols(self):
        assert normalize_tickers(["NVDA", "link in bio", "A-VERY-LONG-NAME", ""]) == ["NVDA"]

    def test_empty_input(self):
        assert normalize_tickers([]) == []


class TestOverlapGrades:
    def test_grade_is_overlap_across_customers(self):
        # NVDA, AMD and GOOGL all buy from TSMC; NVDA alone buys from ASML.
        grades = overlap_grades({
            "NVDA": ["TSMC", "ASML"],
            "AMD": ["TSMC"],
            "GOOGL": ["TSMC", "MU"],
        })
        assert grades["TSMC"] == pytest.approx(3.0)
        assert grades["ASML"] == pytest.approx(1.0)
        assert grades["MU"] == pytest.approx(1.0)

    def test_relevance_weights(self):
        grades = overlap_grades(
            {"NVDA": ["TSMC"], "AMD": ["TSMC"]},
            relevance={"NVDA": 2.0, "AMD": 0.5},
        )
        assert grades["TSMC"] == pytest.approx(2.5)

    def test_deterministic(self):
        edge_map = {"NVDA": ["TSMC", "ASML"], "AMD": ["TSMC"]}
        assert overlap_grades(edge_map) == overlap_grades(edge_map)

    def test_ignores_implausible_suppliers(self):
        grades = overlap_grades({"NVDA": ["TSMC", "link", "now"]})
        assert "link" not in grades
        assert "now" not in grades


class TestUpstreamGrades:
    def test_inherits_base_grade_upstream(self):
        # TSMC (grade 3) is supplied by AMAT and ASML.
        upstream = upstream_grades(
            {"TSMC": ["AMAT", "ASML"]},
            {"TSMC": 3.0, "ASML": 1.0},
        )
        assert upstream["AMAT"] == pytest.approx(3.0)
        assert upstream["ASML"] == pytest.approx(3.0)

    def test_zero_grade_supplier_propagates_nothing(self):
        upstream = upstream_grades({"UNKNOWN": ["AMAT"]}, {"UNKNOWN": 0.0})
        assert upstream == {}

    def test_deterministic(self):
        a = upstream_grades({"TSMC": ["AMAT"]}, {"TSMC": 2.0})
        b = upstream_grades({"TSMC": ["AMAT"]}, {"TSMC": 2.0})
        assert a == b


class TestExpandFrontier:
    def test_depth0_is_seed_plus_competitors(self):
        res = expand_frontier(
            seed_tickers=["NVDA"],
            competitor_set=["NVDA", "AMD"],
            customer_to_suppliers={},
        )
        tickers = {n.ticker: n.depth for n in res.nodes}
        assert tickers["NVDA"] == 0
        assert tickers["AMD"] == 0
        assert res.summary["max_depth_reached"] == 0

    def test_depth1_grades_across_major_set_not_seed_only(self):
        # A supplier shared by GOOGL+META but NOT NVDA must still grade
        # (the CEO's "still check google and meta" rule).
        res = expand_frontier(
            seed_tickers=["NVDA"],
            competitor_set=["NVDA", "AMD"],
            customer_to_suppliers={
                "NVDA": ["TSMC"],
                "GOOGL": ["MU"],
                "META": ["MU"],
            },
            major_set=["NVDA", "AMD", "GOOGL", "META"],
        )
        by_ticker = {n.ticker: n for n in res.nodes}
        assert by_ticker["MU"].depth == 1
        assert by_ticker["MU"].grade == pytest.approx(2.0)

    def test_upstream_expansion_inherits_grade(self):
        res = expand_frontier(
            seed_tickers=["NVDA"],
            competitor_set=["NVDA"],
            customer_to_suppliers={"NVDA": ["TSMC"]},
            supplier_to_suppliers={"TSMC": ["AMAT", "ASML"]},
            major_set=["NVDA"],
            max_depth=3,
        )
        by_ticker = {n.ticker: n for n in res.nodes}
        assert by_ticker["AMAT"].depth == 2
        assert by_ticker["AMAT"].grade == pytest.approx(1.0)

    def test_max_depth_caps_expansion(self):
        res = expand_frontier(
            seed_tickers=["NVDA"],
            competitor_set=["NVDA"],
            customer_to_suppliers={"NVDA": ["A"]},
            supplier_to_suppliers={"A": ["B"], "B": ["C"]},
            major_set=["NVDA"],
            max_depth=2,
        )
        assert res.summary["max_depth_reached"] == 2
        assert "C" not in {n.ticker for n in res.nodes}

    def test_max_nodes_caps(self):
        edges = {f"C{i}": [f"S{i}"] for i in range(20)}
        res = expand_frontier(
            seed_tickers=["NVDA"],
            competitor_set=["NVDA"],
            customer_to_suppliers=edges,
            major_set=list(edges),
            max_nodes=10,
        )
        assert len(res.nodes) <= 10

    def test_max_edges_per_node_caps(self):
        res = expand_frontier(
            seed_tickers=["NVDA"],
            competitor_set=["NVDA"],
            customer_to_suppliers={"NVDA": [f"S{i}" for i in range(100)]},
            major_set=["NVDA"],
            max_edges_per_node=5,
        )
        nvda_edges = [e for e in res.edges if e.source == "NVDA"]
        assert len(nvda_edges) <= 5

    def test_edges_are_point_in_time(self):
        res = expand_frontier(
            seed_tickers=["NVDA"],
            competitor_set=["NVDA"],
            customer_to_suppliers={"NVDA": ["TSMC"]},
            major_set=["NVDA"],
            filed_date="2026-06-30",
        )
        assert res.edges[0].filed_date == "2026-06-30"
        assert res.edges[0].provenance == "frontier_overlap"

    def test_deterministic_full_expansion(self):
        kwargs = dict(
            seed_tickers=["NVDA"],
            competitor_set=["NVDA", "AMD"],
            customer_to_suppliers={"NVDA": ["TSMC", "ASML"], "AMD": ["TSMC"], "GOOGL": ["TSMC", "MU"]},
            supplier_to_suppliers={"TSMC": ["AMAT", "ASML"], "MU": ["LRCX"]},
            major_set=["NVDA", "AMD", "GOOGL", "META"],
            max_depth=3,
        )
        a = expand_frontier(**kwargs)
        b = expand_frontier(**kwargs)
        assert [n.ticker for n in a.nodes] == [n.ticker for n in b.nodes]
        assert [n.grade for n in a.nodes] == pytest.approx([n.grade for n in b.nodes])
        assert [(e.source, e.target) for e in a.edges] == [(e.source, e.target) for e in b.edges]

    def test_no_random_import(self):
        import inspect
        import discovery.frontier as mod

        src = inspect.getsource(mod)
        assert "import random" not in src
        assert "np.random" not in src


class TestBingeBlockPacing:
    def _clock(self, schedule):
        """Return now_fn that advances per call using a scheduled advance list."""
        state = {"t": 0.0, "calls": 0}

        def now():
            if state["calls"] < len(schedule):
                state["t"] += schedule[state["calls"]]
            state["calls"] += 1
            return state["t"]

        return now

    def test_processes_all_items_in_blocks(self):
        done = []
        pacing = BingeBlockPacing(block_size=2, gap_seconds=(0.0, 0.0), max_active_hours=1.0)
        clock = self._clock([0.0] * 20)
        result = pacing.run(
            [1, 2, 3, 4, 5],
            process=done.append,
            now_fn=clock,
            sleep_fn=lambda s: None,
        )
        assert result["processed"] == 5
        assert result["skipped"] == 0
        assert done == [1, 2, 3, 4, 5]

    def test_hard_stop_on_active_hours(self):
        done = []

        def slow(item):
            done.append(item)

        pacing = BingeBlockPacing(block_size=2, gap_seconds=(0.0, 0.0), max_active_hours=0.0001)
        # Each now() advances 1s; active-hours cap (0.36s) trips on item 2.
        clock = self._clock([0.0, 1.0, 1.0, 1.0, 1.0, 1.0])
        with pytest.raises(PacingStopped):
            pacing.run([1, 2, 3, 4], process=slow, now_fn=clock, sleep_fn=lambda s: None)

    def test_skips_failing_items_no_blind_retry(self):
        done = []

        def process(item):
            if item == 2:
                raise RuntimeError("boom")
            done.append(item)

        pacing = BingeBlockPacing(block_size=10, gap_seconds=(0.0, 0.0), max_active_hours=1.0)
        clock = self._clock([0.0] * 10)
        result = pacing.run([1, 2, 3], process=process, now_fn=clock, sleep_fn=lambda s: None)
        assert result["processed"] == 2
        assert result["skipped"] == 1
        assert done == [1, 3]