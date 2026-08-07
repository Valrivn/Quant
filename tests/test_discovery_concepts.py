"""Tests for concept-vs-ticker separation (D-20260806-001 P2, SEC 3.5).

Covers: ticker-vs-concept classification, research-backlog storage, and the
hard refusal of a concept entering the candidate path.
"""

import pytest

from discovery.concepts import (
    Concept,
    ConceptBacklog,
    ConceptToCandidateError,
    is_ticker,
)


class TestClassification:
    def test_ticker_pattern(self):
        assert is_ticker("NVDA")
        assert is_ticker("BRK.B")
        assert is_ticker("A")

    def test_concept_pattern(self):
        assert not is_ticker("double-descent")
        assert not is_ticker("AI-enabling monopoly")
        assert not is_ticker("quant trading")
        assert not is_ticker("123")


class TestConceptBacklog:
    def setup_method(self):
        self.backlog = ConceptBacklog()

    def test_add_and_get(self):
        c = Concept(
            concept_name="double-descent",
            topic="Applied Math",
            first_seen=1000,
            sources=["instagram"],
            linked_tickers=["NVDA"],
            hypothesis="overfit capacity curve",
        )
        self.backlog.add(c)
        assert self.backlog.get("double-descent") is c

    def test_default_status_applied(self):
        c = Concept(concept_name="x", topic="AI", first_seen=1)
        self.backlog.add(c)
        assert c.status == "research_backlog"

    def test_dedup_by_name(self):
        self.backlog.add(Concept(concept_name="c", topic="AI", first_seen=1))
        self.backlog.add(Concept(concept_name="c", topic="AI", first_seen=2))
        assert len(self.backlog.all()) == 1

    def test_concept_cannot_become_candidate(self):
        self.backlog.add(Concept(concept_name="double-descent", topic="AI", first_seen=1))
        with pytest.raises(ConceptToCandidateError):
            self.backlog.to_candidate("double-descent")

    def test_classify_entity(self):
        assert self.backlog.classify("NVDA") == "ticker"
        assert self.backlog.classify("double-descent") == "concept"