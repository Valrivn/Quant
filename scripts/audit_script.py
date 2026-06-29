#!/usr/bin/env python3

"""
Comprehensive audit script for the Psychological Pillar implementation.
Validates all components against specification requirements.
"""

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

def check_database_schema():
    """Verify all required tables exist with correct schema."""
    print("Checking database schema...")
    conn = sqlite3.connect("reddit_quant.db")
    cursor = conn.cursor()
    
    required_tables = [
        "watchlist",
        "daily_aggregations", 
        "psychological_vectors",
        "psychological_regimes",
        "quantitative_dcf_floor",
        "velocity_snapshots",
        "adzuna_job_snapshots",
        "glassdoor_snapshots",
        "comparably_snapshots",
        "hiring_velocity_snapshots",
        "product_intel_reviews",
        "g2_capterra_reviews",
        "app_store_feeds",
        "jobspy_velocity",
        "validation_gate_results",
        "glassdoor_comparably_audit",
        "fintech_messages",
        "signal_provenance",
        "hybrid_scrape_runs",
        "regime_states",
        "bayes_posteriors",
        "velocity_provenance",
        "submissions",
        "scrape_state",
        "composite_scores",
        "risk_signals",
        "sentiment_runs",
        "weight_versions",
        "schema_migrations"
    ]
    
    missing = []
    for table in required_tables:
        cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'")
        if not cursor.fetchone():
            missing.append(table)
    
    conn.close()
    
    if missing:
        print(f"  ❌ MISSING TABLES: {missing}")
        return False
    else:
        print(f"  ✅ All {len(required_tables)} required tables exist")
        return True

def check_data_seeding():
    """Verify historical data was seeded."""
    print("Checking data seeding...")
    conn = sqlite3.connect("reddit_quant.db")
    cursor = conn.cursor()
    
    checks = [
        ("watchlist", "SELECT COUNT(*) FROM watchlist", 10),
        ("daily_aggregations", "SELECT COUNT(*) FROM daily_aggregations", 1000),
        ("psychological_vectors", "SELECT COUNT(*) FROM psychological_vectors", 500),
        ("psychological_regimes", "SELECT COUNT(*) FROM psychological_regimes", 5000),
        ("quantitative_dcf_floor", "SELECT COUNT(*) FROM quantitative_dcf_floor", 1000),
        ("velocity_snapshots", "SELECT COUNT(*) FROM velocity_snapshots", 500),
        ("hiring_velocity_snapshots", "SELECT COUNT(*) FROM hiring_velocity_snapshots", 1000),
        ("product_intel_reviews", "SELECT COUNT(*) FROM product_intel_reviews", 1000),
        ("glassdoor_snapshots", "SELECT COUNT(*) FROM glassdoor_snapshots", 1000),
        ("comparably_snapshots", "SELECT COUNT(*) FROM comparably_snapshots", 1000),
    ]
    
    all_pass = True
    for name, query, min_count in checks:
        cursor.execute(query)
        count = cursor.fetchone()[0]
        if count >= min_count:
            print(f"  ✅ {name}: {count} rows")
        else:
            print(f"  ❌ {name}: {count} rows (expected >= {min_count})")
            all_pass = False
    
    conn.close()
    return all_pass

def check_orchestrator_imports():
    """Verify all orchestrator imports work."""
    print("Checking orchestrator imports...")
    try:
        from psychological.orchestrator import PsychologicalOrchestrator, create_psychological_orchestrator
        from psychological.nlp_engine import NLPEngine, create_nlp_engine
        from psychological.velocity_tracker import VelocityTracker, create_velocity_tracker
        from psychological.state_machine import PsychologicalStateMachine, create_state_machine
        from psychological.behavioral_feature_store import BehavioralFeatureStore, create_behavioral_feature_store
        from psychological.signal_matrix import SignalMatrix, create_signal_matrix
        from psychological.scrapers import (
            RedditScraper, create_old_reddit_scraper,
            GitHubTracker, create_github_tracker,
            CorpAnonymousScraper, create_corp_anonymous_scraper,
            ProductIntelEngine, create_product_intel_engine,
            CrossValidationEngine, create_cross_validation_engine,
            ScraperConfig
        )
        from psychological.scrapers.validation_gate import CrossValidationGate
        from psychological.scrapers.cross_validation import CrossValidationEngine as CVEngine
        print("  ✅ All orchestrator imports successful")
        return True
    except Exception as e:
        print(f"  ❌ Import error: {e}")
        return False

def check_key_files_exist():
    """Verify all key implementation files exist."""
    print("Checking key files...")
    required_files = [
        "psychological/__init__.py",
        "psychological/interfaces.py",
        "psychological/nlp_engine.py",
        "psychological/velocity_tracker.py",
        "psychological/state_machine.py",
        "psychological/behavioral_feature_store.py",
        "psychological/data_fusion.py",
        "psychological/signal_matrix.py",
        "psychological/dcf_floor.py",
        "psychological/engineering_guards.py",
        "psychological/orchestrator.py",
        "psychological/scrapers/__init__.py",
        "psychological/scrapers/lightweight_scraper.py",
        "psychological/scrapers/reddit_custom.py",
        "psychological/scrapers/github_tracker.py",
        "psychological/scrapers/corp_anonymous.py",
        "psychological/scrapers/corp_audit.py",
        "psychological/scrapers/hiring_velocity.py",
        "psychological/scrapers/product_intel.py",
        "psychological/scrapers/validation_gate.py",
        "psychological/scrapers/cross_validation.py",
        "config/constants.py",
        "config/hybrid_config.yaml",
        "config/hybrid_weights.yaml",
        "scripts/seed_historical.py",
        ".github/workflows/ci.yml",
    ]
    
    missing = []
    for f in required_files:
        if not Path(f).exists():
            missing.append(f)
    
    if missing:
        print(f"  ❌ Missing files: {missing}")
        return False
    else:
        print(f"  ✅ All {len(required_files)} key files exist")
        return True

def check_config_structure():
    """Verify configuration structure matches spec."""
    print("Checking config structure...")
    import yaml
    
    with open("config/hybrid_config.yaml") as f:
        config = yaml.safe_load(f)
    
    required_sections = [
        "endpoints", "retry", "scenarios", "fallback", "fusion",
        "optimization", "github_mappings", "adzuna", "psychological",
        "glassdoor", "comparably", "validation_gate", "hiring_velocity",
        "g2_capterra", "app_store", "proxy_pool", "quantitative_dcf"
    ]
    
    missing = [s for s in required_sections if s not in config]
    
    if missing:
        print(f"  ❌ Missing config sections: {missing}")
        return False
    
    psych = config.get("psychological", {})
    required_psych = ["bullish_terms", "bearish_terms", "velocity_windows", "regime_thresholds", "fusion_weights"]
    missing_psych = [p for p in required_psych if p not in psych]
    
    if missing_psych:
        print(f"  ❌ Missing psychological config: {missing_psych}")
        return False
    
    print("  ✅ Config structure matches specification")
    return True

def check_ci_workflow():
    """Verify CI workflow exists and has required jobs."""
    print("Checking CI workflow...")
    import yaml
    
    with open(".github/workflows/ci.yml") as f:
        workflow = yaml.safe_load(f)
    
    required_jobs = ["test", "lint", "audit"]
    jobs = workflow.get("jobs", {})
    missing = [j for j in required_jobs if j not in jobs]
    
    if missing:
        print(f"  ❌ Missing CI jobs: {missing}")
        return False
    
    # Check test job has coverage
    test_job = jobs.get("test", {})
    steps = test_job.get("steps", [])
    has_coverage = any("cov" in str(step) for step in steps)
    
    if not has_coverage:
        print("  ❌ Test job missing coverage check")
        return False
    
    print("  ✅ CI workflow has all required jobs with coverage")
    return True

def run_integration_test():
    """Run a quick integration test."""
    print("Running integration test...")
    import asyncio
    import sys
    sys.path.insert(0, '.')
    
    async def test():
        from psychological.orchestrator import create_psychological_orchestrator
        
        orchestrator = await create_psychological_orchestrator()
        
        # Test regime status
        status = orchestrator.get_regime_status("AAPL")
        if not status:
            print("  ❌ Failed to get regime status for AAPL")
            return False
        
        # Test fused confidence computation
        from psychological.interfaces import RegimeOutput
        regime = RegimeOutput(regime="PANIC_CAPITULATION", contrarian_buy_authorized=True, confidence=0.8)
        fused = orchestrator.compute_fused_confidence(regime, 0.7, 0.6)
        if not (0 <= fused <= 1):
            print(f"  ❌ Fused confidence out of bounds: {fused}")
            return False
        
        # Test DCF signal
        dcf_data = orchestrator.get_dcf_floor_data("AAPL")
        if not dcf_data:
            print("  ❌ Failed to get DCF data")
            return False
        
        # Test cross-validation
        from psychological.scrapers.cross_validation import create_cross_validation_engine
        engine = create_cross_validation_engine()
        result = engine.evaluate_all_layers(
            ticker="AAPL",
            regime_data={"employee_sentiment_proxy": 0.3, "comparably_badge_score": 0.85, "jobspy_zscore_1y": 1.5, "dev_velocity": 0.8, "product_sentiment_proxy": 0.2, "confidence_score": 0.75},
            fintech_sentiment=0.65,
            reddit_bull_bear_ratio=2.5,
            dev_velocity=0.8,
            quant_value=0.7
        )
        
        if "combined_penalty" not in result:
            print("  ❌ Cross-validation missing combined_penalty")
            return False
        
        print("  ✅ Integration test passed")
        return True
    
    return asyncio.run(test())

def main():
    print("=" * 60)
    print("COMPREHENSIVE AUDIT - Psychological Pillar Implementation")
    print("=" * 60)
    
    checks = [
        ("Database Schema", check_database_schema),
        ("Data Seeding", check_data_seeding),
        ("Orchestrator Imports", check_orchestrator_imports),
        ("Key Files Exist", check_key_files_exist),
        ("Config Structure", check_config_structure),
        ("CI Workflow", check_ci_workflow),
        ("Integration Test", run_integration_test),
    ]
    
    results = []
    for name, check_fn in checks:
        try:
            result = check_fn()
            results.append((name, result))
        except Exception as e:
            print(f"  ❌ {name} check failed with exception: {e}")
            results.append((name, False))
    
    print("\n" + "=" * 60)
    print("AUDIT SUMMARY")
    print("=" * 60)
    
    all_pass = True
    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"  {status}: {name}")
        if not result:
            all_pass = False
    
    print("=" * 60)
    if all_pass:
        print("🎉 ALL CHECKS PASSED - Implementation validated!")
        return 0
    else:
        print("⚠️  SOME CHECKS FAILED - Review required")
        return 1

if __name__ == "__main__":
    sys.exit(main())