"""Tests for the Wiki-Census and Diff Layers (B-20260820-001 ruling)."""

import os
from discovery.wiki_sec_diff import diff_wiki_sec, passes_bar
from discovery.wiki_census import run_wiki_census, pit_unlock_check
from discovery.deg_registry import DEGRADED, LIVE


def test_diff_wiki_sec():
    wiki = {"AAPL", "MSFT", "GOOG"}
    sec = {"MSFT", "GOOG", "AMZN"}

    res = diff_wiki_sec(wiki, sec)
    # (a) diff buckets correct on overlapping sets
    assert res["sec_only"] == ["AMZN"]
    assert res["wiki_only"] == ["AAPL"]
    assert res["both"] == ["GOOG", "MSFT"]
    assert res["summary"]["wiki_count"] == 3
    assert res["summary"]["sec_count"] == 3
    assert res["summary"]["overlap_count"] == 2
    assert res["summary"]["wiki_only_count"] == 1

    # (b) passes_bar logic
    assert passes_bar(res, min_novel=1, min_corroborated=2) is True
    assert passes_bar(res, min_novel=2, min_corroborated=2) is False
    assert passes_bar(res, min_novel=1, min_corroborated=3) is False


def test_wiki_census():
    companies = {"Q1": "AAPL", "Q2": "MSFT", "Q3": "INVALID"}
    edges = [
        {"source_qid": "Q1", "target_qid": "Q2", "valid_from": "2020-01-01"},
        {"source_qid": "Q2", "target_qid": "Q3"},
    ]

    def dummy_cik_resolver(ticker):
        if ticker in {"AAPL", "MSFT"}:
            return "CIK_" + ticker
        return None

    def dummy_gate_fn(ticker):
        return ticker == "AAPL"

    # (c) census DEGRADED when live disabled
    res_not_live = run_wiki_census(companies, edges, dummy_cik_resolver, live=False)
    assert res_not_live.status == DEGRADED

    # (d) census validates/rejects with fake cik_resolver
    res_live = run_wiki_census(
        companies,
        edges,
        dummy_cik_resolver,
        gate_fn=dummy_gate_fn,
        live=True,
    )
    assert res_live.status == LIVE
    assert res_live.raw_companies == 3
    assert res_live.validated == 2  # AAPL, MSFT validated; INVALID rejected
    assert res_live.gated == 1  # only AAPL passes gate
    assert res_live.reject_reasons.get("no_cik") == 1
    assert res_live.reject_reasons.get("failed_gate") == 1

    # Check PIT dated fields
    assert res_live.dated_edges == 1
    assert res_live.pct_dated == 50.0

    # (e) pit_unlock_check boundary (49.9 False, 50.0 True)
    assert pit_unlock_check(49.9) is False
    assert pit_unlock_check(50.0) is True
    assert pit_unlock_check(50.1) is True
