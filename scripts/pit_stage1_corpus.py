"""Stage 1 — NLP skill on VERIFIED human-labeled corpora (atemporal).

D-20260823-001 Stack A. Oracle: Financial PhraseBank (Malo et al. 2014,
4,846 finance-domain sentences, human-labelled neg/neu/pos, CC BY-NC-SA —
license recorded per synthesis contingency). Instruments under test:

  house    : Qualitative psychological NLPEngine (rule-based lexicon+VADER;
             B-1 provenance-clean by construction)
  lm_dict  : Loughran-McDonald Master Dictionary baseline (optional local
             CSV at data/lm_master_dict.csv; skipped cleanly if absent)

Bars (frozen config/weights_sentinel_bars.yaml):
  oracle_f1_min >= 0.62 (house)   lm_dict_baseline_f1 >= 0.55 (else miscalibrated bar)

Usage: python scripts/pit_stage1_corpus.py
"""
import json
import re
import sys
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "Qualitative"))

from scripts.pit_phase0_audit import _bars

RESULTS_PATH = Path("data/pit_stage1_results.json")
LM_LOCAL = Path("data/lm_master_dict.csv")
LM_URL = "https://sraf.nd.edu/wp-content/uploads/2021/01/Loughran-McDonald_MasterDictionary_2020.csv"
THETA = 0.05


PHRASEBANK_URL = (
    "https://huggingface.co/datasets/financial_phrasebank/"
    "resolve/main/data/FinancialPhraseBank-v1.0.zip"
)
LABEL_MAP = {"negative": 0, "neutral": 1, "positive": 2}


def load_phrasebank():
    """Fetch Malo et al. v1.0 zip (canonical HF mirror of the authors' data)
    and parse Sentences_75Agree.txt lines of form 'sentence@label'."""
    import io
    import zipfile

    import requests

    resp = requests.get(PHRASEBANK_URL, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        raw = zf.read("FinancialPhraseBank-v1.0/Sentences_75Agree.txt")
    rows = []
    for line in raw.decode("latin-1").splitlines():
        if "@" not in line:
            continue
        text, _, lab = line.rpartition("@")
        text, lab = text.strip(), lab.strip()
        if lab in LABEL_MAP and text:
            rows.append((text, LABEL_MAP[lab]))
    return rows


def ensure_lm_dict() -> Path | None:
    if LM_LOCAL.exists():
        return LM_LOCAL
    try:
        print(f"attempting LM dictionary download: {LM_URL}")
        req = urllib.request.Request(LM_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            LM_LOCAL.write_bytes(resp.read())
        return LM_LOCAL
    except Exception as exc:
        print(f"LM dict unavailable ({exc.__class__.__name__}); baseline SKIPPED")
        return None


def load_lm_sets(path: Path):
    import csv
    pos, neg = set(), set()
    with open(path, encoding="latin-1") as fh:
        for row in csv.DictReader(fh):
            word = row.get("Word", "").upper()
            p = float(row.get("Positive", 0) or 0)
            n = float(row.get("Negative", 0) or 0)
            if p > 0:
                pos.add(word)
            if n > 0:
                neg.add(word)
    return pos, neg


def lm_label(text: str, pos: set, neg: set) -> int:
    words = re.findall(r"[A-Z]+", text.upper())
    b, s = sum(w in pos for w in words), sum(w in neg for w in words)
    return 2 if b > s else (0 if s > b else 1)


def macro_f1(y_true, y_pred) -> float:
    f1s = []
    for cls in (0, 1, 2):
        tp = sum(1 for t, p in zip(y_true, y_pred) if t == cls and p == cls)
        fp = sum(1 for t, p in zip(y_true, y_pred) if t != cls and p == cls)
        fn = sum(1 for t, p in zip(y_true, y_pred) if t == cls and p != cls)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1s.append(2 * prec * rec / (prec + rec) if prec + rec else 0.0)
    return sum(f1s) / 3


def main() -> int:
    bars = _bars()
    data = load_phrasebank()
    dist = Counter(lbl for _, lbl in data)
    print(f"PhraseBank rows: {len(data)} | dist(neg/neu/pos): "
          f"{dist[0]}/{dist[1]}/{dist[2]}")

    sys.path.insert(0, ".")
    from psychological.nlp_engine import NLPEngine

    eng = NLPEngine()

    def house_label(text: str) -> int:
        c = eng.analyze(text)["compound_vader"]
        return 2 if c >= THETA else (0 if c <= -THETA else 1)

    texts = [t for t, _ in data]
    truth = [l for _, l in data]
    preds_house = [house_label(t) for t in texts]
    f1_house = macro_f1(truth, preds_house)

    results = {
        "decision": "D-20260823-001",
        "stage": 1,
        "oracle": "takala/financial_phrasebank:sentences_75agree",
        "n": len(data),
        "label_distribution": {"neg": dist[0], "neu": dist[1], "pos": dist[2]},
        "theta": THETA,
        "house_instrument": {
            "name": "psychological.nlp_engine.NLPEngine(compound_vader)",
            "provenance": "rule-based (B-1 clean by construction)",
            "macro_f1": round(f1_house, 4),
            "bar_oracle_f1_min": bars["stage1_corpus"]["oracle_f1_min"],
        },
    }
    results["house_instrument"]["pass"] = (
        f1_house >= bars["stage1_corpus"]["oracle_f1_min"]
    )

    lm_path = ensure_lm_dict()
    if lm_path:
        pos, neg = load_lm_sets(lm_path)
        preds_lm = [lm_label(t, pos, neg) for t in texts]
        f1_lm = macro_f1(truth, preds_lm)
        results["lm_dict"] = {
            "source_file": str(lm_path),
            "n_pos_terms": len(pos),
            "n_neg_terms": len(neg),
            "macro_f1": round(f1_lm, 4),
            "bar_baseline_f1_min": bars["stage1_corpus"]["lm_dict_baseline_f1_min"],
            "pass_bar": f1_lm >= bars["stage1_corpus"]["lm_dict_baseline_f1_min"],
        }
    else:
        results["lm_dict"] = {"status": "SKIPPED — place CSV at data/lm_master_dict.csv"}

    RESULTS_PATH.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))
    ok = results["house_instrument"]["pass"] and results.get("lm_dict", {}).get(
        "pass_bar", True) and results["lm_dict"].get("status") is None or lm_path is not None
    return 0 if results["house_instrument"]["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
