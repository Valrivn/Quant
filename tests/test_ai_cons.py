"""Tests for the AI-cons taxonomy + screener (B-20260819-001, vector 2).

Covers: pre-registered taxonomy integrity, sector-based relevance (no narrative
required), keyword corroboration, the quant-gate screen, and the "hidden gem"
steal filter (no attention multiplier).
"""

import pytest

from discovery.ai_cons import (
    AI_CONS,
    classify_cons,
    cons_addressed_by,
    get_con,
    hidden_gems,
    screen_ai_cons,
)

SECTOR_MAP = {
    "NVDA": "semiconductor",
    "TSM": "semiconductor",
    "CEG": "utilities",
    "VRT": "hardware_oem",
    "MSFT": "platform_software",
}


class TestTaxonomy:
    def test_ten_preregistered_cons(self):
        assert len(AI_CONS) == 10

    def test_con_ids_unique(self):
        ids = [c.con_id for c in AI_CONS]
        assert len(ids) == len(set(ids))

    def test_get_con_known(self):
        assert get_con("power_demand").label.startswith("Datacenter power")

    def test_get_con_unknown_raises(self):
        with pytest.raises(KeyError):
            get_con("does_not_exist")

    def test_no_random_import(self):
        import inspect
        import discovery.ai_cons as mod

        src = inspect.getsource(mod)
        assert "import random" not in src
        assert "np.random" not in src


class TestSectorRelevance:
    def test_sector_based_relevance_needs_no_text(self):
        # CEG is a utility -> addresses power_demand even if never mentioned.
        assert "power_demand" in cons_addressed_by("CEG", SECTOR_MAP)

    def test_hardware_oem_cooling(self):
        assert "cooling" in cons_addressed_by("VRT", SECTOR_MAP)

    def test_semiconductor_memory_and_manufacturing(self):
        cons = cons_addressed_by("NVDA", SECTOR_MAP)
        assert "memory_bandwidth" in cons
        assert "inference_cost" in cons
        assert "manufacturing" in cons

    def test_platform_software_addresses_nothing(self):
        assert cons_addressed_by("MSFT", SECTOR_MAP) == []

    def test_no_sector_map(self):
        assert cons_addressed_by("CEG") == []

    def test_explicit_ticker_mapping(self):
        assert cons_addressed_by("UNKNOWN", None) == []


class TestKeywordCorroboration:
    def test_keyword_match(self):
        assert "power_demand" in classify_cons("SMR and nuclear power for datacenters")
        assert "cooling" in classify_cons("immersion cooling for AI racks")

    def test_empty_text(self):
        assert classify_cons("") == []

    def test_case_insensitive(self):
        assert "water" in classify_cons("WATER reuse and recovery")


class TestScreen:
    def test_quant_gate_filters(self):
        def gate(tickers):
            return {"CEG": "no_alpha_data"}  # CEG fails

        out = screen_ai_cons(["CEG", "VRT"], SECTOR_MAP, quant_gate=gate)
        assert "power_demand" in out["CEG"]["cons"]
        assert out["CEG"]["quant_pass"] is False
        assert out["VRT"]["quant_pass"] is True

    def test_no_quant_gate_reports_unknown_pass(self):
        out = screen_ai_cons(["CEG"], SECTOR_MAP, quant_gate=None)
        assert out["CEG"]["quant_pass"] is True

    def test_gate_error_fails_closed(self):
        def gate(tickers):
            raise RuntimeError("down")

        out = screen_ai_cons(["CEG"], SECTOR_MAP, quant_gate=gate)
        assert out["CEG"]["quant_pass"] is False
        assert out["CEG"]["quant_reason"] == "quant_gate_failed"

    def test_deterministic_ordering(self):
        a = screen_ai_cons(["VRT", "CEG"], SECTOR_MAP, quant_gate=lambda t: {})
        b = screen_ai_cons(["CEG", "VRT"], SECTOR_MAP, quant_gate=lambda t: {})
        assert list(a) == list(b)


class TestHiddenGems:
    def test_steal_filter_no_attention(self):
        def gate(tickers):
            return {"CEG": "no_alpha_data"}  # CEG fails quant

        gems = hidden_gems(
            ["CEG", "VRT", "MSFT"], SECTOR_MAP, quant_gate=gate, held=()
        )
        tickers = [g["ticker"] for g in gems]
        assert tickers == ["VRT"]  # MSFT addresses no con; CEG fails quant

    def test_held_names_excluded(self):
        gems = hidden_gems(
            ["VRT"], SECTOR_MAP, quant_gate=lambda t: {}, held=["VRT"]
        )
        assert gems == []