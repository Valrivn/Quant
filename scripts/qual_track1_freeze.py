"""QUAL-T1 freeze stamp for D-20260820-001 Track 1 extension.

Snapshots the CURRENT state of every qualitative data lane in the house into
one immutable manifest (JSON + SHA-256 of both databases). Mirrors
WIKI-T1-20260820: freeze now, score forward at the shared checkpoints.
No data is modified — read-only over reddit_quant.db and data/sentinel.db.
"""

import hashlib
import json
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "data" / "qual_track1_freeze_20260820.json"
DBS = {
    "reddit_quant.db": ROOT / "reddit_quant.db",
    "data/sentinel.db": ROOT / "data" / "sentinel.db",
}

TS_RE = re.compile(r"(utc|date|time|_at$|fetched|snapshot)", re.I)
TICKER_RE = re.compile(r"^(ticker|entity|symbol)$", re.I)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _table_profile(conn: sqlite3.Connection, table: str) -> dict:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info([{table}])").fetchall()]
    count = conn.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()[0]
    profile = {"rows": count}
    ts_col = next((c for c in cols if TS_RE.search(c)), None)
    if ts_col and count:
        try:
            lo, hi = conn.execute(
                f"SELECT MIN([{ts_col}]), MAX([{ts_col}]) FROM [{table}]"
            ).fetchone()
            profile["ts_column"] = ts_col
            profile["range"] = [str(lo), str(hi)]
        except Exception:
            pass
    tk_col = next((c for c in cols if TICKER_RE.match(c)), None)
    if tk_col and count:
        try:
            profile["distinct_tickers"] = conn.execute(
                f"SELECT COUNT(DISTINCT [{tk_col}]) FROM [{table}]"
            ).fetchone()[0]
        except Exception:
            pass
    return profile


def main() -> None:
    manifest = {
        "freeze_id": "QUAL-T1-20260820",
        "rule": "D-20260820-001 Track 1 extension — all qualitative lanes",
        "checkpoints": ["2026-11-20", "2027-02-20", "2027-05-20", "2027-08-20"],
        "sources": {},
    }
    for label, path in DBS.items():
        conn = sqlite3.connect(path)
        tabs = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        manifest["sources"][label] = {
            "sha256": _sha256(path),
            "size_bytes": path.stat().st_size,
            "tables": {t: _table_profile(conn, t) for t in sorted(tabs)},
        }
        conn.close()

    payload = json.dumps(manifest, indent=2, sort_keys=True)
    OUT_PATH.write_text(payload, encoding="utf-8")
    print(f"frozen: {OUT_PATH}")
    print(f"manifest_sha256: {hashlib.sha256(payload.encode('utf-8')).hexdigest()}")
    for label, src in manifest["sources"].items():
        print(f"{label}: db_sha256={src['sha256'][:16]}… tables={len(src['tables'])}")


if __name__ == "__main__":
    main()
