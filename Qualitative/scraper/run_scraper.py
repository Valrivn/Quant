#!/usr/bin/env python3
"""
Main CLI entry point for the Reddit Quant Sentiment Pipeline.

This module provides a unified command-line interface for running the various
components of the sentiment analysis pipeline in a modular fashion.

Usage:
    python run_scraper.py scrape          # Run full scraping pipeline
    python run_scraper.py scrape --subreddit wallstreetbets  # Scrape specific subreddit
    python run_scraper.py aggregate       # Run daily aggregation job
    python run_scraper.py purge           # Purge old submissions (>30 days)
    python run_scraper.py backtest        # Run walk-forward backtest
    python run_scraper.py optimize        # Run Bayesian weight optimization
    python run_scraper.py drift           # Check for IC drift and re-optimize
    python run_scraper.py init-db         # Initialize database schema
"""

import argparse
import sys
import logging
from pathlib import Path

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.logging_config import setup_logging, get_logger

logger = get_logger(__name__)

from db.connection import init_db
from db.jobs import purge_old_submissions, run_daily_aggregation
from scraper.reddit_client import RedditUniversalScraper
from backtesting.backtest import run_walk_forward_backtest
from optimization.optuna_search import run_bayesian_optimization, save_optimized_weights_as_challenger
from backtesting.drift_detection import check_ic_drift_and_reoptimize
from config import load_weights, load_hybrid_config, load_fintech_credentials
from psychological.orchestrator import create_psychological_orchestrator


def cmd_scrape(args):
    """Run the full scraping pipeline."""
    logger.debug("Starting Reddit scraping pipeline...")
    scraper = RedditUniversalScraper()
    
    if args.subreddit:
        logger.info(f"Scraping single subreddit: r/{args.subreddit}")
        config = load_weights()
        subreddit_taxonomy = config["subreddit_weights"]
        category_weights = config["category_weights"]
        
        found = False
        for category, subs in subreddit_taxonomy.items():
            if args.subreddit in subs:
                # We'd need to add a method to scrape a single subreddit
                logger.warning("Single subreddit scraping not yet implemented. Running full scrape.")
                found = True
                break
        if not found:
            logger.error(f"Subreddit r/{args.subreddit} not found in taxonomy")
            return 1
    
    scraper.scrape_all()
    logger.info("Scraping pipeline completed.")
    return 0


def cmd_aggregate(args):
    """Run daily aggregation job."""
    logger.debug("Running daily aggregation...")
    date_str = args.date if args.date else None
    run_daily_aggregation(date_str)
    logger.info("Daily aggregation completed.")
    return 0


def cmd_purge(args):
    """Purge old submissions."""
    logger.debug(f"Purging submissions older than {args.days} days...")
    count = purge_old_submissions(args.days)
    logger.info(f"Purged {count} old submissions.")
    return 0


def cmd_backtest(args):
    """Run walk-forward backtest."""
    logger.debug("Running walk-forward backtest...")
    config = load_weights()
    category_weights = config["category_weights"]
    subreddit_weights = config["subreddit_weights"]
    
    results = run_walk_forward_backtest(
        category_weights=category_weights,
        subreddit_weights=subreddit_weights,
        lookback_days=args.lookback,
        objective=args.objective
    )
    
    logger.info(f"Backtest Results:")
    logger.info(f"  IC: {results['ic']:.4f}")
    logger.info(f"  Sharpe: {results['sharpe']:.4f}")
    logger.info(f"  Hit Rate: {results['hit_rate']:.4f}")
    return 0


def cmd_optimize(args):
    """Run Bayesian weight optimization."""
    logger.debug("Running Bayesian weight optimization...")
    results = run_bayesian_optimization(trials=args.trials, objective_metric=args.metric)
    save_optimized_weights_as_challenger(results)
    
    logger.info("Optimization Results:")
    logger.info(f"  Category Weights: {results['category_weights']}")
    logger.info(f"  IC: {results['metrics']['ic']:.4f}")
    logger.info(f"  Sharpe: {results['metrics']['sharpe']:.4f}")
    logger.info(f"  Hit Rate: {results['metrics']['hit_rate']:.4f}")
    return 0


def cmd_drift(args):
    """Check for IC drift and trigger re-optimization if needed."""
    logger.debug("Checking for IC drift...")
    result = check_ic_drift_and_reoptimize(
        decay_threshold=args.threshold,
        recent_window_days=args.window
    )

    reason = result.get("reason", "")
    if reason:
        logger.info(f"Drift check skipped: {reason}")
    elif result["drift_detected"]:
        decay = result.get("decay", 0.0)
        logger.warning(f"Drift detected: {decay:.2%} decay")
        logger.info(f"New challenger Sharpe: {result['new_challenger_sharpe']:.4f}")
    else:
        decay = result.get("decay", 0.0)
        logger.info(f"No drift detected. Decay: {decay:.2%}")
    return 0


def cmd_init_db(args):
    """Initialize database schema."""
    logger.debug("Initializing database...")
    init_db()
    logger.info("Database initialized.")
    return 0


def cmd_scrape_fintech(args):
    """Scrape only fintech APIs (StockTwits, ApeWisdom)."""
    logger.debug("Starting fintech API scraping...")
    from scraper.hybrid_orchestrator import HybridOrchestrator
    import asyncio
    
    orchestrator = HybridOrchestrator()
    tickers = args.tickers.split(",") if args.tickers else None
    results = asyncio.run(orchestrator._scrape_fintech_only(tickers))
    
    for source, result in results.items():
        logger.info(f"{source}: {result.messages_count} messages, tickers: {result.tickers_found}")
    logger.info("Fintech scraping completed.")
    return 0


def cmd_scrape_hybrid(args):
    """Run full hybrid orchestration (fintech primary + reddit fallback)."""
    logger.debug("Starting hybrid orchestration...")
    from scraper.hybrid_orchestrator import HybridOrchestrator
    import asyncio
    
    orchestrator = HybridOrchestrator()
    tickers = args.tickers.split(",") if args.tickers else None
    results = asyncio.run(orchestrator.scrape_all(tickers))
    
    for source, result in results.items():
        logger.info(f"{source}: {result.messages_count} messages, tickers: {result.tickers_found}")
    logger.info("Hybrid orchestration completed.")
    return 0


def cmd_health_check(args):
    """Check health of all data sources."""
    logger.debug("Checking health of all sources...")
    from scraper.fintech_clients.factory import FintechClientFactory
    import asyncio
    
    factory = FintechClientFactory()
    health = asyncio.run(factory.health_check_all())
    
    for source, h in health.items():
        status = "HEALTHY" if h.is_healthy else "UNHEALTHY"
        logger.info(f"{source}: {status} (failures: {h.consecutive_failures}, rate_limit_remaining: {h.rate_limit_remaining})")
    return 0


def cmd_circuit_status(args):
    """Show circuit breaker states."""
    logger.debug("Checking circuit breaker states...")
    from scraper.health_monitor import HealthMonitor
    
    monitor = HealthMonitor()
    status = monitor.get_all_status()
    
    for source, s in status.items():
        logger.info(f"{source}: {s['circuit_state']} | Success: {s['success_rate_100']:.1%} | Latency: {s['avg_latency_ms']:.0f}ms | Can Execute: {s['can_execute']}")
    return 0


def cmd_scrape_psychological(args):
    """Run psychological pillar pipeline (Reddit primary + GitHub + Adzuna)."""
    logger.debug("Starting psychological pipeline...")
    import asyncio
    
    orchestrator = asyncio.run(create_psychological_orchestrator())
    tickers = args.tickers.split(",") if args.tickers else None
    results = asyncio.run(orchestrator.run_full_pipeline(tickers or ["AAPL", "MSFT", "NVDA", "TSLA", "GOOGL", "AMD", "META", "INTC"]))
    
    logger.info(f"Primary result: {results['primary_result']}")
    logger.info(f"Corporate affinities: {results['corporate_affinities']}")
    for ticker, result in results['fused_results'].items():
        logger.info(f"{ticker}: regime={result.get('active_regime')}, contrarian_buy={result.get('contrarian_buy_authorized')}, confidence={result.get('confidence_score'):.2f}, fused_conf={result.get('fused_confidence'):.2f}")
    logger.info("Psychological pipeline completed.")
    return 0


def cmd_regime_status(args):
    """Get current regime status for ticker(s)."""
    logger.debug("Getting regime status...")
    import asyncio
    
    orchestrator = asyncio.run(create_psychological_orchestrator())
    tickers = args.tickers.split(",") if args.tickers else ["AAPL", "MSFT", "NVDA", "TSLA", "GOOGL", "AMD", "META", "INTC"]
    
    for ticker in tickers:
        status = orchestrator.get_regime_status(ticker)
        if status:
            logger.info(f"{ticker}: regime={status['active_regime']}, contrarian_buy={status['contrarian_buy_authorized']}, confidence={status['confidence_score']:.2f}, fused_conf={status.get('fused_confidence', 0):.2f}, ratio={status.get('bull_bear_ratio'):.2f}, vel_sigma={status.get('velocity_sigma'):.2f}")
        else:
            logger.info(f"{ticker}: No regime data available")
    return 0


def cmd_export_parquet(args):
    """Export psychological data to Parquet for backtesting."""
    logger.debug("Exporting psychological data to Parquet...")
    from psychological.behavioral_feature_store import create_behavioral_feature_store
    
    store = create_behavioral_feature_store()
    start_date = args.start_date
    end_date = args.end_date
    
    if not start_date or not end_date:
        logger.error("--start-date and --end-date are required")
        return 1
    
    vectors_file = store.export_parquet(start_date, end_date)
    regimes_file = store.export_regimes_parquet(start_date, end_date)
    
    logger.info(f"Exported vectors to: {vectors_file}")
    logger.info(f"Exported regimes to: {regimes_file}")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Reddit Quant Sentiment Pipeline CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    def add_log_level(parser):
        parser.add_argument("--log-level", type=str, default="INFO",
                           choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                           help="Logging level")
    
    # scrape command
    scrape_parser = subparsers.add_parser("scrape", help="Run scraping pipeline")
    scrape_parser.add_argument("--subreddit", type=str, help="Scrape specific subreddit only")
    add_log_level(scrape_parser)
    
    # aggregate command
    agg_parser = subparsers.add_parser("aggregate", help="Run daily aggregation job")
    agg_parser.add_argument("--date", type=str, help="Date to aggregate (YYYY-MM-DD)")
    add_log_level(agg_parser)
    
    # purge command
    purge_parser = subparsers.add_parser("purge", help="Purge old submissions")
    purge_parser.add_argument("--days", type=int, default=30, help="Retention days")
    add_log_level(purge_parser)
    
    # backtest command
    bt_parser = subparsers.add_parser("backtest", help="Run walk-forward backtest")
    bt_parser.add_argument("--lookback", type=int, default=180, help="Lookback days")
    bt_parser.add_argument("--objective", type=str, default="information_coefficient",
                          choices=["information_coefficient", "sharpe", "hit_rate"])
    add_log_level(bt_parser)
    
    # optimize command
    opt_parser = subparsers.add_parser("optimize", help="Run Bayesian weight optimization")
    opt_parser.add_argument("--trials", type=int, default=50, help="Optuna trials")
    opt_parser.add_argument("--metric", type=str, default="sharpe",
                           choices=["sharpe", "information_coefficient", "hit_rate"])
    add_log_level(opt_parser)
    
    # drift command
    drift_parser = subparsers.add_parser("drift", help="Check IC drift and re-optimize")
    drift_parser.add_argument("--threshold", type=float, default=0.20, help="Decay threshold")
    drift_parser.add_argument("--window", type=int, default=60, help="Recent window days")
    add_log_level(drift_parser)
    
    # init-db command
    init_db_parser = subparsers.add_parser("init-db", help="Initialize database schema")
    add_log_level(init_db_parser)
    
    # scrape-fintech command
    fintech_parser = subparsers.add_parser("scrape-fintech", help="Scrape only fintech APIs (StockTwits, ApeWisdom)")
    fintech_parser.add_argument("--tickers", type=str, help="Comma-separated tickers to scrape")
    add_log_level(fintech_parser)
    
    # scrape-hybrid command
    hybrid_parser = subparsers.add_parser("scrape-hybrid", help="Run full hybrid orchestration (fintech primary + reddit fallback)")
    hybrid_parser.add_argument("--tickers", type=str, help="Comma-separated tickers to scrape")
    add_log_level(hybrid_parser)
    
    # health-check command
    health_parser = subparsers.add_parser("health-check", help="Check health of all data sources")
    add_log_level(health_parser)
    
    # circuit-status command
    circuit_parser = subparsers.add_parser("circuit-status", help="Show circuit breaker states")
    add_log_level(circuit_parser)
    
    # scrape-psychological command
    psych_parser = subparsers.add_parser("scrape-psychological", help="Run psychological pillar pipeline (Reddit + GitHub + Adzuna)")
    psych_parser.add_argument("--tickers", type=str, help="Comma-separated tickers to process")
    add_log_level(psych_parser)
    
    # regime-status command
    regime_parser = subparsers.add_parser("regime-status", help="Get current regime status for ticker(s)")
    regime_parser.add_argument("--tickers", type=str, help="Comma-separated tickers to check")
    add_log_level(regime_parser)
    
    # export-parquet command
    export_parser = subparsers.add_parser("export-parquet", help="Export psychological data to Parquet for backtesting")
    export_parser.add_argument("--start-date", type=str, required=True, help="Start date (YYYY-MM-DD)")
    export_parser.add_argument("--end-date", type=str, required=True, help="End date (YYYY-MM-DD)")
    add_log_level(export_parser)
    
    args = parser.parse_args()
    
    # Setup logging
    log_level = getattr(logging, args.log_level.upper(), logging.INFO)
    setup_logging(level=log_level, log_file="logs/quant_pipeline.log", console=True)
    
    if not args.command:
        parser.print_help()
        return 1
    
    commands = {
        "scrape": cmd_scrape,
        "aggregate": cmd_aggregate,
        "purge": cmd_purge,
        "backtest": cmd_backtest,
        "optimize": cmd_optimize,
        "drift": cmd_drift,
        "init-db": cmd_init_db,
        "scrape-fintech": cmd_scrape_fintech,
        "scrape-hybrid": cmd_scrape_hybrid,
        "health-check": cmd_health_check,
        "circuit-status": cmd_circuit_status,
        "scrape-psychological": cmd_scrape_psychological,
        "regime-status": cmd_regime_status,
        "export-parquet": cmd_export_parquet,
    }
    
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())