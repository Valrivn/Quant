# Reddit Content Policy & User Agreement Compliance Guide

This document outlines the guidelines and architectural safe rails implemented in this codebase to ensure compliance with the **Reddit Content Policy**, **Reddit User Agreement**, and general web scraping standards.

---

## ⚖️ Core Compliance Principles

### 1. API-Free Scraped Feeds & Throttling
- **Protocol:** The scraping client fetches public JSON endpoints (e.g., `https://www.reddit.com/r/{subreddit}.json`).
- **Sleep Delay:** An explicit **2.0-second delay** (`time.sleep(2.0)`) is executed between each subreddit HTTP call. This respects Reddit's servers, prevents server overloading, and mirrors normal browser consumption patterns.
- **Limits:** Scraping is strictly capped at **50 posts per sub per run**. No heavy deep-traversals of historical posts are conducted during daily runs.

### 2. User Privacy & Non-PII Storage
- **No Personal Identifiable Information (PII):** We do **not** collect or store author names, profile details, comments containing username logs, geolocation data, or private message streams.
- **Captured Data:** We only persist the `submission.id`, `title`, `selftext`, metadata counts (`score`, `upvote_ratio`, `num_comments`), and timestamp metrics necessary for aggregation.

### 3. Data Retention Lifecycle
- **Retention Limit:** A raw data retention job (`db/jobs.py`) is implemented to automatically purge raw posts older than **30 days**. This aligns with storage optimization and prevents long-term archival of Reddit user text content.

### 4. Intellectual Property & Commercial Usage
- **Purpose:** Sentiment features extracted are aggregated mathematically (`avg_sentiment`, `weighted_sum`). Raw submissions are never re-published, redistributed, or sold.
- **Usage:** Sentiment indexes represent derived metrics rather than direct copies of Reddit posts.

---

## 🛠️ Compliance Maintenance Checklist
- [x] Rotate User-Agents regularly.
- [x] Do not bypass authentication blocks (rely strictly on public JSON views).
- [x] Run retention jobs daily or weekly.
