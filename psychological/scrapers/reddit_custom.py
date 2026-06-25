import asyncio
import logging
import re
import time
from typing import AsyncGenerator, List, Dict, Optional, Set
from datetime import datetime, timezone
from dataclasses import dataclass

from psychological.scrapers.lightweight_scraper import UnifiedScraperSession, ScraperConfig
from config import load_hybrid_config, SUBREDDIT_TAXONOMY

logger = logging.getLogger(__name__)


from psychological.interfaces import RedditCommentPayload


class RedditScraper:
    BASE_URL = "https://www.reddit.com"

    def __init__(self, config_dict: dict = None, scraper_config: ScraperConfig = None):
        self.config = config_dict or load_hybrid_config().get("psychological", {})
        self.scraper_config = scraper_config or ScraperConfig(headless=True)
        self.session: Optional[UnifiedScraperSession] = None
        self.all_subreddits = self._get_all_subreddits()
        self.ticker_blacklist = self._get_ticker_blacklist()

    def _get_all_subreddits(self) -> List[str]:
        subreddits = set()
        for category, subs in SUBREDDIT_TAXONOMY.items():
            subreddits.update(subs.keys())
        return list(subreddits)

    def _get_ticker_blacklist(self) -> Set[str]:
        from config.constants import TICKER_BLACKLIST
        # Extended blacklist with common English words that appear as uppercase in Reddit posts
        common_words = {
            "THE", "AND", "FOR", "ARE", "BUT", "NOT", "YOU", "ALL", "CAN", "HAS", "HAD", "WAS", "ONE", "TWO", "NEW", "OLD", "SEE", "GET", "GOT", "LET", "PUT", "CALL", "LONG", "SHORT", "MOON", "CRASH", "PUMP", "DUMP", "RIP", "YTD", "CEO", "CFO", "CTO", "IPO", "ETF", "SEC", "FDA", "FOMC", "CPI", "PPI", "GDP", "EPS", "PE", "ROE", "ROA", "EBITDA", "FCF", "DCF", "AI", "ML", "GPU", "CPU", "API", "SDK", "UI", "UX", "DB", "SQL", "AWS", "GCP", "K8S", "CI", "CD", "PR", "QA", "DEV", "OPS", "SRE", "PM", "PO", "CTO", "VP", "DIR", "MGR", "ENG", "TECH", "SALES", "HR", "IT", "FIN", "OPS", "MKT", "BD", "R&D", "Q1", "Q2", "Q3", "Q4", "FY", "TTM", "YOY", "QOQ", "MOM", "WOW", "DOD", "AH", "PM", "AM", "EST", "PST", "CST", "MST", "UTC", "GMT", "EDT", "PDT", "CDT", "MDT",
            "WHAT", "MOVES", "YOUR", "JUNE", "THIS", "THAT", "WITH", "FROM", "HAVE", "BEEN", "WERE", "THEY", "THEIR", "THERE", "THEN", "THAN", "WHEN", "WHERE", "WHICH", "WHO", "WHOM", "WHOSE", "WHY", "HOW", "ITS", "OUR", "OUT", "OVER", "OWN", "SAME", "SUCH", "VERY", "WELL", "WILL", "WOULD", "ABOUT", "AFTER", "AGAIN", "BELOW", "COULD", "EVERY", "FIRST", "FOUND", "GREAT", "GROUP", "HAND", "HIGH", "HOME", "LARGE", "LAST", "LEFT", "LIFE", "LIGHT", "LIKE", "LINE", "LITTLE", "LONG", "LOOK", "MADE", "MAKE", "MAN", "MANY", "MAY", "MIGHT", "MOST", "MUST", "NEVER", "NEXT", "NIGHT", "ONLY", "OPEN", "ORDER", "OTHER", "PART", "PLACE", "POINT", "POWER", "PUBLIC", "RIGHT", "SAID", "SAME", "SAW", "SAY", "SEE", "SEEM", "SEEN", "SHALL", "SHOULD", "SHOW", "SIDE", "SINCE", "SMALL", "SOUND", "STILL", "STUDY", "SYSTEM", "TAKE", "TELL", "THOSE", "THOUGH", "THOUGHT", "THROUGH", "THUS", "TOGETHER", "TOO", "TOOK", "TURN", "UNDER", "UNTIL", "UPON", "USED", "USES", "USING", "USUALLY", "VARIOUS", "WANT", "WAY", "WAYS", "WEEK", "WEEKS", "WENT", "WHERE", "WHILE", "WHITE", "WHOLE", "WITHIN", "WITHOUT", "WORK", "WORLD", "YEAR", "YEARS", "YOUNG", "ZUCK", "KOSPI", "SPCX", "TOP", "BRACE", "DUDE", "HOPE", "WELL", "DOING", "RETIREMENT", "ACCOUNT", "GROUND", "ONLY", "THESE", "WERE", "PUTS", "IF", "ALL", "IN", "ON", "USD", "ZUCK", "BRACE", "KOSPI", "SPCX", "TOP", "RIP",
            "TO", "IS", "IT", "AS", "AT", "BE", "BY", "DO", "GO", "HE", "HI", "IF", "IN", "IT", "ME", "MY", "NO", "OF", "ON", "OR", "SO", "UP", "US", "WE", "AM", "AN", "AS", "AT", "BE", "BY", "DO", "GO", "HE", "HI", "IF", "IN", "IS", "IT", "ME", "MY", "NO", "OF", "ON", "OR", "SO", "TO", "UP", "US", "WE", "HIS", "HER", "HIM", "SHE", "THEM", "THEN", "THAN", "THAT", "THIS", "THOSE", "THESE", "THERE", "WHERE", "WHEN", "WHY", "HOW", "WHO", "WHOM", "WHOSE", "WHICH", "WHAT", "WHICH", "WHILE", "WITH", "WITHIN", "WITHOUT", "YOUR", "YOU", "YOURS", "OUR", "OURS", "MY", "MINE", "HIS", "HERS", "ITS", "THEIR", "THEIRS"
        }
        return set(TICKER_BLACKLIST) | common_words

    async def initialize(self) -> None:
        self.session = UnifiedScraperSession(self.scraper_config)
        self.session.initialize()
        logger.info("RedditScraper initialized")

    async def close(self) -> None:
        if self.session:
            self.session.close()
            self.session = None

    async def __aenter__(self) -> "RedditScraper":
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    def _extract_tickers(self, text: str) -> List[str]:
        if not text:
            return []
        tickers = set()
        words = re.findall(r'\b[A-Z]{1,5}\b', text.upper())
        for word in words:
            if word not in self.ticker_blacklist and len(word) >= 2:
                tickers.add(word)
        return list(tickers)

    def _submission_relevant(self, title: str, selftext: str, ticker_set: Set[str]) -> bool:
        text = f"{title} {selftext}".upper()
        found = self._extract_tickers(text)
        return any(t in ticker_set for t in found)

    async def harvest_raw_commentary(
        self,
        tickers: List[str],
        limit_per_subreddit: int = 100,
        sort: str = "new"
    ) -> AsyncGenerator[RedditCommentPayload, None]:
        if not self.session:
            await self.initialize()

        ticker_set = set(t.upper() for t in tickers)
        processed_count = 0

        for subreddit_name in self.all_subreddits:
            try:
                url = f"{self.BASE_URL}/r/{subreddit_name}/{sort}/"
                logger.info(f"Scraping r/{subreddit_name} ({sort})...")

                success = await self.session.throttled_get(url, wait_for="shreddit-post", timeout=30)
                if not success:
                    logger.warning(f"Failed to load r/{subreddit_name}")
                    continue

                posts = self._extract_posts(limit_per_subreddit)

                for post in posts:
                    # Extract comments from all posts, filter by ticker in comments
                    comments = await self._extract_comments(post["permalink"], ticker_set)
                    for comment in comments:
                        yield comment
                        processed_count += 1

                    await asyncio.sleep(0.5)

            except Exception as e:
                logger.error(f"Error scraping r/{subreddit_name}: {e}")
                continue

        logger.info(f"Harvested {processed_count} relevant comments across {len(self.all_subreddits)} subreddits")

    def _extract_posts(self, limit: int) -> List[Dict]:
        posts = []
        try:
            elements = self.session.get_sb().find_elements("shreddit-post")[:limit]
            for elem in elements:
                try:
                    post_data = {
                        "id": elem.get_attribute("id") or "",
                        "title": "",
                        "selftext": "",
                        "score": 0,
                        "permalink": "",
                        "subreddit": "",
                        "created_utc": 0
                    }

                    post_data["title"] = elem.get_attribute("post-title") or ""
                    post_data["permalink"] = f"https://www.reddit.com{elem.get_attribute('permalink')}" if elem.get_attribute("permalink") else ""
                    post_data["subreddit"] = elem.get_attribute("subreddit-prefixed-name") or ""
                    if post_data["subreddit"].startswith("r/"):
                        post_data["subreddit"] = post_data["subreddit"][2:]

                    score_text = elem.get_attribute("score") or "0"
                    post_data["score"] = self._parse_score(score_text)

                    created_str = elem.get_attribute("created-timestamp") or "0"
                    try:
                        post_data["created_utc"] = int(float(created_str))
                    except (ValueError, TypeError):
                        post_data["created_utc"] = 0

                    post_data["selftext"] = elem.get_attribute("post-selftext") or ""

                    posts.append(post_data)
                except Exception as e:
                    logger.debug(f"Error extracting post: {e}")
                    continue
        except Exception as e:
            logger.error(f"Error finding posts: {e}")

        return posts

    def _parse_score(self, score_text: str) -> int:
        try:
            score_text = score_text.replace(",", "").replace("k", "000").replace("K", "000")
            if "k" in score_text.lower():
                return int(float(score_text.lower().replace("k", "")) * 1000)
            return int(score_text)
        except (ValueError, AttributeError):
            return 0

    async def _extract_comments(self, permalink: str, ticker_set: Set[str]) -> List[RedditCommentPayload]:
        comments = []
        try:
            comment_url = f"{permalink}?sort=new"
            # Load the page without waiting for specific elements
            success = await self.session.throttled_get(comment_url, wait_for=None, timeout=15)
            if not success:
                return comments

            # Wait a bit for comments to load
            import time
            time.sleep(2)
            
            comment_elements = self.session.get_sb().find_elements("shreddit-comment")
            for elem in comment_elements:
                try:
                    if elem.get_attribute("deleted") == "true" or elem.get_attribute("removed") == "true":
                        continue

                    text_elem = elem.find_element("css selector", "div[slot='comment'] p, p[slot='comment']")
                    if not text_elem:
                        text_elem = elem.find_element("css selector", "p")
                    if not text_elem:
                        continue

                    comment_text = text_elem.text
                    comment_tickers = self._extract_tickers(comment_text)
                    relevant_tickers = [t for t in comment_tickers if t in ticker_set]

                    if not relevant_tickers:
                        continue

                    score_text = elem.get_attribute("score") or "0"
                    score = self._parse_score(score_text)

                    created_str = elem.get_attribute("created-timestamp") or "0"
                    try:
                        created_utc = int(float(created_str))
                    except (ValueError, TypeError):
                        created_utc = 0

                    subreddit = ""
                    try:
                        sub_elem = elem.find_element("css selector", "a[slot='subreddit']")
                        if sub_elem:
                            subreddit = sub_elem.text.replace("r/", "")
                    except Exception:
                        pass

                    for ticker in relevant_tickers:
                        comments.append(RedditCommentPayload(
                            ticker=ticker,
                            text=comment_text,
                            subreddit=subreddit,
                            created_utc=created_utc,
                            score=score
                        ))
                except Exception as e:
                    logger.debug(f"Error extracting comment: {e}")
                    continue

        except Exception as e:
            logger.error(f"Error extracting comments from {permalink}: {e}")

        return comments


async def create_reddit_scraper(config_dict: dict = None, scraper_config: ScraperConfig = None) -> RedditScraper:
    return RedditScraper(config_dict, scraper_config)


async def create_old_reddit_scraper(config_dict: dict = None, scraper_config: ScraperConfig = None) -> RedditScraper:
    return RedditScraper(config_dict, scraper_config)


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()

    async def test():
        async with await create_reddit_scraper() as scraper:
            async for comment in scraper.harvest_raw_commentary(["AAPL", "TSLA"], limit_per_subreddit=5):
                print(f"[{comment.subreddit}] {comment.ticker}: {comment.text[:100]}...")

    asyncio.run(test())