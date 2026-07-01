import asyncio
import logging
import time
from typing import AsyncGenerator, List, Dict, Optional, Set
from datetime import datetime, timezone
import praw
from praw.models import Comment
from config import load_hybrid_config, SUBREDDIT_TAXONOMY

logger = logging.getLogger(__name__)


class RedditPrimaryScraper:
    def __init__(self, config_dict: dict = None, reddit_config: dict = None):
        hybrid_config = load_hybrid_config()
        self.config = config_dict or hybrid_config.get("psychological", {})
        self.reddit_config = reddit_config or hybrid_config.get("endpoints", {}).get("reddit", {})
        
        self.reddit = praw.Reddit(
            client_id=self.reddit_config.get("client_id"),
            client_secret=self.reddit_config.get("client_secret"),
            user_agent=self.reddit_config.get("user_agent", "quant-psychological/1.0"),
            read_only=True
        )
        
        self.all_subreddits = self._get_all_subreddits()
        self.ticker_patterns = self._compile_ticker_patterns()
        self.rate_limit_delay = 1.0
        self.max_retries = 3
        
    def _get_all_subreddits(self) -> List[str]:
        subreddits = set()
        for category, subs in SUBREDDIT_TAXONOMY.items():
            subreddits.update(subs.keys())
        return list(subreddits)
        
    def _compile_ticker_patterns(self) -> Dict[str, str]:
        return {}
        
    def _extract_tickers(self, text: str) -> List[str]:
        import re
        tickers = set()
        words = re.findall(r'\b[A-Z]{1,5}\b', text.upper())
        for word in words:
            if word not in {'THE', 'AND', 'FOR', 'ARE', 'BUT', 'NOT', 'YOU', 'ALL', 'CAN', 'HAS', 'HAD', 'WAS', 'ONE', 'TWO', 'NEW', 'OLD', 'SEE', 'GET', 'GOT', 'LET', 'PUT', 'CALL', 'LONG', 'SHORT', 'MOON', 'CRASH', 'PUMP', 'DUMP', 'RIP', 'YTD', 'CEO', 'CFO', 'CTO', 'IPO', 'ETF', 'SEC', 'FDA', 'FOMC', 'CPI', 'PPI', 'GDP', 'EPS', 'PE', 'ROE', 'ROA', 'EBITDA', 'FCF', 'DCF', 'AI', 'ML', 'GPU', 'CPU', 'API', 'SDK', 'UI', 'UX', 'DB', 'SQL', 'AWS', 'GCP', 'K8S', 'CI', 'CD', 'PR', 'QA', 'DEV', 'OPS', 'SRE', 'PM', 'PO', 'CTO', 'VP', 'DIR', 'MGR', 'ENG', 'TECH', 'SALES', 'HR', 'IT', 'FIN', 'OPS', 'MKT', 'BD', 'R&D', 'Q1', 'Q2', 'Q3', 'Q4', 'FY', 'TTM', 'YOY', 'QOQ', 'MOM', 'WOW', 'DOD', 'AH', 'PM', 'AM', 'EST', 'PST', 'CST', 'MST', 'UTC', 'GMT', 'EDT', 'PDT', 'CDT', 'MDT'}:
                tickers.add(word)
        return list(tickers)
        
    async def harvest_raw_commentary(self, tickers: List[str], 
                                      limit_per_subreddit: int = 100) -> AsyncGenerator[Dict, None]:
        ticker_set = set(t.upper() for t in tickers)
        
        for subreddit_name in self.all_subreddits:
            try:
                subreddit = self.reddit.subreddit(subreddit_name)
                
                for submission in subreddit.new(limit=limit_per_subreddit):
                    if not self._submission_relevant(submission, ticker_set):
                        continue
                        
                    submission.comments.replace_more(limit=0)
                    comments = submission.comments.list()
                    
                    for comment in comments:
                        if not isinstance(comment, Comment):
                            continue
                            
                        comment_text = comment.body
                        comment_tickers = self._extract_tickers(comment_text)
                        
                        relevant_tickers = [t for t in comment_tickers if t in ticker_set]
                        if not relevant_tickers:
                            continue
                            
                        for ticker in relevant_tickers:
                            yield {
                                "ticker": ticker,
                                "text": comment_text,
                                "subreddit": subreddit_name,
                                "created_utc": int(comment.created_utc),
                                "score": comment.score
                            }
                            
                    await asyncio.sleep(self.rate_limit_delay)
                    
            except praw.exceptions.RedditAPIException as e:
                logger.warning(f"Reddit API error for r/{subreddit_name}: {e}")
                await self._handle_rate_limit(e)
            except Exception as e:
                logger.error(f"Error scraping r/{subreddit_name}: {e}")
                
    def _submission_relevant(self, submission, ticker_set: Set[str]) -> bool:
        text = f"{submission.title} {submission.selftext}".upper()
        found = self._extract_tickers(text)
        return any(t in ticker_set for t in found)
        
    async def _handle_rate_limit(self, exception: Exception) -> None:
        for attempt in range(self.max_retries):
            delay = self.rate_limit_delay * (2 ** attempt)
            logger.info(f"Rate limited, waiting {delay}s (attempt {attempt + 1}/{self.max_retries})")
            await asyncio.sleep(delay)
            return


async def create_reddit_scraper(config_dict: dict = None, reddit_config: dict = None) -> RedditPrimaryScraper:
    return RedditPrimaryScraper(config_dict, reddit_config)


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()
    
    async def test():
        scraper = await create_reddit_scraper()
        async for comment in scraper.harvest_raw_commentary(["AAPL", "TSLA"], limit_per_subreddit=5):
            print(comment)
            
    asyncio.run(test())