"""Regression tests for the sentinel discovery funnel (discovery/sentinel/).

Locks in the B-20260809-003 bug fixes with no network access:
  1. G2 margin leg skips balance-only filings (NVDA 10-K row regression).
  2. per-CIK fallback coalesces candidate US-GAAP tags across fiscal ends
     (CAPEX under PaymentsToAcquireProductiveAssets).
  3. claim_batch reclaims stale 'processing' rows (crash-safe queue).
  4. G3 verdict is a dict like G1/G2 (tuple crash regression).
Plus the fail-closed contract: gates, config loader, governor circuit breaker,
and the alt-data coverage floor.
"""

import sqlite3
import time

import pandas as pd
import pytest

from discovery.sentinel import altdata_lane, gates, governor, orchestrator, queue as q
from discovery.sentinel import sec_lane
from discovery.sentinel.config import SentinelConfigError, load_sentinel_config


def _fund_rows(rows):
    return pd.DataFrame(rows)


def _qtr(fiscal_end, filed, **over):
    row = {
        "ticker": "T", "fiscal_end": fiscal_end, "filed_date": filed,
        "form": "10-Q", "qtrs": 1, "ocf": 100.0, "capex": 10.0,
        "revenue": 1000.0, "gross_profit": 500.0, "gross_margin": 0.50,
        "cash": 500.0, "current_assets": 400.0, "current_liabilities": 100.0,
        "total_assets": 1000.0, "total_liabilities": 400.0, "equity": 600.0,
        "retained_earnings": 300.0, "ebit": 120.0,
    }
    row.update(over)
    return row


# --------------------------------------------------------------------------- #
# G2 fundamentals: balance-only skip + fail-closed paths
# --------------------------------------------------------------------------- #

class TestG2Fundamentals:
    def test_balance_only_filing_does_not_break_margin_leg(self):
        # Last filing (by date) is a 10-K-style balance-only row with no
        # revenue/margin; three margin-bearing 10-Q quarters precede it.
        rows = [
            _qtr("2025-01-31", "2025-03-15", gross_margin=0.71),
            _qtr("2025-04-30", "2025-06-10", gross_margin=0.72),
            _qtr("2025-07-31", "2025-09-12", gross_margin=0.73),
            _qtr("2025-10-31", "2026-01-15", revenue=None, gross_profit=None,
                 gross_margin=None, ocf=None, capex=None),
        ]
        ok, reason, metrics = gates.g2_fundamentals(_fund_rows(rows), "2026-02-01")
        assert ok, reason
        assert metrics["margins_present"] == 3

    def test_all_balance_only_trailing_quarters_is_a_coverage_gap(self):
        rows = [
            _qtr("2025-01-31", "2025-03-15", gross_margin=0.71),
            _qtr("2025-04-30", "2025-06-10", gross_margin=None),
            _qtr("2025-07-31", "2025-09-12", gross_margin=None),
            _qtr("2025-10-31", "2026-01-15", revenue=None, gross_margin=None, ocf=None, capex=None),
        ]
        ok, reason, _ = gates.g2_fundamentals(_fund_rows(rows), "2026-02-01")
        assert not ok
        assert reason.startswith("g2:margin_coverage_gap")

    def test_low_margin_fails(self):
        rows = [
            _qtr("2025-01-31", "2025-03-15", gross_margin=0.10),
            _qtr("2025-04-30", "2025-06-10", gross_margin=0.12),
            _qtr("2025-07-31", "2025-09-12", gross_margin=0.15),
        ]
        ok, reason, _ = gates.g2_fundamentals(_fund_rows(rows), "2026-02-01")
        assert not ok
        assert reason.startswith("g2:margin<0.2")

    def test_ocf_leg_fails_closed(self):
        rows = [
            _qtr("2025-01-31", "2025-03-15", ocf=-10.0),
            _qtr("2025-04-30", "2025-06-10", ocf=-20.0),
            _qtr("2025-07-31", "2025-09-12", ocf=100.0),
            _qtr("2025-10-31", "2026-01-15", ocf=110.0),
        ]
        ok, reason, _ = gates.g2_fundamentals(_fund_rows(rows), "2026-02-01")
        assert not ok
        assert reason.startswith("g2:ocf_pos<3")

    def test_capex_untracked_fails_closed(self):
        rows = [_qtr(f"2025-0{i}-01", f"2025-0{i}-15", capex=None) for i in range(1, 5)]
        ok, reason, _ = gates.g2_fundamentals(_fund_rows(rows), "2026-02-01")
        assert not ok
        assert reason.startswith("g2:capex_untracked")

    def test_empty_fundamentals_fails_closed(self):
        ok, reason, _ = gates.g2_fundamentals(pd.DataFrame(), "2026-02-01")
        assert not ok
        assert reason == "g2:no_data"


# --------------------------------------------------------------------------- #
# G1 survival / solvency
# --------------------------------------------------------------------------- #

class TestG1Survival:
    def test_insufficient_quarters_fails_closed(self):
        rows = [_qtr("2025-01-31", "2025-03-15")]
        ok, reason, _ = gates.g1_survival_solvency(_fund_rows(rows), "2026-02-01")
        assert not ok
        assert reason.startswith("insufficient_data:1/4q")

    def test_no_cash_fails_closed(self):
        rows = [_qtr(f"2024-0{i}-01", f"2024-0{i}-15", cash=None) for i in range(1, 5)]
        ok, reason, _ = gates.g1_survival_solvency(_fund_rows(rows), "2026-02-01")
        assert not ok
        assert reason == "g1:no_cash"

    def test_low_altman_z_fails(self):
        rows = [
            _qtr(f"2024-0{i}-01", f"2024-0{i}-15", total_assets=1000.0,
                 total_liabilities=950.0, equity=50.0, current_assets=100.0,
                 current_liabilities=400.0, retained_earnings=-900.0,
                 ebit=-50.0, revenue=10.0)
            for i in range(1, 5)
        ]
        ok, reason, metrics = gates.g1_survival_solvency(_fund_rows(rows), "2026-02-01")
        assert not ok
        assert reason.startswith("g1:altman_z<1.1")
        assert metrics["altman_z"] < 1.1

    def test_healthy_company_passes(self):
        rows = [_qtr(f"2024-0{i}-01", f"2024-0{i}-15") for i in range(1, 5)]
        ok, reason, metrics = gates.g1_survival_solvency(_fund_rows(rows), "2026-02-01")
        assert ok, reason
        assert metrics["altman_z"] > 1.1

    def test_book_equity_floor_mode(self):
        # Book-equity Altman Z of ~0.95 (between the market-value 1.1 cutoff
        # and the conservative 0.6 book-equity cutoff): passes only when the
        # book-equity floor is applied.
        rows = [
            _qtr(f"2024-0{i}-01", f"2024-0{i}-15", total_assets=1000.0,
                 total_liabilities=600.0, equity=250.0, current_assets=500.0,
                 current_liabilities=400.0, retained_earnings=150.0,
                 ebit=50.0, revenue=200.0)
            for i in range(1, 5)
        ]
        ok, reason, metrics = gates.g1_survival_solvency(
            _fund_rows(rows), "2026-02-01", z_floor=1.1)
        assert not ok
        assert reason.startswith("g1:altman_z<1.1")
        ok2, reason2, metrics2 = gates.g1_survival_solvency(
            _fund_rows(rows), "2026-02-01", z_floor=1.1, z_book_equity_floor=0.6)
        assert ok2, reason2
        assert metrics2["z_floor"] == 0.6

    def test_sparse_latest_row_falls_back_to_complete_row(self):
        # companyfacts outer join can emit a fiscal end with only some fields
        # (e.g. cash only, no balance sheet). The gate must evaluate the most
        # recent COMPLETE row instead of failing on the sparse one.
        rows = [_qtr(f"2024-0{i}-01", f"2024-0{i}-15") for i in range(1, 5)]
        rows.append(_qtr("2025-01-31", "2026-02-01", revenue=None, gross_profit=None,
                         total_assets=None, total_liabilities=None, equity=None,
                         retained_earnings=None, ebit=None, current_assets=None,
                         current_liabilities=None, cash=1234.0))
        ok, reason, metrics = gates.g1_survival_solvency(_fund_rows(rows), "2026-02-01")
        assert ok, reason
        assert metrics["altman_z"] > 1.1
        assert metrics["cash"] == 1234.0


# --------------------------------------------------------------------------- #
# PIT filter: no lookahead
# --------------------------------------------------------------------------- #

class TestPitFilter:
    def test_future_filing_excluded(self):
        rows = [
            _qtr("2025-01-31", "2025-03-15"),
            _qtr("2026-06-30", "2026-07-15"),  # filed after as_of -> excluded
        ]
        out = gates.pit_filter(_fund_rows(rows), "2026-02-01")
        assert len(out) == 1
        assert out.iloc[0]["fiscal_end"] == pd.Timestamp("2025-01-31")


# --------------------------------------------------------------------------- #
# Queue: crash-safe claim_batch + attempt caps
# --------------------------------------------------------------------------- #

class TestClaimBatch:
    def _conn(self):
        return q.connect(":memory:")

    def test_reclaims_stale_processing_rows(self):
        conn = self._conn()
        now = int(time.time())
        conn.execute(
            "INSERT INTO sentinel_queue (ticker, source, source_key, stage, attempts, created_utc, updated_utc) "
            "VALUES ('STALE', 't', 's', 'processing', 0, ?, ?)",
            (now - 2000, now - 2000),
        )
        conn.execute(
            "INSERT INTO sentinel_queue (ticker, source, source_key, stage, attempts, created_utc, updated_utc) "
            "VALUES ('FRESH', 't', 'f', 'processing', 0, ?, ?)",
            (now, now),
        )
        conn.commit()
        items = q.claim_batch(conn, 10, 3)
        assert [r["ticker"] for r in items] == ["STALE"]
        conn.close()

    def test_fresh_processing_row_is_not_reclaimed(self):
        conn = self._conn()
        now = int(time.time())
        conn.execute(
            "INSERT INTO sentinel_queue (ticker, source, source_key, stage, attempts, created_utc, updated_utc) "
            "VALUES ('LIVE', 't', 'l', 'processing', 0, ?, ?)",
            (now, now),
        )
        conn.commit()
        assert q.claim_batch(conn, 10, 3) == []
        conn.close()

    def test_max_attempts_respected(self):
        conn = self._conn()
        now = int(time.time())
        conn.execute(
            "INSERT INTO sentinel_queue (ticker, source, source_key, stage, attempts, created_utc, updated_utc) "
            "VALUES ('EXHAUSTED', 't', 'e', 'pending', 3, ?, ?)",
            (now, now),
        )
        conn.commit()
        assert q.claim_batch(conn, 10, 3) == []
        conn.close()

    def test_claim_increments_attempts_and_marks_processing(self):
        conn = self._conn()
        q.enqueue(conn, "T", "cli", "k1")
        items = q.claim_batch(conn, 10, 3)
        assert [r["ticker"] for r in items] == ["T"]
        assert items[0]["stage"] == "processing"
        assert items[0]["attempts"] == 1
        conn.close()


# --------------------------------------------------------------------------- #
# Queue: dedup guard — one row per ticker (B-20260820-001)
# --------------------------------------------------------------------------- #

class TestDedupGuard:
    def _conn(self):
        return q.connect(":memory:")

    def test_enqueue_blocks_same_ticker_different_source(self):
        """A second enqueue for the same ticker (different source+key) must be
        ignored and must NOT count as a new row."""
        conn = self._conn()
        assert q.enqueue(conn, "X", "cli", "cli:X") is True
        assert q.enqueue(conn, "X", "ig", "ig:X") is False
        assert q.enqueue(conn, "X", "frontier", "frontier:X") is False
        assert conn.execute("SELECT COUNT(*) FROM sentinel_queue").fetchone()[0] == 1
        conn.close()

    def test_enqueue_allows_different_tickers(self):
        conn = self._conn()
        assert q.enqueue(conn, "A", "cli", "cli:A") is True
        assert q.enqueue(conn, "B", "cli", "cli:B") is True
        assert conn.execute("SELECT COUNT(*) FROM sentinel_queue").fetchone()[0] == 2
        conn.close()

    def test_dedup_queue_removes_later_duplicates(self):
        """dedup_queue keeps the earliest row per ticker and removes the rest.

        Simulates legacy databases that had UNIQUE(source, source_key) instead
        of UNIQUE(ticker). We temporarily drop the ticker-level constraint,
        insert duplicates, then run dedup_queue.
        """
        conn = self._conn()
        now = int(time.time())
        # Drop the ticker-level unique constraint to simulate legacy schema
        conn.execute("DROP INDEX IF EXISTS idx_sentinel_queue_ticker")
        # Also need to handle the inline UNIQUE(ticker) — rebuild without it
        conn.execute("CREATE TABLE sentinel_queue_bak AS SELECT * FROM sentinel_queue")
        conn.execute("DROP TABLE sentinel_queue")
        conn.execute(
            "CREATE TABLE sentinel_queue ("
            "    id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "    ticker TEXT NOT NULL, source TEXT NOT NULL, source_key TEXT NOT NULL,"
            "    stage TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,"
            "    last_error TEXT, raw_json TEXT,"
            "    created_utc INTEGER NOT NULL, updated_utc INTEGER NOT NULL,"
            "    UNIQUE(source, source_key)"
            ")"
        )
        conn.execute("INSERT INTO sentinel_queue SELECT * FROM sentinel_queue_bak")
        conn.execute("DROP TABLE sentinel_queue_bak")
        conn.commit()

        # Now force-insert duplicates (simulates legacy data)
        conn.execute(
            "INSERT INTO sentinel_queue (ticker, source, source_key, stage, attempts, created_utc, updated_utc) "
            "VALUES ('DUP', 'cli', 'cli:DUP', 'pending', 0, ?, ?)",
            (now - 100, now - 100),
        )
        conn.execute(
            "INSERT INTO sentinel_queue (ticker, source, source_key, stage, attempts, created_utc, updated_utc) "
            "VALUES ('DUP', 'ig', 'ig:DUP', 'pending', 0, ?, ?)",
            (now, now),
        )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM sentinel_queue WHERE ticker='DUP'").fetchone()[0] == 2

        removed = q.dedup_queue(conn)
        assert removed == 1
        row = conn.execute("SELECT id, source_key FROM sentinel_queue WHERE ticker='DUP'").fetchone()
        assert row["source_key"] == "cli:DUP"  # earliest kept
        assert conn.execute("SELECT COUNT(*) FROM sentinel_queue WHERE ticker='DUP'").fetchone()[0] == 1
        conn.close()

    def test_duplicate_enqueue_does_not_increment_queue_status(self):
        """queue_status counts must not grow when a duplicate ticker is enqueued."""
        conn = self._conn()
        q.enqueue(conn, "Q", "cli", "cli:Q")
        before = q.queue_status(conn)
        q.enqueue(conn, "Q", "ig", "ig:Q")  # duplicate — should be ignored
        after = q.queue_status(conn)
        assert before == after
        conn.close()

    def test_dedup_queue_returns_zero_when_no_duplicates(self):
        conn = self._conn()
        q.enqueue(conn, "A", "s1", "k1")
        q.enqueue(conn, "B", "s1", "k2")
        assert q.dedup_queue(conn) == 0
        conn.close()

    def test_connect_creates_ticker_unique_index(self):
        """Fresh in-memory DB must enforce UNIQUE(ticker) on sentinel_queue."""
        conn = self._conn()
        # The constraint can appear as a named index or as an inline UNIQUE in DDL.
        named_idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_sentinel_queue_ticker'"
        ).fetchone()
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='sentinel_queue'"
        ).fetchone()
        has_constraint = (named_idx is not None) or (
            table_sql and "UNIQUE(ticker)" in (table_sql[0] or "")
        )
        assert has_constraint, "sentinel_queue must enforce UNIQUE(ticker)"
        conn.close()


# --------------------------------------------------------------------------- #
# per-CIK fallback: tag coalescing across fiscal ends
# --------------------------------------------------------------------------- #

class TestPerCikFallback:
    def _fake_xbrl(self, monkeypatch):
        import valuation_alpha.datastore.xbrl_financials as xf

        ppe = {"PaymentsToAcquirePropertyPlantAndEquipment": "ppe"}
        prod = {"PaymentsToAcquireProductiveAssets": "prod"}

        def fake_extract(facts, fields):
            friendly = list(fields.keys())[0]
            tag = list(fields.values())[0]
            if tag in ppe:
                dates, vals = ["2020-01-31", "2021-01-31"], [10.0, 20.0]
            elif tag in prod:
                dates, vals = ["2024-01-31", "2025-01-31"], [30.0, 40.0]
            else:
                dates, vals = ["2024-01-31", "2025-01-31"], [500.0, 600.0]
            idx = pd.to_datetime(dates)
            df = pd.DataFrame({friendly: vals}, index=idx)
            df.index.name = "fiscal_end"
            df["filed_date"] = pd.to_datetime(dates)
            return df

        monkeypatch.setattr(xf, "fetch_companyfacts", lambda *a, **k: {"fake": True})
        monkeypatch.setattr(xf, "extract_quarterly_financials", fake_extract)

    def test_capex_coalesced_across_tags(self, monkeypatch):
        self._fake_xbrl(monkeypatch)
        conn = q.connect(":memory:")
        cfg = {"lanes": {"sec": {"user_agent": "test"}}}
        stored = sec_lane.sync_per_cik_fallback(conn, ["T"], lambda t: "1234", cfg)
        assert stored > 0
        old = conn.execute(
            "SELECT capex FROM sentinel_fundamentals WHERE ticker='T' AND fiscal_end='2020-01-31'"
        ).fetchone()
        recent = conn.execute(
            "SELECT capex FROM sentinel_fundamentals WHERE ticker='T' AND fiscal_end='2025-01-31'"
        ).fetchone()
        assert old is not None and old["capex"] == 10.0
        assert recent is not None and recent["capex"] == 40.0
        conn.close()

    def test_missing_companyfacts_skips_ticker(self, monkeypatch):
        import valuation_alpha.datastore.xbrl_financials as xf
        monkeypatch.setattr(xf, "fetch_companyfacts", lambda *a, **k: {})
        conn = q.connect(":memory:")
        stored = sec_lane.sync_per_cik_fallback(conn, ["T"], lambda t: "1234",
                                                {"lanes": {"sec": {"user_agent": "u"}}})
        assert stored == 0
        conn.close()

    def test_gross_profit_derived_from_cost_of_revenue(self, monkeypatch):
        # Filer does not tag GrossProfit (Alphabet/Walmart/Qualcomm pattern);
        # the fallback must derive gross_profit = revenue - cost and the margin.
        import valuation_alpha.datastore.xbrl_financials as xf

        def fake_extract(facts, fields):
            friendly = list(fields.keys())[0]
            tag = list(fields.values())[0]
            if tag == "GrossProfit":
                return pd.DataFrame()
            idx = pd.to_datetime(["2024-01-31", "2025-01-31"])
            if tag == "CostOfRevenue":
                vals = [400.0, 600.0]
            elif tag == "Revenues":
                vals = [1000.0, 1500.0]
            else:
                vals = [100.0, 200.0]
            df = pd.DataFrame({friendly: vals}, index=idx)
            df.index.name = "fiscal_end"
            df["filed_date"] = pd.to_datetime(idx)
            return df

        monkeypatch.setattr(xf, "fetch_companyfacts", lambda *a, **k: {"fake": True})
        monkeypatch.setattr(xf, "extract_quarterly_financials", fake_extract)
        conn = q.connect(":memory:")
        cfg = {"lanes": {"sec": {"user_agent": "test"}}}
        stored = sec_lane.sync_per_cik_fallback(conn, ["T"], lambda t: "1234", cfg)
        assert stored > 0
        r = conn.execute(
            "SELECT revenue, gross_profit, gross_margin FROM sentinel_fundamentals"
            " WHERE ticker='T' AND fiscal_end='2025-01-31'"
        ).fetchone()
        assert r["revenue"] == 1500.0
        assert r["gross_profit"] == 900.0  # 1500 - 600 derived from CostOfRevenue
        assert abs(r["gross_margin"] - 0.6) < 1e-9
        conn.close()


# --------------------------------------------------------------------------- #
# SEC bulk datasets: filed-date parsing + max-qtrs grouping
# --------------------------------------------------------------------------- #

class TestSecBulk:
    def test_read_sub_parses_yyyymmdd_filed_and_filters_forms(self):
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(
                "sub.txt",
                "adsh\tcik\tform\tfiled\n"
                "0001\t1045810\t10-K\t20250226\n"
                "0002\t1045810\t8-K\t20250226\n"
                "0003\t1045810\t10-Q\t2025-02-26\n",
            )
        buf.seek(0)
        zh = zipfile.ZipFile(buf)
        sub = sec_lane._read_sub(zh)
        assert sub["0001"]["filed"] == "2025-02-26"
        assert sub["0003"]["filed"] == "2025-02-26"
        assert "0002" not in sub  # 8-K is outside the 10-K/10-Q scope

    def test_rows_to_upserts_prefers_max_qtrs_per_field(self):
        rows = [
            {"ticker": "T", "fiscal_end": "20250131", "filed_date": "2025-02-26",
             "form": "10-K", "qtrs": 1, "friendly": "revenue", "value": 10.0},
            {"ticker": "T", "fiscal_end": "20250131", "filed_date": "2025-02-26",
             "form": "10-K", "qtrs": 3, "friendly": "revenue", "value": 30.0},
            {"ticker": "T", "fiscal_end": "20250131", "filed_date": "2025-02-26",
             "form": "10-K", "qtrs": 4, "friendly": "revenue", "value": 40.0},
            {"ticker": "T", "fiscal_end": "20250131", "filed_date": "2025-02-26",
             "form": "10-K", "qtrs": 0, "friendly": "cash", "value": 50.0},
            {"ticker": "T", "fiscal_end": "20250131", "filed_date": "2025-02-26",
             "form": "10-K", "qtrs": 4, "friendly": "gross_profit", "value": 20.0},
        ]
        recs = sec_lane._rows_to_upserts(rows)
        assert len(recs) == 1
        assert recs[0]["revenue"] == 40.0  # full-year cumulative wins
        assert recs[0]["cash"] == 50.0
        assert recs[0]["gross_margin"] == 0.5


# --------------------------------------------------------------------------- #
# Orchestrator: G3 dict normalization + pass/fail routing
# --------------------------------------------------------------------------- #

class TestOrchestrator:
    def _scfg(self):
        return load_sentinel_config()["sentinel"]

    def _seed_passing_fundamentals(self, conn):
        for i in range(6):
            rows = [{
                "ticker": "PASS", "fiscal_end": f"2024-0{j+1}-01",
                "filed_date": f"2024-0{j+1}-15", "form": "10-Q", "qtrs": 1,
                "ocf": 1000.0, "capex": 100.0, "revenue": 5000.0,
                "gross_profit": 2500.0, "gross_margin": 0.50, "cash": 5000.0,
                "current_assets": 4000.0, "current_liabilities": 1000.0,
                "total_assets": 10000.0, "total_liabilities": 4000.0,
                "equity": 6000.0, "retained_earnings": 3000.0, "ebit": 1200.0,
            } for j in range(6)]
            for rec in rows:
                q.upsert_fundamental(conn, rec)

    def test_process_item_g3_is_a_dict(self, monkeypatch):
        conn = q.connect(":memory:")
        self._seed_passing_fundamentals(conn)
        rconn = sqlite3.connect(":memory:")
        rconn.row_factory = sqlite3.Row
        monkeypatch.setattr(orchestrator.altdata_lane, "g3_altdata",
                            lambda *a, **k: (True, "", {"coverage_sources": 1}))
        item = {"ticker": "PASS", "source": "cli", "id": 1}
        verdicts = orchestrator.process_item(conn, rconn, item, self._scfg(), lambda t: None)
        for gate in ("g1_survival", "g2_fundamentals", "g3_altdata"):
            assert isinstance(verdicts[gate], dict)
            assert "passed" in verdicts[gate]
        assert orchestrator._item_passed(verdicts) is None
        conn.close()
        rconn.close()

    def test_run_pass_marks_passed_with_g3_dict(self, monkeypatch):
        conn = q.connect(":memory:")
        self._seed_passing_fundamentals(conn)
        q.enqueue(conn, "PASS", "cli", "cli:PASS")
        rconn = sqlite3.connect(":memory:")
        rconn.row_factory = sqlite3.Row
        monkeypatch.setattr(orchestrator.altdata_lane, "g3_altdata",
                            lambda *a, **k: (True, "", {"coverage_sources": 1}))
        res = orchestrator.run_pass(conn, rconn, self._scfg(), lambda t: None)
        assert res == {"processed": 1, "passed": 1, "failed": 0}
        row = conn.execute("SELECT stage FROM sentinel_queue WHERE ticker='PASS'").fetchone()
        assert row["stage"] == "passed"
        conn.close()
        rconn.close()

    def test_run_pass_fails_closed_when_g3_no_coverage(self):
        conn = q.connect(":memory:")
        self._seed_passing_fundamentals(conn)
        q.enqueue(conn, "PASS", "cli", "cli:PASS")
        rconn = sqlite3.connect(":memory:")
        rconn.row_factory = sqlite3.Row
        rconn.execute("CREATE TABLE daily_aggregations (date TEXT, ticker TEXT, mention_count REAL)")
        res = orchestrator.run_pass(conn, rconn, self._scfg(), lambda t: None)
        assert res["processed"] == 1 and res["passed"] == 0 and res["failed"] == 1
        row = conn.execute("SELECT stage, last_error FROM sentinel_queue WHERE ticker='PASS'").fetchone()
        assert row["stage"] == "failed"
        assert row["last_error"] == "g3:no_altdata_coverage"
        conn.close()
        rconn.close()


# --------------------------------------------------------------------------- #
# Alt-data lane: coverage floor + z-score
# --------------------------------------------------------------------------- #

class TestAltdataLane:
    def _cfg(self):
        return load_sentinel_config()["sentinel"]

    def test_g3_no_coverage_fails_closed(self):
        conn = q.connect(":memory:")
        rconn = sqlite3.connect(":memory:")
        rconn.row_factory = sqlite3.Row
        rconn.execute("CREATE TABLE daily_aggregations (date TEXT, ticker TEXT, mention_count REAL)")
        ok, reason, metrics = altdata_lane.g3_altdata(rconn, conn, "T", self._cfg())
        assert not ok
        assert reason == "g3:no_altdata_coverage"
        assert metrics["coverage_sources"] == 0
        conn.close()
        rconn.close()

    def test_reddit_z_spike_passes_floor(self):
        conn = q.connect(":memory:")
        rconn = sqlite3.connect(":memory:")
        rconn.row_factory = sqlite3.Row
        rconn.execute("CREATE TABLE daily_aggregations (date TEXT, ticker TEXT, mention_count REAL)")
        now = int(time.time())
        rows = []
        for i in range(20):
            d = (time.time() - (20 - i) * 86400)
            count = 10.0 if i < 13 else 100.0
            rows.append((d, i, count))
        for d, i, count in rows:
            import datetime
            date = datetime.datetime.fromtimestamp(d, tz=datetime.timezone.utc).strftime("%Y-%m-%d")
            rconn.execute(
                "INSERT INTO daily_aggregations (date, ticker, mention_count) VALUES (?, 'T', ?)",
                (date, count),
            )
        rconn.commit()
        ok, reason, metrics = altdata_lane.g3_altdata(rconn, conn, "T", self._cfg(), as_of=now)
        assert ok, reason
        assert metrics["coverage_sources"] >= 1
        conn.close()
        rconn.close()

    def test_reddit_z_insufficient_observations_returns_none(self):
        rconn = sqlite3.connect(":memory:")
        rconn.row_factory = sqlite3.Row
        rconn.execute("CREATE TABLE daily_aggregations (date TEXT, ticker TEXT, mention_count REAL)")
        rconn.execute("INSERT INTO daily_aggregations (date, ticker, mention_count) VALUES ('2026-01-01','T',5)")
        rconn.commit()
        assert altdata_lane.reddit_z(rconn, "T", window_days=90, min_observations=5) is None
        rconn.close()

    def test_reddit_z_aggregates_subreddit_rows_per_date(self):
        # daily_aggregations stores one row per (ticker, date, subreddit); the
        # z-score must sum mentions per date, not treat each row as an
        # observation (real-DB tuple-crash regression).
        import datetime
        rconn = sqlite3.connect(":memory:")
        rconn.row_factory = sqlite3.Row
        rconn.execute(
            "CREATE TABLE daily_aggregations (date TEXT, ticker TEXT, subreddit TEXT, mention_count REAL)")
        as_of = 1760000000
        base = datetime.datetime.fromtimestamp(as_of - 20 * 86400, tz=datetime.timezone.utc)
        for i in range(10):
            d = base + datetime.timedelta(days=i)
            rconn.execute(
                "INSERT INTO daily_aggregations (date, ticker, subreddit, mention_count) VALUES (?, 'T', 'wsb', ?)",
                (d.strftime("%Y-%m-%d"), 10.0 + i))
        spike = datetime.datetime.fromtimestamp(as_of - 9 * 86400, tz=datetime.timezone.utc)
        rconn.execute(
            "INSERT INTO daily_aggregations (date, ticker, subreddit, mention_count) VALUES (?, 'T', 'wsb', ?)",
            (spike.strftime("%Y-%m-%d"), 60.0))
        rconn.execute(
            "INSERT INTO daily_aggregations (date, ticker, subreddit, mention_count) VALUES (?, 'T', 'options', ?)",
            (spike.strftime("%Y-%m-%d"), 40.0))
        rconn.commit()
        z = altdata_lane.reddit_z(rconn, "T", window_days=90, min_observations=5, as_of=as_of)
        assert isinstance(z, float)
        assert z > 0  # spike day totals 100 (60+40) vs baseline ~10

    def test_reddit_z_all_recent_data_uses_value_baseline(self):
        # Regression: when every observation falls inside the last 7 days the
        # baseline fallback previously assigned dated[:N] -- a list of
        # (ts, count) tuples -- and statistics.mean crashed.
        import datetime
        rconn = sqlite3.connect(":memory:")
        rconn.row_factory = sqlite3.Row
        rconn.execute(
            "CREATE TABLE daily_aggregations (date TEXT, ticker TEXT, subreddit TEXT, mention_count REAL)")
        as_of = 1760000000
        for i in range(6):
            d = datetime.datetime.fromtimestamp(as_of - i * 86400, tz=datetime.timezone.utc)
            rconn.execute(
                "INSERT INTO daily_aggregations (date, ticker, subreddit, mention_count) VALUES (?, 'T', 'wsb', ?)",
                (d.strftime("%Y-%m-%d"), 5.0))
        rconn.commit()
        z = altdata_lane.reddit_z(rconn, "T", window_days=90, min_observations=5, as_of=as_of)
        assert isinstance(z, float)
        rconn.close()


# --------------------------------------------------------------------------- #
# GitHub snapshots: one row per repo per UTC day
# --------------------------------------------------------------------------- #


class TestGithubSnapshots:
    def _conn(self):
        return q.connect(":memory:")

    def test_upsert_dedupes_within_day_and_exists_today(self):
        conn = self._conn()
        now = int(time.time())
        q.upsert_github_snapshot(conn, "MSFT", "microsoft/vscode", 100)
        q.upsert_github_snapshot(conn, "MSFT", "microsoft/vscode", 101)
        rows = conn.execute(
            "SELECT repo_name, stars FROM sentinel_github_snapshots WHERE ticker = 'MSFT'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["stars"] == 101
        assert q.github_snapshot_exists_today(conn, "MSFT")
        assert not q.github_snapshot_exists_today(conn, "NVDA")
        conn.close()


# --------------------------------------------------------------------------- #
# Governor: fail-closed circuit breaker
# --------------------------------------------------------------------------- #

class TestGovernor:
    def test_open_circuit_blocks(self):
        conn = q.connect(":memory:")
        governor.circuit_state(conn, "sec", failure_threshold=3, success_threshold=1, timeout_seconds=60)
        for _ in range(3):
            governor.record_failure(conn, "sec", timeout_seconds=60)
        assert governor.circuit_state(conn, "sec") == "OPEN"
        assert not governor.circuit_allow(conn, "sec")
        conn.close()

    def test_half_open_recovers_on_success(self):
        conn = q.connect(":memory:")
        governor.circuit_state(conn, "sec", failure_threshold=3, success_threshold=1, timeout_seconds=60)
        for _ in range(3):
            governor.record_failure(conn, "sec", timeout_seconds=60)
        # Force the probe window to open (next_probe_utc in the past).
        conn.execute(
            "UPDATE sentinel_circuits SET next_probe_utc = 1 WHERE circuit_key = 'sec'")
        conn.commit()
        assert governor.circuit_state(conn, "sec") == "HALF_OPEN"
        governor.record_success(conn, "sec", success_threshold=1)
        assert governor.circuit_state(conn, "sec") == "CLOSED"
        assert governor.circuit_allow(conn, "sec")
        conn.close()

    def test_throttle_first_call_allowed(self):
        conn = q.connect(":memory:")
        assert governor.throttle(conn, "bucket", rate=1.0, burst=2.0, max_wait_seconds=0)
        conn.close()

    def test_throttle_refills_after_burst_exhausted(self):
        # A lane whose refill interval exceeds its request latency must still
        # accrue tokens across polls (regression: the fail path used to reset
        # last_refill and discard the accrued time, so the bucket never
        # refilled and slow lanes always timed out).
        conn = q.connect(":memory:")
        assert governor.throttle(conn, "bucket", rate=0.5, burst=1.0, max_wait_seconds=0)
        # Drain the bucket; the next call must NOT mint a token instantly.
        conn.execute(
            "UPDATE sentinel_rate_limits SET tokens = 0, last_refill = ? WHERE bucket_key = 'bucket'",
            (int(time.time()),),
        )
        conn.commit()
        assert not governor.throttle(conn, "bucket", rate=0.5, burst=1.0, max_wait_seconds=0)
        # But it must refill within the wait budget by accruing across polls.
        assert governor.throttle(conn, "bucket", rate=0.5, burst=1.0, max_wait_seconds=5)
        conn.close()


# --------------------------------------------------------------------------- #
# Config loader: fail-closed validation
# --------------------------------------------------------------------------- #

class TestConfigFailClosed:
    def test_loads_valid_config(self):
        cfg = load_sentinel_config()
        assert cfg["sentinel"]["enabled"] is True
        assert cfg["sentinel"]["gates"]["g1_survival"]["altman_z_floor"] == 1.1

    def test_unknown_top_level_key_rejected(self, tmp_path):
        import yaml
        cfg = load_sentinel_config()
        cfg["bogus"] = 1
        p = tmp_path / "bad.yaml"
        p.write_text(yaml.safe_dump(cfg))
        with pytest.raises(SentinelConfigError):
            load_sentinel_config(str(p))

    def test_missing_required_gate_key_rejected(self, tmp_path):
        import yaml
        cfg = load_sentinel_config()
        del cfg["sentinel"]["gates"]["g1_survival"]["altman_z_floor"]
        p = tmp_path / "bad.yaml"
        p.write_text(yaml.safe_dump(cfg))
        with pytest.raises(SentinelConfigError):
            load_sentinel_config(str(p))

    def test_nan_lane_rate_rejected(self, tmp_path):
        import yaml
        cfg = load_sentinel_config()
        cfg["sentinel"]["lanes"]["sec"]["rate_per_second"] = float("nan")
        p = tmp_path / "bad.yaml"
        p.write_text(yaml.safe_dump(cfg))
        with pytest.raises(SentinelConfigError):
            load_sentinel_config(str(p))
