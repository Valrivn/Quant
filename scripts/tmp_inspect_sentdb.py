import sqlite3, os
conn = sqlite3.connect("data/sentiment_training.db")
tabs = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print("tables:", tabs)
for t in tabs:
    n = conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
    print(f"  {t}: {n} rows")
print("\nsentiment_training by source/split/label:")
for r in conn.execute(
    "SELECT source, split, label, COUNT(*) FROM sentiment_training GROUP BY source, split, label ORDER BY source, split, label"
).fetchall():
    print("  ", r)
conn.close()