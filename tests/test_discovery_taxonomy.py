"""Tests for the discovery trend-feed taxonomy (D-20260806-001 P1).

Covers: 8-topic completeness, source taxonomy structure, determinism (no RNG),
and the fixed topic ordering used as the deterministic tie-break priority.
"""

import pytest

from discovery.taxonomy import (
    TOPICS,
    SOURCE_TAXONOMY,
    SourceClass,
    Gating,
    get_topics,
    get_source,
    structured_source_ids,
    video_source_ids,
)

EXPECTED_TOPICS = {
    "LLM",
    "Statistics",
    "Opensource AI",
    "AI",
    "Stocks",
    "Applied Math",
    "Quant trading",
    "Datascience",
}


class TestTopicTaxonomy:
    def test_exactly_eight_topics(self):
        assert len(TOPICS) == 8

    def test_topic_set_matches_spec(self):
        assert set(TOPICS) == EXPECTED_TOPICS

    def test_topics_are_unique(self):
        assert len(set(TOPICS)) == len(TOPICS)

    def test_get_topics_returns_same_tuple(self):
        assert get_topics() == TOPICS

    def test_topic_order_is_stable(self):
        # Order is the deterministic tie-break priority; must be stable.
        assert TOPICS[0] == "LLM"
        assert TOPICS[-1] == "Datascience"


class TestSourceTaxonomy:
    def test_structured_sources_are_immediate_scope(self):
        assert set(structured_source_ids()) == {
            "sec_edgar_new_filers",
            "reddit",
            "stocktwits",
            "apewisdom",
        }

    def test_video_sources_are_sandbox_gated(self):
        assert set(video_source_ids()) == {"instagram", "tiktok"}
        for sid in video_source_ids():
            assert get_source(sid).gating == Gating.SANDBOX

    def test_all_structured_sources_full_gating(self):
        for sid in structured_source_ids():
            assert get_source(sid).gating == Gating.FULL

    def test_source_ids_unique(self):
        ids = [s.source_id for s in SOURCE_TAXONOMY]
        assert len(ids) == len(set(ids))

    def test_get_source_unknown_raises(self):
        with pytest.raises(KeyError):
            get_source("does_not_exist")

    def test_source_classes_covered(self):
        classes = {s.source_class for s in SOURCE_TAXONOMY}
        assert SourceClass.STRUCTURED in classes
        assert SourceClass.VIDEO in classes
        assert SourceClass.AI_MODEL_SURVEY in classes
        assert SourceClass.COMPANY_EXPLORATION in classes


class TestDeterminism:
    def test_no_random_import_in_taxonomy(self):
        """Taxonomy must be deterministic constants only (no RNG)."""
        import discovery.taxonomy as mod
        import inspect

        src = inspect.getsource(mod)
        assert "random" not in src
        assert "np.random" not in src
        assert "epsilon" not in src

    def test_taxonomy_is_immutable_tuples(self):
        assert isinstance(TOPICS, tuple)
        assert isinstance(SOURCE_TAXONOMY, tuple)