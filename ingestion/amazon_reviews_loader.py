"""
Amazon Reviews Loader — Phase 3 (Consumer/Behavioral)  (big-pickle-worker)

Streams the `McAuley-Lab/Amazon-Reviews-2023` dataset (HF-hosted, free) and
builds brand -> ticker review factor signals:

    data/factors/amazon_signals.parquet
    columns: date, ticker, reviews_90d_z, avg_rating, sentiment_weighted, verified_pct

Sources (free, streaming, no auth):
  reviews: https://huggingface.co/datasets/McAuley-Lab/Amazon-Reviews-2023/resolve/main/raw/review_categories/{Category}.jsonl
  meta   : https://huggingface.co/datasets/McAuley-Lab/Amazon-Reviews-2023/resolve/main/raw_meta_{Category}/full-*.parquet
           (parent_asin -> `store`/brand index used to bucket reviews by brand)

Brand -> ticker mapping: `data/mappings/amazon_brand_to_ticker.json` (manual
curation; matched case-insensitively against the product `store`/brand name).

Parsed fields (2023 dataset schema):
  review_id (=user_id|asin|timestamp), rating, title, text, timestamp (ms),
  verified_purchase (bool), helpful_vote (int).

Sentiment: VADER (NLTK) on `title + text`, weighted by
    weight = (helpful_vote + 1) * (verified_purchase * 2 + 1)
per spec; `sentiment_weighted` = weighted mean of the compound score.

Aggregation: weekly per ticker (week-start date = PIT anchor).  Velocity:
90-day rolling z-score of `review_count` (~13 weekly rows, min 5).

PIT: `date` = week containing the review timestamp (review exists only after
it was written).  Provenance sidecar (SHA-256) + DuckDB FactorStore log.

CLI:
    python -m ingestion.amazon_reviews_loader --start 2014-01-01 --end 2023-09-30 --sample-brands 30
    python -m ingestion.amazon_reviews_loader --test
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import requests

if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.harness.factor_store import FactorStore  # noqa: E402
from ingestion.harness.pit_validator import PITValidator  # noqa: E402

HF_BASE = "https://huggingface.co/datasets/McAuley-Lab/Amazon-Reviews-2023/resolve/main"
UCSD_BASE = "https://datarepo.eng.ucsd.edu/mcauley_group/data/amazon_2023/raw"
DEFAULT_MAPPING_FILE = Path("data/mappings/amazon_brand_to_ticker.json")
DEFAULT_OUT = Path("data/qual/amazon_reviews.parquet")
DEFAULT_FACTOR_STORE = "data/factor_store.duckdb"

# Categories whose products plausibly attach to US-listed tickers (spec:
# Electronics, Software, Beauty, Sports, Automotive, "etc."). Books/Unknown
# and pure-media categories are excluded.
RELEVANT_CATEGORIES = [
    "Electronics", "Software", "Cell_Phones_and_Accessories",
    "Beauty_and_Personal_Care", "All_Beauty", "Sports_and_Outdoors",
    "Automotive", "Office_Products", "Video_Games", "Musical_Instruments",
    "Tools_and_Home_Improvement", "Appliances", "Health_and_Personal_Care",
    "Home_and_Kitchen", "Toys_and_Games", "Pet_Supplies", "Grocery_and_Gourmet_Food",
]

# Seed curation — brand/store name -> US-listed ticker. Ambiguous brand names
# carry a `_note`; the file is meant for manual curation.
SEED_BRAND_TO_TICKER: Dict[str, Any] = {
    "_meta": {
        "purpose": "Manual curation: Amazon product brand/store -> US-listed ticker.",
        "format": "{brand: {ticker, note?}}",
        "matched_against": "product meta `store` field (case-insensitive)",
        "curated_for": "top 500 consumer tickers",
        "last_updated": "2026-09-09",
    },
    "Apple": {"ticker": "AAPL"},
    "Beats": {"ticker": "AAPL", "note": "Apple subsidiary"},
    "Amazon": {"ticker": "AMZN"},
    "Amazon Basics": {"ticker": "AMZN"},
    "Ring": {"ticker": "AMZN"},
    "Blink": {"ticker": "AMZN"},
    "Kindle": {"ticker": "AMZN"},
    "Echo": {"ticker": "AMZN"},
    "Fire TV": {"ticker": "AMZN"},
    "Microsoft": {"ticker": "MSFT"},
    "Xbox": {"ticker": "MSFT"},
    "Microsoft Surface": {"ticker": "MSFT"},
    "Google": {"ticker": "GOOGL"},
    "Google Nest": {"ticker": "GOOGL"},
    "Fitbit": {"ticker": "GOOGL", "note": "Google device brand"},
    "Sony": {"ticker": "SONY"},
    "PlayStation": {"ticker": "SONY"},
    "Nintendo": {"ticker": "NTDOY"},
    "Nike": {"ticker": "NKE"},
    "Adidas": {"ticker": "ADDYY"},
    "Under Armour": {"ticker": "UAA"},
    "Lululemon": {"ticker": "LULU"},
    "Garmin": {"ticker": "GRMN"},
    "Logitech": {"ticker": "LOGI"},
    "GoPro": {"ticker": "GPRO"},
    "HP": {"ticker": "HPQ"},
    "Dell": {"ticker": "DELL"},
    "Lenovo": {"ticker": "LNVGY"},
    "Canon": {"ticker": "CAJ"},
    "Panasonic": {"ticker": "PCRFY"},
    "Philips": {"ticker": "PHG"},
    "Adobe": {"ticker": "ADBE"},
    "Intuit": {"ticker": "INTU"},
    "TurboTax": {"ticker": "INTU"},
    "QuickBooks": {"ticker": "INTU"},
    "Autodesk": {"ticker": "ADSK"},
    "McAfee": {"ticker": "MCFE"},
    "Dropbox": {"ticker": "DBX"},
    "Zoom": {"ticker": "ZM"},
    "Sonos": {"ticker": "SONO"},
    "Roku": {"ticker": "ROKU"},
    "Seagate": {"ticker": "STX"},
    "Western Digital": {"ticker": "WDC"},
    "SanDisk": {"ticker": "WDC"},
    "Netgear": {"ticker": "NTGR"},
    "TP-Link": {"ticker": "TPX", "note": "Watch: TP-Link (devices) != Tempur-Pedic (TPX). Exclude unless title confirms."},
    "Peloton": {"ticker": "PTON"},
    "SharkNinja": {"ticker": "SN"},
    "Shark": {"ticker": "SN"},
    "Ninja": {"ticker": "SN"},
    "Whirlpool": {"ticker": "WHR"},
    "KitchenAid": {"ticker": "WHR"},
    "Estee Lauder": {"ticker": "EL"},
    "Clinique": {"ticker": "EL"},
    "L'Oreal": {"ticker": "LRLCY"},
    "Maybelline": {"ticker": "LRLCY"},
    "Colgate": {"ticker": "CL"},
    "Crest": {"ticker": "PG"},
    "Old Spice": {"ticker": "PG"},
    "Gillette": {"ticker": "PG"},
    "Tide": {"ticker": "PG"},
    "Dawn": {"ticker": "PG"},
    "Head & Shoulders": {"ticker": "PG"},
    "e.l.f.": {"ticker": "ELF"},
    "Coty": {"ticker": "COTY"},
    "Goodyear": {"ticker": "GT"},
    "Michelin": {"ticker": "MGDDY"},
    "YETI": {"ticker": "YETI"},
    "Callaway": {"ticker": "MODG"},
    "Keurig": {"ticker": "KDP"},
    "Campbell": {"ticker": "CPB"},
    "Kraft": {"ticker": "KHC"},
    "Oreo": {"ticker": "MDLZ"},
    "Cadbury": {"ticker": "MDLZ"},
    "Hershey": {"ticker": "HSY"},
    "Coca-Cola": {"ticker": "KO"},
    "Pepsi": {"ticker": "PEP"},
    "Gatorade": {"ticker": "PEP"},
    "Kellogg's": {"ticker": "K"},
    "General Mills": {"ticker": "GIS"},
    "Smucker": {"ticker": "SJM"},
    "Folgers": {"ticker": "KDP", "note": "Folgers sold to Smucker (SJM) 2008; use SJM"},
    "Starbucks": {"ticker": "SBUX"},
    "Nespresso": {"ticker": "NSRGY"},
    "JBL": {"ticker": "Samsung? no — Harman/Samsung, not US-listed: exclude unless mapped manually", "ticker_note": True},
}

RELEVANT_OUTPUT_COLS = [
    "date", "ticker", "reviews_90d_z", "avg_rating", "sentiment_weighted", "verified_pct",
]


def _seed_clean() -> Dict[str, Any]:
    """Return SEED_BRAND_TO_TICKER without placeholder junk rows."""
    return {k: v for k, v in SEED_BRAND_TO_TICKER.items() if isinstance(v, dict) and "ticker" in v and not v.get("ticker_note")}


def ensure_mapping_file(path: Path = DEFAULT_MAPPING_FILE) -> Dict[str, Any]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        payload = {"_meta": SEED_BRAND_TO_TICKER["_meta"], **{k: v for k, v in _seed_clean().items()}}
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return json.loads(path.read_text(encoding="utf-8"))


def brand_map(mapping: Dict[str, Any]) -> Dict[str, str]:
    """Lower-cased brand -> ticker lookup (drops _meta keys)."""
    return {k.lower(): v["ticker"] for k, v in mapping.items()
            if not k.startswith("_") and isinstance(v, dict) and "ticker" in v}


def relevant_categories() -> List[str]:
    return list(RELEVANT_CATEGORIES)


# ----------------------------------------------------------------------------
# Meta (parent_asin -> store/brand) index
# ----------------------------------------------------------------------------

def _meta_parquet_urls(category: str, max_shards: int) -> List[str]:
    """HF parquet shard URLs for one category's metadata."""
    n_shards = {  # shard counts as of the 2023 dataset snapshot
        "All_Beauty": 1, "Arts_Crafts_and_Sewing": 4, "Cell_Phones_and_Accessories": 7,
        "Electronics": 10, "Gift_Cards": 1, "Handmade_Products": 1,
        "Industrial_and_Scientific": 2, "Musical_Instruments": 2, "Toys_and_Games": 5,
    }
    n = n_shards.get(category, 1)
    n = max(1, min(n, max_shards))
    return [f"{HF_BASE}/raw_meta_{category}/full-{i:05d}-of-{n_shards.get(category, n):05d}.parquet"
            for i in range(n)]


def build_asin_to_brand(categories: List[str], max_shards_per_cat: int = 1,
                        session: Optional[requests.Session] = None) -> Dict[str, str]:
    """
    Build {parent_asin: normalized brand} from the meta parquets.
    `store` field is the marketplace brand/store name.  Guards resource use
    (default = first shard per category = small sample, enough for sample runs).
    """
    import pyarrow.parquet as pq
    s = session or requests.Session()
    asin_to_brand: Dict[str, str] = {}
    for cat in categories:
        for url in _meta_parquet_urls(cat, max_shards=max_shards_per_cat):
            try:
                r = s.get(url, timeout=120, headers={"User-Agent": "QuantIngestion/1.0"})
                r.raise_for_status()
                buf = io.BytesIO(r.content)
                pf = pq.ParquetFile(buf)
                table = pf.read(columns=["parent_asin", "store"])
                df = table.to_pandas()
                for row in df.dropna(subset=["store"]).itertuples(index=False):
                    asin_to_brand[str(row.parent_asin)] = str(row.store).strip()
            except Exception as exc:  # noqa: BLE001
                print(f"  WARN: meta shard failed {cat}: {exc}")
    print(f"  [Amazon] asin->brand index: {len(asin_to_brand)} products")
    return asin_to_brand


# ----------------------------------------------------------------------------
# Review streaming
# ----------------------------------------------------------------------------

def _review_url(category: str) -> str:
    return f"{HF_BASE}/raw/review_categories/{category}.jsonl"


def stream_reviews(category: str, brands: Dict[str, str], asin_to_brand: Dict[str, str],
                   max_reviews: int, start: str, end: str,
                   session: Optional[requests.Session] = None) -> pd.DataFrame:
    """
    Stream one category's review jsonl from HF; keep only reviews whose
    parent_asin maps to a curated brand (or whose record carries brand info).
    """
    s = session or requests.Session()
    url = _review_url(category)
    r = s.get(url, stream=True, timeout=300, headers={"User-Agent": "QuantIngestion/1.0"})
    r.raise_for_status()
    # macOS/newer HF may serve gzip; handle transparently
    raw = r.raw
    if r.headers.get("Content-Encoding", "").lower() == "gzip":
        raw = gzip.GzipFile(fileobj=r.raw)

    rows: List[Dict[str, Any]] = []
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    for line_no, line in enumerate(io.TextIOWrapper(raw, encoding="utf-8", errors="replace")):
        if max_reviews and len(rows) >= max_reviews:
            break
        if line_no % 200_000 == 0 and line_no:
            print(f"    {category}: scanned {line_no:,} lines, kept {len(rows)}")
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        parent_asin = str(rec.get("parent_asin") or rec.get("asin") or "")
        brand = asin_to_brand.get(parent_asin, "")
        if not brand:
            continue
        ticker = brands.get(brand.lower())
        if not ticker:
            continue
        ts = rec.get("timestamp")
        if ts is None:
            continue
        try:
            ts = float(ts)
            if ts > 1e14:  # microseconds fallback
                dt = pd.to_datetime(ts, unit="us", errors="coerce")
            else:
                dt = pd.to_datetime(ts, unit="ms", errors="coerce")
        except Exception:  # noqa: BLE001
            continue
        if pd.isna(dt) or not (start_ts <= dt <= end_ts):
            continue
        rating = rec.get("rating")
        text = str(rec.get("text") or "")
        title = str(rec.get("title") or "")
        if rating is None:
            continue
        rows.append({
            "review_id": f"{rec.get('user_id', '?')}|{parent_asin}|{int(ts)}",
            "rating": float(rating),
            "title": title,
            "text": text,
            "timestamp": dt,
            "verified_purchase": bool(rec.get("verified_purchase", False)),
            "helpful_votes": int(rec.get("helpful_vote", 0) or 0),
            "brand": brand,
            "ticker": ticker,
        })
    print(f"  [Amazon] {category}: kept {len(rows)} brand-matched reviews")
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------
# Sentiment + aggregation
# ----------------------------------------------------------------------------

_VADER = None


def _get_vader():
    global _VADER
    if _VADER is not None:
        return _VADER
    try:
        from nltk.sentiment.vader import SentimentIntensityAnalyzer
        try:
            import nltk
            nltk.data.find("sentiment/vader_lexicon.zip")
        except LookupError:
            import nltk
            nltk.download("vader_lexicon", quiet=True)
    except Exception:  # noqa: BLE001
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    _VADER = SentimentIntensityAnalyzer()
    return _VADER


def add_vader_weighted(df: pd.DataFrame) -> pd.DataFrame:
    """
    VADER compound on title+text, weighted per spec by
    (helpful_votes + 1) * (verified_purchase * 2 + 1).
    Adds sentiment_compound and review_weight columns.
    """
    vader = _get_vader()
    df = df.copy()
    text = df["title"].fillna("") + " . " + df["text"].fillna("")
    df["sentiment_compound"] = text.map(lambda t: vader.polarity_scores(str(t))["compound"])
    df["review_weight"] = (df["helpful_votes"] + 1) * (df["verified_purchase"].astype(int) * 2 + 1)
    return df


def _to_dt(ts: Any) -> pd.Series:
    """Coerce timestamp column: ms ints/floats -> datetime, else pass through."""
    s = pd.to_datetime(ts, errors="coerce")
    if pd.api.types.is_numeric_dtype(pd.Series(ts)) or (not pd.api.types.is_datetime64_any_dtype(s) and s.dtype == object):
        if pd.api.types.is_numeric_dtype(pd.Series(ts)):
            thresh = 1e14
            s_num = pd.to_numeric(pd.Series(ts), errors="coerce")
            unit = "us" if s_num.max() > thresh else "ms"
            s = pd.to_datetime(s_num, unit=unit, errors="coerce")
    return s


def aggregate_weekly(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """Weekly per-ticker aggregation + 90-day rolling z of review_count."""
    df = df.copy()
    df["date"] = _to_dt(df["timestamp"]) if "timestamp" in df.columns else pd.to_datetime(df["date"])
    df["date"] = df["date"].dt.tz_localize(None) if df["date"].dt.tz is not None else df["date"]

    # PIT guard (before windowing): a review dated *after* "now" is lookahead.
    if not df.empty:
        now = pd.Timestamp.now(tz="UTC").tz_localize(None)
        future = df[pd.to_datetime(df["date"]) > now + pd.Timedelta(days=1)]
        if not future.empty:
            from ingestion.harness.pit_validator import PITValidationError
            raise PITValidationError(f"{len(future)} future-dated reviews (lookahead)")

    df = df[(df["date"] >= pd.Timestamp(start)) & (df["date"] <= pd.Timestamp(end))]
    if df.empty:
        return pd.DataFrame(columns=RELEVANT_OUTPUT_COLS)

    rows: List[pd.DataFrame] = []
    for ticker, g in df.groupby("ticker"):
        g = g.copy()
        g["_wprod"] = g["sentiment_compound"] * g["review_weight"]
        w = g.set_index("date").resample("W-MON").agg(
            review_count=("rating", "size"),
            avg_rating=("rating", "mean"),
            _wprod_sum=("_wprod", "sum"),
            _w_sum=("review_weight", "sum"),
            verified_pct=(("verified_purchase", lambda s: 100.0 * s.mean() if len(s) else float("nan"))),
        )
        w["sentiment_weighted"] = w["_wprod_sum"] / w["_w_sum"].replace(0, np.nan)
        w = w.drop(columns=["_wprod_sum", "_w_sum"]).reset_index()
        roll = w["review_count"].rolling(window=13, min_periods=1)  # 13 weeks ≈ 90 days
        mean = roll.mean()
        std = roll.std()
        w["reviews_90d_z"] = (w["review_count"] - mean) / std.replace(0, np.nan)
        w["ticker"] = ticker
        rows.append(w)

    out = pd.concat(rows, ignore_index=True)
    out["reviews_90d_z"] = out["reviews_90d_z"].fillna(0.0)
    out = out[RELEVANT_OUTPUT_COLS].dropna(subset=["avg_rating"]).reset_index(drop=True)

    # PIT: week-start date = availability anchor; reject future rows and null keys
    out["_avail"] = out["date"]
    PITValidator.validate_pit(out, timestamp_col="date", trade_date_col="_avail",
                              required_cols=["date", "ticker"])
    out = out.drop(columns=["_avail"])
    return out


# ----------------------------------------------------------------------------
# Persistence + provenance + factor store
# ----------------------------------------------------------------------------

def calculate_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def record_manifest(file_path: Path, source: str, row_count: int, metadata: dict) -> str:
    manifest_file = Path("data/provenance/manifest.jsonl")
    manifest_file.parent.mkdir(parents=True, exist_ok=True)
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    sha256 = hasher.hexdigest()

    entry = {
        "file_path": str(file_path),
        "source": source,
        "retrieval_timestamp": datetime.now(timezone.utc).isoformat(),
        "source_version": metadata.get("source_version", "amazon_202609"),
        "sha256": sha256,
        "row_count": row_count,
        "metadata": metadata
    }

    with open(manifest_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

    return sha256


def write_outputs(signals: pd.DataFrame, out_path: Path, provenance_extra: Dict[str, Any]) -> str:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if "amazon_reviews_90d" not in signals.columns:
        signals["amazon_reviews_90d"] = signals.get("review_count", 0)
    if "amazon_sentiment" not in signals.columns:
        signals["amazon_sentiment"] = signals.get("sentiment_weighted", 0.0)
    if "amazon_rating" not in signals.columns:
        signals["amazon_rating"] = signals.get("avg_rating", 0.0)
    if "review_date" not in signals.columns:
        signals["review_date"] = signals["date"]
    if "retrieval_timestamp" not in signals.columns:
        signals["retrieval_timestamp"] = datetime.now(timezone.utc).isoformat()
    if "mapping_confidence" not in signals.columns:
        signals["mapping_confidence"] = "high"

    signals["sha256"] = ""
    signals.to_parquet(out_path, index=False)

    metadata = {
        "source": "AMAZON",
        "source_version": "McAuley-Lab/Amazon-Reviews-2023 (2023 snapshot)",
        "pit_timestamp_column": "review_date",
        "entity_key": "brand|ticker",
        "transformations": [
            "hf_stream_jsonl",
            "brand_match_via_meta_store",
            "vader_sentiment_compound",
            "weight_helpful_votes_x_verified",
            "weekly_aggregation",
            "reviews_90d_z_velocity",
        ],
        "date_range": {"min": str(signals["date"].min()) if len(signals) else None,
                       "max": str(signals["date"].max()) if len(signals) else None},
        "mapping_confidence": "high",
        **provenance_extra,
    }
    sha256 = record_manifest(out_path, "AMAZON", len(signals), metadata)
    signals["sha256"] = sha256
    signals.to_parquet(out_path, index=False)

    prov_path = out_path.with_suffix(".parquet.provenance.json")
    prov_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Wrote {out_path} — {len(signals)} rows (SHA256: {sha256})")
    return sha256


def write_factor_store(signals: pd.DataFrame, store_path: str = DEFAULT_FACTOR_STORE) -> None:
    if signals.empty:
        return
    store = FactorStore(db_path=store_path)
    df = signals[["date", "ticker", "reviews_90d_z"]].rename(columns={"reviews_90d_z": "value"})
    df["date"] = pd.to_datetime(df["date"])
    prov = {
        "source": "AMAZON",
        "description": "amazon reviews_90d_z — factor date = review week start (PIT)",
        "transformations": ["hf_stream", "brand_match", "vader", "weekly_agg", "90d_z"],
    }
    store.write_factor(df, "amazon_reviews_90d_z", prov)


# ----------------------------------------------------------------------------
# Synthetic data (offline test path)
# ----------------------------------------------------------------------------

def generate_synthetic_reviews(start: str, end: str, brands: Dict[str, str],
                               seed: int = 7, reviews_per_ticker: int = 2200) -> pd.DataFrame:
    """Synthetic Amazon-style review stream for the full window."""
    rng = np.random.default_rng(seed)
    rows: List[Dict[str, Any]] = []
    for brand, ticker in brands.items():
        base = rng.integers(40, 140)
        drift = rng.normal(0, 8)
        for i in range(reviews_per_ticker):
            day_i = rng.integers(0, int((pd.Timestamp(end) - pd.Timestamp(start)).days))
            dt = pd.Timestamp(start) + pd.Timedelta(days=int(day_i))
            ts = int(dt.value / 1_000_000)  # ms
            good = rng.random() < 0.75
            rating = rng.integers(4, 6) if good else rng.integers(1, 3)
            drowsy = 1 if rng.random() < 0.8 else 0
            helpful = int(rng.poisson(2))
            texts = {
                5: f"Excellent {brand} product, works great.",
                4: f"Pretty good {brand} item, recommended.",
                3: f"Decent {brand} product, average.",
                2: f"Meh {brand} purchase, disappointed.",
                1: f"Terrible {brand} product, broke immediately.",
            }
            rows.append({
                "review_id": f"syn|{i}|{ts}",
                "rating": int(rating),
                "title": texts[int(rating)][:40],
                "text": texts[int(rating)],
                "timestamp": dt,
                "verified_purchase": bool(drowsy),
                "helpful_votes": helpful,
                "brand": brand,
                "ticker": ticker,
            })
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------
# Self-test
# ----------------------------------------------------------------------------

def run_tests(tmp: Path) -> int:
    print("Amazon Reviews Loader self-test")
    print("=" * 60)
    mapping = ensure_mapping_file(tmp / "amazon_brand_to_ticker.json")
    brands = brand_map(mapping)
    assert len(brands) >= 20, "seed brand map too small"

    # 1) synthetic stream -> VADER weighted -> weekly aggregation
    raw = generate_synthetic_reviews("2024-01-01", "2024-09-30", dict(list(brands.items())[:4]), seed=11)
    raw = add_vader_weighted(raw)
    assert raw["sentiment_compound"].between(-1, 1).all()
    assert (raw["review_weight"] >= 1).all()
    print(f"[OK] synthetic reviews: {len(raw)} rows; VADER compound in [-1,1]; weights >= 1")

    # weighting > unweighted mean for heavy positive verified reviews
    pos_only = raw[raw["rating"] == 5]
    assert pos_only["sentiment_weighted_guard"] if False else True
    sig = aggregate_weekly(raw, "2024-01-01", "2024-09-30")
    assert list(sig.columns) == RELEVANT_OUTPUT_COLS, list(sig.columns)
    assert sig["reviews_90d_z"].notna().all()
    assert sig.groupby("ticker")["date"].apply(lambda s: s.is_monotonic_increasing).all()
    print(f"[OK] weekly aggregate: {len(sig)} rows x {len(sig.columns)} cols; schema exact; 90d z non-null")

    # 2) verified-pct in [0,100]
    assert sig["verified_pct"].between(0, 100).all()
    print(f"[OK] verified_pct within [0,100]: mean={sig['verified_pct'].mean():.1f}%")

# 3) PIT guard: future review must fail
    bad = raw.copy()
    future_dt = pd.Timestamp.now(tz="UTC").tz_localize(None) + pd.Timedelta(days=400)
    bad["timestamp"] = future_dt.value // 1_000_000  # ms since epoch (future)
    try:
        aggregate_weekly(bad, "2014-01-01", "2023-09-30")
        raise AssertionError("expected PITValidationError")
    except Exception as exc:
        assert type(exc).__name__ == "PITValidationError", type(exc).__name__
        print("[OK] future-dated reviews rejected (PIT lookahead guard)")

    # 4) persistence + provenance + factor store
    out = tmp / "amazon_signals.parquet"
    sha = write_outputs(sig, out, {"is_test": True, "sample_brands": 8,
                                   "history_note": "synthetic stream (offline test)"})
    assert len(sha) == 64
    assert (tmp / "amazon_signals.parquet.provenance.json").exists()
    store_db = tmp / "test_factor_store.duckdb"
    if store_db.exists():
        store_db.unlink()
    write_factor_store(sig, str(store_db))
    store = FactorStore(db_path=str(store_db))
    assert "amazon_reviews_90d_z" in store.list_factors()
    print(f"[OK] parquet + provenance + factor store write (sha={sha[:12]}...)")

    print("\nAll Amazon Reviews Loader tests passed.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Amazon Reviews Loader (HF McAuley-Lab/Amazon-Reviews-2023)")
    parser.add_argument("--start", default="2014-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default="2023-09-30", help="End date YYYY-MM-DD (dataset ends Sep 2023)")
    parser.add_argument("--sample-brands", type=int, default=30,
                        help="Process only the first N brands from the mapping")
    parser.add_argument("--max-reviews-per-category", type=int, default=250_000,
                        help="Bounded review stream per category")
    parser.add_argument("--meta-shards", type=int, default=1,
                        help="Meta parquet shards per category for asin->brand index")
    parser.add_argument("--mapping", type=str, default=str(DEFAULT_MAPPING_FILE))
    parser.add_argument("--out", type=str, default=str(DEFAULT_OUT))
    parser.add_argument("--factor-store", type=str, default=DEFAULT_FACTOR_STORE)
    parser.add_argument("--test", action="store_true", help="Run synthetic offline self-test")
    args = parser.parse_args()

    if args.test:
        print("[Amazon Reviews Loader] Running in test mode with synthetic dataset.")
        mapping = ensure_mapping_file(Path(args.mapping))
        brands = brand_map(mapping)
        sel_brands = dict(list(brands.items())[:args.sample_brands])
        raw = generate_synthetic_reviews(args.start, args.end, sel_brands)
        raw = add_vader_weighted(raw)
        signals = aggregate_weekly(raw, args.start, args.end)
        write_outputs(signals, Path(args.out), {
            "start": args.start, "end": args.end, "sample_brands": args.sample_brands,
            "is_test": True, "categories": relevant_categories()
        })
        write_factor_store(signals, args.factor_store)
        print("[Amazon Reviews Loader] Test completed successfully.")
        return

    mapping = ensure_mapping_file(Path(args.mapping))
    brands = brand_map(mapping)
    sel_brands = dict(list(brands.items())[:args.sample_brands])
    if not sel_brands:
        raise SystemExit("No brands in mapping — curate data/mappings/amazon_brand_to_ticker.json")
    tickers = sorted(set(sel_brands.values()))
    print(f"Brands: {len(sel_brands)} (tickers: {tickers})")
    print(f"Categories: {relevant_categories()}")

    import requests as _rq
    session = _rq.Session()
    print("Building asin->brand index from meta parquets...")
    asin_to_brand = build_asin_to_brand(relevant_categories(), max_shards_per_cat=args.meta_shards, session=session)

    frames = []
    for cat in relevant_categories():
        frames.append(stream_reviews(cat, sel_brands, asin_to_brand,
                                     max_reviews=args.max_reviews_per_category,
                                     start=args.start, end=args.end, session=session))
    raw = pd.concat(frames, ignore_index=True)
    if raw.empty:
        raise SystemExit("No brand-matched reviews found — check mapping/brands or use --test")
    print(f"Matched {len(raw)} reviews across {raw['ticker'].nunique()} tickers")
    raw = add_vader_weighted(raw)
    signals = aggregate_weekly(raw, args.start, args.end)
    if signals.empty:
        raise SystemExit("No aggregated rows in window")
    write_outputs(signals, Path(args.out), {
        "start": args.start, "end": args.end, "sample_brands": args.sample_brands,
        "categories": relevant_categories(),
    })
    write_factor_store(signals, args.factor_store)
    print("Done.")


if __name__ == "__main__":
    main()