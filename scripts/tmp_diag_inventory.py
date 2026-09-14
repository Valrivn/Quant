"""Diagnosis 1: clean-data inventory across the qualitative DBs.

Classifies each qualitative/reddit source as REAL vs SYNTHETIC using
content markers, and counts distinct/useful raw text.
"""

import sqlite3
import os

DBS = {
    "reddit_quant.db": os.path.join(os.getcwd(), "reddit_quant.db"),
}

def q(conn, sql, *args):
    return conn.execute(sql, args).fetchall()

def main():
    conn = sqlite3.connect(DBS["reddit_quant.db"])
    print("=" * 70)
    print(" CLEAN-DATA INVENTORY  (reddit_quant.db)")
    print("=" * 70)

    n = q(conn, "SELECT COUNT(*) FROM submissions")[0][0]
    d_title = q(conn, "SELECT COUNT(DISTINCT title) FROM submissions")[0][0]
    d_sf = q(conn, "SELECT COUNT(DISTINCT selftext) FROM submissions")[0][0]
    templ = q(conn, "SELECT COUNT(*) FROM submissions WHERE title LIKE '%What are your thoughts on%'")[0][0]
    print(f"\n[submissions] {n:,} rows | distinct titles={d_title} | distinct selftext={d_sf} | templated={templ:,}")
    print("  -> VERDICT: SYNTHETIC (seeded by populate_qualitative_loop.py)")

    n = q(conn, "SELECT COUNT(*) FROM instagram_raw_mentions")[0][0]
    dcap = q(conn, "SELECT COUNT(DISTINCT caption) FROM instagram_raw_mentions")[0][0]
    templ_ig = q(conn, "SELECT COUNT(*) FROM instagram_raw_mentions WHERE caption LIKE 'Check out this amazing%'")[0][0]
    print(f"\n[instagram_raw_mentions] {n:,} rows | distinct captions={dcap} | templated={templ_ig:,}")
    print("  -> VERDICT: SYNTHETIC (single template)")

    n = q(conn, "SELECT COUNT(*) FROM psychological_vectors")[0][0]
    sample_txt = q(conn, "SELECT COUNT(*) FROM psychological_vectors WHERE raw_text LIKE 'Sample text%'")[0][0]
    d_raw = q(conn, "SELECT COUNT(DISTINCT raw_text) FROM psychological_vectors")[0][0]
    print(f"\n[psychological_vectors] {n:,} rows | 'Sample text'={sample_txt:,} | distinct raw_text={d_raw:,}")
    print("  -> VERDICT: MIXED but raw_text largely placeholder")

    n = q(conn, "SELECT COUNT(*) FROM fintech_messages")[0][0]
    d_txt = q(conn, "SELECT COUNT(DISTINCT text) FROM fintech_messages")[0][0]
    srcs = q(conn, "SELECT DISTINCT source FROM fintech_messages")
    sc_min, sc_max = q(conn, "SELECT MIN(sentiment_score), MAX(sentiment_score) FROM fintech_messages")[0]
    print(f"\n[fintech_messages] {n:,} rows | distinct text={d_txt:,} | sources={[s[0] for s in srcs]} | sentiment range={sc_min},{sc_max}")
    print("  -> VERDICT: REAL (apewisdom WSB trending summaries) — objective aggregates, no free-form opinion")

    n = q(conn, "SELECT COUNT(*) FROM daily_aggregations")[0][0]
    d_tk = q(conn, "SELECT COUNT(DISTINCT ticker) FROM daily_aggregations")[0][0]
    d_src = q(conn, "SELECT DISTINCT source FROM daily_aggregations")
    print(f"\n[daily_aggregations] {n:,} rows | tickers={d_tk} | sources={[s[0] for s in d_src]}")
    print("  -> Aggregates only (mention counts/avg_sentiment) — no raw text; provenance uncertain")

    # Real gold corpora (the only clean labeled text)
    sdb = sqlite3.connect("data/sentiment_training.db")
    tot = q(sdb, "SELECT COUNT(*) FROM sentiment_training")[0][0]
    print(f"\n[sentiment_training.db] {tot:,} rows of REAL gold-labeled text (fiqa/semeval/stocktwits/phrasebank)")
    clean_eval = q(sdb, "SELECT COUNT(*) FROM sentiment_training WHERE split IN ('val','locked_test')")[0][0]
    print(f"  of which val+locked_test (unseen) = {clean_eval:,}")

    # Hand-graded
    import csv
    hg = [r for r in csv.DictReader(open("data/handgrade_samples_v1.csv", encoding="utf-8-sig"))]
    filled = [r for r in hg if r["ceo_label"].strip()]
    print(f"\n[handgrade_samples_v1.csv] {len(hg)} rows | CEO-graded: {len(filled)}")
    conn.close()
    sdb.close()

    print("=" * 70)


if __name__ == "__main__":
    main()