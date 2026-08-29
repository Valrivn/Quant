import sys
import os
import logging
from datetime import datetime, timezone

# Add project roots to path
sys.path.insert(0, r"C:\Users\Hayden\Quant\Qualitative")
sys.path.insert(0, r"C:\Users\Hayden\Quant")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("run_instagram_pipeline")

from db.connection import get_connection, connection_context
from db.schema_discovery import create_discovery_tables
from Qualitative.psychological.scrapers.instagram_primary import fetch_instagram_mentions, InstagramConfig
from discovery.ig_experiment import _validate_ticker

def run_pipeline(limit=100):
    logger.info("Initializing discovery tables...")
    conn = get_connection()
    create_discovery_tables(conn)
    
    logger.info("Loading Instagram Configuration...")
    config = InstagramConfig()
    # Use standard hashtags for finding relevant tech/trading content
    config.hashtags = [
        "semiconductors", "nvidia", "asml", "liquidcooling",
        "advancedpackaging", "highnaeuv", "stocks", "investing",
        "trading", "wallstreetbets"
    ]
    # Keep delays reasonable but anti-bot safe
    config.min_delay = 5.0
    config.max_delay = 10.0
    config.max_pages_per_session = 5
    
    logger.info(f"Fetching Instagram mentions (limit={limit})...")
    mentions = fetch_instagram_mentions(limit=limit, config=config)
    logger.info(f"Scraped {len(mentions)} raw mentions from Instagram.")
    
    if not mentions:
        logger.warning("No mentions scraped from Instagram.")
        return
        
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now_ts = int(datetime.now(timezone.utc).timestamp())
    
    valid_count = 0
    with connection_context() as conn:
        cursor = conn.cursor()
        for m in mentions:
            entity = m.get("entity", "").strip().upper()
            if entity.startswith("$"):
                entity = entity[1:]
            if not entity:
                continue
                
            # Validate ticker (resolves CIK)
            if not _validate_ticker(entity):
                logger.debug(f"Ticker {entity} failed validation, skipping.")
                continue
                
            valid_count += 1
            # 1. Insert into discovery_mentions
            external_id = m.get("external_id")
            source_confidence = m.get("source_confidence", 0.6)
            volume_or_rank = m.get("volume_or_rank")
            sentiment = m.get("sentiment")
            if sentiment is None:
                sentiment = 0.0
            topic = m.get("topic", "Stocks")
            
            try:
                cursor.execute("""
                    INSERT OR REPLACE INTO discovery_mentions 
                    (source_id, entity, topic, fetch_ts, source_confidence, volume_or_rank, sentiment, external_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, ("instagram", entity, topic, now_ts, source_confidence, volume_or_rank, sentiment, external_id))
            except Exception as e:
                logger.warning(f"Failed to insert raw mention for {entity}: {e}")
            
            # 2. Insert/Update daily_aggregations
            category = "tech_product" if entity in ["NVDA", "AMD", "ASML", "AVGO", "INTC"] else "retail_options"
            subreddit = "instagram"
            
            # Simplified weight based on source_confidence
            post_weight = source_confidence
            
            cursor.execute("""
                INSERT OR IGNORE INTO daily_aggregations 
                (ticker, date, category, subreddit, mention_count, avg_sentiment, weighted_sum, total_weight, source)
                VALUES (?, ?, ?, ?, 0, 0.0, 0.0, 0.0, 'instagram')
            """, (entity, today_str, category, subreddit))
            
            cursor.execute("""
                UPDATE daily_aggregations 
                SET mention_count = mention_count + 1,
                    avg_sentiment = (avg_sentiment * (mention_count - 1) + ?) / mention_count,
                    weighted_sum = weighted_sum + ?,
                    total_weight = total_weight + ?
                WHERE ticker=? AND date=? AND category=? AND subreddit=? AND source='instagram'
            """, (sentiment, sentiment * post_weight, post_weight, entity, today_str, category, subreddit))
            
    logger.info(f"Pipeline complete! Processed {valid_count} validated mentions into db.")

if __name__ == "__main__":
    run_pipeline(limit=25)
