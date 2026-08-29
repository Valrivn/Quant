"""FinBERT sentiment grading for the IG discovery channel (B-20260817-001).

Grads raw Instagram captions with the FinBERT financial-language model
(ProsusAI/finbert), which is fine-tuned on the Financial PhraseBank dataset —
4,845 financial sentences annotated by 16 finance-experienced (PhD) annotators.
This makes FinBERT the strongest single-caption grader available, replacing the
hand-written lexicon with a model trained on PhD-labelled ground truth.

No-fail invariant: any error path returns ``None`` (or an unchanged row), so
this module can never break the discovery pipeline. The model is loaded lazily
once per process and pinned to CUDA when available.
"""

import logging
import os

logger = logging.getLogger(__name__)

_FINBERT_MODEL = "ProsusAI/finbert"
_FINBERT_PIPELINE = None
_FINBERT_LABELS = ("positive", "neutral", "negative")
_FINBERT_LABEL_TO_SCORE = {"positive": 1.0, "neutral": 0.0, "negative": -1.0}
_LIVE_ENV = "DISCOVERY_LIVE"


def _finbert_enabled() -> bool:
    """True only during live scraping (same gate as the live fetchers).

    Offline test runs and offline pipeline passes must not load the model.
    """
    return os.environ.get(_LIVE_ENV) == "1"


def _load_finbert_pipeline():
    """Lazily build the FinBERT text-classification pipeline (CUDA first)."""
    global _FINBERT_PIPELINE
    if _FINBERT_PIPELINE is not None:
        return _FINBERT_PIPELINE
    try:
        import torch
        from transformers import pipeline

        device = 0 if torch.cuda.is_available() else -1
        _FINBERT_PIPELINE = pipeline(
            "text-classification", model=_FINBERT_MODEL, device=device
        )
        logger.info("FinBERT loaded on device=%s", device)
    except Exception as exc:  # noqa: BLE001 - no-fail invariant
        logger.warning("FinBERT load failed; sentiment grading disabled: %s", exc)
        _FINBERT_PIPELINE = False
    return _FINBERT_PIPELINE


def grade_text(text: str):
    """Grade a single caption with FinBERT.

    Returns a dict ``{"label", "score", "confidence"}`` or ``None``. ``score``
    maps positive -> 1.0, neutral -> 0.0, negative -> -1.0 so it stays on the
    same [-1, 1] scale the lexicon-based ``compute_sentiment`` emits.
    """
    if not text or not text.strip():
        return None
    if not _finbert_enabled():
        return None
    pipe = _load_finbert_pipeline()
    if not pipe:
        return None
    try:
        result = pipe(text[:512], truncation=True)[0]
        label = str(result["label"]).lower()
        score = _FINBERT_LABEL_TO_SCORE.get(label)
        if score is None:
            return None
        return {
            "label": label,
            "score": float(score),
            "confidence": float(result["score"]),
        }
    except Exception as exc:  # noqa: BLE001 - no-fail invariant
        logger.warning("FinBERT grading failed; skipping: %s", exc)
        return None


def grade_batch(texts):
    """Grade a list of captions, returning one result (or None) per item.

    Uses the pipeline's batched path for throughput on long lists.
    """
    if not texts:
        return []
    if not _finbert_enabled():
        return [None] * len(texts)
    pipe = _load_finbert_pipeline()
    if not pipe:
        return [None] * len(texts)
    cleaned = [(t or "").strip() for t in texts]
    out = [None] * len(cleaned)
    indexes = [i for i, t in enumerate(cleaned) if t]
    if not indexes:
        return out
    try:
        results = pipe([cleaned[i][:512] for i in indexes], truncation=True)
        for i, result in zip(indexes, results):
            label = str(result["label"]).lower()
            score = _FINBERT_LABEL_TO_SCORE.get(label)
            if score is not None:
                out[i] = {
                    "label": label,
                    "score": float(score),
                    "confidence": float(result["score"]),
                }
    except Exception as exc:  # noqa: BLE001 - no-fail invariant
        logger.warning("FinBERT batch grading failed; skipping: %s", exc)
    return out
