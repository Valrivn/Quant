"""Lane Gamma pipeline tests — flat data structure, deterministic bounds, temporal alignment."""

import json
import math
import sqlite3
import time
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Any
from unittest.mock import patch, MagicMock, AsyncMock

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config" / "hybrid_config.yaml"
CF_MEMORY_PATH = PROJECT_ROOT / "config" / "cloudflare_strategy_memory.json"


@pytest.fixture(autouse=True)
def _patch_db_path(tmp_path):
    """Redirect DB_PATH to a temp file so tests don't touch production DB."""
    test_db = tmp_path / "test_reddit_quant.db"
    import opencode_scripts.lanes.lane_gamma.ingest_all as ig
    og_path = ig.DB_PATH
    ig.DB_PATH = test_db
    yield
    ig.DB_PATH = og_path


@pytest.fixture
def ingest_module():
    from opencode_scripts.lanes.lane_gamma import ingest_all as ig
    return ig


@pytest.fixture
def mock_conn(ingest_module, tmp_path):
    db_path = tmp_path / "test_reddit_quant.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    ingest_module.DB_PATH = db_path
    ingest_module.create_lane_gamma_tables(conn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS glassdoor_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            rating REAL,
            created_at INTEGER NOT NULL,
            UNIQUE(ticker, date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_aggregations (
            ticker TEXT,
            date TEXT,
            category TEXT,
            subreddit TEXT,
            mention_count INTEGER,
            avg_sentiment REAL,
            weighted_sum REAL,
            total_weight REAL,
            PRIMARY KEY (ticker, date, category, subreddit)
        )
    """)
    conn.commit()
    yield conn
    conn.close()


class TestTanhClamp:
    def test_tanh_clamp_bounds(self, ingest_module):
        ig = ingest_module
        for z in [-1e6, -100, -10, -1, 0, 1, 10, 100, 1e6]:
            clamped = ig.tanh_clamp(z)
            assert -1.0 <= clamped <= 1.0, f"tanh_clamp({z}) = {clamped} out of bounds"
            assert clamped == ig.tanh_clamp(z), "deterministic"

    def test_tanh_clamp_zero(self, ingest_module):
        assert ingest_module.tanh_clamp(0.0) == 0.0

    def test_tanh_clamp_monotonic(self, ingest_module):
        inputs = [-100, -10, -1, -0.1, 0, 0.1, 1, 10, 100]
        outputs = [ingest_module.tanh_clamp(z) for z in inputs]
        assert all(outputs[i] <= outputs[i + 1] for i in range(len(outputs) - 1))

    def test_tanh_clamp_unit_bounds(self, ingest_module):
        for z in [-1e6, 0, 1e6]:
            u = ingest_module.tanh_clamp_unit(z)
            assert 0.0 <= u <= 1.0, f"tanh_clamp_unit({z}) = {u} out of [0,1]"

    def test_tanh_clamp_unit_zero(self, ingest_module):
        assert ingest_module.tanh_clamp_unit(0.0) == 0.5


class TestGuardFunctions:
    def test_guard_float_none(self, ingest_module):
        assert ingest_module.guard_float(None) == 0.0

    def test_guard_float_nan(self, ingest_module):
        assert ingest_module.guard_float(float("nan")) == 0.0

    def test_guard_float_inf(self, ingest_module):
        assert ingest_module.guard_float(float("inf")) == 0.0

    def test_guard_float_valid(self, ingest_module):
        assert ingest_module.guard_float(42.5) == 42.5
        assert ingest_module.guard_float("3.14") == 3.14

    def test_guard_float_default(self, ingest_module):
        assert ingest_module.guard_float(None, default=-1.0) == -1.0

    def test_guard_int_none(self, ingest_module):
        assert ingest_module.guard_int(None) == 0

    def test_guard_int_valid(self, ingest_module):
        assert ingest_module.guard_int(42) == 42

    def test_guard_int_nan(self, ingest_module):
        assert ingest_module.guard_int(float("nan")) == 0


class TestBoundedZScore:
    def test_bounded_z_score_zero(self, ingest_module):
        z = ingest_module.bounded_z_score(0, 0, 1)
        assert -1.0 < z < 1.0
        assert z == 0.0

    def test_bounded_z_score_extreme(self, ingest_module):
        z = ingest_module.bounded_z_score(1e6, 0, 1)
        assert -1.0 <= z <= 1.0
        assert z > 0.99

    def test_bounded_z_score_negative(self, ingest_module):
        z = ingest_module.bounded_z_score(-1e6, 0, 1)
        assert -1.0 <= z <= 1.0
        assert z < -0.99

    def test_bounded_z_score_std_guard(self, ingest_module):
        z = ingest_module.bounded_z_score(10, 0, 0)
        assert -1.0 <= z <= 1.0


class TestExtractGlassdoorRating:
    def test_star_rating(self, ingest_module):
        html = '<html>4.2 ★ based on reviews</html>'
        assert ingest_module._extract_glassdoor_rating(html) == 4.2

    def test_out_of_5(self, ingest_module):
        html = '<html>Rating: 3.8 out of 5</html>'
        assert ingest_module._extract_glassdoor_rating(html) == 3.8

    def test_rating_num_class(self, ingest_module):
        html = '<html><span class="ratingNum">4.5</span></html>'
        assert ingest_module._extract_glassdoor_rating(html) == 4.5

    def test_overall_rating_json(self, ingest_module):
        html = '<html>overallRating: 4.1</html>'
        assert ingest_module._extract_glassdoor_rating(html) == 4.1

    def test_no_rating(self, ingest_module):
        html = '<html>No rating found here</html>'
        assert ingest_module._extract_glassdoor_rating(html) is None


class TestConfigLoading:
    def test_load_config(self, ingest_module):
        cfg = ingest_module.load_config()
        assert "companies" in cfg
        assert isinstance(cfg["companies"], dict)

    def test_get_company_config(self, ingest_module):
        cfg = ingest_module.load_config()
        nvda = ingest_module.get_company_config(cfg, "NVDA")
        assert nvda.get("sec_cik") == "0001045810"
        assert nvda.get("github_org") == "NVIDIA"

    def test_get_company_config_amzn(self, ingest_module):
        cfg = ingest_module.load_config()
        amzn = ingest_module.get_company_config(cfg, "AMZN")
        assert amzn.get("github_org") == "amzn"
        assert amzn.get("sec_cik") == "0001018724"

    def test_get_company_config_intc(self, ingest_module):
        cfg = ingest_module.load_config()
        intc = ingest_module.get_company_config(cfg, "INTC")
        assert intc.get("sec_cik") == "0000050863"
        assert intc.get("github_org") == "intel"
        assert intc.get("glassdoor_slug") == "intel"

    def test_get_tickers(self, ingest_module):
        tickers = ingest_module.get_tickers()
        for t in ["NVDA", "AVGO", "INTC", "AMD", "MSFT", "GOOGL", "META", "TSLA", "AAPL", "AMZN"]:
            assert t in tickers, f"{t} missing from tickers: {tickers}"


class TestPublicationLagMatrix:
    def test_lag_matrix_defaults(self, ingest_module):
        lag = ingest_module.PublicationLagMatrix()
        assert lag.lag_for("reddit_velocity") == 0
        assert lag.lag_for("employee_sentiment") == 3

    def test_lag_matrix_adjust(self, ingest_module):
        lag = ingest_module.PublicationLagMatrix()
        from datetime import datetime, timezone, timedelta
        base = datetime(2026, 6, 1, tzinfo=timezone.utc)
        adjusted = lag.adjust_timestamp("employee_sentiment", base)
        assert adjusted == base + timedelta(days=3)

    def test_lag_matrix_zero_lag(self, ingest_module):
        lag = ingest_module.PublicationLagMatrix()
        base = datetime(2026, 6, 1, tzinfo=timezone.utc)
        adjusted = lag.adjust_timestamp("reddit_velocity", base)
        assert adjusted == base

    def test_lag_from_config(self, ingest_module):
        cfg = ingest_module.load_config()
        lag = ingest_module.PublicationLagMatrix.from_config(cfg)
        assert isinstance(lag, ingest_module.PublicationLagMatrix)


class TestCoverageMatrix:
    def test_build_coverage_matrix_structure(self, ingest_module, mock_conn):
        ig = ingest_module
        ig.create_lane_gamma_tables(mock_conn)

        tickers = ["NVDA", "AVGO", "INTC"]
        for t in tickers:
            mock_conn.execute("""
                INSERT OR IGNORE INTO sec_xbrl_facts
                (ticker, cik, accession_number, filing_date, fact_name, fact_value, unit, segment, fiscal_year, fiscal_period, source_url, fetched_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (t, "CIK", "ACC", "2025-01-15", "Revenues", 1000.0, "USD", "", 2024, "FY", "url", int(time.time())))
            mock_conn.execute("""
                INSERT OR IGNORE INTO github_org_metrics
                (ticker, org_name, repo_name, stars, forks, open_issues, watchers, language, description, topics, created_at_api, updated_at_api, fetched_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (t, "org", f"org/{t}", 100, 50, 5, 200, "Python", "desc", "[]", "2024-01-01T00:00:00Z", "2025-01-01T00:00:00Z", int(time.time())))
            mock_conn.execute("""
                INSERT OR REPLACE INTO glassdoor_snapshots (ticker, date, rating, created_at)
                VALUES (?, ?, ?, ?)
            """, (t, "2026-06-29", 4.2, int(time.time())))
            mock_conn.execute("""
                INSERT OR IGNORE INTO daily_aggregations (ticker, date, category, subreddit, mention_count, avg_sentiment, weighted_sum, total_weight)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (t, "2026-06-28", "tech_product", "wallstreetbets", 50, 0.3, 15.0, 50.0))
        mock_conn.commit()

        reddit_report = {t: {"total_records": 100, "latest_date": "2026-06-28", "categories": 4, "subreddits": 3, "status": "OK"} for t in tickers}
        glassdoor_results: Dict[str, Optional[float]] = {t: 4.2 for t in tickers}

        matrix = ig.build_coverage_matrix(tickers, 6, 9, reddit_report, glassdoor_results)
        assert len(matrix) == len(tickers)
        for t in tickers:
            assert matrix[t]["sec_status"] == "OK"
            assert matrix[t]["github_status"] == "OK"
            assert matrix[t]["reddit_status"] == "OK"
            assert matrix[t]["glassdoor_current"] == "OK"
            assert matrix[t]["glassdoor_current_rating"] == 4.2

    def test_coverage_matrix_missing_ticker(self, ingest_module, mock_conn):
        ig = ingest_module
        ig.create_lane_gamma_tables(mock_conn)

        tickers = ["NVDA", "AVGO"]
        reddit_report = {t: {"total_records": 0, "latest_date": None, "status": "NO_DATA"} for t in tickers}
        glassdoor_results: Dict[str, Optional[float]] = {}

        matrix = ig.build_coverage_matrix(tickers, 0, 0, reddit_report, glassdoor_results)
        for t in tickers:
            assert matrix[t]["sec_status"] == "MISSING"
            assert matrix[t]["reddit_status"] == "MISSING"
            assert matrix[t]["glassdoor_current"] == "BLOCKED_403"


class TestAuditReport:
    def test_write_audit_md_creates_file(self, ingest_module, tmp_path):
        ig = ingest_module
        tickers = ["NVDA", "AVGO"]
        matrix = {
            "NVDA": {"sec_xbrl_records": 5, "sec_status": "OK", "github_repos": 50, "github_status": "OK",
                     "glassdoor_records": 10, "glassdoor_current": "OK", "glassdoor_current_rating": 4.2,
                     "reddit_agg_records": 1000, "reddit_status": "OK", "reddit_latest_date": "2026-06-28"},
            "AVGO": {"sec_xbrl_records": 0, "sec_status": "MISSING", "github_repos": 0, "github_status": "MISSING",
                     "glassdoor_records": 0, "glassdoor_current": "BLOCKED_403", "glassdoor_current_rating": None,
                     "reddit_agg_records": 0, "reddit_status": "MISSING", "reddit_latest_date": "N/A"},
        }
        ig.write_audit_md(tickers, matrix, 5, 50, 123.4)

        audit_path = ig.PROJECT_ROOT / "lane_results" / "data_completeness_audit.md"
        assert audit_path.exists()
        content = audit_path.read_text()
        assert "NVDA" in content
        assert "AVGO" in content
        assert "4/4 sources" in content
        assert "123.4" in content
        assert "tanh_clamp" in content
        assert "PublicationLagMatrix" in content
        assert "CF Bypass Strategy 3" in content


class TestCFStrategyMemory:
    def test_log_cf_strategy_performance(self, ingest_module, tmp_path):
        ig = ingest_module
        og_path = ig.CF_MEMORY_PATH
        test_cf_path = tmp_path / "cloudflare_strategy_memory.json"
        ig.CF_MEMORY_PATH = test_cf_path

        try:
            ig.log_cf_strategy_performance(
                sec_errors=["AVGO: no CIK"],
                github_errors=[],
                glassdoor_results={"NVDA": 4.2, "AVGO": None, "INTC": 3.8},
                elapsed=45.67,
            )
            assert test_cf_path.exists()
            memory = json.loads(test_cf_path.read_text())
            assert "distributed_serp_api" in memory
            dsa = memory["distributed_serp_api"]
            assert "performance_metrics" in dsa
            pm = dsa["performance_metrics"]
            assert pm["glassdoor_success_rate"] >= 0.0
            assert pm["last_run_elapsed_seconds"] == 45.7
            assert "last_run_timestamp" in pm
        finally:
            ig.CF_MEMORY_PATH = og_path


class TestMockRun:
    @pytest.mark.asyncio
    async def test_main_mock_only_returns_structure(self, ingest_module):
        ig = ingest_module
        result = await ig.main_mock_only()
        assert "tickers" in result
        assert "sec_total" in result
        assert "github_total" in result
        assert "reddit_report" in result
        assert "glassdoor_results" in result
        assert len(result["tickers"]) >= 10
        assert result["sec_total"] > 0
        assert result["github_total"] > 0

    @pytest.mark.asyncio
    async def test_main_mock_only_db_tables(self, ingest_module):
        ig = ingest_module
        result = await ig.main_mock_only()

        conn = ig.get_conn()
        try:
            sec_count = conn.execute("SELECT COUNT(*) FROM sec_xbrl_facts").fetchone()[0]
            github_count = conn.execute("SELECT COUNT(*) FROM github_org_metrics").fetchone()[0]
            glassdoor_count = conn.execute("SELECT COUNT(*) FROM glassdoor_snapshots").fetchone()[0]

            assert sec_count == result["sec_total"]
            assert github_count == result["github_total"]
            assert glassdoor_count == len(result["tickers"])
        finally:
            conn.close()

    @pytest.mark.asyncio
    async def test_main_mock_only_deterministic(self, ingest_module):
        ig = ingest_module
        r1 = await ig.main_mock_only()
        r2 = await ig.main_mock_only()
        assert r1["sec_total"] == r2["sec_total"]
        assert r1["github_total"] == r2["github_total"]


class TestXBRLFactNames:
    def test_xbrl_fact_names_includes_avgo_fallback(self, ingest_module):
        assert "RevenueFromContractWithCustomer" in ingest_module.XBRL_FACT_NAMES
        assert "RevenueFromContractWithCustomerExcludingAssessedTax" in ingest_module.XBRL_FACT_NAMES
        assert "Revenue" in ingest_module.XBRL_FACT_NAMES


class TestUserAgents:
    def test_user_agents_are_diverse(self, ingest_module):
        assert len(ingest_module.USER_AGENTS) >= 4
        uas = ingest_module.USER_AGENTS
        assert any("Chrome" in ua for ua in uas)
        assert any("Safari" in ua for ua in uas)
        assert any("Macintosh" in ua for ua in uas)


class TestGuardDivisionEdgeCases:
    def test_bounded_z_score_std_guard_not_nan(self, ingest_module):
        for _ in range(100):
            z = ingest_module.bounded_z_score(5.0, 0, 0)
            assert not math.isnan(z)
            assert not math.isinf(z)

    def test_guard_float_string_edge(self, ingest_module):
        assert ingest_module.guard_float("not_a_number") == 0.0
        assert ingest_module.guard_float("") == 0.0

    def test_guard_int_float_string(self, ingest_module):
        assert ingest_module.guard_int(3.14) == 3
        assert ingest_module.guard_int("42") == 42
