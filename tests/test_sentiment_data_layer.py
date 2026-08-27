"""
Unit tests for the FinBERT sentiment data layer.
"""

import os
import sqlite3
import pytest
import tempfile
from Qualitative.psychological.sentiment_training.data_layer import (
    normalize_text,
    build_dataset,
    SOURCE_PRIORITY
)


def test_normalize_text():
    # Whitespace normalization
    assert normalize_text("  hello    world  ") == "hello world"
    # Case normalization
    assert normalize_text("HeLLo WoRLd") == "hello world"
    # Length truncation (512 chars)
    long_text = "a" * 1000
    assert len(normalize_text(long_text)) == 512
    # None/Empty handling
    assert normalize_text("") == ""
    assert normalize_text(None) == ""


def test_deduplication_priority(monkeypatch):
    # Test deduplication directly by mocking downloaders in build_dataset
    # We will verify that if the same text is in multiple sources, we keep the one with higher priority
    # Priorities: phrasebank (4) > fiqa (3) > semeval (2) > stocktwits (1)
    mock_phrasebank = [
        {"text": "Text A", "label": 1, "score": 0.0, "source": "phrasebank", "orig_score": 0.0, "orig_label": "neutral"},
    ]
    mock_fiqa = [
        {"text": "Text A", "label": 2, "score": 0.5, "source": "fiqa", "orig_score": 0.5, "orig_label": "0.5"},
        {"text": "Text B", "label": 2, "score": 0.5, "source": "fiqa", "orig_score": 0.5, "orig_label": "0.5"},
    ]
    mock_semeval = [
        {"text": "Text B", "label": 1, "score": 0.0, "source": "semeval", "orig_score": None, "orig_label": "1"},
        {"text": "Text C", "label": 0, "score": -1.0, "source": "semeval", "orig_score": None, "orig_label": "0"},
    ]
    mock_stocktwits = [
        {"text": "Text C", "label": 2, "score": 1.0, "source": "stocktwits", "orig_score": 1.0, "orig_label": "0"},
    ]

    monkeypatch.setattr("Qualitative.psychological.sentiment_training.data_layer.download_phrasebank", lambda cache: mock_phrasebank)
    monkeypatch.setattr("Qualitative.psychological.sentiment_training.data_layer.download_fiqa", lambda cache: mock_fiqa)
    monkeypatch.setattr("Qualitative.psychological.sentiment_training.data_layer.download_semeval", lambda cache: mock_semeval)
    monkeypatch.setattr("Qualitative.psychological.sentiment_training.data_layer.download_stocktwits", lambda cache: mock_stocktwits)

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_sentiment_dedup.db")
        build_dataset(db_path, cache_dir=tmpdir)

        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.cursor()
            
            # Text A: phrasebank (4) vs fiqa (3) -> kept phrasebank
            cursor.execute("SELECT source, label FROM sentiment_training WHERE text = 'text a'")
            row_a = cursor.fetchone()
            assert row_a is not None
            assert row_a[0] == "phrasebank"

            # Text B: fiqa (3) vs semeval (2) -> kept fiqa
            cursor.execute("SELECT source, label FROM sentiment_training WHERE text = 'text b'")
            row_b = cursor.fetchone()
            assert row_b is not None
            assert row_b[0] == "fiqa"

            # Text C: semeval (2) vs stocktwits (1) -> kept semeval
            cursor.execute("SELECT source, label FROM sentiment_training WHERE text = 'text c'")
            row_c = cursor.fetchone()
            assert row_c is not None
            assert row_c[0] == "semeval"
        finally:
            conn.close()


def test_build_dataset_and_splits(monkeypatch):
    # Mock downloaders to return custom data
    mock_phrasebank = [
        {"text": "PhraseBank item 1", "label": 2, "score": 1.0, "source": "phrasebank", "orig_score": 1.0, "orig_label": "positive"},
        {"text": "PhraseBank item 2", "label": 1, "score": 0.0, "source": "phrasebank", "orig_score": 0.0, "orig_label": "neutral"},
        {"text": "PhraseBank item 3", "label": 0, "score": -1.0, "source": "phrasebank", "orig_score": -1.0, "orig_label": "negative"},
        # Add duplicate to test deduplication: PhraseBank has higher priority than StockTwits
        {"text": "Duplicate item", "label": 2, "score": 1.0, "source": "phrasebank", "orig_score": 1.0, "orig_label": "positive"},
    ]
    # 120 items (40 per label) to test split ratios and ensure locked_test is populated
    for i in range(120):
        mock_phrasebank.append({
            "text": f"PhraseBank extra {i}",
            "label": i % 3,
            "score": float((i % 3) - 1),
            "source": "phrasebank",
            "orig_score": float((i % 3) - 1),
            "orig_label": str(i % 3)
        })

    mock_fiqa = [
        {"text": "FiQA item 1", "label": 2, "score": 0.8, "source": "fiqa", "orig_score": 0.8, "orig_label": "0.8"},
        {"text": "FiQA item 2", "label": 0, "score": -0.6, "source": "fiqa", "orig_score": -0.6, "orig_label": "-0.6"},
    ]

    mock_semeval = [
        {"text": "SemEval item 1", "label": 1, "score": 0.0, "source": "semeval", "orig_score": None, "orig_label": "1"},
    ]

    mock_stocktwits = [
        # This is a duplicate of 'Duplicate item' but lower priority. Should be discarded.
        {"text": "Duplicate item", "label": 0, "score": -1.0, "source": "stocktwits", "orig_score": -1.0, "orig_label": "1"},
        {"text": "StockTwits item 1", "label": 2, "score": 1.0, "source": "stocktwits", "orig_score": 1.0, "orig_label": "0"},
    ]

    monkeypatch.setattr("Qualitative.psychological.sentiment_training.data_layer.download_phrasebank", lambda cache: mock_phrasebank)
    monkeypatch.setattr("Qualitative.psychological.sentiment_training.data_layer.download_fiqa", lambda cache: mock_fiqa)
    monkeypatch.setattr("Qualitative.psychological.sentiment_training.data_layer.download_semeval", lambda cache: mock_semeval)
    monkeypatch.setattr("Qualitative.psychological.sentiment_training.data_layer.download_stocktwits", lambda cache: mock_stocktwits)

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_sentiment.db")
        stats = build_dataset(db_path, cache_dir=tmpdir)

        # Verify DB exists
        assert os.path.exists(db_path)

        # Check DB connection using try...finally
        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode")
            mode = cursor.fetchone()[0]
            assert mode.lower() == "wal"

            # Verify deduplication: 'Duplicate item' should be kept only once and with 'phrasebank' source
            cursor.execute("SELECT source, label FROM sentiment_training WHERE text = 'duplicate item'")
            rows = cursor.fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "phrasebank"

            # Verify all split categories are represented and disjoint
            cursor.execute("SELECT id, split FROM sentiment_training")
            split_rows = cursor.fetchall()
            all_ids = set()
            splits = {"train": set(), "val": set(), "test": set(), "locked_test": set()}
            for rid, split in split_rows:
                all_ids.add(rid)
                splits[split].add(rid)

            # Ensure splits are disjoint and cover all rows
            assert len(all_ids) == sum(len(s) for s in splits.values())
            for s1_name, s1_set in splits.items():
                for s2_name, s2_set in splits.items():
                    if s1_name != s2_name:
                        assert s1_set.isdisjoint(s2_set)

            # Verify that locked_test contains subset of expected ratio
            assert len(splits["train"]) > 0
            assert len(splits["val"]) > 0
            assert len(splits["test"]) > 0
            assert len(splits["locked_test"]) > 0
            assert "locked_test" in stats["splits"]
        finally:
            conn.close()
