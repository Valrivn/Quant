"""Harvest the full ApeWisdom historical leaderboard from the Wayback Machine.

Ruling D-20260827-004 (MODIFY hybrid): use real archived ApeWisdom output as
Track-A history so no fabricated proxy is needed and the vendor's own weighting
is preserved exactly as published.

Steps:
1. CDX inventory of capured root leaderboard pages (status 200).
2. Fetch each archived snapshot, parse the server-rendered ranking table
   (# Name Symbol Mentions [24h] Upvotes) with BeautifulSoup.
3. Persist to reddit_quant.db.apewisdom_history (PIT by capture date).

Per-era schema is stable: rows are <tr> with
  td[data-sort] + td[name-td] + span.badge-company{<TICKER>} +
  td.td-center.rh-sm[data-sort=mentions] + optional % cell + td.td-right[data-sort=upvotes].
2021-era rows lack the 24h % cell and the leading fav-star cell.

CLI: python scripts/harvest_apewisdom_history.py [--limit N] [--from YYYYMMDD] [--only-timestamps ts1,ts2]
"""

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html import unescape

from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "reddit_quant.db")
CDX_URL = ("https://web.archive.org/cdx/search/cdx?url=apewisdom.io/&output=json"
           "&filter=statuscode:200&fl=timestamp,original,length")
SNAP_URL = "https://web.archive.org/web/{ts}/https://apewisdom.io/"
CDX_CACHE = os.path.join(ROOT, "data", "apewisdom_wayback_cdx.json")
UA = {"User-Agent": "Mozilla/5.0 (house-of-quant harvest; research)"}

DD = re.compile(r"\d{14}")


def fetch(url, retries=2, timeout=45):
    last = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5)
    raise RuntimeError(f"fetch failed: {url} :: {last}")


def cdx_inventory():
    if os.path.exists(CDX_CACHE):
        with open(CDX_CACHE, encoding="utf-8") as f:
            rows = json.load(f)
    else:
        raw = fetch(CDX_URL)
        parsed = json.loads(raw)
        rows = [{"timestamp": r[0], "original": r[1], "length": int(r[2] if r[2] else 0)}
                for r in parsed[1:] if DD.fullmatch(r[0])]
        with open(CDX_CACHE, "w", encoding="utf-8") as f:
            json.dump(rows, f)
    return rows


def _num(txt):
    if txt is None:
        return None
    s = re.sub(r"[^\d.:+-]", "", unescape(txt)).replace(",", "")
    try:
        return int(s) if "." not in s else float(s)
    except ValueError:
        return None


def parse_snapshot(html, ts):
    """Return list of row dicts from one archived leaderboard page."""
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for tr in soup.find_all("tr"):
        badge = tr.find("span", class_="badge-company")
        if badge is None:
            continue
        ticker = badge.get_text(strip=True).upper()
        if not re.fullmatch(r"[A-Z.$^]{1,8}", ticker):
            continue
        tds = [td for td in tr.find_all("td")]
        rank = moves = mentions = upvotes = None
        pct24h = None
        rcount = 0
        for td in tds:
            cls = td.get("class") or []
            ds = td.get("data-sort")
            if ds is not None and "td-center" in cls:
                if rcount == 0:
                    mentions = _num(ds)
                    rcount += 1
                else:
                    v = _num(ds)
                    if v is not None and v != mentions:
                        pct24h = v / 100.0 if abs(v) > 1 else v
                    break
        for td in tds:
            cls = td.get("class") or []
            txt = td.get_text(strip=True)
            mv = td.get("moves")
            if mv is not None and txt and txt.replace(",", "").isdigit():
                rank = _num(txt)
                moves = _num(mv)
                break
        vals = [td for td in tds if td.get("data-sort") is not None and "td-right" in (td.get("class") or [])]
        if vals:
            upvotes = _num(vals[-1].get("data-sort"))
        if rank is None or mentions is None or upvotes is None:
            continue
        rows.append({
            "snapshot_ts": ts,
            "capture_date": f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}",
            "rank": rank, "moves": moves, "ticker": ticker,
            "mentions": mentions, "upvotes": upvotes, "pct24h": pct24h,
        })
    return rows


def work(ts):
    time.sleep(0.35)
    try:
        html = fetch(SNAP_URL.format(ts=ts))
        rows = parse_snapshot(html, ts)
        return ts, rows
    except Exception as e:  # noqa: BLE001
        return ts, None, e


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--from", dest="frm", default="20000101")
    ap.add_argument("--to", dest="to", default="29991231")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--force-cdx", action="store_true")
    args = ap.parse_args()

    if args.force_cdx and os.path.exists(CDX_CACHE):
        os.remove(CDX_CACHE)
    inv = cdx_inventory()
    inv = [r for r in inv if args.frm <= r["timestamp"][:8] <= args.to]
    print(f"CDX inventory: {len(inv)} snapshots", flush=True)
    if args.limit:
        inv = inv[: args.limit]

    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS apewisdom_history (
        snapshot_ts TEXT, capture_date TEXT, rank INTEGER, moves INTEGER,
        ticker TEXT, mentions INTEGER, upvotes INTEGER, pct24h REAL,
        era TEXT, provenance TEXT, fetched_at TEXT,
        PRIMARY KEY (snapshot_ts, ticker))""")
    existing = {r[0] for r in conn.execute("SELECT DISTINCT snapshot_ts FROM apewisdom_history")}
    todo = [r["timestamp"] for r in inv if r["timestamp"] not in existing]
    print(f"To harvest: {len(todo)} snapshots (skip {len(inv) - len(todo)} existing)", flush=True)

    ok = bad = 0
    rows_total = 0
    fails = []
    fetched_at = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    from concurrent.futures import as_completed
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = {ex.submit(work, ts): ts for ts in todo}
        for f in as_completed(futs):
            ts = futs[f]
            try:
                res = f.result()
            except Exception as e:  # noqa: BLE001
                bad += 1
                fails.append((ts, str(e)))
                print(f"  {ts} -> WORKER ERROR (bad={bad})", flush=True)
                continue
            if res is None:
                bad += 1
                fails.append((ts, "empty"))
                print(f"  {ts} -> NO ROWS (bad={bad})", flush=True)
                continue
            rows = res[1]
            if not rows:
                bad += 1
                fails.append((ts, "no-rows"))
                print(f"  {ts} -> NO ROWS (bad={bad})", flush=True)
                continue
            cur = conn.cursor()
            cur.executemany(
                "INSERT OR REPLACE INTO apewisdom_history "
                "(snapshot_ts,capture_date,rank,moves,ticker,mentions,upvotes,pct24h,era,provenance,fetched_at) "
                "VALUES (:snapshot_ts,:capture_date,:rank,:moves,:ticker,:mentions,:upvotes,:pct24h,:era,:provenance,:fetched_at)",
                [{**r, "era": r["snapshot_ts"][:4], "provenance": SNAP_URL.format(ts=ts),
                  "fetched_at": fetched_at} for r in rows],
            )
            conn.commit()
            ok += 1
            rows_total += len(rows)
            if ok % 50 == 0:
                print(f"  ...progress ok={ok} bad={bad} rows={rows_total} (latest {ts})", flush=True)
    print(f"DONE ok={ok} bad={bad} rows={rows_total}")
    if fails:
        with open(os.path.join(ROOT, "data", "apewisdom_harvest_fails.txt"), "a", encoding="utf-8") as f:
            for ts, msg in fails:
                f.write(f"{ts}\t{msg}\n")
        print(f"fail log >> data/apewisdom_harvest_fails.txt ({len(fails)} entries)")
    conn.close()


if __name__ == "__main__":
    main()