"""Script to run production Google Trends company/product scraper test run."""

import argparse
import json
import logging
import sys
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from ingestion.data_lake import DataLakeStore
from ingestion.google_trends import (
    CompanySpec,
    ParallelGoogleTrendsPipeline,
    ProxyPool,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Run Production Google Trends Company Scraper")
    parser.add_argument(
        "--config",
        type=str,
        default=str(PROJECT_ROOT / "config" / "google_trends_config.json"),
        help="Path to configuration JSON file",
    )
    parser.add_argument(
        "--companies",
        type=int,
        default=3,
        help="Number of sample companies to run (default 3)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=2,
        help="Number of concurrent worker threads",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate keyword framework and task plan without sending external network requests",
    )

    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        logger.error(f"Config file not found: {config_path}")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        config_data = json.load(f)

    # Parse company specs
    comp_list = config_data.get("sample_companies", [])[: args.companies]
    companies = []
    for c_item in comp_list:
        spec = CompanySpec(
            ticker=c_item["ticker"],
            name=c_item["name"],
            sector=c_item["sector"],
            ceo=c_item.get("ceo"),
            products=c_item.get("products", []),
            competitors=c_item.get("competitors", []),
            custom_keywords=c_item.get("custom_keywords", {}),
        )
        companies.append(spec)

    print("=" * 70)
    print("PRODUCTION GOOGLE TRENDS COMPANY/PRODUCT SCRAPER")
    print("=" * 70)
    print(f"Loaded {len(companies)} company specification(s):")
    for comp in companies:
        print(f"  - [{comp.ticker}] {comp.name} ({comp.sector}) | Products: {comp.products[:3]}")

    if args.dry_run:
        print("\n--- DRY RUN: KEYWORD FRAMEWORK GENERATION ---")
        from ingestion.google_trends import KeywordGenerator
        for comp in companies:
            tiers = KeywordGenerator.generate_keywords(comp, config_data.get("tier_limits"))
            print(f"\n[{comp.ticker}] Keyword Tiers:")
            for t_name, kws in tiers.items():
                print(f"  * {t_name.upper()}: {kws}")
        print("\nDry run completed successfully.")
        return

    # Anti-scrape proxy setup
    proxy_urls = config_data.get("proxies", [])
    proxy_pool = ProxyPool(
        proxy_urls=proxy_urls,
        max_requests_per_hour_per_proxy=config_data.get("scraper_settings", {}).get("max_requests_per_hour_per_proxy", 100),
    )

    data_lake = DataLakeStore()
    pipeline = ParallelGoogleTrendsPipeline(
        data_lake=data_lake,
        proxy_pool=proxy_pool,
        max_workers=args.workers,
        min_delay=config_data.get("scraper_settings", {}).get("min_delay_sec", 2.0),
        max_delay=config_data.get("scraper_settings", {}).get("max_delay_sec", 6.0),
    )

    print("\nStarting execution pipeline...")
    report = pipeline.run_company_pipeline(
        companies=companies,
        geos=config_data.get("default_geos", ["US"]),
        timeframes=config_data.get("default_timeframes", ["today 5-y"]),
        tier_limits=config_data.get("tier_limits"),
    )

    print("\n" + "=" * 70)
    print("SCRAPER RUN SUMMARY REPORT")
    print("=" * 70)
    print(f"Status:          {report['status']}")
    print(f"Total Tasks:     {report['total_tasks']}")
    print(f"Skipped Tasks:   {report['skipped_tasks']} (already completed in checkpoint)")
    print(f"Completed Tasks: {report['completed_tasks']}")
    print(f"Failed Tasks:    {report['failed_tasks']}")
    print(f"Saved Datasets:  {report['saved_file_count']}")

    print("\nCataloged Series in Data Lake:")
    catalog = data_lake.list_series(domain="google_trends")
    for item in catalog[:10]:
        print(f"  - [{item['domain']}] {item['series_id']} ({item['row_count']} rows) -> {item['file_path']}")


if __name__ == "__main__":
    main()
