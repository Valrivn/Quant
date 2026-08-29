import sqlite3, os

for db_path in ['reddit_quant.db', 'data/reddit_quant.db']:
    if not os.path.exists(db_path):
        print(f'{db_path}: NOT FOUND')
        continue
    conn = sqlite3.connect(db_path)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    print(f'\n=== {db_path} ===')
    for t in tables:
        count = conn.execute(f'SELECT COUNT(*) FROM [{t}]').fetchone()[0]
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info([{t}])').fetchall()]
        print(f'  {t}: {count} rows')
        print(f'    cols: {cols}')
        if count > 0:
            sample = conn.execute(f'SELECT * FROM [{t}] LIMIT 1').fetchone()
            print(f'    sample: {sample[:5]}...' if sample else '    empty')
    conn.close()
