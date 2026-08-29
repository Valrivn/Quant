"""Greenhouse and Lever job postings fetcher for alternative-data quant pipeline.

Fetches hiring signals from open/public Greenhouse and Lever endpoints.
Deduplicates results and appends/saves to data/job_postings.csv.
"""

import os
import csv
import time
import requests
from datetime import datetime, timezone
from typing import List, Dict, Optional, Any

# Verification: default well-known US tech companies list with confirmed endpoints
DEFAULT_COMPANIES = [
    {"company": "stripe", "source": "greenhouse", "ticker": None},
    {"company": "airbnb", "source": "greenhouse", "ticker": "ABNB"},
    {"company": "databricks", "source": "greenhouse", "ticker": None},
    {"company": "palantir", "source": "lever", "ticker": "PLTR"},
    {"company": "pinterest", "source": "greenhouse", "ticker": "PINS"},
    {"company": "hubspot", "source": "greenhouse", "ticker": "HUBS"},
    {"company": "roblox", "source": "greenhouse", "ticker": "RBLX"},
    {"company": "datadog", "source": "greenhouse", "ticker": "DDOG"},
    {"company": "figma", "source": "greenhouse", "ticker": None},
    {"company": "toast", "source": "greenhouse", "ticker": "TOST"},
    {"company": "lyft", "source": "greenhouse", "ticker": "LYFT"},
    {"company": "reddit", "source": "greenhouse", "ticker": "RDDT"},
    {"company": "spotify", "source": "lever", "ticker": "SPOT"},
    {"company": "pagerduty", "source": "greenhouse", "ticker": "PD"},
    {"company": "affirm", "source": "greenhouse", "ticker": "AFRM"},
    {"company": "coursera", "source": "greenhouse", "ticker": "COUR"},
    {"company": "elastic", "source": "greenhouse", "ticker": "ESTC"},
    {"company": "twilio", "source": "greenhouse", "ticker": "TWLO"},
    {"company": "plaid", "source": "lever", "ticker": None},
    {"company": "gusto", "source": "greenhouse", "ticker": None},
    {"company": "mongodb", "source": "greenhouse", "ticker": "MDB"},
    {"company": "okta", "source": "greenhouse", "ticker": "OKTA"},
    {"company": "instacart", "source": "greenhouse", "ticker": "CART"},
    {"company": "robinhood", "source": "greenhouse", "ticker": "HOOD"},
    {"company": "gitlab", "source": "greenhouse", "ticker": "GTLB"},
]

HEADERS = {
    "User-Agent": "Quant-Alternative-Data-Fetcher/1.0 (contact@example.com)"
}

CSV_FILE = "data/job_postings.csv"
CSV_COLUMNS = [
    "source", "company", "ticker", "job_id", "title", "location", "first_published", "scraped_at"
]


def request_with_retry(url: str, timeout: float = 10.0, max_retries: int = 5) -> Optional[requests.Response]:
    """Helper to request a URL, retrying with exponential backoff on HTTP 429."""
    delay = 1.0
    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=HEADERS, timeout=timeout)
            if response.status_code == 429:
                time.sleep(delay)
                delay *= 2
                continue
            return response
        except (requests.RequestException, Exception):
            if attempt == max_retries - 1:
                raise
            time.sleep(delay)
            delay *= 2
    return None


def fetch_greenhouse_jobs(company_token: str) -> List[Dict[str, Any]]:
    """Fetch jobs from Greenhouse public API."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{company_token}/jobs?content=false"
    res = request_with_retry(url)
    if not res:
        return []
    if res.status_code == 404:
        return []
    res.raise_for_status()
    data = res.json()
    jobs = data.get("jobs", [])
    parsed = []
    scraped_at = datetime.now(timezone.utc).isoformat()
    for job in jobs:
        job_id = str(job.get("id"))
        title = job.get("title")
        location_data = job.get("location")
        if isinstance(location_data, dict):
            location = location_data.get("name")
        else:
            location = location_data
        
        # Greenhouse has first_published or updated_at
        first_published = job.get("first_published") or job.get("updated_at")
        
        parsed.append({
            "source": "greenhouse",
            "company": company_token,
            "job_id": job_id,
            "title": title,
            "location": location,
            "first_published": first_published,
            "scraped_at": scraped_at
        })
    return parsed


def fetch_lever_jobs(company_token: str) -> List[Dict[str, Any]]:
    """Fetch jobs from Lever public API."""
    url = f"https://api.lever.co/v0/postings/{company_token}?mode=json"
    res = request_with_retry(url)
    if not res:
        return []
    if res.status_code == 404:
        return []
    res.raise_for_status()
    data = res.json()
    parsed = []
    scraped_at = datetime.now(timezone.utc).isoformat()
    for item in data:
        job_id = str(item.get("id"))
        title = item.get("text") or item.get("title")
        categories = item.get("categories", {})
        location = categories.get("location") if isinstance(categories, dict) else None
        
        created_at_ms = item.get("createdAt")
        first_published = None
        if created_at_ms is not None:
            try:
                first_published = datetime.fromtimestamp(created_at_ms / 1000.0, tz=timezone.utc).isoformat()
            except Exception:
                first_published = str(created_at_ms)
        
        parsed.append({
            "source": "lever",
            "company": company_token,
            "job_id": job_id,
            "title": title,
            "location": location,
            "first_published": first_published,
            "scraped_at": scraped_at
        })
    return parsed


def parse_date(date_str: Optional[str]) -> datetime:
    """Parse date strings to datetime objects for comparison, defaulting to epoch if error/none."""
    if not date_str:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        # Standard ISO 8601 parsing handles Z, offsets
        # replace Z with +00:00 to support Python 3.10 and earlier fromisoformat in some OSes
        clean_str = date_str.replace("Z", "+00:00")
        return datetime.fromisoformat(clean_str)
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def read_existing_postings() -> Dict[tuple, Dict[str, Any]]:
    """Read existing job postings from CSV."""
    existing = {}
    if not os.path.exists(CSV_FILE):
        return existing
    try:
        with open(CSV_FILE, mode="r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = (row.get("source"), row.get("job_id"))
                if key not in existing:
                    existing[key] = row
                else:
                    # Keep the one with earliest first_published
                    dt_existing = parse_date(existing[key].get("first_published"))
                    dt_new = parse_date(row.get("first_published"))
                    if dt_new < dt_existing:
                        existing[key] = row
    except Exception:
        # If corrupt or unreadable, start fresh
        pass
    return existing


def write_postings(postings: Dict[tuple, Dict[str, Any]]):
    """Write job postings back to CSV."""
    os.makedirs(os.path.dirname(CSV_FILE), exist_ok=True)
    with open(CSV_FILE, mode="w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in sorted(postings.values(), key=lambda x: (x.get("source", ""), x.get("company", ""), x.get("job_id", ""))):
            # Ensure fields match CSV_COLUMNS
            writer.writerow({col: row.get(col) for col in CSV_COLUMNS})


def run_pipeline(companies_input: Optional[List[Any]] = None) -> int:
    """Run the job board fetching pipeline.
    
    Accepts a list of inputs:
    - Dicts/tuples with {"company", "source", "ticker"}
    - Or strings (will try to resolve source/ticker from default list, or fallback to trying both APIs).
    """
    if companies_input is None:
        companies_input = DEFAULT_COMPANIES

    # Resolve company inputs
    resolved_companies = []
    for item in companies_input:
        if isinstance(item, dict):
            resolved_companies.append({
                "company": item["company"],
                "source": item.get("source"),
                "ticker": item.get("ticker")
            })
        elif isinstance(item, tuple):
            resolved_companies.append({
                "company": item[0],
                "source": item[1] if len(item) > 1 else None,
                "ticker": item[2] if len(item) > 2 else None
            })
        elif isinstance(item, str):
            # Check default list first
            match = next((d for d in DEFAULT_COMPANIES if d["company"] == item), None)
            if match:
                resolved_companies.append(match)
            else:
                resolved_companies.append({
                    "company": item,
                    "source": None,
                    "ticker": None
                })

    existing = read_existing_postings()
    new_count = 0

    for comp_info in resolved_companies:
        company = comp_info["company"]
        source = comp_info["source"]
        ticker = comp_info["ticker"]

        jobs = []
        if source == "greenhouse":
            try:
                jobs = fetch_greenhouse_jobs(company)
            except Exception as e:
                print(f"Error fetching {company} from greenhouse: {e}")
        elif source == "lever":
            try:
                jobs = fetch_lever_jobs(company)
            except Exception as e:
                print(f"Error fetching {company} from lever: {e}")
        else:
            # Try both if source not specified
            try:
                jobs = fetch_greenhouse_jobs(company)
                if jobs:
                    source = "greenhouse"
            except Exception:
                pass
            if not jobs:
                try:
                    jobs = fetch_lever_jobs(company)
                    if jobs:
                        source = "lever"
                except Exception:
                    pass

        # Update ticker for all fetched jobs if available
        for job in jobs:
            job["ticker"] = ticker
            key = (job["source"], job["job_id"])
            if key not in existing:
                existing[key] = job
                new_count += 1
            else:
                # Keep earliest first_published
                dt_existing = parse_date(existing[key].get("first_published"))
                dt_new = parse_date(job.get("first_published"))
                if dt_new < dt_existing:
                    existing[key] = job
                    new_count += 1

        # Polite rate limiting (>=1s sleep)
        time.sleep(1.0)

    write_postings(existing)
    return new_count


def slugify_token(name: str) -> str:
    """Slugify a company name into a candidate job-board token."""
    stop = {
        "inc", "incorporated", "corp", "corporation", "plc", "ltd", "limited",
        "llc", "co", "company", "group", "holdings", "holding", "the",
        "international", "intl", "technologies", "technology", "solutions",
    }
    words = [w.strip(".,'&()-") for w in name.lower().split()]
    words = [w for w in words if w and w not in stop]
    token = "-".join(words)
    token = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in token)
    while "--" in token:
        token = token.replace("--", "-")
    return token.strip("-")


HARVEST_PROGRESS_FILE = "data/job_harvest_progress.json"


def load_harvest_progress() -> Dict[str, str]:
    """Load already-probed candidate tokens -> status ('hit'/'miss')."""
    import json

    try:
        with open(HARVEST_PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_harvest_progress(progress: Dict[str, str]):
    """Persist probed-candidate status."""
    import json

    with open(HARVEST_PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f)


def build_candidates_from_tickers(
    csv_path: str = "data/ticker_master.csv",
) -> List[Dict[str, Any]]:
    """Derive unique candidate board tokens from the ticker master table."""
    candidates: Dict[str, Dict[str, Any]] = {}
    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("is_etf") == "1":
                continue
            token = slugify_token(row.get("company_name", ""))
            if not token or len(token) < 3 or token in candidates:
                continue
            candidates[token] = {"company": token, "source": None, "ticker": row["ticker"]}
    return list(candidates.values())


def harvest_from_tickers(
    flush_every: int = 25,
    max_candidates: int = 0,
    csv_path: str = "data/ticker_master.csv",
) -> int:
    """Probe Greenhouse/Lever for every ticker-derived candidate token.

    Progress persists after each company and the postings CSV is flushed
    every ``flush_every`` companies so partial runs are never lost.
    Returns total new/updated rows.
    """
    candidates = build_candidates_from_tickers(csv_path)
    progress = load_harvest_progress()
    todo = [c for c in candidates if c["company"] not in progress]
    if max_candidates:
        todo = todo[:max_candidates]

    existing = read_existing_postings()
    new_count = 0
    since_flush = 0

    for i, comp in enumerate(todo, 1):
        token = comp["company"]
        status = "miss"
        jobs: List[Dict[str, Any]] = []
        try:
            jobs = fetch_greenhouse_jobs(token)
            if jobs:
                comp["source"] = status = "greenhouse"
        except Exception:
            jobs = []
        if not jobs:
            try:
                jobs = fetch_lever_jobs(token)
                if jobs:
                    comp["source"] = status = "lever"
            except Exception:
                jobs = []

        for job in jobs:
            job["ticker"] = comp["ticker"]
            key = (job["source"], job["job_id"])
            if key not in existing:
                existing[key] = job
                new_count += 1
            else:
                dt_new = parse_date(job.get("first_published"))
                dt_old = parse_date(existing[key].get("first_published"))
                if dt_new < dt_old:
                    existing[key] = job

        progress[token] = status
        since_flush += 1
        if since_flush >= flush_every:
            write_postings(existing)
            save_harvest_progress(progress)
            print(f"[harvest {i}/{len(todo)}] rows={len(existing)} (+{new_count})")
            since_flush = 0

        time.sleep(1.0)

    write_postings(existing)
    save_harvest_progress(progress)
    return new_count


if __name__ == "__main__":
    import sys

    if "--harvest" in sys.argv:
        idx = sys.argv.index("--harvest")
        limit = int(sys.argv[idx + 1]) if len(sys.argv) > idx + 1 else 0
        print(f"Harvesting job boards from ticker master (limit={limit or 'all'})...")
        count = harvest_from_tickers(max_candidates=limit)
        print(f"Harvest complete. New/updated jobs written: {count}")
    else:
        print("Starting Greenhouse & Lever jobs pipeline...")
        count = run_pipeline()
        print(f"Pipeline complete. New/updated jobs written: {count}")
