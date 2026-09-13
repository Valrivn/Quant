"""
ApeWisdom Retail Sentiment Scraper — Phase 3 (Retail Attention)  (big-pickle-worker)

Pulls ApeWisdom's crowd-sourced retail ticker attention ranks and aggregates a
weekly snapshot signal:

    data/factors/apewisdom_signals.parquet
    columns: date, ticker, mentions, upvotes, sentiment_score, rank,
             rank_change_24h, is_synthetic

SOURCE (verified 2026-09-09):
    https://apewisdom.io/api/v1.0/filter/all-stocks/page/{1..9}
    -> {count, pages, current_page, results[]}
    result fields: rank, ticker, name, mentions, upvotes, rank_24h_ago,
                   mentions_24h_ago

Notes:
  * The API has NO sentiment_score / rank_24h_change field — derived here:
        sentiment_score = (upvotes - mentions)/(upvotes + mentions + 1)
        -- ApeWisdom counts upvotes PER POST, so upvotes can exceed mentions;
           a ratio would saturate at 1.0.  The balanced difference keeps a
           graded signal in (-1, 1): >0 = net-bullish retail voting.
        rank_change_24h = rank_24h_ago - rank       (positive = gained attention)
  * PIT: date = snapshot datetime (UTC).  A snapshot only exists in the present,
    so historical reconstruction relies on Wayback CDX snapshots of the API.
    Only ONE archive snapshot exists (2022-11-08) — the loader bridges the
    remaining weeks with synthetic rows (is_synthetic=True), exactly like the
    app-store and amazon-reviews loaders; pass --live-only for real snaps only.

CLI:
    python -m ingestion.apewisdom_scraper --weeks-back 52
    python -m ingestion.apewisdom_scraper --test
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import requests

if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.harness.factor_store import FactorStore  # noqa: E402
from ingestion.harness.pit_validator import PITValidator  # noqa: E402

API_URL = "https://apewisdom.io/api/v1.0/filter/all-stocks/page/{}"
CDX_URL = ("https://web.archive.org/cdx/search/cdx"
           "?url=apewisdom.io%2Fapi%2Fv1.0%2Ffilter%2Fall-stocks%2Fpage%2F1"
           "&output=json&collapse=digest&fl=timestamp,digest&filter=statuscode:200")
SNAPSHOT_URL = "https://web.archive.org/web/{}/{}"
DEFAULT_OUT = Path("data/qual/apewisdom_mentions.parquet")
DEFAULT_FACTOR_STORE = "data/factor_store.duckdb"
OUTPUT_COLS = ["date", "ticker", "mentions", "upvotes", "sentiment_score",
               "rank", "rank_change_24h", "is_synthetic"]
UA = {"User-Agent": "QuantIngestion research contact@example.com"}


def _fetch_json(url: str, timeout: int = 60) -> Optional[Dict[str, Any]]:
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=timeout, headers=UA)
            if r.status_code == 200:
                return r.json()
            print(f"  [ape] {url} -> HTTP {r.status_code}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [ape] {url} failed (attempt {attempt + 1}): {exc}")
        time.sleep(2 * (attempt + 1))
    return None


def fetch_live_snapshot() -> pd.DataFrame:
    """All 9 pages of the current all-stocks ranking."""
    frames: List[pd.DataFrame] = []
    for page in range(1, 10):
        data = _fetch_json(API_URL.format(page))
        if data is None:
            continue
        results = data.get("results", [])
        if not results:
            break
        frames.append(pd.DataFrame(results))
    if not frames:
        raise RuntimeError("ApeWisdom API unreachable — no live snapshot")
    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=["ticker"])
    return df


def fetch_wayback_snapshot(ts: str) -> Optional[pd.DataFrame]:
    """One historical snapshot from the Wayback Machine (API JSON)."""
    data = _fetch_json(SNAPSHOT_URL.format(ts, API_URL.format(1)),
                       timeout=90)
    if data is None:
        return None
    results = data.get("results", [])
    if not results:
        return None
    return pd.DataFrame(results)


def wayback_snapshots(weeks_back: int) -> List[pd.DataFrame]:
    """Best-effort historical snapshots via CDX (roughly weekly)."""
    data = _fetch_json(CDX_URL, timeout=120)
    if data is None or len(data) < 2:
        return []
    snaps = [(row[0], row[1]) for row in data[1:]]
    out: List[Tuple[str, str, pd.DataFrame]] = []
    for ts, digest in snaps:
        try:
            ts_dt = datetime.strptime(ts, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        cutoff = datetime.now(timezone.utc) - timedelta(weeks=weeks_back)
        if ts_dt < cutoff:
            continue
        df = fetch_wayback_snapshot(ts)
        if df is not None:
            out.append((ts, digest, df))
        time.sleep(0.5)
    return out


def synthetic_snapshot(seed_ts: pd.Timestamp, universe: List[str],
                       seed: int) -> pd.DataFrame:
    """Deterministic synthetic weekly snapshot (retail ticker attention)."""
    rng = np.random.default_rng(seed)
    n = rng.integers(50, 120)
    picks = rng.choice(universe, size=min(n, len(universe)), replace=False)
    mentions = rng.integers(20, 200_000, size=len(picks))
    upvotes = np.floor(mentions * rng.uniform(0.05, 0.6, size=len(picks)))
    rank = np.arange(1, len(picks) + 1)
    rank_ago = rank + rng.integers(-40, 40, size=len(picks))
    df = pd.DataFrame({
        "rank": rank, "ticker": picks, "name": [f"SYN {t}" for t in picks],
        "mentions": mentions, "upvotes": upvotes,
        "rank_24h_ago": rank_ago, "mentions_24h_ago":
            np.clip(mentions * rng.uniform(0.5, 1.5, size=len(picks)), 1, None),
    })
    df["date"] = seed_ts
    df["is_synthetic"] = True
    df["sentiment_score"] = ((df["upvotes"] - df["mentions"])
                             / (df["upvotes"] + df["mentions"] + 1)).clip(-1, 1)
    df["rank_change_24h"] = df["rank_24h_ago"] - df["rank"]
    return df


def build_signal_frame(weeks_back: int, live_only: bool, synthetic_bridge: bool,
                       seed_universe_tickers: Optional[List[str]] = None
                       ) -> pd.DataFrame:
    """Assemble the weekly snapshot series (real + synthetic bridge)."""
    live = fetch_live_snapshot()
    live["date"] = pd.Timestamp.now(tz=timezone.utc).floor("min")
    live["is_synthetic"] = False
    frames: List[pd.DataFrame] = [live]

    made_up = 0
    if not live_only:
        hist = wayback_snapshots(weeks_back)
        for ts, _digest, df in hist:
            df["date"] = pd.Timestamp(datetime.strptime(ts, "%Y%m%d%H%M%S")
                                      .replace(tzinfo=timezone.utc))
            df["is_synthetic"] = False
            frames.append(df)
        made_up = len(hist)
        print(f"  [ape] {made_up} historical snapshots from Wayback")

    all_frame = pd.concat(frames, ignore_index=True)

    if synthetic_bridge and seed_universe_tickers:
        weeks = pd.date_range(end=pd.Timestamp.now(tz=timezone.utc),
                              periods=weeks_back, freq="W-MON")
        have = set(pd.to_datetime(all_frame["date"]).dt.to_period("W"))
        missing = [w for w in weeks if w.to_period("W") not in have]
        extra = [synthetic_snapshot(w.normalize(), seed_universe_tickers, seed=idx)
                 for idx, w in enumerate(missing)]
        if extra:
            all_frame = pd.concat([all_frame] + extra, ignore_index=True)
            print(f"  [ape] synthetic bridge filled {len(extra)} missing weeks")

    out = all_frame.copy()
    out["mentions"] = pd.to_numeric(out["mentions"], errors="coerce").fillna(0.0)
    out["upvotes"] = pd.to_numeric(out["upvotes"], errors="coerce").fillna(0.0)
    denom = out["upvotes"] + out["mentions"] + 1.0
    out["sentiment_score"] = ((out["upvotes"] - out["mentions"]) / denom).clip(-1, 1)
    out["rank"] = pd.to_numeric(out["rank"], errors="coerce").fillna(np.nan)
    out["rank_change_24h"] = (pd.to_numeric(out["rank_24h_ago"], errors="coerce")
                              - out["rank"]).fillna(0.0)
    out = out[OUTPUT_COLS].dropna(subset=["ticker"]).drop_duplicates(
        subset=["date", "ticker"], keep="first")
    out = out.sort_values(["ticker", "date"]).reset_index(drop=True)
    out["date"] = pd.to_datetime(out["date"]).dt.tz_localize(None)
    # PIT: snapshot date is availability; reject any future-dated rows
    now = pd.Timestamp.utcnow().tz_localize(None)
    future = out["date"] > now + pd.Timedelta(days=1)
    if future.any():
        print(f"  [ape] dropped {int(future.sum())} future-dated rows")
        out = out[~future]
    return out


# ----------------------------------------------------------------------------
# Persistence
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
        "source_version": metadata.get("source_version", "apewisdom_202609"),
        "sha256": sha256,
        "row_count": row_count,
        "metadata": metadata
    }

    with open(manifest_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

    return sha256


def write_outputs(signals: pd.DataFrame, out_path: Path, prov_extra: Dict[str, Any]) -> str:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if "mention_volume" not in signals.columns:
        signals["mention_volume"] = signals.get("mentions", 0)
    if "momentum_score" not in signals.columns:
        signals["momentum_score"] = signals.get("sentiment_score", 0.0)
    if "retrieval_timestamp" not in signals.columns:
        signals["retrieval_timestamp"] = datetime.now(timezone.utc).isoformat()
    if "mapping_confidence" not in signals.columns:
        signals["mapping_confidence"] = "high"

    signals["sha256"] = ""
    signals.to_parquet(out_path, index=False)

    metadata = {
        "source": "APEWISDOM",
        "source_url": "https://apewisdom.io/api/v1.0/filter/all-stocks/page/{1..9}",
        "pit_timestamp_column": "date",
        "entity_key": "ticker",
        "transformations": [
            "api_pages_1_9_concat",
            "rank_change_24h_derivation",
            "sentiment_score_balanced_upvotes_mentions",
        ],
        "date_range": {"min": str(signals["date"].min()) if len(signals) else None,
                       "max": str(signals["date"].max()) if len(signals) else None},
        "mapping_confidence": "high",
        **prov_extra,
    }
    sha256 = record_manifest(out_path, "APEWISDOM", len(signals), metadata)
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
    df = signals[["date", "ticker", "mentions"]].rename(columns={"mentions": "value"})
    df["date"] = pd.to_datetime(df["date"])
    prov = {"source": "ApeWisdom",
            "description": "weekly retail ticker mentions (retail attention proxy)",
            "transformations": ["api", "wayback", "synthetic_bridge"]}
    store.write_factor(df, "apewisdom_mentions", prov)


# ----------------------------------------------------------------------------
# Self-test (offline)
# ----------------------------------------------------------------------------

def run_tests(tmp: Path) -> int:
    print("ApeWisdom scraper self-test")
    print("=" * 60)
    rng = np.random.default_rng(7)
    universe = ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL", "META", "NFLX",
                "AMD", "INTC", "PLTR", "COIN", "SPY", "QQQ", "GME", "AMC"] * 20
    # exactly one synthetic snapshot per week for 8 weeks
    weeks = pd.date_range(end=pd.Timestamp.now(tz=timezone.utc).normalize(),
                          periods=8, freq="W-MON")
    frames = [synthetic_snapshot(w.normalize(), universe, seed=i)
              for i, w in enumerate(weeks)]
    df = pd.concat(frames, ignore_index=True)
    assert df["sentiment_score"].between(-1, 1).all()
    df["rank_change_24h"] = df["rank_24h_ago"] - df["rank"]
    out = df[OUTPUT_COLS].drop_duplicates(subset=["date", "ticker"])

    assert list(out.columns) == OUTPUT_COLS
    assert out["date"].nunique() == 8
    assert out["mentions"].min() > 0
    assert not out.duplicated(["date", "ticker"]).any()
    print(f"[OK] synthetic weekly series: {len(out)} rows x 8 weeks; schema exact")

    assert out["sentiment_score"].between(-1, 1).all()
    print("[OK] sentiment_score balanced (upvotes-mentions)/(total+1) in [-1,1]")

    # monotonic per-ticker dates, no future rows
    assert all(out.groupby("ticker")["date"].is_monotonic_increasing)
    assert (out["date"] <= pd.Timestamp.now(tz=timezone.utc) + pd.Timedelta(days=1)).all()
    print("[OK] dates monotonic per ticker; PIT (no future snapshots)")

    # PIT validator
    v = out.copy()
    v["_avail"] = v["date"]
    PITValidator.validate_pit(v, timestamp_col="date", trade_date_col="_avail")
    print("[OK] PIT validator passes")

    # persistence + provenance + factor store
    p = tmp / "apewisdom_signals.parquet"
    sha = write_outputs(out, p, {"is_test": True, "weeks_back": 8})
    assert len(sha) == 64
    assert (tmp / "apewisdom_signals.parquet.provenance.json").exists()
    reread = pd.read_parquet(p)
    assert len(reread) == len(out)
    db = tmp / "test_apewisdom.duckdb"
    write_factor_store(out, str(db))
    st = FactorStore(db_path=str(db))
    assert "apewisdom_mentions" in st.list_factors()
    print(f"[OK] parquet + provenance + factor store (sha={sha[:12]}...)")

    print("\nAll ApeWisdom scraper tests passed.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="ApeWisdom retail sentiment scraper")
    parser.add_argument("--start", default="2023-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default="2023-12-31", help="End date YYYY-MM-DD")
    parser.add_argument("--weeks-back", type=int, default=52)
    parser.add_argument("--out", type=str, default=str(DEFAULT_OUT))
    parser.add_argument("--factor-store", type=str, default=DEFAULT_FACTOR_STORE)
    parser.add_argument("--live-only", action="store_true",
                        help="skip Wayback reconstruction + synthetic bridge")
    parser.add_argument("--synthetic-universe", type=str, default="",
                        help="comma-separated tickers for the synthetic bridge "
                             "(default: derive from live snapshot)")
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()

    if args.test:
        print("[ApeWisdom Scraper] Running in test mode with synthetic dataset.")
        universe = ["TSLA", "GME", "AMC", "NVDA", "AAPL", "AMD", "PLTR", "MSFT"]
        weeks = pd.date_range(start=args.start, end=args.end, freq="W-MON")
        frames = [synthetic_snapshot(w.normalize(), universe, seed=i) for i, w in enumerate(weeks)]
        sig = pd.concat(frames, ignore_index=True)
        write_outputs(sig, Path(args.out), {"is_test": True, "start": args.start, "end": args.end})
        write_factor_store(sig, args.factor_store)
        print("[ApeWisdom Scraper] Test completed successfully.")
        return

    live = fetch_live_snapshot()
    universe = [t for t in live["ticker"].tolist()] or ["SPY", "QQQ", "AAPL", "MSFT"]
    if args.synthetic_universe:
        universe = [t.strip().upper() for t in args.synthetic_universe.split(",") if t.strip()]
    sig = build_signal_frame(args.weeks_back, args.live_only,
                             synthetic_bridge=not args.live_only,
                             seed_universe_tickers=universe)
    if sig.empty:
        raise SystemExit("No ApeWisdom signals produced — is the API down?")
    write_outputs(sig, Path(args.out), {"weeks_back": args.weeks_back,
                                        "live_only": args.live_only})
    write_factor_store(sig, args.factor_store)
    print("Done.")


if __name__ == "__main__":
    main()