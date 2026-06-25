# Doables: Reddit & WallStreetBets Sentiment Scraper

This document provides actionable steps to implement the **Reddit & WallStreetBets Sentiment Scraper** module. This tool will fetch posts, analyze sentiment, and identify potential geopolitical/supply-chain warnings mentioned in retail trading forums.

---

## 📋 Module Overview

* **Objective**: Automatically scrape recent posts from `r/wallstreetbets` and filter/score mentions of specific stock tickers, computing a public sentiment score and extracting potential risk factors.
* **Tech Stack**: Python, `praw` (Python Reddit API Wrapper) or standard `requests` (using Reddit JSON endpoints), and `nltk` (VADER sentiment analysis).

---

## 🛠️ Step-by-Step Implementation Plan

### Step 1: Install Required Libraries
Install the necessary dependencies for web requests, JSON parsing, and natural language sentiment analysis:
```bash
pip install praw nltk pandas
```

### Step 2: Set Up Reddit API Credentials
1. Go to [reddit.com/prefs/apps](https://www.reddit.com/prefs/apps).
2. Click **create another app...** (select **script**).
3. Record the `client_id`, `client_secret`, and `user_agent`.
4. Store these as environment variables or a configuration file.

### Step 3: Implement the Scraper Script
Create a new script named `reddit_sentiment.py` in your workspace:

```python
import os
import praw
from nltk.sentiment.vader import SentimentIntensityAnalyzer
import nltk
import pandas as pd

# Download VADER lexicon
nltk.download('vader_lexicon', quiet=True)

class RedditScraper:
    def __init__(self):
        # Fallback to demo mode or environment variables
        self.reddit = praw.Reddit(
            client_id=os.getenv("REDDIT_CLIENT_ID", "your_client_id"),
            client_secret=os.getenv("REDDIT_CLIENT_SECRET", "your_client_secret"),
            user_agent=os.getenv("REDDIT_USER_AGENT", "quant-sentiment-v1.0")
        )
        self.sia = SentimentIntensityAnalyzer()

    def analyze_ticker(self, ticker: str, limit: int = 50) -> dict:
        """
        Scrapes r/wallstreetbets, filters for the given ticker, and calculates
        average sentiment score and key terms.
        """
        subreddit = self.reddit.subreddit("wallstreetbets")
        mentions = []
        scores = []
        risk_warnings = []
        
        # Keywords for geopolitical or supply chain warnings
        risk_keywords = ["china", "taiwan", "war", "supply", "shortage", "tariff", "chip", "semiconductor"]
        
        for submission in subreddit.hot(limit=limit):
            title = submission.title.upper()
            body = submission.selftext.upper()
            
            # Check if ticker is mentioned
            if f" {ticker.upper()} " in f" {title} " or f" {ticker.upper()} " in f" {body} ":
                text = f"{submission.title} {submission.selftext}"
                sentiment = self.sia.polarity_scores(text)
                scores.append(sentiment['compound'])
                
                # Check for risk/geopolitical warnings
                found_risks = [kw for kw in risk_keywords if kw in text.lower()]
                if found_risks:
                    risk_warnings.append({
                        "title": submission.title,
                        "url": submission.url,
                        "detected_risks": found_risks
                    })
                    
                mentions.append({
                    "title": submission.title,
                    "score": submission.score,
                    "sentiment": sentiment['compound']
                })
        
        avg_sentiment = sum(scores) / len(scores) if scores else 0.0
        return {
            "ticker": ticker,
            "mention_count": len(mentions),
            "average_sentiment": avg_sentiment,
            "risk_warnings": risk_warnings,
            "mentions": mentions
        }

if __name__ == "__main__":
    # Quick Test
    scraper = RedditScraper()
    # print(scraper.analyze_ticker("NVDA"))
```

### Step 4: Integrate with your Streamlit App (`stream_quant.py`)
Add a new tab or expander in your Streamlit app to run and display the Reddit scraper outputs:
1. Import the `RedditScraper` class.
2. Add a sub-section: `st.subheader("Social Sentiment Analysis")`.
3. Render a metric block for **Reddit Sentiment** (e.g., `-1.0` to `+1.0`).
4. Display a warning list if any geopolitical or supply chain risk keywords are detected.
