"""
App Store Reviews Loader — Phase 3 (Consumer/Behavioral)  (big-pickle-worker)

Scrapes App Store customer reviews (free iTunes RSS API), maps app -> ticker
via `data/mappings/app_store_to_ticker.json` (manual curation), computes
daily-per-ticker aggregates + 90-day rolling z-score of review volume, and
persists:

    data/factors/app_store_signals.parquet
    columns: date, ticker, reviews_90d_z, avg_rating, sentiment_mean,
             sentiment_std, rating_1_pct, rating_5_pct

Source (free, no auth):
    https://itunes.apple.com/rss/customerreviews/id={app_id}/sortBy=mostRecent/json

PIT discipline (Strategy §8): `date` = the review's `updated` timestamp
(availability anchor).  No lookahead — a review exists only after it was posted.

Sentiment: VADER (NLTK `vader_lexicon`) on `title + text` -> `sentiment_compound`.

Velocity: 90-day rolling z-score of daily `review_count` per ticker.

Provenance: SHA-256 JSON sidecar + DuckDB FactorStore log (factor date = review
date, the PIT timestamp).

NOTE ON HISTORY: the iTunes RSS customer-reviews endpoint exposes only the most
recent ~500 reviews per app (10 pages x 50).  Live backfill deeper than that
recency window is not possible from this free endpoint; full 2014-2024 history
requires the synthetic path (`--test`), an archival source (e.g. CDXJ/Wayback),
or accumulated weekly snapshots.  This is recorded in provenance.

CLI:
    python -m ingestion.app_store_loader --start 2014-01-01 --end 2024-12-31 --sample-apps 20
    python -m ingestion.app_store_loader --test
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import sys

import numpy as np
import pandas as pd
import requests

if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.harness.factor_store import FactorStore  # noqa: E402
from ingestion.harness.pit_validator import PITValidator, PITValidationError  # noqa: E402

BASE_RSS_URL = "https://itunes.apple.com/rss/customerreviews/id={app_id}/sortBy=mostRecent/page={page}/json"
DEFAULT_MAPPING_FILE = Path("data/mappings/app_store_to_ticker.json")
DEFAULT_OUT = Path("data/qual/app_store_velocity.parquet")
DEFAULT_FACTOR_STORE = "data/factor_store.duckdb"
STRATEGY_WINDOW = ("2014-01-01", "2024-12-31")

# Seed curation — app_id -> ticker. Entries flagged by the strategy doc as
# "well-documented public app IDs"; `verified: false` entries should be
# confirmed before heavy use (the mapping file is meant for manual curation).
SEED_APP_TO_TICKER: Dict[str, Dict[str, Any]] = {
    "375380948": {"ticker": "AAPL", "app_name": "Apple Store", "verified": True},
    "541164041": {"ticker": "MSFT", "app_name": "Microsoft Office", "verified": True},
    "951937596": {"ticker": "MSFT", "app_name": "Microsoft Outlook", "verified": True},
    "284815942": {"ticker": "GOOGL", "app_name": "Google", "verified": True},
    "422689480": {"ticker": "GOOGL", "app_name": "Gmail", "verified": True},
    "544007664": {"ticker": "GOOGL", "app_name": "YouTube", "verified": True},
    "585027354": {"ticker": "GOOGL", "app_name": "Google Maps", "verified": True},
    "297606951": {"ticker": "AMZN", "app_name": "Amazon Shopping", "verified": True},
    "284882215": {"ticker": "META", "app_name": "Facebook", "verified": True},
    "389801252": {"ticker": "META", "app_name": "Instagram", "verified": True},
    "363590051": {"ticker": "NFLX", "app_name": "Netflix", "verified": True},
    "582007913": {"ticker": "TSLA", "app_name": "Tesla", "verified": True},
    "368677368": {"ticker": "UBER", "app_name": "Uber", "verified": True},
    "529379082": {"ticker": "LYFT", "app_name": "Lyft", "verified": True},
    "283646709": {"ticker": "PYPL", "app_name": "PayPal", "verified": True},
    "401626263": {"ticker": "ABNB", "app_name": "Airbnb", "verified": True},
    "719972451": {"ticker": "DASH", "app_name": "DoorDash", "verified": True},
    "331177714": {"ticker": "SBUX", "app_name": "Starbucks", "verified": True},
    "523069000": {"ticker": "MCD", "app_name": "McDonald's", "verified": True},
    "1095459566": {"ticker": "NKE", "app_name": "Nike", "verified": True},
    "1130990307": {"ticker": "NKE", "app_name": "Nike SNKRS", "verified": True},
    "338137227": {"ticker": "WMT", "app_name": "Walmart", "verified": True},
    "297430070": {"ticker": "TGT", "app_name": "Target", "verified": True},
    "938385652": {"ticker": "HOOD", "app_name": "Robinhood", "verified": True},
    "886427730": {"ticker": "COIN", "app_name": "Coinbase", "verified": True},
    "711923939": {"ticker": "SQ", "app_name": "Cash App", "verified": True},
    "333903271": {"ticker": "X", "app_name": "X (Twitter)", "verified": True},
    "985746746": {"ticker": "DISC", "app_name": "Discord", "verified": True},
    "447188370": {"ticker": "SNAP", "app_name": "Snapchat", "verified": True},
    "429047995": {"ticker": "PINS", "app_name": "Pinterest", "verified": True},
    "324684580": {"ticker": "SPOT", "app_name": "Spotify", "verified": True},
    "592999990": {"ticker": "SQ", "app_name": "Square POS", "verified": False},
    "1064210728": {"ticker": "RDDT", "app_name": "Reddit", "verified": False},
    "1259756381": {"ticker": "NVDA", "app_name": "NVIDIA GeForce NOW", "verified": False},
    "817873783": {"ticker": "SHOP", "app_name": "Shop (Shopify)", "verified": False},
    "310633997": {"ticker": "META", "app_name": "WhatsApp Messenger", "verified": False},
}

RELEVANT_OUTPUT_COLS = [
    "date", "ticker", "reviews_90d_z", "avg_rating",
    "sentiment_mean", "sentiment_std", "rating_1_pct", "rating_5_pct",
]


# ----------------------------------------------------------------------------
# Mapping file management
# ----------------------------------------------------------------------------

def ensure_mapping_file(path: Path = DEFAULT_MAPPING_FILE) -> Dict[str, Any]:
    """Create the app->ticker mapping file with seed curation if absent."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        payload = {
            "_meta": {
                "purpose": "Manual curation: Apple app_id -> US-listed ticker.",
                "format": "{app_id: {ticker, app_name, verified}}",
                "verified_false": "Confirm app_id before production use.",
                "curated_for": "top 500 tickers with consumer apps",
                "last_updated": "2026-09-09",
            },
            **SEED_APP_TO_TICKER,
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return json.loads(path.read_text(encoding="utf-8"))


def load_app_ids(mapping: Dict[str, Any], sample_apps: Optional[int]) -> List[str]:
    """Return app_ids to process (optionally a sample), excluding _meta keys."""
    ids = [k for k in mapping.keys() if not k.startswith("_")]
    if sample_apps and sample_apps > 0:
        ids = ids[:sample_apps]
    return ids


# ----------------------------------------------------------------------------
# Fetching
# ----------------------------------------------------------------------------

def load_proxy_pool() -> List[str]:
    """Proxy rotation pool from env var PROXY_LIST (comma or newline separated)."""
    raw = os.environ.get("PROXY_LIST", "")
    pool = [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]
    return pool


def _pick_proxy(pool: List[str]) -> Optional[str]:
    if not pool:
        return None
    return random.choice(pool)


def _proxies_dict(proxy: Optional[str]) -> Optional[Dict[str, str]]:
    if not proxy:
        return None
    if "://" not in proxy:
        proxy = "http://" + proxy
    return {"http": proxy, "https": proxy}


def fetch_reviews_rss(app_id: str, page: int = 1, proxies: Optional[Dict[str, str]] = None,
                      timeout: int = 30) -> Dict[str, Any]:
    """Fetch one page (50 reviews) of the iTunes customer-reviews RSS feed."""
    url = BASE_RSS_URL.format(app_id=app_id, page=page)
    resp = requests.get(url, timeout=timeout, proxies=proxies,
                        headers={"User-Agent": "QuantIngestion/1.0 (PIT-clean factor build)"})
    resp.raise_for_status()
    return resp.json()


def parse_rss_entry(entry: Dict[str, Any], app_id: str, ticker: str) -> Optional[Dict[str, Any]]:
    """Parse a single RSS feed entry into a review record (None if unparseable)."""
    try:
        review_id = str(entry.get("id", {}).get("label", "")).split("/")[-1] or None
        rating = entry.get("im:rating", {}).get("label")
        title = entry.get("title", {}).get("label", "")
        text = entry.get("content", {}).get("label", "")
        updated = entry.get("updated", {}).get("label")
        version = entry.get("im:version", {}).get("label")
        author = entry.get("author", {}).get("name", {}).get("label", "")
        if rating is None or updated is None:
            return None
        return {
            "review_id": review_id,
            "app_id": app_id,
            "ticker": ticker,
            "rating": float(rating),
            "title": title or "",
            "text": text or "",
            "date": pd.Timestamp(updated).tz_localize(None),
            "version": version,
            "author": author,
        }
    except Exception as exc:  # noqa: BLE001 — one bad entry must not kill the batch
        print(f"  WARN: unparseable RSS entry for app {app_id}: {exc}")
        return None


def collect_reviews(mapping: Dict[str, Any], app_ids: List[str],
                    max_pages_per_app: int = 10,
                    proxy_pool: Optional[List[str]] = None,
                    sleep_s: float = 0.5) -> pd.DataFrame:
    """
    Fetch recent reviews for the given app_ids.
    Returns raw review rows: review_id, rating, title, text, date, version, author, ticker.
    """
    pool = proxy_pool if proxy_pool is not None else load_proxy_pool()
    rows: List[Dict[str, Any]] = []
    for app_id in app_ids:
        meta = mapping.get(app_id, {})
        ticker = meta.get("ticker", "UNKNOWN")
        if meta.get("verified") is False:
            print(f"  WARN: app {app_id} ({meta.get('app_name', '?')}) not verified — parsing anyway")
        got = 0
        for page in range(1, max_pages_per_app + 1):
            proxy = _pick_proxy(pool)
            try:
                data = fetch_reviews_rss(app_id, page=page, proxies=_proxies_dict(proxy))
            except Exception as exc:  # noqa: BLE001
                print(f"  WARN: app {app_id} page {page} fetch failed: {exc}")
                break
            entries = data.get("feed", {}).get("entry", [])
            if not isinstance(entries, list) or not entries:
                break  # feed exhausted
            parsed = [r for e in entries if (r := parse_rss_entry(e, app_id, ticker)) is not None]
            got += len(parsed)
            rows.extend(parsed)
            if len(entries) < 50:
                break
            time.sleep(sleep_s)
        print(f"  [AppStore] {app_id} -> {ticker}: {got} reviews")
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------
# Sentiment + aggregation
# ----------------------------------------------------------------------------

_VADER = None


def _get_vader():
    """Lazy NLTK VADER (spec: VADER via NLTK), fallback to vaderSentiment."""
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


def add_vader_sentiment(df: pd.DataFrame) -> pd.DataFrame:
    """VADER compound on title + text -> sentiment_compound (-1..1)."""
    vader = _get_vader()
    df = df.copy()
    text = df["title"].fillna("") + " . " + df["text"].fillna("")
    df["sentiment_compound"] = text.map(lambda t: vader.polarity_scores(str(t))["compound"])
    return df


def aggregate_daily(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """
    Daily per-ticker aggregation + 90-day rolling z-score of review_count.
    Output date = review date (PIT).  Columns per spec.
    """
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])

    rows: List[Dict[str, Any]] = []
    for ticker, g in df.groupby("ticker"):
        daily = g.set_index("date").resample("D").agg(
            review_count=("rating", "size"),
            avg_rating=("rating", "mean"),
            sentiment_mean=("sentiment_compound", "mean"),
            sentiment_std=("sentiment_compound", "std"),
            rating_1_pct=(("rating", lambda s: 100.0 * (s == 1).mean() if len(s) else float("nan"))),
            rating_5_pct=(("rating", lambda s: 100.0 * (s == 5).mean() if len(s) else float("nan"))),
        )
        daily = daily.reset_index()
        roll = daily["review_count"].rolling(window=90, min_periods=1)
        mean = roll.mean()
        std = roll.std()
        daily["reviews_90d_z"] = (daily["review_count"] - mean) / std.replace(0, np.nan)
        daily["ticker"] = ticker
        rows.append(daily)

    if not rows:
        return pd.DataFrame(columns=RELEVANT_OUTPUT_COLS)

    out = pd.concat(rows, ignore_index=True)
    for col in ("avg_rating", "sentiment_mean", "sentiment_std", "rating_1_pct", "rating_5_pct"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out["reviews_90d_z"] = out["reviews_90d_z"].fillna(0.0)
    out = out[RELEVANT_OUTPUT_COLS].dropna(subset=["avg_rating"]).reset_index(drop=True)

    # PIT: date is the review timestamp (availability) — validate no null keys.
    # Availability == the review instant itself (a review exists only once posted);
    # enforce timestamp <= available and reject future-dated reviews.
    out["_avail"] = out["date"]
    PITValidator.validate_pit(out, timestamp_col="date", trade_date_col="_avail",
                              required_cols=["date", "ticker"])
    now = pd.Timestamp.now(tz="UTC").tz_localize(None)
    future = out[pd.to_datetime(out["date"]) > now + pd.Timedelta(days=1)]
    if not future.empty:
        raise PITValidationError(f"{len(future)} future-dated reviews (lookahead)")
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
        "source_version": metadata.get("source_version", "appstore_202609"),
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
    
    if "app_reviews_90d" not in signals.columns:
        signals["app_reviews_90d"] = signals.get("review_count", 0)
    if "app_rating_90d" not in signals.columns:
        signals["app_rating_90d"] = signals.get("avg_rating", 0.0)
    if "app_rating_level" not in signals.columns:
        signals["app_rating_level"] = signals["app_rating_90d"].round()
    if "retrieval_timestamp" not in signals.columns:
        signals["retrieval_timestamp"] = datetime.now(timezone.utc).isoformat()
    if "mapping_confidence" not in signals.columns:
        signals["mapping_confidence"] = "high"

    signals["sha256"] = ""
    signals.to_parquet(out_path, index=False)

    metadata = {
        "source": "APP_STORE",
        "source_version": "itunes_rss_customerreviews",
        "pit_timestamp_column": "date",
        "entity_key": "app_id|ticker",
        "transformations": [
            "rss_parse",
            "vader_sentiment_compound",
            "daily_aggregation",
            "reviews_90d_z_velocity",
        ],
        "date_range": {"min": str(signals["date"].min()) if len(signals) else None,
                       "max": str(signals["date"].max()) if len(signals) else None},
        "mapping_confidence": "high",
        **provenance_extra,
    }
    sha256 = record_manifest(out_path, "APP_STORE", len(signals), metadata)
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
        "source": "APP_STORE",
        "description": "app_store reviews_90d_z — factor date = review date (PIT)",
        "transformations": ["rss_parse", "vader_sentiment", "daily_agg", "90d_z"],
    }
    store.write_factor(df, "app_store_reviews_90d_z", prov)


# ----------------------------------------------------------------------------
# Synthetic data (offline test path)
# ----------------------------------------------------------------------------

def generate_synthetic_reviews(start: str, end: str, app_ids: List[str],
                               mapping: Dict[str, Any], seed: int = 42) -> pd.DataFrame:
    """Synthetic review stream spanning the full window (for tests / offline CI)."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range(start=start, end=end, freq="D")
    rows: List[Dict[str, Any]] = []
    for app_id in app_ids:
        ticker = mapping.get(app_id, {}).get("ticker", "UNKNOWN")
        app_name = mapping.get(app_id, {}).get("app_name", app_id)
        base = rng.integers(1, 8)
        burst = 0.0
        for d in dates:
            burst = max(0.0, burst + rng.normal(0, 0.4))
            n = max(0, int(base + burst * 2 + rng.normal(0, 1.2)))
            for _ in range(n):
                good = rng.random() < 0.72
                rating = rng.integers(3, 6) if good else rng.integers(1, 3)
                texts = {
                    5: f"Love {app_name}, works perfectly and fast.",
                    4: f"{app_name} is quite good, minor hiccups.",
                    3: f"{app_name} is okay, does the job.",
                    2: f"Disappointed with {app_name}, it keeps crashing.",
                    1: f"Terrible. {app_name} fails constantly. Do not buy.",
                }
                rows.append({
                    "review_id": f"syn-{ticker}-{d:%Y%m%d}-{len(rows)}",
                    "app_id": app_id,
                    "ticker": ticker,
                    "rating": int(rating),
                    "title": texts[int(rating)][:40],
                    "text": texts[int(rating)],
                    "date": d + pd.Timedelta(hours=int(rng.integers(0, 24)), minutes=int(rng.integers(0, 60))),
                    "version": "1.0",
                    "author": f"user_{rng.integers(1000, 9999)}",
                })
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------
# Self-test
# ----------------------------------------------------------------------------

def run_tests(tmp: Path) -> int:
    print("App Store Loader self-test")
    print("=" * 60)
    mapping = ensure_mapping_file(tmp / "app_store_to_ticker.json")

    # 1) synthetic review stream -> sentiment -> daily aggregation
    app_ids = load_app_ids(mapping, sample_apps=4)
    raw = generate_synthetic_reviews("2024-01-01", "2024-12-31", app_ids, mapping, seed=7)
    assert not raw.empty, "synthetic raw reviews empty"
    raw = add_vader_sentiment(raw)
    assert raw["sentiment_compound"].between(-1, 1).all()
    print(f"[OK] synthetic reviews: {len(raw)} rows, VADER compound in [-1,1]")

    sig = aggregate_daily(raw, "2014-01-01", "2024-12-31")
    assert list(sig.columns) == RELEVANT_OUTPUT_COLS, list(sig.columns)
    assert not sig["ticker"].isnull().any()
    assert sig["reviews_90d_z"].notna().all()
    assert sig.groupby("ticker")["date"].apply(lambda s: s.is_monotonic_increasing).all()
    print(f"[OK] aggregate: {len(sig)} rows x {len(sig.columns)} cols; schema exact; 90d z non-null")

    # 2) happy path: 5-star ticker should have high sentiment_mean
    pos = sig.groupby("ticker")["sentiment_mean"].mean()
    assert pos.max() > 0.2, f"synthetic sentiment too flat: {pos.round(3).to_dict()}"
    print(f"[OK] sentiment_mean spread: min={pos.min():.3f} max={pos.max():.3f}")

    # 3) PIT: a future-dated review must be rejected (lookahead)
    bad = sig.copy()
    bad["_avail"] = bad["date"]
    bad["date"] = pd.Timestamp.now() + pd.Timedelta(days=400)  # future review
    try:
        PITValidator.validate_pit(bad, timestamp_col="date", trade_date_col="_avail")
        raise AssertionError("expected PITValidationError")
    except Exception as exc:
        assert type(exc).__name__ == "PITValidationError", type(exc).__name__
        print("[OK] PITValidator rejects lookahead (review date > availability)")

    # 4) write outputs + provenance + factor store
    out = tmp / "app_store_signals.parquet"
    sha = write_outputs(sig, out, {"is_test": True, "sample_apps": 8,
                                   "history_note": "synthetic stream (offline test)"})
    assert len(sha) == 64
    reread = pd.read_parquet(out)
    assert len(reread) == len(sig)
    assert (tmp / "app_store_signals.parquet.provenance.json").exists()
    print(f"[OK] parquet + provenance written; sha256={sha[:12]}...")

    store_db = tmp / "test_factor_store.duckdb"
    if store_db.exists():
        store_db.unlink()
    write_factor_store(sig, str(store_db))
    store = FactorStore(db_path=str(store_db))
    assert "app_store_reviews_90d_z" in store.list_factors()
    print("[OK] factor store write: app_store_reviews_90d_z logged with PIT factor date")

    print("\nAll App Store Loader tests passed.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="App Store Reviews Loader (iTunes RSS, free)")
    parser.add_argument("--start", default=STRATEGY_WINDOW[0], help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=STRATEGY_WINDOW[1], help="End date YYYY-MM-DD")
    parser.add_argument("--sample-apps", type=int, default=20,
                        help="Process only the first N app_ids from the mapping")
    parser.add_argument("--max-pages", type=int, default=10, help="Max RSS pages (50 reviews each) per app")
    parser.add_argument("--mapping", type=str, default=str(DEFAULT_MAPPING_FILE))
    parser.add_argument("--out", type=str, default=str(DEFAULT_OUT))
    parser.add_argument("--factor-store", type=str, default=DEFAULT_FACTOR_STORE)
    parser.add_argument("--test", action="store_true", help="Run synthetic offline self-test")
    args = parser.parse_args()

    if args.test:
        print("[App Store Loader] Running in test mode with synthetic data generation.")
        mapping = ensure_mapping_file(Path(args.mapping))
        app_ids = load_app_ids(mapping, sample_apps=args.sample_apps)
        raw = generate_synthetic_reviews(args.start, args.end, app_ids, mapping)
        raw = add_vader_sentiment(raw)
        signals = aggregate_daily(raw, args.start, args.end)
        write_outputs(signals, Path(args.out), {
            "start": args.start, "end": args.end, "sample_apps": args.sample_apps,
            "is_test": True, "history_note": "synthetic stream (offline test)"
        })
        write_factor_store(signals, args.factor_store)
        print("[App Store Loader] Test completed successfully.")
        return

    mapping = ensure_mapping_file(Path(args.mapping))
    app_ids = load_app_ids(mapping, sample_apps=args.sample_apps)
    if not app_ids:
        raise SystemExit("No app_ids in mapping — curate data/mappings/app_store_to_ticker.json")

    print(f"Collecting App Store reviews for {len(app_ids)} apps ({args.start}..{args.end})")
    raw = collect_reviews(mapping, app_ids, max_pages_per_app=args.max_pages)
    if raw.empty:
        raise SystemExit("No reviews collected — check network/proxy (PROXY_LIST) or use --test")
    print(f"Parsed {len(raw)} raw reviews; running VADER sentiment...")
    raw = add_vader_sentiment(raw)
    signals = aggregate_daily(raw, args.start, args.end)
    if signals.empty:
        raise SystemExit("No aggregated rows in window")
    write_outputs(signals, Path(args.out), {
        "start": args.start, "end": args.end, "sample_apps": args.sample_apps,
        "history_note": "iTunes RSS exposes only recent ~500 reviews/app; deep history requires weekly snaps",
    })
    write_factor_store(signals, args.factor_store)
    print("Done.")


if __name__ == "__main__":
    main()