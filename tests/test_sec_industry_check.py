"""Tests for discovery/sec_industry_check.py (100% offline)."""

import os
import pytest
from discovery.sec_industry_check import (
    check_members,
    check_ticker,
    classify,
    compare,
    fetch_sec_sic,
    sec_submissions_df_from_response,
)


def test_sec_submissions_df_from_response_canned():
    payload = {
        "cik": "0000320193",
        "sic": "3571",
        "sicDescription": "ELECTRONIC COMPUTERS",
        "industry": "Technology",
        "name": "Apple Inc.",
    }
    parsed = sec_submissions_df_from_response(payload)
    assert parsed["cik"] == "0000320193"
    assert parsed["sic"] == "3571"
    assert parsed["sic_description"] == "ELECTRONIC COMPUTERS"
    assert parsed["industry"] == "Technology"
    assert parsed["name"] == "Apple Inc."


def test_sec_submissions_df_from_response_missing_keys():
    parsed = sec_submissions_df_from_response({})
    assert parsed["cik"] == ""
    assert parsed["sic"] is None
    assert parsed["sic_description"] is None
    assert parsed["industry"] is None
    assert parsed["name"] is None


def test_classify():
    assert classify("3571") == "manufacturing"
    assert classify("7372") == "services"
    assert classify("1311") == "oil_gas"
    assert classify("6021") == "finance"
    assert classify("4911") == "utilities"
    assert classify("0111") == "agriculture"
    assert classify("9999") == "other"
    assert classify(None) == "other"
    assert classify("invalid") == "other"


def test_compare_ok_and_mismatch():
    res_ok = compare(damodaran_sic="3571", sec_sic="3571")
    assert res_ok["status"] == "ok"

    res_mismatch = compare(
        damodaran_sic="3571",
        sec_sic="7372",
        damodaran_industry="Computers",
        sec_industry="Services",
    )
    assert res_mismatch["status"] == "mismatch"
    assert "3571" in res_mismatch["reason"]
    assert "7372" in res_mismatch["reason"]


def test_compare_unknown_and_fallback():
    # Non-numeric SIC
    res_non_num = compare(damodaran_sic="ABC", sec_sic="XYZ")
    assert res_non_num["status"] == "unknown"

    # Token match fallback when one SIC missing
    res_token_ok = compare(
        damodaran_sic=None,
        sec_sic=None,
        damodaran_industry="Software Services",
        sec_industry="Software",
    )
    assert res_token_ok["status"] == "ok"

    res_token_mismatch = compare(
        damodaran_sic=None,
        sec_sic=None,
        damodaran_industry="Software",
        sec_industry="Banking",
    )
    assert res_token_mismatch["status"] == "mismatch"

    # Missing both metadata
    res_none = compare(damodaran_sic="3571", sec_sic=None)
    assert res_none["status"] == "unknown"


def test_check_ticker_pure():
    cik_lookup = {"AAPL": "0000320193"}
    damodaran_row = {"sic_code": "3571", "industry_group": "Computers"}

    def fake_fetcher(cik):
        return {
            "sic": "3571",
            "sic_description": "ELECTRONIC COMPUTERS",
            "industry": "Technology",
            "cik": cik,
            "name": "Apple Inc.",
        }

    res = check_ticker("AAPL", cik_lookup, fake_fetcher, damodaran_row)
    assert res["ticker"] == "AAPL"
    assert res["cik"] == "0000320193"
    assert res["status"] == "ok"
    assert res["sec_sic"] == "3571"


def test_check_members_ordering_and_live_gate(monkeypatch):
    monkeypatch.delenv("DISCOVERY_LIVE", raising=False)

    cik_lookup = {"MSFT": "7890", "AAPL": "1234", "FAIL": "9999"}
    members = [
        {"ticker": "MSFT", "sic_code": "7372", "industry_group": "Software"},
        {"ticker": "AAPL", "sic_code": "3571", "industry_group": "Computers"},
        {"ticker": "FAIL", "sic_code": "1000", "industry_group": "Mining"},
    ]

    def fake_fetcher(cik):
        if cik == "7890":
            return {"sic": "7372", "sic_description": "SERVICES", "industry": "Software"}
        elif cik == "1234":
            return {"sic": "9999", "sic_description": "OTHER", "industry": "Other"}
        return {"sic": None, "sic_description": None, "industry": None}

    results = check_members(members, cik_lookup, fake_fetcher)
    assert len(results) == 3
    statuses = [r["status"] for r in results]
    assert statuses == sorted(statuses, reverse=True)

    # Test live-gate exception when fake_fetcher not injected and DISCOVERY_LIVE not 1
    with pytest.raises(RuntimeError, match="network disabled"):
        fetch_sec_sic("1234")

    with pytest.raises(RuntimeError, match="network disabled"):
        check_members(members, cik_lookup)
