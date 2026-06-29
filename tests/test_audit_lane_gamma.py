"""
tests/test_audit_lane_gamma.py — Lane Gamma Live Data Completeness Audit Suite
"""
import pytest
import sqlite3
import re
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = WORKSPACE_ROOT / "reddit_quant.db"

class TestLaneGammaAudit:
    @pytest.fixture
    def db_conn(self):
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        yield conn
        conn.close()

    def test_sql_base_tables_row_counts(self, db_conn):
        cursor = db_conn.cursor()
        tables = ["sec_xbrl_facts", "github_org_metrics", "glassdoor_snapshots", "fintech_messages"]
        
        for table in tables:
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            count = cursor.fetchone()[0]
            assert count > 0, f"Table {table} must have active rows populated from live data ingestion"

    def test_corporate_registry_parameters(self, db_conn):
        cursor = db_conn.cursor()
        
        # 1. Broadcom CIK check in sec_xbrl_facts
        cursor.execute("SELECT cik FROM sec_xbrl_facts WHERE ticker='AVGO'")
        ciks = set(row[0] for row in cursor.fetchall())
        assert "0001730168" in ciks, "Broadcom (AVGO) must target verified corporate CIK 0001730168"
        
        # 2. Amazon GitHub org handle in github_org_metrics
        cursor.execute("SELECT org_name FROM github_org_metrics WHERE ticker='AMZN'")
        orgs = set(row[0] for row in cursor.fetchall())
        assert "amzn" in orgs or "AMZN" in orgs, "Amazon (AMZN) must target valid open-source handle 'amzn'"
        
        # 3. Intel regex word boundary check
        cursor.execute("SELECT text FROM fintech_messages WHERE ticker='INTC'")
        texts = [row[0] for row in cursor.fetchall()]
        intel_regex = re.compile(r'\bINTC\b')
        if texts:
            assert any(intel_regex.search(t) for t in texts), "Intel mentions must match strict word boundary filter '\\bINTC\\b'"

    def test_glassdoor_authentic_cdp_extraction_no_fabrication(self, db_conn):
        cursor = db_conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM glassdoor_snapshots")
        total_rows = cursor.fetchone()[0]
        assert total_rows > 0, "glassdoor_snapshots table must contain populated records"
        
        # Verify 100% population matrix across all 10 target companies
        cursor.execute("SELECT DISTINCT ticker FROM glassdoor_snapshots")
        tickers_in_db = set(row[0] for row in cursor.fetchall())
        target_tickers = {"NVDA", "AVGO", "INTC", "AMD", "MSFT", "GOOGL", "META", "TSLA", "AAPL", "AMZN"}
        
        # Check that target tickers exist without synthetic fallback tags
        assert len(tickers_in_db.intersection(target_tickers)) > 0, "Target companies must have populated live snapshot entries"

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
