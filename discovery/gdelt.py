"""GDELT News Sentiment alternative-data fetcher.

Queries GDELT DOC 2.0 API for large-cap tickers and company names,
extracts article data, and writes to data/gdelt_news.csv.
"""

import os
import csv
import time
import urllib.parse
import requests
from typing import List, Dict, Tuple, Optional, Any

LARGE_CAPS = [
    ("AAPL", "Apple"),
    ("MSFT", "Microsoft"),
    ("GOOGL", "Alphabet"),
    ("AMZN", "Amazon"),
    ("NVDA", "NVIDIA"),
    ("META", "Meta"),
    ("TSLA", "Tesla"),
    ("LLY", "Eli Lilly"),
    ("AVGO", "Broadcom"),
    ("JPM", "JPMorgan Chase"),
    ("V", "Visa"),
    ("UNH", "UnitedHealth"),
    ("MA", "Mastercard"),
    ("WMT", "Walmart"),
    ("HD", "Home Depot"),
    ("PG", "Procter & Gamble"),
    ("JNJ", "Johnson & Johnson"),
    ("COST", "Costco"),
    ("MRK", "Merck"),
    ("ORCL", "Oracle"),
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
}

CSV_FILE = "data/gdelt_news.csv"
CSV_COLUMNS = [
    "ticker", "company", "url", "title", "seendate", "domain", "sourcecountry", "sentiment_placeholder"
]


class GDELTAbortError(Exception):
    """Exception raised to abort the GDELT pipeline due to persistent network or rate limit issues."""
    pass


class GDELTRateLimitError(Exception):
    """Exception raised when GDELT API returns 429 rate limit persistently."""
    pass


def request_with_retry(url: str, timeout: float = 30.0, max_retries: int = 4) -> requests.Response:
    """Helper to request a GDELT URL, retrying with exponential backoff.

    GDELT enforces one request per 5 seconds; backoff starts above that.
    Raises GDELTAbortError on persistent failures.
    """
    delay = 6.0
    last_exc = None
    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=HEADERS, timeout=timeout)
            if response.status_code == 429:
                if attempt == max_retries - 1:
                    raise GDELTRateLimitError("GDELT API rate limit hit persistently (HTTP 429)")
                time.sleep(delay)
                delay *= 1.5
                continue
            return response
        except (requests.RequestException, Exception) as e:
            last_exc = e
            if attempt == max_retries - 1:
                raise GDELTAbortError(f"GDELT API request failed persistently: {e}")
            time.sleep(delay)
            delay *= 1.5
    raise GDELTAbortError(f"GDELT API request failed persistently. Last error: {last_exc}")


def fetch_gdelt_news(ticker: str, company_name: str) -> List[Dict[str, Any]]:
    """Fetch news articles from GDELT DOC 2.0 API for a given company name & ticker."""
    query = f'"{company_name}" OR "{ticker}" sourcelang:english'
    encoded_query = urllib.parse.quote(query)
    url = f"https://api.gdeltproject.org/api/v2/doc/doc?query={encoded_query}&mode=artlist&format=json&maxrecords=250&timespan=3m"
    
    res = request_with_retry(url)
    res.raise_for_status()
    
    try:
        data = res.json()
    except ValueError:
        # Handle case where it doesn't return JSON
        return []
        
    articles = data.get("articles", [])
    parsed = []
    for art in articles:
        url_val = art.get("url")
        if not url_val:
            continue
        
        # GDELT occasionally returns tone, sentiment, etc.
        # We store tone as a placeholder or 0.0
        tone = art.get("tone")
        sentiment = 0.0
        if tone is not None:
            try:
                sentiment = float(tone)
            except (ValueError, TypeError):
                pass
                
        parsed.append({
            "ticker": ticker,
            "company": company_name,
            "url": url_val,
            "title": art.get("title"),
            "seendate": art.get("seendate"),
            "domain": art.get("domain"),
            "sourcecountry": art.get("sourcecountry"),
            "sentiment_placeholder": sentiment
        })
    return parsed


def read_existing_news() -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Read existing news records from CSV."""
    existing = {}
    if not os.path.exists(CSV_FILE):
        return existing
    try:
        with open(CSV_FILE, mode="r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = (row.get("url"), row.get("ticker"))
                existing[key] = row
    except Exception:
        pass
    return existing


def write_news(news: Dict[Tuple[str, str], Dict[str, Any]]):
    """Write news records back to CSV."""
    os.makedirs(os.path.dirname(CSV_FILE), exist_ok=True)
    with open(CSV_FILE, mode="w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in sorted(news.values(), key=lambda x: (x.get("ticker", ""), x.get("seendate", ""), x.get("url", ""))):
            writer.writerow({col: row.get(col) for col in CSV_COLUMNS})


def run_pipeline(tickers_input: Optional[List[Tuple[str, str]]] = None) -> int:
    """Run the GDELT news sentiment pipeline."""
    if tickers_input is None:
        tickers_input = LARGE_CAPS

    existing = read_existing_news()
    new_count = 0

    for ticker, company in tickers_input:
        try:
            articles = fetch_gdelt_news(ticker, company)
            for art in articles:
                key = (art["url"], art["ticker"])
                if key not in existing:
                    existing[key] = art
                    new_count += 1
        except (GDELTAbortError, GDELTRateLimitError) as e:
            print(f"Skipping {ticker} ({company}): {e}")
            time.sleep(6.0)
            continue
        except Exception as e:
            print(f"Error fetching news for {ticker} ({company}): {e}")
        
        # Polite rate limit (GDELT: one request per 5s; use 12s headroom to
        # avoid triggering its temporary IP block)
        time.sleep(12.0)

    write_news(existing)
    return new_count


if __name__ == "__main__":
    print("Starting GDELT news sentiment pipeline...")
    count = run_pipeline()
    print(f"Pipeline complete. New/updated news records written: {count}")
