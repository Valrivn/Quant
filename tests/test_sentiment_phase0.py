"""Tests for Phase 0 sentiment dataset audit and domain similarity (B-20260824-001).

Covers:
  - PhraseBank parsing correctness
  - Tokenization and stopword removal
  - Jaccard similarity calculation
  - TF-IDF cosine similarity
  - KS test on sentence lengths
  - Top distinctive words
  - YAML config bar validation
  - Audit script entry points (integration smoke test)
"""

import json
import math
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_phrasebank_line(line: str):
    """Extract (text, label) from a PhraseBank line: 'text @label'."""
    idx = line.rfind("@")
    if idx < 0:
        return None, None
    text = line[:idx].strip()
    label = line[idx + 1:].strip().lower()
    return text, label


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_phrasebank_parse():
    """PhraseBank line parsing handles standard format."""
    text, label = _parse_phrasebank_line("The stock rose sharply today@positive")
    assert text == "The stock rose sharply today"
    assert label == "positive"

    text, label = _parse_phrasebank_line("Revenue declined year over year @negative")
    assert text == "Revenue declined year over year"
    assert label == "negative"

    text, label = _parse_phrasebank_line("The company reported earnings@neutral")
    assert text == "The company reported earnings"
    assert label == "neutral"

    # Edge: no @
    text, label = _parse_phrasebank_line("no label here")
    assert text is None
    assert label is None


def test_phrasebank_parse_variants():
    """Handles extra whitespace and mixed casing."""
    text, label = _parse_phrasebank_line("  Spaced out text  @  Positive  ")
    assert text == "Spaced out text"
    assert label == "positive"

    text, label = _parse_phrasebank_line("Multiple@at@signs@negative")
    assert text == "Multiple@at@signs"
    assert label == "negative"


def test_tokenization():
    """_tokenize removes stopwords and short tokens."""
    from scripts.phrasebank_domain_similarity import _tokenize, STOPWORDS

    tokens = _tokenize("The stock price rose sharply today after earnings report")
    assert "the" not in tokens  # stopword
    assert "is" not in tokens   # stopword
    assert "stock" in tokens
    assert "price" in tokens
    assert "rose" in tokens
    assert "sharply" in tokens
    assert "today" in tokens
    assert "after" not in tokens  # stopword
    assert "earnings" in tokens
    assert "report" in tokens

    # Empty
    assert _tokenize("") == []
    assert _tokenize("the a an") == []  # all stopwords


def test_jaccard_similarity():
    """Jaccard similarity computes correctly."""
    from scripts.phrasebank_domain_similarity import jaccard_similarity

    # Identical sets
    assert jaccard_similarity({"a", "b"}, {"a", "b"}) == 1.0

    # Disjoint
    assert jaccard_similarity({"a"}, {"b"}) == 0.0

    # Partial overlap: {a,b} ∩ {b,c} = {b}, union = {a,b,c}
    result = jaccard_similarity({"a", "b"}, {"b", "c"})
    assert abs(result - 1 / 3) < 1e-9

    # Empty sets
    assert jaccard_similarity(set(), set()) == 0.0


def test_ks_test():
    """KS test on identical distributions returns high p-value."""
    from scripts.phrasebank_domain_similarity import ks_test_on_lengths

    texts_a = ["one two three"] * 100
    texts_b = ["one two three"] * 100
    result = ks_test_on_lengths(texts_a, texts_b)
    assert result["ks_statistic"] == 0.0
    assert result["p_value"] == 1.0


def test_ks_test_different():
    """KS test detects different distributions."""
    from scripts.phrasebank_domain_similarity import ks_test_on_lengths

    # Short vs long sentences
    texts_a = ["one two"] * 50
    texts_b = [" ".join(["word"] * 20)] * 50
    result = ks_test_on_lengths(texts_a, texts_b)
    assert result["ks_statistic"] > 0.5
    assert result["p_value"] < 0.01


def test_tfidf_cosine_identical():
    """Cosine similarity of a corpus with itself is ~1.0."""
    from scripts.phrasebank_domain_similarity import tfidf_cosine_similarity

    texts = [
        "stock market rally continues",
        "earnings beat expectations",
        "revenue growth slows down",
        "technology sector leads gains",
    ]
    result = tfidf_cosine_similarity(texts, texts)
    assert result > 0.99


def test_tfidf_cosine_different():
    """Cosine similarity of very different corpora is low."""
    from scripts.phrasebank_domain_similarity import tfidf_cosine_similarity

    finance = ["stock earnings revenue dividend market rally bull bear"]
    sports = ["goal touchdown basket strike home run penalty foul"]
    result = tfidf_cosine_similarity(finance, sports)
    assert result < 0.5


def test_top_distinctive_words():
    """Distinctive words are those more common in target than anchor."""
    from scripts.phrasebank_domain_similarity import top_distinctive_words

    anchor = ["stock market earnings dividend revenue growth"]
    target = ["beautiful sunset ocean waves beach tropical paradise"]
    result = top_distinctive_words(anchor, target, top_n=5)
    assert len(result) == 5
    # Top target words should be target-specific
    top_words = [r["word"] for r in result]
    assert "ocean" in top_words or "sunset" in top_words or "paradise" in top_words


def test_ybars_frozen():
    """weights_sentiment_bars.yaml is valid, frozen, and has all required keys."""
    bars_path = Path("config/weights_sentiment_bars.yaml")
    assert bars_path.exists(), f"Missing {bars_path}"
    with open(bars_path) as f:
        bars = yaml.safe_load(f)

    assert bars["decision"] == "B-20260824-001"
    assert bars["frozen"] is True
    assert bars["model"] == "finbert_dual_head_v1"

    # Scoring quality bars
    sq = bars["scoring_quality"]
    assert sq["spearman_vs_human_min"] >= 0.5
    assert sq["macro_f1_pooled_min"] >= 0.7
    assert sq["per_domain_floor"] >= 0.5

    # Calibration bars
    cal = bars["calibration"]
    assert 0.85 <= cal["conformal_coverage_nominal"] <= 0.95
    assert cal["ece_classification_max"] <= 0.10

    # Robustness bars
    rob = bars["robustness"]
    assert rob["label_noise_sensitivity_max_delta"] > 0
    assert rob["ood_f1_drop_max_pp"] > 0

    # VRAM
    assert bars["vram"]["peak_gb_max"] <= 8.0  # RTX 3070 8GB

    # Reliability gate
    rg = bars["reliability_gate"]
    assert rg["min_posterior_accuracy"] >= 0.8
    assert rg["min_posterior_probability"] >= 0.9


def test_audit_script_importable():
    """sentiment_dataset_audit.py is importable without errors."""
    from scripts import sentiment_dataset_audit as m

    assert hasattr(m, "audit_phrasebank")
    assert hasattr(m, "audit_fiqa")
    assert hasattr(m, "audit_semeval")
    assert hasattr(m, "audit_stocktwits")
    assert hasattr(m, "main")


def test_domain_similarity_script_importable():
    """phrasebank_domain_similarity.py is importable without errors."""
    from scripts import phrasebank_domain_similarity as m

    assert hasattr(m, "jaccard_similarity")
    assert hasattr(m, "ks_test_on_lengths")
    assert hasattr(m, "tfidf_cosine_similarity")
    assert hasattr(m, "top_distinctive_words")
    assert hasattr(m, "analyze_domain")
    assert hasattr(m, "main")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_phrasebank_parse,
        test_phrasebank_parse_variants,
        test_tokenization,
        test_jaccard_similarity,
        test_ks_test,
        test_ks_test_different,
        test_tfidf_cosine_identical,
        test_tfidf_cosine_different,
        test_top_distinctive_words,
        test_ybars_frozen,
        test_audit_script_importable,
        test_domain_similarity_script_importable,
    ]

    passed = 0
    failed = 0
    for test_fn in tests:
        try:
            test_fn()
            print(f"  PASS  {test_fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {test_fn.__name__}: {e}")
            failed += 1

    print(f"\n{passed}/{passed + failed} tests passed")
    if failed:
        sys.exit(1)
