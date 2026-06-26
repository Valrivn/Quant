import os
import sqlite3
import numpy as np
from datetime import datetime, timedelta
import random

def seed_historical_data():
    print('Seeding baseline metrics...')
    db_path = "reddit_quant.db"
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    tickers = ["AAPL", "MSFT", "NVDA", "TSLA", "GOOGL", "AMD", "META", "AMZN", "AVGO", "JPM"]
    subreddits = ["wallstreetbets", "stocks", "options", "SecurityAnalysis", "ValueInvesting", "LocalLLaMA", "hardware", "geopolitics", "economics", "supplychain"]
    categories = ["retail_options", "tech_product", "fundamental_institutional", "macro_geopolitical"]
    
    start_date = datetime(2021, 1, 1)
    end_date = datetime(2026, 6, 26)
    
    print("Seeding watchlist...")
    for ticker in tickers:
        cursor.execute("""
            INSERT OR IGNORE INTO watchlist (ticker, added_at, active, notes)
            VALUES (?, ?, 1, 'Seeded baseline')
        """, (ticker, int(start_date.timestamp())))
    
    print("Seeding daily_aggregations...")
    current_date = start_date
    while current_date <= end_date:
        date_str = current_date.strftime("%Y-%m-%d")
        for ticker in tickers:
            for category in categories:
                for subreddit in subreddits:
                    if random.random() < 0.3:
                        mention_count = random.randint(1, 50)
                        avg_sentiment = random.uniform(-0.8, 0.8)
                        weighted_sum = avg_sentiment * mention_count
                        total_weight = mention_count * random.uniform(0.5, 1.5)
                        cursor.execute("""
                            INSERT OR IGNORE INTO daily_aggregations 
                            (ticker, date, category, subreddit, mention_count, avg_sentiment, weighted_sum, total_weight, source)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'reddit')
                        """, (ticker, date_str, category, subreddit, mention_count, avg_sentiment, weighted_sum, total_weight))
        current_date += timedelta(days=7)
        if current_date.day <= 7:
            print(f"  Progress: {date_str}")
    
    print("Seeding psychological_vectors...")
    for ticker in tickers:
        for i in range(100):
            timestamp = int((start_date + timedelta(days=random.randint(0, 1980), hours=random.randint(0, 23))).timestamp())
            source_prov = f"reddit:{random.choice(subreddits)}"
            compound = random.uniform(-1, 1)
            bull = random.randint(0, 20)
            bear = random.randint(0, 20)
            ratio = bull / max(bear, 1)
            velocity = random.uniform(0, 100)
            sigma = random.uniform(-3, 3)
            accel = random.uniform(-10, 10)
            cursor.execute("""
                INSERT INTO psychological_vectors 
                (ticker, timestamp, source_provenance, raw_text, compound_vader, bull_bear_ratio, 
                 bullish_count, bearish_count, mention_velocity, comment_volume_sigma, acceleration, 
                 employee_sentiment_proxy, dev_fork_acceleration, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (ticker, timestamp, source_prov, f"Sample text for {ticker}", compound, ratio, bull, bear, 
                  velocity, sigma, accel, random.uniform(-1, 1), random.uniform(-1, 1), '{}'))
    
    print("Seeding psychological_regimes...")
    current_date = start_date
    regimes = ["APATHY", "GRASSROOTS", "EUPHORIA", "PANIC_CAPITULATION"]
    while current_date <= end_date:
        date_str = current_date.strftime("%Y-%m-%d")
        for ticker in tickers:
            if random.random() < 0.4:
                regime = random.choice(regimes)
                contrarian = regime == "PANIC_CAPITULATION"
                confidence = random.uniform(0.3, 0.9)
                ratio = random.uniform(0.2, 5.0)
                sigma = random.uniform(-2, 3)
                cursor.execute("""
                    INSERT OR IGNORE INTO psychological_regimes 
                    (ticker, date, active_regime, contrarian_buy_authorized, confidence_score, 
                     bull_bear_ratio, velocity_sigma, employee_sentiment_proxy, dev_velocity,
                     fintech_confirmation_score, quantitative_value_signal)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (ticker, date_str, regime, contrarian, confidence, ratio, sigma,
                      random.uniform(-1, 1), random.uniform(-1, 1), random.uniform(0, 1), random.uniform(0, 1)))
        current_date += timedelta(days=1)
        if current_date.day == 1:
            print(f"  Progress: {date_str}")
    
    print("Seeding quantitative_dcf_floor...")
    for ticker in tickers:
        for i in range(50):
            date_obj = start_date + timedelta(days=random.randint(0, 1980))
            date_str = date_obj.strftime("%Y-%m-%d")
            current_price = random.uniform(50, 500)
            floor = current_price * random.uniform(0.6, 0.95)
            ceiling = current_price * random.uniform(1.05, 1.5)
            margin = (ceiling - current_price) / ceiling
            cursor.execute("""
                INSERT OR IGNORE INTO quantitative_dcf_floor 
                (ticker, date, intrinsic_floor, intrinsic_ceiling, margin_of_safety, current_price, wacc, fcf_projection, terminal_value, model_version, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (ticker, date_str, floor, ceiling, margin, current_price, 0.10, random.uniform(1e9, 1e11), random.uniform(1e11, 1e12), "stub_v1", int(date_obj.timestamp())))
    
    print("Seeding velocity_snapshots...")
    for ticker in tickers:
        for i in range(200):
            window_start = int((start_date + timedelta(days=random.randint(0, 1980), hours=random.randint(0, 20))).timestamp())
            window_type = random.choice(["1h", "4h", "24h"])
            window_end = window_start + {"1h": 3600, "4h": 14400, "24h": 86400}[window_type]
            cursor.execute("""
                INSERT OR IGNORE INTO velocity_snapshots 
                (ticker, window_start, window_end, window_type, mention_count, comment_volume, unique_authors)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (ticker, window_start, window_end, window_type, random.randint(1, 100), random.randint(1, 200), random.randint(1, 50)))
    
    print("Seeding adzuna_job_snapshots...")
    for ticker in tickers:
        current_date = start_date
        while current_date <= end_date:
            date_str = current_date.strftime("%Y-%m-%d")
            if random.random() < 0.2:
                job_count = random.randint(10, 1000)
                cursor.execute("""
                    INSERT OR IGNORE INTO adzuna_job_snapshots 
                    (ticker, company_name, date, job_count, job_count_7d_ago, job_count_30d_ago, delta_7d_pct, delta_30d_pct)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (ticker, ticker, date_str, job_count, random.randint(10, 1000), random.randint(10, 1000), 
                      random.uniform(-0.5, 0.5), random.uniform(-0.3, 0.3)))
            current_date += timedelta(days=7)
    
    print("Seeding glassdoor_snapshots and comparably_snapshots...")
    for ticker in tickers:
        current_date = start_date
        while current_date <= end_date:
            date_str = current_date.strftime("%Y-%m-%d")
            if random.random() < 0.15:
                cursor.execute("""
                    INSERT OR IGNORE INTO glassdoor_snapshots 
                    (ticker, date, rating, created_at)
                    VALUES (?, ?, ?, ?)
                """, (ticker, date_str, random.uniform(2.5, 4.5), int(current_date.timestamp())))
                cursor.execute("""
                    INSERT OR IGNORE INTO comparably_snapshots 
                    (ticker, date, rating, created_at)
                    VALUES (?, ?, ?, ?)
                """, (ticker, date_str, random.uniform(60, 95), int(current_date.timestamp())))
            current_date += timedelta(days=30)
    
    print("Seeding hiring_velocity_snapshots...")
    for ticker in tickers:
        current_date = start_date
        while current_date <= end_date:
            date_str = current_date.strftime("%Y-%m-%d")
            if random.random() < 0.2:
                job_count = random.randint(50, 2000)
                cursor.execute("""
                    INSERT OR IGNORE INTO hiring_velocity_snapshots 
                    (ticker, date, job_count, delta_30d, zscore_1y, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (ticker, date_str, job_count, random.uniform(-0.5, 0.5), random.uniform(-3, 3), int(current_date.timestamp())))
            current_date += timedelta(days=7)
    
    print("Seeding product_intel_reviews...")
    platforms = ["g2", "capterra", "app_store"]
    for ticker in tickers:
        for i in range(50):
            date_obj = start_date + timedelta(days=random.randint(0, 1980))
            date_str = date_obj.strftime("%Y-%m-%d")
            platform = random.choice(platforms)
            rating = random.uniform(2.0, 5.0)
            vader = random.uniform(-1, 1)
            cursor.execute("""
                INSERT INTO product_intel_reviews 
                (ticker, date, platform, rating, review_text, vader_compound, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (ticker, date_str, platform, rating, f"Review for {ticker}", vader, int(date_obj.timestamp())))
    
    print("Seeding g2_capterra_reviews...")
    for ticker in tickers:
        for i in range(30):
            date_obj = start_date + timedelta(days=random.randint(0, 1980))
            date_str = date_obj.strftime("%Y-%m-%d")
            platform = random.choice(["g2", "capterra"])
            rating = random.uniform(2.5, 4.8)
            keywords = random.choice(["rushed updates,broken builds", "technical debt,legacy code", "regression bugs", ""])
            cursor.execute("""
                INSERT INTO g2_capterra_reviews 
                (ticker, date, platform, product_name, rating, review_text, review_date, keywords_detected, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (ticker, date_str, platform, f"{ticker} Product", rating, f"Review for {ticker}", date_str, keywords, int(date_obj.timestamp())))
    
    print("Seeding app_store_feeds...")
    for ticker in tickers:
        for i in range(30):
            date_obj = start_date + timedelta(days=random.randint(0, 1980))
            date_str = date_obj.strftime("%Y-%m-%d")
            rating = random.uniform(2.0, 5.0)
            vader = random.uniform(-1, 1)
            cursor.execute("""
                INSERT INTO app_store_feeds 
                (ticker, date, platform, app_id, app_name, rating, review_text, review_date, vader_compound, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (ticker, date_str, "apple", f"com.{ticker.lower()}.app", f"{ticker} App", rating, f"Review for {ticker}", date_str, vader, int(date_obj.timestamp())))
    
    print("Seeding jobspy_velocity...")
    sources = ["linkedin", "indeed", "themuse"]
    for ticker in tickers:
        current_date = start_date
        while current_date <= end_date:
            date_str = current_date.strftime("%Y-%m-%d")
            if random.random() < 0.1:
                for source in sources:
                    job_count = random.randint(100, 5000)
                    cursor.execute("""
                        INSERT OR IGNORE INTO jobspy_velocity 
                        (ticker, date, source, job_count, job_count_8_runs_ago, delta_30d, mean_252_runs, std_252_runs, zscore_1y, ghost_job_flag, operational_fracture_flag, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (ticker, date_str, source, job_count, random.randint(100, 5000), random.uniform(-0.5, 0.5), 
                          random.uniform(1000, 3000), random.uniform(100, 500), random.uniform(-3, 3), 0, 0, int(current_date.timestamp())))
            current_date += timedelta(days=7)
    
    print("Seeding validation_gate_results...")
    for ticker in tickers:
        current_date = start_date
        while current_date <= end_date:
            date_str = current_date.strftime("%Y-%m-%d")
            if random.random() < 0.1:
                gd_norm = random.uniform(0, 1)
                comp_norm = random.uniform(0, 1)
                div = abs(gd_norm - comp_norm)
                penalty = max(0.1, np.exp(-5.0 * max(0, div - 0.2)))
                override = penalty < 0.4
                cursor.execute("""
                    INSERT OR IGNORE INTO validation_gate_results 
                    (ticker, date, normalized_glassdoor, normalized_comparably, divergence, penalty_multiplier, override_triggered, confidence_floor, kappa, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (ticker, date_str, gd_norm, comp_norm, div, penalty, override, 0.40, 5.0, int(current_date.timestamp())))
            current_date += timedelta(days=30)
    
    conn.commit()
    conn.close()
    print('Historical seeding complete!')

if __name__ == '__main__':
    seed_historical_data()