import os
import sqlite3
import random
from datetime import datetime, timedelta

def seed_historical_instagram():
    print("Seeding historical Instagram mentions in reddit_quant.db...")
    db_path = "reddit_quant.db"
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    tickers = ["NVDA", "AMD", "MSFT", "GOOGL", "META", "TSLA", "AAPL", "AMZN"]
    
    # We will seed 1000 mentions spread from 2021 to 2026
    start_date = datetime(2021, 1, 1)
    end_date = datetime(2026, 6, 26)
    delta_days = (end_date - start_date).days
    
    bullish_captions = [
        "NVIDIA Blackwell chips are next level, buying more $NVDA calls 🚀🚀",
        "Jensen Huang is a genius. NVDA to the moon! #nvidia #gpu",
        "Advanced packaging and co-packaged optics demand is insane for NVDA.",
        "NVIDIA NIM microservices are going to dominate enterprise AI. Bullish!",
        "Long $NVDA. The H100 and B200 backlog is years long.",
        "NVIDIA graphics cards are still the gold standard for AI research.",
        "Just added to my NVIDIA position. Solid earnings beat expected."
    ]
    
    bearish_captions = [
        "NVIDIA is overvalued at these levels. Time to buy $NVDA puts.",
        "Is the AI bubble popping? NVDA looking bearish here.",
        "Competition from AMD and custom silicon might hurt NVIDIA margins.",
        "Insiders are selling NVDA. Jensen Huang dumping shares.",
        "Shorting $NVDA here, too much hype priced in.",
        "NVIDIA margins might face headwind from TSMC wafer price hikes."
    ]
    
    neutral_captions = [
        "Watching NVIDIA price action today. Consolidation pattern forming.",
        "NVIDIA launches new CUDA update. Developers checking it out.",
        "Discussing NVIDIA GPU allocation policies at the conference.",
        "NVDA stock flat today ahead of the FOMC meeting.",
        "Comparing NVIDIA vs AMD hardware specs for our local cluster."
    ]
    
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    
    inserted = 0
    for _ in range(1200):
        ticker = random.choice(tickers)
        # Higher density of NVDA as requested by the user
        if random.random() < 0.4:
            ticker = "NVDA"
            
        r_days = random.randint(0, delta_days)
        r_hours = random.randint(0, 23)
        r_mins = random.randint(0, 59)
        post_date = start_date + timedelta(days=r_days, hours=r_hours, minutes=r_mins)
        fetch_ts = int(post_date.timestamp())
        
        # Pick caption category
        rand = random.random()
        if rand < 0.45:
            caption = random.choice(bullish_captions)
            sentiment = random.uniform(0.1, 1.8)
        elif rand < 0.8:
            caption = random.choice(bearish_captions)
            sentiment = random.uniform(-1.8, -0.1)
        else:
            caption = random.choice(neutral_captions)
            sentiment = 0.0
            
        # Customize caption for other tickers if selected
        if ticker != "NVDA":
            caption = caption.replace("NVDA", ticker).replace("NVIDIA", ticker).replace("Nvidia", ticker)
            
        shortcode = "".join(random.choice(alphabet) for _ in range(11))
        record_id = f"{ticker}_{shortcode}"
        
        views = random.randint(1000, 500000)
        comments = random.randint(5, 2500)
        followers = random.randint(1000, 2000000)
        verified = 1 if random.random() < 0.15 else 0
        ext_id = f"https://www.instagram.com/p/{shortcode}/"
        
        cursor.execute("""
            INSERT OR IGNORE INTO instagram_raw_mentions (
                id, ticker, shortcode, caption, sentiment, views, comments, followers, verified, fetch_ts, external_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            record_id,
            ticker,
            shortcode,
            caption,
            sentiment,
            views,
            comments,
            followers,
            verified,
            fetch_ts,
            ext_id
        ))
        if cursor.rowcount > 0:
            inserted += 1
            
    conn.commit()
    conn.close()
    print(f"Successfully seeded {inserted} historical Instagram mentions.")

if __name__ == "__main__":
    seed_historical_instagram()
