"""Tests for the deterministic trend ranker (D-20260806-001 P2, SEC 3.4).

Covers: formula, determinism (identical input -> identical output), tie-break
(topic priority, then lexicographic ticker), agreement_count, config fail-closed
validation, and the CI grep-asserted no-RNG audit.
"""

import inspect
import os
from pathlib import Path

import pytest

from discovery.ranker import (
    DeterministicRanker,
    RankInput,
    normalize_rank,
    velocity_z,
    topic_relevance,
)
from discovery.config_loader import (
    load_discovery_config,
    DiscoveryConfigError,
)


def _input(entity, topic="Stocks", ranks=None, m7=0.0, m28=0.0, ad=0, clout=0):
    return RankInput(
        entity=entity,
        topic=topic,
        source_ranks=ranks or {},
        mentions_7d=m7,
        mentions_28d=m28,
        ad_flag=ad,
        clout_flag=clout,
    )


class TestFormula:
    def test_normalize_rank(self):
        assert normalize_rank(1) == 1.0
        assert normalize_rank(2) == 0.5
        assert normalize_rank(0) == 0.0
        assert normalize_rank(None) == 0.0

    def test_velocity_z(self):
        assert velocity_z(10, 5) == pytest.approx(5 / 6)
        assert velocity_z(0, 0) == 0.0

    def test_topic_relevance_priority(self):
        prio = ["LLM", "Statistics", "AI", "Stocks"]
        assert topic_relevance("LLM", prio) == 1.0
        assert topic_relevance("Stocks", prio) == 0.0
        assert topic_relevance("Unknown", prio) == 0.0

    def test_score_uses_source_weights(self):
        r = DeterministicRanker()
        inp = _input("NVDA", ranks={"reddit": 1, "stocktwits": 2})
        out = r.trend_score(inp)
        # source term = 0.15*1.0 + 0.10*0.5 = 0.20
        assert out.components["source_term"] == pytest.approx(0.20)

    def test_agreement_count(self):
        r = DeterministicRanker()
        assert r.agreement_count({"reddit": 1, "stocktwits": 1}) == 2
        assert r.agreement_count({}) == 0


class TestDeterminism:
    def test_identical_input_identical_output(self):
        r = DeterministicRanker()
        inputs = [
            _input("NVDA", "AI", {"reddit": 1, "stocktwits": 2}, m7=10, m28=5),
            _input("MSFT", "AI", {"reddit": 2}, m7=3, m28=4),
        ]
        a = r.rank(inputs)
        b = r.rank(inputs)
        assert [(x.entity, x.score) for x in a] == [(x.entity, x.score) for x in b]

    def test_tie_break_topic_priority_then_lexicographic(self):
        r = DeterministicRanker()
        # Same score, different topics -> higher topic priority first.
        inputs = [
            _input("B", "Stocks", {"reddit": 1}),
            _input("A", "LLM", {"reddit": 1}),
        ]
        ranked = r.rank(inputs)
        assert ranked[0].entity == "A"  # LLM priority > Stocks

        # Same score AND same topic -> lexicographic ticker.
        inputs2 = [
            _input("ZZZ", "AI", {"reddit": 1}),
            _input("AAA", "AI", {"reddit": 1}),
        ]
        ranked2 = r.rank(inputs2)
        assert ranked2[0].entity == "AAA"

    def test_top_k_cap(self):
        r = DeterministicRanker()
        inputs = [_input(f"T{i}", "AI", {"reddit": i + 1}) for i in range(20)]
        ranked = r.rank(inputs)
        assert len(ranked) == r.top_k == 10

    def test_ad_penalty_lowers_score(self):
        r = DeterministicRanker()
        base = r.trend_score(_input("X", "AI", {"reddit": 1}))
        ad = r.trend_score(_input("X", "AI", {"reddit": 1}, ad=1))
        assert ad.score < base.score

    def test_ad_exclusion_when_configured_exclude(self):
        r = DeterministicRanker()
        # ad is configured "exclude" -> hard exclusion.
        inputs = [_input("X", "AI", {"reddit": 1}, ad=1)]
        assert r.rank(inputs) == []


class TestConfigFailClosed:
    def test_loads_valid_config(self):
        cfg = load_discovery_config()
        assert cfg["discovery"]["enabled"] is False
        assert cfg["caps"]["top_k"] == 10

    def test_unknown_key_rejected(self, tmp_path):
        import yaml
        cfg = load_discovery_config()
        cfg["ranker"]["bogus"] = 1.0
        p = tmp_path / "bad.yaml"
        p.write_text(yaml.safe_dump(cfg))
        with pytest.raises(DiscoveryConfigError):
            load_discovery_config(str(p))

    def test_non_normalized_weights_rejected(self, tmp_path):
        import yaml
        cfg = load_discovery_config()
        cfg["ranker"]["w_vel"] = 0.99  # breaks the sum-to-1.0 invariant
        p = tmp_path / "bad.yaml"
        p.write_text(yaml.safe_dump(cfg))
        with pytest.raises(DiscoveryConfigError):
            load_discovery_config(str(p))

    def test_missing_required_key_rejected(self, tmp_path):
        import yaml
        cfg = load_discovery_config()
        del cfg["caps"]["top_k"]
        p = tmp_path / "bad.yaml"
        p.write_text(yaml.safe_dump(cfg))
        with pytest.raises(DiscoveryConfigError):
            load_discovery_config(str(p))

    def test_nan_rejected(self, tmp_path):
        import yaml
        cfg = load_discovery_config()
        cfg["ranker"]["w_vel"] = float("nan")
        p = tmp_path / "bad.yaml"
        p.write_text(yaml.safe_dump(cfg))
        with pytest.raises(DiscoveryConfigError):
            load_discovery_config(str(p))


class TestNoRngAudit:
    def test_no_random_imports_in_discovery(self):
        """CI grep-asserted: no random / np.random / epsilon in discovery/."""
        root = Path(__file__).resolve().parent.parent / "discovery"
        forbidden = ["import random", "np.random", "random.", "np.random.", "epsilon"]
        for py in sorted(root.glob("*.py")):
            src = py.read_text(encoding="utf-8")
            for token in forbidden:
                assert token not in src, f"{py.name} contains forbidden RNG token {token!r}"