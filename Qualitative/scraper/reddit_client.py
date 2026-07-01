import os
import time
import sqlite3
import yaml
import logging
import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

import praw
from db.connection import get_db_connection
from db.schema import get_or_create_partition
from scraper.engine import QuantSentimentEngine
from scraper.health_monitor import CircuitBreaker, CircuitOpenError

logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config", "reddit_weights.yaml"))
CREDENTIALS_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config", "reddit_credentials.yaml"))

def load_weights_config() -> Dict[str, Any]:
    with open(CONFIG_PATH, 'r') as f:
        config = yaml.safe_load(f)
    return config

def load_credentials() -> Dict[str, Any]:
    with open(CREDENTIALS_PATH, 'r') as f:
        config = yaml.safe_load(f)
    return config.get("reddit", {})

class RedditUniversalScraper:
    def __init__(self, circuit_breaker: CircuitBreaker = None):
        creds = load_credentials()
        self.reddit = praw.Reddit(
            client_id=creds.get("client_id", ""),
            client_secret=creds.get("client_secret", ""),
            user_agent=creds.get("user_agent", "quant-sentiment-scraper/1.0"),
            username=creds.get("username") or None,
            password=creds.get("password") or None,
        )
        self.engine = QuantSentimentEngine()
        self.circuit_breaker = circuit_breaker
        self.source_name = "reddit"

    async def scrape_fallback_async(self, tickers: List[str] = None) -> 'ScrapeResult':
        """Async wrapper for hybrid orchestrator."""
        from scraper.hybrid_orchestrator import ScrapeResult
        if self.circuit_breaker and not self.circuit_breaker.can_execute():
            raise CircuitOpenError("Reddit circuit breaker OPEN")

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._scrape_fallback_sync, tickers)

    def _scrape_fallback_sync(self, tickers: List[str] = None) -> 'ScrapeResult':
        """Synchronous fallback scrape with reduced scope."""
        from scraper.hybrid_orchestrator import ScrapeResult
        start = datetime.utcnow()
        try:
            priority_subs = ["wallstreetbets", "stocks", "options", "SecurityAnalysis"]
            # Run limited scrape for fallback
            self._scrape_priority_subreddits(priority_subs)
            
            # Count new messages from today
            from db.connection import get_db_connection
            today = datetime.utcnow().strftime("%Y-%m-%d")
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM daily_aggregations WHERE date=? AND source='reddit'", (today,))
            count = cursor.fetchone()[0]
            conn.close()
            
            return ScrapeResult(
                source="reddit", messages_count=count, tickers_found=tickers or [],
                duration_ms=int((datetime.utcnow() - start).total_seconds() * 1000), errors=[]
            )
        except Exception as e:
            logger.error(f"Reddit fallback failed: {e}")
            return ScrapeResult(source="reddit", messages_count=0, tickers_found=[],
                              duration_ms=int((datetime.utcnow() - start).total_seconds() * 1000), errors=[str(e)])

    def _scrape_priority_subreddits(self, priority_subs: List[str]):
        """Scrape only priority subreddits for fallback."""
        config = load_weights_config()
        subreddit_taxonomy = config["subreddit_weights"]
        category_weights = config["category_weights"]
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        now = int(time.time())
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        
        for category, subs in subreddit_taxonomy.items():
            for sub, weight in subs.items():
                if sub not in priority_subs:
                    continue
                logger.info(f"Fallback scraping r/{sub} (Category: {category})...")
                
                cursor.execute("SELECT last_cursor FROM scrape_state WHERE subreddit=? AND sort=?", (sub, "hot"))
                row = cursor.fetchone()
                last_cursor = row[0] if row else None
                
                posts = self.fetch_subreddit_posts(sub, sort="hot", limit=30, after=last_cursor)
                if not posts:
                    continue
                
                next_cursor = posts[-1].id if posts else None
                
                for post in posts:
                    post_id = post.id
                    
                    cursor.execute("SELECT 1 FROM submissions WHERE id = ?", (post_id,))
                    if cursor.fetchone():
                        continue
                    
                    title = post.title or ""
                    selftext = post.selftext or ""
                    score = post.score or 0
                    upvote_ratio = post.upvote_ratio or 1.0
                    num_comments = post.num_comments or 0
                    created_utc = int(post.created_utc) if post.created_utc else now
                    post_url = post.url or ""
                    
                    partition_table = get_or_create_partition(conn, created_utc)
                    
                    cursor.execute(f"""
                        INSERT OR REPLACE INTO {partition_table} 
                        (id, subreddit, category, title, selftext, score, upvote_ratio, num_comments, url, created_utc, scraped_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (post_id, sub, category, title, selftext, score, upvote_ratio, num_comments, post_url, created_utc, now))
                    
                    combined_text = f"{title} {selftext}"
                    tickers = self.engine.extract_tickers(combined_text)
                    sentiment = self.engine.analyze_sentiment(combined_text)
                    risks = self.engine.scan_risks(combined_text)
                    
                    for ticker in tickers:
                        hours_old = max(1, (now - created_utc) / 3600.0)
                        recency_weight = 1.0 / (1.0 + hours_old / 24.0)
                        post_weight = (1 + score / 100.0) * recency_weight
                        
                        cursor.execute("""
                            INSERT OR IGNORE INTO daily_aggregations (ticker, date, category, subreddit, mention_count, avg_sentiment, weighted_sum, total_weight, source)
                            VALUES (?, ?, ?, ?, 0, 0.0, 0.0, 0.0, 'reddit')
                        """, (ticker, today_str, category, sub))
                        
                        cursor.execute("""
                            UPDATE daily_aggregations 
                            SET mention_count = mention_count + 1,
                                avg_sentiment = (avg_sentiment * (mention_count - 1) + ?) / mention_count,
                                weighted_sum = weighted_sum + ?,
                                total_weight = total_weight + ?
                            WHERE ticker=? AND date=? AND category=? AND subreddit=?
                        """, (sentiment, sentiment * post_weight, post_weight, ticker, today_str, category, sub))
                        
                        for risk_type, count in risks.items():
                            if count > 0:
                                cursor.execute("""
                                    INSERT OR IGNORE INTO risk_signals (ticker, date, risk_type, category, frequency)
                                    VALUES (?, ?, ?, ?, 0)
                                """, (ticker, today_str, risk_type, category))
                                
                                cursor.execute("""
                                    UPDATE risk_signals
                                    SET frequency = frequency + ?
                                    WHERE ticker=? AND date=? AND risk_type=? AND category=?
                                """, (count, ticker, today_str, risk_type, category))
                                
                cursor.execute("""
                    INSERT OR REPLACE INTO scrape_state (subreddit, sort, last_cursor, last_run)
                    VALUES (?, ?, ?, ?)
                """, (sub, "hot", next_cursor, now))
                conn.commit()
        
        self.compute_composite_scores(conn, today_str, category_weights, subreddit_taxonomy)
        conn.commit()
        conn.close()

    def fetch_subreddit_posts(self, subreddit: str, sort: str = "hot", limit: int = 50, after: Optional[str] = None) -> List[praw.models.Submission]:
        try:
            sub = self.reddit.subreddit(subreddit)
            if sort == "hot":
                posts = list(sub.hot(limit=limit))
            elif sort == "new":
                posts = list(sub.new(limit=limit))
            elif sort == "top":
                posts = list(sub.top(limit=limit, time_filter="day"))
            else:
                posts = list(sub.hot(limit=limit))
            
            if after:
                posts = [p for p in posts if p.id != after]
            return posts
        except Exception as e:
            logger.error(f"Exception fetching r/{subreddit}: {e}")
            return []

    def scrape_all(self):
        config = load_weights_config()
        subreddit_taxonomy = config["subreddit_weights"]
        category_weights = config["category_weights"]
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        now = int(time.time())
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        
        # Record model version details
        model_info = self.engine.get_model_version()
        cursor.execute("""
            INSERT INTO sentiment_runs (date, model_version, lexicon_hash, nltk_version, analyzer_config, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (today_str, model_info["model_version"], model_info["lexicon_hash"], 
              model_info["nltk_version"], model_info["analyzer_config"], now))
        
        conn.commit()
        
        for category, subs in subreddit_taxonomy.items():
            for sub, weight in subs.items():
                logger.info(f"Scraping r/{sub} (Category: {category})...")
                
                cursor.execute("SELECT last_cursor FROM scrape_state WHERE subreddit=? AND sort=?", (sub, "hot"))
                row = cursor.fetchone()
                last_cursor = row[0] if row else None
                
                posts = self.fetch_subreddit_posts(sub, sort="hot", limit=50, after=last_cursor)
                if not posts:
                    continue
                
                next_cursor = posts[-1].id if posts else None
                
                for post in posts:
                    post_id = post.id
                    
                    # Deduplication check across all historical submissions
                    cursor.execute("SELECT 1 FROM submissions WHERE id = ?", (post_id,))
                    if cursor.fetchone():
                        # Post has already been scraped and processed, skip to avoid double counting
                        continue
                    
                    title = post.title or ""
                    selftext = post.selftext or ""
                    score = post.score or 0
                    upvote_ratio = post.upvote_ratio or 1.0
                    num_comments = post.num_comments or 0
                    created_utc = int(post.created_utc) if post.created_utc else now
                    post_url = post.url or ""
                    
                    # Determine appropriate monthly partition table
                    partition_table = get_or_create_partition(conn, created_utc)
                    
                    # Insert into partition
                    cursor.execute(f"""
                        INSERT OR REPLACE INTO {partition_table} 
                        (id, subreddit, category, title, selftext, score, upvote_ratio, num_comments, url, created_utc, scraped_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (post_id, sub, category, title, selftext, score, upvote_ratio, num_comments, post_url, created_utc, now))
                    
                    combined_text = f"{title} {selftext}"
                    tickers = self.engine.extract_tickers(combined_text)
                    sentiment = self.engine.analyze_sentiment(combined_text)
                    risks = self.engine.scan_risks(combined_text)
                    
                    for ticker in tickers:
                        hours_old = max(1, (now - created_utc) / 3600.0)
                        recency_weight = 1.0 / (1.0 + hours_old / 24.0)
                        post_weight = (1 + score / 100.0) * recency_weight
                        
                        cursor.execute("""
                            INSERT OR IGNORE INTO daily_aggregations (ticker, date, category, subreddit, mention_count, avg_sentiment, weighted_sum, total_weight)
                            VALUES (?, ?, ?, ?, 0, 0.0, 0.0, 0.0)
                        """, (ticker, today_str, category, sub))
                        
                        cursor.execute("""
                            UPDATE daily_aggregations 
                            SET mention_count = mention_count + 1,
                                avg_sentiment = (avg_sentiment * (mention_count - 1) + ?) / mention_count,
                                weighted_sum = weighted_sum + ?,
                                total_weight = total_weight + ?
                            WHERE ticker=? AND date=? AND category=? AND subreddit=?
                        """, (sentiment, sentiment * post_weight, post_weight, ticker, today_str, category, sub))
                        
                        for risk_type, count in risks.items():
                            if count > 0:
                                cursor.execute("""
                                    INSERT OR IGNORE INTO risk_signals (ticker, date, risk_type, category, frequency)
                                    VALUES (?, ?, ?, ?, 0)
                                """, (ticker, today_str, risk_type, category))
                                
                                cursor.execute("""
                                    UPDATE risk_signals
                                    SET frequency = frequency + ?
                                    WHERE ticker=? AND date=? AND risk_type=? AND category=?
                                """, (count, ticker, today_str, risk_type, category))
                                
                cursor.execute("""
                    INSERT OR REPLACE INTO scrape_state (subreddit, sort, last_cursor, last_run)
                    VALUES (?, ?, ?, ?)
                """, (sub, "hot", next_cursor, now))
                conn.commit()

        # Calculate composite weighted sentiment scores
        self.compute_composite_scores(conn, today_str, category_weights, subreddit_taxonomy)
        conn.commit()
        conn.close()
        logger.info("Scrape complete and aggregated.")

    def compute_composite_scores(self, conn: sqlite3.Connection, date_str: str, category_weights: dict, subreddit_taxonomy: dict):
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT ticker FROM daily_aggregations WHERE date=?", (date_str,))
        tickers = [row[0] for row in cursor.fetchall()]
        
        for ticker in tickers:
            cursor.execute("""
                SELECT category, subreddit, 
                       CASE WHEN total_weight > 0 THEN weighted_sum / total_weight ELSE 0 END as weighted_sentiment
                FROM daily_aggregations 
                WHERE ticker=? AND date=?
            """, (ticker, date_str))
            cat_data = cursor.fetchall()
            
            composite = 0.0
            total_weight = 0.0
            
            for row in cat_data:
                category, subreddit, sentiment = row[0], row[1], row[2]
                cat_w = category_weights.get(category, 0.0)
                sub_w = subreddit_taxonomy.get(category, {}).get(subreddit, 0.0)
                combined_weight = cat_w * sub_w
                composite += sentiment * combined_weight
                total_weight += combined_weight
                
            if total_weight > 0:
                composite = composite / total_weight
                cursor.execute("""
                    INSERT OR REPLACE INTO composite_scores (ticker, date, composite_sentiment)
                    VALUES (?, ?, ?)
                """, (ticker, date_str, composite))

if __name__ == "__main__":
    scraper = RedditUniversalScraper()
    scraper.scrape_all()
