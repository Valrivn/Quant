import sqlite3
from pathlib import Path

c = sqlite3.connect("data/pit_sandbox.db")
n, lo, hi = c.execute("SELECT COUNT(*), MIN(event_ts), MAX(event_ts) FROM pit_transcripts").fetchone()
sz = c.execute("SELECT SUM(LENGTH(content)) FROM pit_transcripts").fetchone()[0]
print(f"PIT SANDBOX : {n} transcripts | {sz/1e9:.2f} GB raw text | {lo[:10]} .. {hi[:10]}")
for yr, cnt in c.execute(
    "SELECT SUBSTR(event_ts,1,4) y, COUNT(*) FROM pit_transcripts GROUP BY 1 ORDER BY 1"
):
    print(f"   {yr}: {cnt}")

print("\nREDDIT QUANT DB (house social corpus):")
c2 = sqlite3.connect("reddit_quant.db")
tabs = [r[0] for r in c2.execute("SELECT name FROM sqlite_master WHERE type='table'")]
total_txt = 0
for t in tabs:
    try:
        n2 = c2.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        if not n2:
            continue
        txt_cols = [
            d[1] for d in c2.execute(f'PRAGMA table_info("{t}")')
            if d[2].upper().startswith("TEXT")
        ]
        sz2 = 0
        for col in txt_cols[:3]:
            try:
                sz2 += c2.execute(f'SELECT SUM(LENGTH([{col}])) FROM "{t}"').fetchone()[0] or 0
            except Exception:
                pass
        total_txt += sz2
        print(f"   {t}: {n2} rows | ~{sz2/1e6:.0f} MB text")
    except Exception:
        pass

print("\nCSV/OTHER DATA DIR:")
for p in sorted(Path("data").glob("*")):
    if p.suffix.lower() in (".csv", ".json", ".db", ".yaml", ".jsonl"):
        mb = p.stat().st_size / 1e6
        rows = ""
        if p.suffix == ".csv" and mb < 200:
            try:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    rows = f" | ~{sum(1 for _ in fh):,} lines"
            except Exception:
                pass
        print(f"   {p.name}: {mb:.1f} MB{rows}")
