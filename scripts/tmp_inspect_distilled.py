import sqlite3, os

db = "data/reddit_distilled.db"
print("exists:", os.path.exists(db), "size:", os.path.getsize(db) if os.path.exists(db) else 0)
conn = sqlite3.connect(db)
tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print("tables:", tables)
for t in tables:
    n = conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
    print(f"  {t}: {n} rows")
print("\nlabeled_by breakdown:")
for r in conn.execute("SELECT COALESCE(labeled_by,'UNLABELED'), COUNT(*) FROM reddit_labels GROUP BY labeled_by").fetchall():
    print(" ", r)
print("\nconfidence breakdown:")
for r in conn.execute("SELECT COALESCE(confidence,'NULL'), COUNT(*) FROM reddit_labels GROUP BY confidence").fetchall():
    print(" ", r)
print("\nlabel breakdown (where labeled):")
for r in conn.execute("SELECT label, COUNT(*) FROM reddit_labels WHERE labeled_by IS NOT NULL AND label IS NOT NULL GROUP BY label").fetchall():
    print(" ", r)
print("\nsample labeled row:")
for r in conn.execute("SELECT id, label, score_continuous, confidence, labeled_by, batch_id FROM reddit_labels WHERE label IS NOT NULL LIMIT 3").fetchall():
    print(" ", r)
conn.close()