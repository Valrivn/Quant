"""Consensus pipeline + persistence tests (D-20260816-001, P1).

Offline-first: collectors are injected fixtures, never the network. Verifies
the sorted-data output shape, SET-ASIDE handling in the pass, and the store
round-trip into the additive consensus tables.
"""

import asyncio
import pytest

from discovery.consensus.config import load_consensus_config
from discovery.consensus.pipeline import run_consensus_pass, SortedCompanyRow
from discovery.consensus.store import persist_consensus_run


@pytest.fixture(scope="module")
def cfg():
    return load_consensus_config()


def _review(site, n, star=None, skew=None, records=None, rwv=None, nwv=None):
    d = {"n": n}
    if star is not None:
        d["star_level"] = star
    if skew is not None:
        d["skewness"] = skew
    if records is not None:
        d["records"] = records
    if rwv is not None:
        d["recent_weekly_volume"] = rwv
    if nwv is not None:
        d["normal_weekly_volume"] = nwv
    return d


class TestPipelinePass:
    def test_sorted_output_shape(self, cfg):
        collectors = {s: (lambda c, s=s: _review(s, 120 + (0 if s != "g2" else 30), star=4.0))
                      for s in ["glassdoor", "indeed", "g2", "capterra", "trustpilot"]}
        talent = lambda c: {"senior_mentions": 6, "hiring_velocity": 100.0}
        quant = lambda c: {"transaction_volume": 0.7, "sec_attrition_velocity": 0.3}

        rows = asyncio.run(run_consensus_pass(
            [("A", "Sec"), ("B", "Sec"), ("C", "Sec")],
            cfg,
            review_collectors=collectors,
            talent_collector=talent,
            quantifiable_collector=quant,
        ))
        assert len(rows) == 3
        assert all(isinstance(r, SortedCompanyRow) for r in rows)
        scores = [r.composite_score for r in rows]
        assert scores == sorted(scores, reverse=True)  # deterministic desc sort
        for r in rows:
            assert r.usable_sources >= 2
            assert r.total_reviews >= 150

    def test_set_aside_surfaces_in_pass(self, cfg):
        collectors = {s: (lambda c, s=s: _review(s, 20)) for s in ["glassdoor", "trustpilot"]}
        rows = asyncio.run(run_consensus_pass([("T", "Sec")], cfg, review_collectors=collectors,
                                   talent_collector=lambda c: None,
                                   quantifiable_collector=lambda c: None))
        assert rows[0].total_reviews == 40
        assert "SET-ASIDE" in rows[0].flags

    def test_live_gate_is_fail_closed(self):
        import os
        from discovery.consensus.collectors import (
            LiveFetchDisabled, make_review_collector,
        )
        os.environ["DISCOVERY_LIVE"] = "0"
        try:
            coll = make_review_collector("glassdoor")
            with pytest.raises(LiveFetchDisabled):
                # The default live collector raises when called without the flag.
                import asyncio
                asyncio.run(coll("TestCompany"))
        finally:
            os.environ["DISCOVERY_LIVE"] = ""


class TestStoreRoundTrip:
    def test_persist_and_reload(self, cfg, tmp_path):
        import sqlite3
        from db.schema_consensus import create_consensus_tables

        conn = sqlite3.connect(tmp_path / "t.db")
        conn.row_factory = sqlite3.Row
        create_consensus_tables(conn)

        collectors = {s: (lambda c, s=s: _review(s, 130, star=4.1))
                      for s in ["glassdoor", "indeed", "g2", "capterra", "trustpilot"]}
        rows = asyncio.run(run_consensus_pass(
            [("X", "Sec")], cfg,
            review_collectors=collectors,
            talent_collector=lambda c: {"senior_mentions": 5, "hiring_velocity": 80.0},
            quantifiable_collector=lambda c: {"transaction_volume": 0.6},
        ))
        run_ts = persist_consensus_run(rows, conn=conn, run_ts=12345)
        assert run_ts == 12345

        row = conn.execute(
            "SELECT ticker, composite_score, flags FROM consensus_company_rows WHERE run_ts=?",
            (12345,),
        ).fetchone()
        assert row["ticker"] == "X"
        assert row["composite_score"] > 0.0
        assert "SET-ASIDE" not in row["flags"]