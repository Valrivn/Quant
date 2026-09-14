import sqlite3
conn = sqlite3.connect("reddit_quant.db")
c = conn.cursor()

print("total submissions:", c.execute("SELECT COUNT(*) FROM submissions").fetchone()[0])
print("distinct ids:", c.execute("SELECT COUNT(DISTINCT id) FROM submissions").fetchone()[0])
print("distinct titles:", c.execute("SELECT COUNT(DISTINCT title) FROM submissions").fetchone()[0])
print(
    "templated titles (what are your thoughts on):",
    c.execute("SELECT COUNT(*) FROM submissions WHERE title LIKE '%What are your thoughts on%'").fetchone()[0],
)
print("titles with 'thoughts':", c.execute("SELECT COUNT(*) FROM submissions WHERE title LIKE '%thoughts%'").fetchone()[0])
print("selftext 'Sample text':", c.execute("SELECT COUNT(*) FROM submissions WHERE selftext LIKE '%Sample text%'").fetchone()[0])
print("by subreddit:")
for r in c.execute("SELECT subreddit, COUNT(*) FROM submissions GROUP BY subreddit ORDER BY 2 DESC LIMIT 12"):
    print("  ", r)
print("sample 6 ids/titles:")
for r in c.execute("SELECT id, subreddit, title FROM submissions LIMIT 6"):
    print("  ", r)
conn.close()