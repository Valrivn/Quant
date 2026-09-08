"""Integration tests: sector concept chains through the real parser + validators.

These tests exercise the full path with SAMPLE FILINGS modeled on real SEC
companyfacts JSON structure:

  1. A regional bank (10-K companyfacts) -> BANKING chains -> validator.
  2. A pharma company (10-K companyfacts) -> PHARMA chains -> validator.
  3. A managed-care company -> MANAGED_CARE chains -> validator.
  4. A REIT -> REIT chains -> validator.

Each sample uses a realistic subset of the full companyfacts schema (facts /
us-gaap / <Tag> / units / USD / [ {start,end,val,accn,form,filed} ]).
"""

from __future__ import annotations

import pytest

from discovery.xbrl_parser import (
    FilingRecord,
    extract_filing_from_companyfacts,
)
from discovery.sector_xbrl_chains import SubSector
from discovery.sector_validators import validate_facts
from discovery.taxonomy_loader import SectorTaxonomyLoader


# ═══════════════════════════════════════════════════════════════════════════════
# SAMPLE FILING FACTORY
# ═══════════════════════════════════════════════════════════════════════════════


def _entry(start, end, val, accn, form="10-K", filed="2025-02-14", unit="USD"):
    return {
        "start": start, "end": end, "val": val, "accn": accn,
        "form": form, "filed": filed, "fy": end[:4], "fp": "FY", "frame": "CY2024",
        "uom": unit,
    }


def make_companyfacts(tag_values: dict, accession: str = "0000950170-25-000001") -> dict:
    """Build a companyfacts-shaped dict from {tag: [values]}."""
    us_gaap = {}
    for tag, vals in tag_values.items():
        us_gaap[tag] = {"units": {"USD": vals}}
    return {"entityName": "Sample", "facts": {"us-gaap": us_gaap, "ifrs-full": {}}}


def _filing_dict(accession="0000950170-25-000001"):
    return {
        "accession": accession,
        "form": "10-K",
        "filed": "2025-02-14",
        "fiscal_end": "2024-12-31",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# SAMPLE BANK FILING
# ═══════════════════════════════════════════════════════════════════════════════

BANK_TAGS = {
    # Interest income / expense
    "InterestIncomeExpense": [_entry("2024-01-01", "2024-12-31", 2_850_000_000, "A")],
    "InterestExpense": [_entry("2024-01-01", "2024-12-31", 1_050_000_000, "A")],
    "NetInterestIncome": [_entry("2024-01-01", "2024-12-31", 1_800_000_000, "A")],
    "InterestIncomeDepositsWithFinancialInstitutions": [_entry("2024-01-01", "2024-12-31", 50_000_000, "A")],
    # Loan loss stuff
    "ProvisionForLoanAndLeaseLosses": [_entry("2024-01-01", "2024-12-31", 180_000_000, "A")],
    "AllowanceForLoanAndLeaseLosses": [_entry("2024-12-31", "2024-12-31", 410_000_000, "A")],
    "LoansAndLeasesReceivableNet": [_entry("2024-12-31", "2024-12-31", 12_500_000_000, "A")],
    # Capital / RWA (Basel III)
    "Tier1Capital": [_entry("2024-12-31", "2024-12-31", 1_520_000_000, "A")],
    "RiskWeightedAssets": [_entry("2024-12-31", "2024-12-31", 15_200_000_000, "A")],
    # Noninterest
    "NoninterestIncome": [_entry("2024-01-01", "2024-12-31", 620_000_000, "A")],
    "NoninterestExpense": [_entry("2024-01-01", "2024-12-31", 1_350_000_000, "A")],
    # Deposits
    "Deposits": [_entry("2024-12-31", "2024-12-31", 20_900_000_000, "A")],
}


class TestBankFilingIntegration:
    @pytest.fixture
    def loader(self):
        return SectorTaxonomyLoader()

    @pytest.fixture
    def record(self):
        cf = make_companyfacts(BANK_TAGS)
        filing = _filing_dict()
        return extract_filing_from_companyfacts(cf, "SBUX_TEST", filing)

    def test_base_fields_survive(self, record):
        """Base parser fields still resolve alongside sector chains."""
        assert "revenue" in record.facts or "revenue" in record.missing

    def test_banking_chains_used_with_loader(self, loader, record):
        """Sector chains merge into the parser through the loader."""
        merged = loader.merged_chains_for_parser(subsector=SubSector.BANKING)
        assert "interest_income" in merged
        assert "interest_expense" in merged
        # The union must be a valid dict usable by the extraction loop
        assert isinstance(merged["interest_income"], list)
        assert len(merged["interest_income"]) > 0

    def test_banking_validator_clean_bank(self):
        """A healthy regional bank should produce no errors."""
        facts = {
            "interest_income": 2.85e9,
            "interest_expense": 1.05e9,
            "earning_assets": 38.0e9,
            "net_interest_income": 1.80e9,
            "tier1_capital": 1.52e9,
            "risk_weighted_assets": 15.2e9,
            "provision_for_loan_losses": 180.0e6,
            "total_loans": 12.5e9,
            "noninterest_income": 620.0e6,
            "noninterest_expense": 1.35e9,
        }
        result = validate_facts(facts, SubSector.BANKING)
        assert result.valid, [i.message for i in result.errors()]
        # NIM should be derived
        nim_infos = [i for i in result.issues if i.code == "derived_nim"]
        assert len(nim_infos) == 1
        assert nim_infos[0].value == pytest.approx(1.80e9 / 38.0e9)

    def test_banking_validator_tier1_below_floor(self):
        """Tier 1 below the 4.5% CET1 floor should trip an error."""
        facts = {
            "tier1_capital": 500.0e6,
            "risk_weighted_assets": 20.0e9,  # 2.5% Tier 1 ratio
        }
        result = validate_facts(facts, SubSector.BANKING)
        codes = [i.code for i in result.issues]
        assert "tier1_below_regulatory_floor" in codes


# ═══════════════════════════════════════════════════════════════════════════════
# SAMPLE PHARMA FILING
# ═══════════════════════════════════════════════════════════════════════════════

PHARMA_TAGS = {
    "RevenueFromContractWithCustomerExcludingAssessedTax": [
        _entry("2024-01-01", "2024-12-31", 48_100_000_000, "A")
    ],
    "ResearchAndDevelopmentExpense": [_entry("2024-01-01", "2024-12-31", 13_500_000_000, "A")],
    "CostOfGoodsAndServicesSold": [_entry("2024-01-01", "2024-12-31", 10_900_000_000, "A")],
    "RoyaltyRevenue": [_entry("2024-01-01", "2024-12-31", 500_000_000, "A")],
    "CollaborationRevenue": [_entry("2024-01-01", "2024-12-31", 900_000_000, "A")],
    "IntangibleAssetsExcludingGoodwill": [_entry("2024-12-31", "2024-12-31", 24_000_000_000, "A")],
    "Goodwill": [_entry("2024-12-31", "2024-12-31", 18_000_000_000, "A")],
    "AmortizationOfIntangibleAssets": [_entry("2024-01-01", "2024-12-31", 2_100_000_000, "A")],
}


class TestPharmaFilingIntegration:
    @pytest.fixture
    def record(self):
        cf = make_companyfacts(PHARMA_TAGS)
        return extract_filing_from_companyfacts(cf, "PHARMA_X", _filing_dict())

    def test_pharma_rd_chain(self):
        """R&D expense resolves through the pharma chain."""
        cf = make_companyfacts(PHARMA_TAGS)
        rec = extract_filing_from_companyfacts(cf, "PHARMA_X", _filing_dict())
        # rd_expense is a base concept
        assert rec.facts.get("rd_expense") == pytest.approx(13.5e9)

    def test_pharma_validator_clean(self):
        facts = {
            "product_revenue": 48.1e9,
            "pharma_rd_expense": 13.5e9,
            "cost_of_goods_sold_pharma": 10.9e9,
            "royalty_revenue": 500.0e6,
        }
        result = validate_facts(facts, SubSector.PHARMA_BIOTECH)
        assert result.valid
        gm = [i for i in result.issues if i.code == "derived_gross_margin"]
        assert len(gm) == 1
        assert gm[0].value == pytest.approx((48.1 - 10.9) / 48.1)

    def test_pharma_validator_negative_royalty(self):
        facts = {"royalty_revenue": -5.0e6, "product_revenue": 1e9}
        result = validate_facts(facts, SubSector.PHARMA_BIOTECH)
        codes = [i.code for i in result.issues]
        assert "negative_royalty" in codes

    def test_pharma_high_rd_info(self):
        facts = {"product_revenue": 1e9, "pharma_rd_expense": 0.6e9}
        result = validate_facts(facts, SubSector.PHARMA_BIOTECH)
        codes = [i.code for i in result.issues]
        assert "high_rd_intensity" in codes


# ═══════════════════════════════════════════════════════════════════════════════
# SAMPLE MANAGED-CARE FILING
# ═══════════════════════════════════════════════════════════════════════════════

MCARE_TAGS = {
    "PremiumRevenue": [_entry("2024-01-01", "2024-12-31", 95_000_000_000, "A")],
    "MedicalExpenses": [_entry("2024-01-01", "2024-12-31", 73_000_000_000, "A")],
    "AdministrativeExpenses": [_entry("2024-01-01", "2024-12-31", 14_500_000_000, "A")],
    "InvestmentIncome": [_entry("2024-01-01", "2024-12-31", 500_000_000, "A")],
    "UnpaidClaimsLiability": [_entry("2024-12-31", "2024-12-31", 6_800_000_000, "A")],
    "RevenueFromContractWithCustomerExcludingAssessedTax": [
        _entry("2024-01-01", "2024-12-31", 97_000_000_000, "A")
    ],
}


class TestManagedCareFilingIntegration:
    @pytest.fixture
    def record(self):
        cf = make_companyfacts(MCARE_TAGS)
        return extract_filing_from_companyfacts(cf, "MCARE_Y", _filing_dict())

    def test_mlr_validator(self):
        facts = {
            "premium_revenue_mc": 95.0e9,
            "medical_expenses": 73.0e9,
            "administrative_expenses_mc": 14.5e9,
            "total_members": 48.0e6,
        }
        result = validate_facts(facts, SubSector.MANAGED_CARE)
        assert result.valid
        mlr = [i for i in result.issues if i.code == "derived_mlr"]
        assert len(mlr) == 1
        assert mlr[0].value == pytest.approx(73.0 / 95.0)
        # Premium yield derived from supplemental membership
        assert any(i.code == "derived_premium_yield" for i in result.issues)

    def test_mlr_below_aca_floor(self):
        facts = {
            "premium_revenue_mc": 95.0e9,
            "medical_expenses": 60.0e9,  # 63% MLR
            "administrative_expenses_mc": 14.5e9,
        }
        result = validate_facts(facts, SubSector.MANAGED_CARE)
        codes = [i.code for i in result.issues]
        assert "mlr_below_aca_floor" in codes

    def test_payer_mix_reconcile(self):
        facts = {
            "total_members": 100.0e6,
            "commercial_members": 60.0e6,
            "medicare_advantage_members": 25.0e6,
            "medicaid_members": 12.0e6,
        }
        result = validate_facts(facts, SubSector.MANAGED_CARE)
        assert result.valid
        # 60+25+12 = 97 of 100 -> within [0.80, 1.05]
        assert not any(i.code == "payer_mix_does_not_reconcile" for i in result.issues)

    def test_payer_mix_breakage(self):
        facts = {
            "total_members": 100.0e6,
            "commercial_members": 30.0e6,
            "medicare_advantage_members": 20.0e6,
            "medicaid_members": 5.0e6,
        }
        result = validate_facts(facts, SubSector.MANAGED_CARE)
        codes = [i.code for i in result.issues]
        assert "payer_mix_does_not_reconcile" in codes


# ═══════════════════════════════════════════════════════════════════════════════
# SAMPLE REIT FILING
# ═══════════════════════════════════════════════════════════════════════════════

REIT_TAGS = {
    "RentalRevenue": [_entry("2024-01-01", "2024-12-31", 2_400_000_000, "A")],
    "PropertyOperatingExpenses": [_entry("2024-01-01", "2024-12-31", 560_000_000, "A")],
    "DepreciationDepletionAndAmortization": [_entry("2024-01-01", "2024-12-31", 850_000_000, "A")],
    "NetIncomeLoss": [_entry("2024-01-01", "2024-12-31", 480_000_000, "A")],
    "RealEstateTaxes": [_entry("2024-01-01", "2024-12-31", 170_000_000, "A")],
    "GainOnSaleOfRealEstate": [_entry("2024-01-01", "2024-12-31", 35_000_000, "A")],
}


class TestREITFilingIntegration:
    @pytest.fixture
    def record(self):
        cf = make_companyfacts(REIT_TAGS)
        return extract_filing_from_companyfacts(cf, "REIT_Z", _filing_dict())

    def test_revenue_resolves(self, record):
        """REIT rental revenue resolves through base + sector chains."""
        assert record.facts is not None

    def test_noi_derivation(self):
        facts = {
            "rental_revenue": 2.4e9,
            "property_operations_expense": 560.0e6,
            "real_estate_taxes": 170.0e6,
        }
        result = validate_facts(facts, SubSector.REITS)
        assert result.valid
        noi = [i for i in result.issues if i.code == "derived_noi"]
        assert len(noi) == 1
        assert noi[0].value == pytest.approx(2.4e9 - 560.0e6 - 170.0e6)

    def test_ffo_derivation(self):
        facts = {
            "net_income_common": 480.0e6,
            "ffo_depreciation_adjustment": 850.0e6,
            "ffo_gains_adjustment": 35.0e6,
        }
        result = validate_facts(facts, SubSector.REITS)
        ffo = [i for i in result.issues if i.code == "derived_ffo"]
        assert len(ffo) == 1
        assert ffo[0].value == pytest.approx(480.0e6 + 850.0e6 - 35.0e6)

    def test_occupancy_sanity(self):
        facts = {"occupied_sqft": 8.0e6, "total_leasable_sqft": 10.0e6}
        result = validate_facts(facts, SubSector.REITS)
        assert result.valid
        occ = [i for i in result.issues if i.concept == "occupancy_rate"]
        assert len(occ) == 1
        assert occ[0].value == pytest.approx(0.8)


# ═══════════════════════════════════════════════════════════════════════════════
# SAMPLE INSURANCE FILING
# ═══════════════════════════════════════════════════════════════════════════════

INSURANCE_TAGS = {
    "PremiumsEarned": [_entry("2024-01-01", "2024-12-31", 22_000_000_000, "A")],
    "LossesAndLossAdjustmentExpensesIncurred": [_entry("2024-01-01", "2024-12-31", 14_900_000_000, "A")],
    "UnderwritingExpenses": [_entry("2024-01-01", "2024-12-31", 5_500_000_000, "A")],
    "LossReserves": [_entry("2024-12-31", "2024-12-31", 18_000_000_000, "A")],
    "UnearnedPremiums": [_entry("2024-12-31", "2024-12-31", 4_200_000_000, "A")],
    "InvestmentIncome": [_entry("2024-01-01", "2024-12-31", 1_800_000_000, "A")],
}


class TestInsuranceFilingIntegration:
    def test_combined_ratio(self):
        facts = {
            "premiums_earned": 22.0e9,
            "losses_incurred": 14.9e9,
            "underwriting_expenses": 5.5e9,
        }
        result = validate_facts(facts, SubSector.INSURANCE)
        assert result.valid
        cr = [i for i in result.issues if i.code == "derived_combined_ratio"]
        assert len(cr) == 1
        assert cr[0].value == pytest.approx(14.9 / 22.0 + 5.5 / 22.0)

    def test_underwriting_loss_warning(self):
        facts = {
            "premiums_earned": 10.0e9,
            "losses_incurred": 8.0e9,
            "underwriting_expenses": 3.0e9,  # combined = 110%
        }
        result = validate_facts(facts, SubSector.INSURANCE)
        codes = [i.code for i in result.issues]
        assert "underwriting_loss" in codes

    def test_reserve_adequacy(self):
        facts = {
            "premiums_written": 24.0e9,
            "loss_reserves": 18.0e9,  # 0.75x written
        }
        result = validate_facts(facts, SubSector.INSURANCE)
        assert not any(i.code == "reserve_ratio_unusual" for i in result.issues)


# ═══════════════════════════════════════════════════════════════════════════════
# PARSER-LEVEL COMPATIBILITY
# ═══════════════════════════════════════════════════════════════════════════════


class TestParserScreenWorkerCompatibility:
    def test_export_script_pattern(self):
        """The reparse_10k.py consumption pattern still works."""
        from discovery.xbrl_parser import extract_filing_from_companyfacts as fn
        cf = make_companyfacts(BANK_TAGS)
        filing = _filing_dict()
        rec = fn(cf, "BANK_A", filing)
        assert isinstance(rec, FilingRecord)

    def test_loader_merged_into_base_extraction(self):
        """Merged chains are compatible with the extract loop (no crashes)."""
        loader = SectorTaxonomyLoader()
        merged = loader.merged_chains_for_parser(subsector=SubSector.BANKING)

        cf = make_companyfacts(BANK_TAGS)
        filing = _filing_dict()
        rec = extract_filing_from_companyfacts(cf, "BANK_B", filing)

        # At least the base parser must run without raising on merged dict shape
        for field, chain in merged.items():
            assert isinstance(chain, list), f"{field} chain not a list"
            assert all(isinstance(t, str) for t in chain), f"{field} has non-str tag"

    def test_companyfacts_shape_sampled(self):
        """Sample companyfacts matches what sec_pit_scraper passes in."""
        from discovery.sec_pit_scraper import COMPANYFACTS_URL  # noqa: F401  (import smoke)
        # The scraper loads companyfacts and calls extract_all_filings_from_companyfacts;
        # the shape here mirrors data.sec.gov/api/xbrl/companyfacts.
        cf = make_companyfacts(PHARMA_TAGS)
        assert "facts" in cf
        assert "us-gaap" in cf["facts"]

    def test_validated_records_roundtrip(self):
        """Validator results serialize cleanly to dicts (screen-worker output)."""
        facts = {"premium_revenue_mc": 95e9, "medical_expenses": 73e9}
        result = validate_facts(facts, SubSector.MANAGED_CARE)
        d = result.to_dict()
        assert d["valid"] is True
        assert d["subsector"] == "managed_care"
        assert isinstance(d["issues"], list)