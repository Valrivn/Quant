import sqlite3
import os

db_path = 'reddit_quant.db'
backup_db = 'data/archive/quarantine_synthetic/reddit_fake_submissions_backup.db'
os.makedirs('data/archive/quarantine_synthetic', exist_ok=True)

con = sqlite3.connect(db_path)
total_before = con.execute('SELECT count(1) FROM submissions').fetchone()[0]
print(f'Total submissions before: {total_before}')

# Query fake templated count
fake_cnt = con.execute("SELECT count(1) FROM submissions WHERE title LIKE 'What are your thoughts on %'").fetchone()[0]
print(f'Detected templated fake submissions: {fake_cnt}')

if fake_cnt > 0:
    if os.path.exists(backup_db):
        os.remove(backup_db)
    con_bk = sqlite3.connect(backup_db)
    schema = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='submissions'").fetchone()[0]
    con_bk.execute(schema)
    
    # Copy fake submissions
    con.execute(f"ATTACH DATABASE '{backup_db}' AS bk")
    con.execute("INSERT INTO bk.submissions SELECT * FROM submissions WHERE title LIKE 'What are your thoughts on %'")
    con.commit()
    con.execute("DETACH DATABASE bk")
    con_bk.commit()
    con_bk.close()
    print(f'Safely backed up {fake_cnt} fake submissions to {backup_db}.')

    # Delete fake submissions from main db
    con.execute("DELETE FROM submissions WHERE title LIKE 'What are your thoughts on %'")
    con.commit()
    print('Vacuuming primary database...')
    con.execute("VACUUM")
    total_after = con.execute('SELECT count(1) FROM submissions').fetchone()[0]
    print(f'Total submissions after purge: {total_after}')

con.close()
