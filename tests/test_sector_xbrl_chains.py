"""Tests for sector-specific XBRL concept chains (SEC 4.x).

Covers:
  - Banking concept chains (interest income/expense, NII, provisions, Basel III)
  - Insurance concept chains (premiums, loss ratios, reserves)
  - Asset management concept chains (AUM, fees, expense ratios)
  - REIT concept chains (FFO, AFFO, NOI, occupancy)
  - Pharma/biotech concept chains (R&D, royalties, IP)
  - Medical device concept chains (revenue, warranty, deferred)
  - Healthcare services concept chains (patient revenue, KPIs)
  - Managed care concept chains (MLR, membership, premium yield)
  - Concept relationship graph integrity
  - Taxonomy loader integration
  - Provenance tracking
  - Compatibility with existing xbrl_parser
"""

import pytest
from typing import Dict, List, Set

from discovery.sector_xbrl_chains import (
    ALL_SECTOR_CONCEPTS,
    ALL_SECTOR_RELATIONS,
    BANKING_CONCEPTS,
    INSURANCE_CONCEPTS,
    ASSET_MGMT_CONCEPTS,
    REIT_CONCEPTS,
    PHARMA_BIOTECH_CONCEPTS,
    MED_DEVICES_CONCEPTS,
    HEALTH_SERVICES_CONCEPTS,
    MANAGED_CARE_CONCEPTS,
    BANKING_RELATIONS,
    INSURANCE_RELATIONS,
    REIT_RELATIONS,
    ASSET_MGMT_RELATIONS,
    PHARMA_RELATIONS,
    HEALTH_SERVICES_RELATIONS,
    MANAGED_CARE_RELATIONS,
    ConceptNode,
    ProvenanceRecord,
    ProvenanceSource,
    Relation,
    RelationType,
    Sector,
    SubSector,
    build_xbrl_concept_map,
    get_concepts_by_sector,
    get_concepts_by_subsector,
    get_sector_concept,
    get_children,
    get_parents,
    get_derived_ratios,
    list_all_subsectors,
    list_all_derived_ratios,
    validate_concept_graph,
)
from discovery.taxonomy_loader import SectorTaxonomyLoader


# ═══════════════════════════════════════════════════════════════════════════════
# BANKING
# ═══════════════════════════════════════════════════════════════════════════════


class TestBankingConcepts:
    def test_interest_income_chain(self):
        node = BANKING_CONCEPTS["interest_income"]
        assert node.sector == Sector.FINANCIAL
        assert SubSector.BANKING in node.sub_sectors
        assert len(node.chain) >= 3
        assert node.chain[0] == "InterestIncomeExpense"

    def test_interest_expense_chain(self):
        node = BANKING_CONCEPTS["interest_expense"]
        assert "InterestExpense" in node.chain
        assert node.provenance.source == ProvenanceSource.FASB_ASC

    def test_net_interest_income_chain(self):
        node = BANKING_CONCEPTS["net_interest_income"]
        assert "NetInterestIncome" in node.chain
        assert node.parent_concept == "net_revenue"

    def test_provision_for_loan_losses(self):
        node = BANKING_CONCEPTS["provision_for_loan_losses"]
        assert "ProvisionForLoanAndLeaseLosses" in node.chain
        assert "ProvisionForCreditLosses" in node.chain

    def test_tier1_capital(self):
        node = BANKING_CONCEPTS["tier1_capital"]
        assert "Tier1Capital" in node.chain
        assert "CommonEquityTier1Capital" in node.chain

    def test_risk_weighted_assets(self):
        node = BANKING_CONCEPTS["risk_weighted_assets"]
        assert "RiskWeightedAssets" in node.chain

    def test_total_rwa(self):
        node = BANKING_CONCEPTS["total_rwa"]
        assert "TotalRiskWeightedAssets" in node.chain

    def test_deposits(self):
        node = BANKING_CONCEPTS["deposits"]
        assert "Deposits" in node.chain
        assert "CustomerAccounts" in node.chain

    def test_all_bankings_concepts_have_sector(self):
        for key, node in BANKING_CONCEPTS.items():
            assert node.sector == Sector.FINANCIAL, f"{key} missing financial sector"
            assert SubSector.BANKING in node.sub_sectors, f"{key} missing banking subsector"

    def test_net_revenue_is_computed(self):
        node = BANKING_CONCEPTS["net_revenue"]
        assert node.is_computed is True
        assert len(node.chain) == 0

    def test_provenance_references(self):
        for key, node in BANKING_CONCEPTS.items():
            assert node.provenance.source in (
                ProvenanceSource.FASB_ASC, ProvenanceSource.INTERNAL
            ), f"{key} has unexpected provenance source"


class TestBankingRelations:
    def test_nii_components(self):
        children = get_children("net_interest_income")
        assert "interest_income" in children
        assert "interest_expense" in children

    def test_net_revenue_components(self):
        children = get_children("net_revenue")
        assert "net_interest_income" in children
        assert "noninterest_income" in children

    def test_interest_expense_sign(self):
        """Interest expense should subtract from NII."""
        relations = [
            r for r in ALL_SECTOR_RELATIONS
            if r.to_concept == "net_interest_income" and r.from_concept == "interest_expense"
        ]
        assert len(relations) == 1
        assert relations[0].weight == -1.0

    def test_total_loans_member_of_assets(self):
        """Total loans should be a member of total assets."""
        relations = [
            r for r in ALL_SECTOR_RELATIONS
            if r.from_concept == "total_loans" and r.to_concept == "total_assets"
        ]
        assert len(relations) == 1
        assert relations[0].relation_type == RelationType.DEFINITION_MEMBER_OF


# ═══════════════════════════════════════════════════════════════════════════════
# INSURANCE
# ═══════════════════════════════════════════════════════════════════════════════


class TestInsuranceConcepts:
    def test_premiums_earned(self):
        node = INSURANCE_CONCEPTS["premiums_earned"]
        assert "PremiumsEarned" in node.chain
        assert SubSector.INSURANCE in node.sub_sectors
        assert node.provenance.reference == "ASC 944-20"

    def test_loss_reserves(self):
        node = INSURANCE_CONCEPTS["loss_reserves"]
        assert "LossReserves" in node.chain
        assert node.provenance.source == ProvenanceSource.NAIC_SAP

    def test_unearned_premium_reserve(self):
        node = INSURANCE_CONCEPTS["unearned_premium_reserve"]
        assert "UnearnedPremiums" in node.chain

    def test_deferred_acquisition_costs(self):
        node = INSURANCE_CONCEPTS["deferred_acquisition_costs"]
        assert "DeferredAcquisitionCosts" in node.chain

    def test_losses_incurred(self):
        node = INSURANCE_CONCEPTS["losses_incurred"]
        assert len(node.chain) >= 3

    def test_investment_income(self):
        node = INSURANCE_CONCEPTS["investment_income"]
        assert "InvestmentIncome" in node.chain


class TestInsuranceRelations:
    def test_loss_ratio_derived(self):
        """Loss ratio = losses_incurred / premiums_earned."""
        derived = get_derived_ratios()
        assert "loss_ratio" in derived

    def test_combined_ratio_derived(self):
        derived = get_derived_ratios()
        assert "combined_ratio" in derived


# ═══════════════════════════════════════════════════════════════════════════════
# ASSET MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════


class TestAssetMgmtConcepts:
    def test_aum(self):
        node = ASSET_MGMT_CONCEPTS["aum"]
        assert "AssetsUnderManagement" in node.chain
        assert SubSector.ASSET_MANAGEMENT in node.sub_sectors

    def test_management_fees(self):
        node = ASSET_MGMT_CONCEPTS["management_fee_revenue"]
        assert "InvestmentAdvisoryFees" in node.chain

    def test_performance_fees(self):
        node = ASSET_MGMT_CONCEPTS["performance_fee_revenue"]
        assert "PerformanceFees" in node.chain

    def test_expense_ratio_related(self):
        derived = get_derived_ratios()
        assert "expense_ratio" in derived


class TestAssetMgmtRelations:
    def test_total_fee_revenue_components(self):
        children = get_children("total_investment_fee_revenue")
        assert "management_fee_revenue" in children
        assert "performance_fee_revenue" in children
        assert "brokerage_commissions" in children


# ═══════════════════════════════════════════════════════════════════════════════
# REITs
# ═══════════════════════════════════════════════════════════════════════════════


class TestREITConcepts:
    def test_rental_revenue(self):
        node = REIT_CONCEPTS["rental_revenue"]
        assert "RentalRevenue" in node.chain
        assert SubSector.REITS in node.sub_sectors

    def test_noi_is_computed(self):
        node = REIT_CONCEPTS["noi"]
        assert node.is_computed is True

    def test_ffo_components(self):
        assert "net_income_common" in REIT_CONCEPTS
        assert "ffo_depreciation_adjustment" in REIT_CONCEPTS
        assert "ffo_gains_adjustment" in REIT_CONCEPTS

    def test_affo_components(self):
        assert "straight_line_rent_adjustment" in REIT_CONCEPTS
        assert "tenant_improvements" in REIT_CONCEPTS
        assert "leasing_commissions" in REIT_CONCEPTS

    def test_occupancy_supplemental(self):
        node = REIT_CONCEPTS["occupied_sqft"]
        assert node.is_computed is True
        assert len(node.chain) == 0  # not in XBRL


class TestREITRelations:
    def test_noi_components(self):
        """NOI = rental_revenue - property_operations - real_estate_taxes."""
        children = get_children("noi")
        assert "rental_revenue" in children
        assert "property_operations_expense" in children
        assert "real_estate_taxes" in children

    def test_ffo_components(self):
        """FFO = net_income + depreciation - gains."""
        children = get_children("ffo")
        assert "net_income_common" in children
        assert "ffo_depreciation_adjustment" in children
        assert "ffo_gains_adjustment" in children

    def test_ffo_gains_negative_weight(self):
        """Gains on sale should be subtracted from FFO."""
        relations = [
            r for r in ALL_SECTOR_RELATIONS
            if r.to_concept == "ffo" and r.from_concept == "ffo_gains_adjustment"
        ]
        assert len(relations) == 1
        assert relations[0].weight == -1.0

    def test_occupancy_ratio_derived(self):
        derived = get_derived_ratios()
        assert "occupancy_rate" in derived

    def test_noi_property_ops_negative(self):
        """Property operations should subtract from NOI."""
        relations = [
            r for r in ALL_SECTOR_RELATIONS
            if r.to_concept == "noi" and r.from_concept == "property_operations_expense"
        ]
        assert len(relations) == 1
        assert relations[0].weight == -1.0


# ═══════════════════════════════════════════════════════════════════════════════
# PHARMA / BIOTECH
# ═══════════════════════════════════════════════════════════════════════════════


class TestPharmaBiotechConcepts:
    def test_rd_expense(self):
        node = PHARMA_BIOTECH_CONCEPTS["pharma_rd_expense"]
        assert "ResearchAndDevelopmentExpense" in node.chain
        assert SubSector.PHARMA_BIOTECH in node.sub_sectors
        assert node.sector == Sector.HEALTHCARE

    def test_royalty_revenue(self):
        node = PHARMA_BIOTECH_CONCEPTS["royalty_revenue"]
        assert "RoyaltyRevenue" in node.chain
        assert "LicenseRevenue" in node.chain

    def test_collaboration_revenue(self):
        node = PHARMA_BIOTECH_CONCEPTS["collaboration_revenue"]
        assert "CollaborationRevenue" in node.chain

    def test_intangible_assets(self):
        node = PHARMA_BIOTECH_CONCEPTS["intangible_assets_gross"]
        assert "IntangibleAssetsGrossExcludingGoodwill" in node.chain

    def test_goodwill(self):
        node = PHARMA_BIOTECH_CONCEPTS["goodwill"]
        assert "Goodwill" in node.chain


class TestPharmaRelations:
    def test_gross_margin_components(self):
        children = get_children("gross_margin_pharma")
        assert "product_revenue" in children
        assert "cost_of_goods_sold_pharma" in children

    def test_total_revenue_components(self):
        children = get_children("total_revenue_pharma")
        assert "product_revenue" in children
        assert "royalty_revenue" in children
        assert "collaboration_revenue" in children


# ═══════════════════════════════════════════════════════════════════════════════
# MEDICAL DEVICES
# ═══════════════════════════════════════════════════════════════════════════════


class TestMedDevicesConcepts:
    def test_device_product_revenue(self):
        node = MED_DEVICES_CONCEPTS["device_product_revenue"]
        assert SubSector.MEDICAL_DEVICES in node.sub_sectors

    def test_warranty_reserve(self):
        node = MED_DEVICES_CONCEPTS["warranty_reserve"]
        assert "WarrantyReserve" in node.chain
        assert "ProductWarrantyLiability" in node.chain

    def test_deferred_revenue(self):
        node = MED_DEVICES_CONCEPTS["deferred_revenue_devices"]
        assert "DeferredRevenue" in node.chain


# ═══════════════════════════════════════════════════════════════════════════════
# HEALTHCARE SERVICES
# ═══════════════════════════════════════════════════════════════════════════════


class TestHealthcareServicesConcepts:
    def test_patient_revenue(self):
        node = HEALTH_SERVICES_CONCEPTS["patient_revenue"]
        assert "PatientRevenue" in node.chain
        assert SubSector.HEALTHCARE_SERVICES in node.sub_sectors

    def test_salaries_and_wages(self):
        node = HEALTH_SERVICES_CONCEPTS["salaries_and_wages"]
        assert "SalariesAndWages" in node.chain

    def test_discharges_supplemental(self):
        node = HEALTH_SERVICES_CONCEPTS["discharges"]
        assert node.is_computed is True
        assert len(node.chain) == 0


class TestHealthcareServicesRelations:
    def test_revenue_per_visit_derived(self):
        derived = get_derived_ratios()
        assert "revenue_per_visit" in derived

    def test_operating_expenses_components(self):
        children = get_children("operating_expenses_healthcare")
        assert "salaries_and_wages" in children
        assert "supplies_expense" in children
        assert "provider_fees" in children


# ═══════════════════════════════════════════════════════════════════════════════
# MANAGED CARE
# ═══════════════════════════════════════════════════════════════════════════════


class TestManagedCareConcepts:
    def test_premium_revenue_mc(self):
        node = MANAGED_CARE_CONCEPTS["premium_revenue_mc"]
        assert SubSector.MANAGED_CARE in node.sub_sectors

    def test_medical_expenses(self):
        node = MANAGED_CARE_CONCEPTS["medical_expenses"]
        assert "MedicalExpenses" in node.chain

    def test_total_members_supplemental(self):
        node = MANAGED_CARE_CONCEPTS["total_members"]
        assert node.is_computed is True
        assert node.unit == "shares"

    def test_medicaid_members(self):
        node = MANAGED_CARE_CONCEPTS["medicaid_members"]
        assert node.is_computed is True

    def test_unpaid_claims_liability(self):
        node = MANAGED_CARE_CONCEPTS["unpaid_claims_liability"]
        assert "UnpaidClaimsLiability" in node.chain


class TestManagedCareRelations:
    def test_medical_loss_ratio_derived(self):
        derived = get_derived_ratios()
        assert "medical_loss_ratio" in derived

    def test_administrative_ratio_derived(self):
        derived = get_derived_ratios()
        assert "administrative_ratio" in derived

    def test_payer_mix_commercial_derived(self):
        derived = get_derived_ratios()
        assert "payer_mix_commercial" in derived

    def test_payer_mix_ma_derived(self):
        derived = get_derived_ratios()
        assert "payer_mix_ma" in derived


# ═══════════════════════════════════════════════════════════════════════════════
# GRAPH INTEGRITY
# ═══════════════════════════════════════════════════════════════════════════════


class TestConceptGraphIntegrity:
    def test_global_registry_populated(self):
        assert len(ALL_SECTOR_CONCEPTS) >= 60

    def test_global_relations_populated(self):
        assert len(ALL_SECTOR_RELATIONS) >= 20

    def test_no_duplicate_concept_keys(self):
        keys = list(ALL_SECTOR_CONCEPTS.keys())
        assert len(keys) == len(set(keys))

    def test_all_concepts_have_sector(self):
        for key, node in ALL_SECTOR_CONCEPTS.items():
            assert isinstance(node.sector, Sector), f"{key} missing sector"

    def test_all_concepts_have_subsector(self):
        for key, node in ALL_SECTOR_CONCEPTS.items():
            assert len(node.sub_sectors) > 0, f"{key} has no subsector"

    def test_validation_passes(self):
        errors = validate_concept_graph()
        # Should be empty for a well-formed graph
        assert errors == [], f"Validation errors: {errors}"

    def test_relations_reference_valid_concepts(self):
        concept_keys = set(ALL_SECTOR_CONCEPTS.keys())
        derived = set(get_derived_ratios())
        for r in ALL_SECTOR_RELATIONS:
            assert r.from_concept in concept_keys or r.from_concept in derived, \
                f"Relation source {r.from_concept} not found"
            assert r.to_concept in concept_keys or r.to_concept in derived, \
                f"Relation target {r.to_concept} not found"

    def test_no_self_relations(self):
        for r in ALL_SECTOR_RELATIONS:
            assert r.from_concept != r.to_concept, \
                f"Self-relation on {r.from_concept}"


# ═══════════════════════════════════════════════════════════════════════════════
# FILTERS & QUERIES
# ═══════════════════════════════════════════════════════════════════════════════


class TestConceptFilters:
    def test_financial_sector_filter(self):
        fin = get_concepts_by_sector(Sector.FINANCIAL)
        assert len(fin) > 0
        for k, v in fin.items():
            assert v.sector == Sector.FINANCIAL

    def test_healthcare_sector_filter(self):
        hc = get_concepts_by_sector(Sector.HEALTHCARE)
        assert len(hc) > 0
        for k, v in hc.items():
            assert v.sector == Sector.HEALTHCARE

    def test_subsector_filter_banking(self):
        banks = get_concepts_by_subsector(SubSector.BANKING)
        assert len(banks) >= 12
        for k, v in banks.items():
            assert SubSector.BANKING in v.sub_sectors

    def test_subsector_filter_insurance(self):
        ins = get_concepts_by_subsector(SubSector.INSURANCE)
        assert len(ins) >= 10
        for k, v in ins.items():
            assert SubSector.INSURANCE in v.sub_sectors

    def test_subsector_filter_reits(self):
        reits = get_concepts_by_subsector(SubSector.REITS)
        assert len(reits) >= 10

    def test_subsector_filter_pharma(self):
        pharma = get_concepts_by_subsector(SubSector.PHARMA_BIOTECH)
        assert len(pharma) >= 8

    def test_subsector_filter_managed_care(self):
        mc = get_concepts_by_subsector(SubSector.MANAGED_CARE)
        assert len(mc) >= 8

    def test_build_xbrl_concept_map_filters_empty_chains(self):
        """build_xbrl_concept_map should only return concepts with XBRL tags."""
        mapping = build_xbrl_concept_map()
        for key, chain in mapping.items():
            assert len(chain) > 0, f"{key} has empty chain in concept map"

    def test_build_xbrl_concept_map_sector_filter(self):
        fin_map = build_xbrl_concept_map(sector=Sector.FINANCIAL)
        hc_map = build_xbrl_concept_map(sector=Sector.HEALTHCARE)
        # They should be disjoint
        overlap = set(fin_map.keys()) & set(hc_map.keys())
        assert overlap == set()

    def test_list_all_subsectors(self):
        subsectors = list_all_subsectors()
        assert SubSector.BANKING in subsectors
        assert SubSector.HEALTHCARE_SERVICES in subsectors
        assert len(subsectors) >= 8


# ═══════════════════════════════════════════════════════════════════════════════
# PROVENANCE
# ═══════════════════════════════════════════════════════════════════════════════


class TestProvenance:
    def test_all_concepts_have_provenance(self):
        for key, node in ALL_SECTOR_CONCEPTS.items():
            p = node.provenance
            assert isinstance(p, ProvenanceRecord), f"{key} missing provenance"
            assert isinstance(p.source, ProvenanceSource), f"{key} bad provenance source"
            assert p.version != "", f"{key} missing provenance version"

    def test_fasb_asc_references(self):
        """Banking and insurance should use ASC references."""
        for key in ("interest_income", "interest_expense", "net_interest_income"):
            node = BANKING_CONCEPTS[key]
            assert node.provenance.source == ProvenanceSource.FASB_ASC

    def test_naic_sap_references(self):
        """Insurance reserves should use NAIC SAP."""
        for key in ("loss_reserves", "statutory_capital_surplus"):
            node = INSURANCE_CONCEPTS[key]
            assert node.provenance.source == ProvenanceSource.NAIC_SAP

    def test_internal_references_for_supplemental(self):
        """Supplemental data (not in XBRL) uses INTERNAL provenance."""
        for key in ("occupied_sqft", "total_leasable_sqft", "discharges",
                     "total_members", "commercial_members"):
            if key in ALL_SECTOR_CONCEPTS:
                node = ALL_SECTOR_CONCEPTS[key]
                assert node.provenance.source == ProvenanceSource.INTERNAL

    def test_relation_provenance(self):
        """All relations should have provenance."""
        for r in ALL_SECTOR_RELATIONS:
            assert isinstance(r.provenance, ProvenanceRecord)
            assert isinstance(r.provenance.source, ProvenanceSource)


# ═══════════════════════════════════════════════════════════════════════════════
# TAXONOMY LOADER
# ═══════════════════════════════════════════════════════════════════════════════


class TestSectorTaxonomyLoader:
    @pytest.fixture
    def loader(self):
        return SectorTaxonomyLoader()

    def test_loader_instantiation(self, loader):
        assert loader is not None

    def test_get_concept_chains_all(self, loader):
        chains = loader.get_concept_chains()
        # Should include both base and sector chains
        assert "revenue" in chains  # base
        assert "interest_income" in chains  # sector

    def test_get_concept_chains_financial(self, loader):
        chains = loader.get_concept_chains(sector=Sector.FINANCIAL)
        assert "interest_income" in chains
        # Should NOT include healthcare concepts
        assert "pharma_rd_expense" not in chains

    def test_get_concept_chains_healthcare(self, loader):
        chains = loader.get_concept_chains(sector=Sector.HEALTHCARE)
        assert "pharma_rd_expense" in chains
        # Should NOT include banking concepts
        assert "interest_income" not in chains

    def test_get_concept_chains_subsector(self, loader):
        chains = loader.get_concept_chains(subsector=SubSector.BANKING)
        assert "interest_income" in chains
        assert "net_interest_income" in chains
        # Insurance concepts should NOT be present
        assert "premiums_earned" not in chains

    def test_get_concept_chains_no_base(self, loader):
        chains = loader.get_concept_chains(include_base=False)
        assert "revenue" not in chains  # base chain excluded
        assert "interest_income" in chains  # sector chain included

    def test_get_chain(self, loader):
        chain = loader.get_chain("interest_income")
        assert "InterestIncomeExpense" in chain

    def test_get_chain_base_fallback(self, loader):
        """Base chain fallback works for non-sector concepts."""
        chain = loader.get_chain("revenue")
        assert "RevenueFromContractWithCustomerExcludingAssessedTax" in chain

    def test_get_node(self, loader):
        node = loader.get_node("interest_income")
        assert node is not None
        assert node.sector == Sector.FINANCIAL

    def test_get_node_missing(self, loader):
        node = loader.get_node("nonexistent_concept")
        assert node is None

    def test_get_provenance(self, loader):
        prov = loader.get_provenance("interest_income")
        assert prov is not None
        assert prov["source"] == "fasb_asc"

    def test_get_children(self, loader):
        children = loader.get_children("net_interest_income")
        assert "interest_income" in children

    def test_get_parents(self, loader):
        parents = loader.get_parents("interest_income")
        assert "net_interest_income" in parents

    def test_get_relations(self, loader):
        rels = loader.get_relations("interest_income")
        assert len(rels) > 0
        assert any(r["to"] == "net_interest_income" for r in rels)

    def test_get_derived_ratios(self, loader):
        ratios = loader.get_derived_ratios()
        assert "medical_loss_ratio" in ratios
        assert "loss_ratio" in ratios

    def test_validate(self, loader):
        errors = loader.validate()
        assert errors == []

    def test_merged_chains_for_parser(self, loader):
        merged = loader.merged_chains_for_parser()
        # Should be usable in xbrl_parser context
        assert "revenue" in merged
        assert "interest_income" in merged

    def test_to_dict(self, loader):
        d = loader.to_dict()
        assert "concepts" in d
        assert "relations" in d
        assert "derived_ratios" in d
        assert "validation_errors" in d
        assert d["validation_errors"] == []

    def test_to_dict_concept_structure(self, loader):
        d = loader.to_dict()
        interest = d["concepts"]["interest_income"]
        assert "chain" in interest
        assert "sector" in interest
        assert "provenance" in interest
        assert interest["sector"] == "financial"

    def test_summary(self, loader):
        s = loader.summary()
        assert s["total_concepts"] >= 60
        assert s["total_relations"] >= 20
        assert "financial" in s["by_sector"]
        assert "healthcare" in s["by_sector"]
        assert len(s["sub_sectors"]) >= 8


# ═══════════════════════════════════════════════════════════════════════════════
# COMPATIBILITY WITH EXISTING xbrl_parser
# ═══════════════════════════════════════════════════════════════════════════════


class TestXBRLParserCompatibility:
    def test_base_chains_unchanged(self):
        """The base CONCEPT_CHAINS should not be modified by sector imports."""
        from discovery.xbrl_parser import CONCEPT_CHAINS
        assert "revenue" in CONCEPT_CHAINS
        assert "ebit" in CONCEPT_CHAINS
        # Should still be a dict of lists
        assert isinstance(CONCEPT_CHAINS["revenue"], list)

    def test_sector_chains_extend_not_replace(self):
        """Sector chains add new keys, don't replace base keys."""
        from discovery.xbrl_parser import CONCEPT_CHAINS
        base_keys = set(CONCEPT_CHAINS.keys())
        sector_keys = set(ALL_SECTOR_CONCEPTS.keys())
        # New sector keys should be disjoint from base keys
        overlap = base_keys & sector_keys
        assert overlap == set(), f"Overlapping keys: {overlap}"

    def test_loader_does_not_mutate_base(self):
        """SectorTaxonomyLoader should not mutate the original CONCEPT_CHAINS."""
        from discovery.xbrl_parser import CONCEPT_CHAINS
        original_revenue = list(CONCEPT_CHAINS["revenue"])
        _ = SectorTaxonomyLoader()
        assert CONCEPT_CHAINS["revenue"] == original_revenue


# ═══════════════════════════════════════════════════════════════════════════════
# SERIALIZATION
# ═══════════════════════════════════════════════════════════════════════════════


class TestSerialization:
    def test_to_dict_is_json_serializable(self, tmp_path):
        import json
        loader = SectorTaxonomyLoader()
        d = loader.to_dict()
        path = tmp_path / "taxonomy.json"
        with open(path, "w") as f:
            json.dump(d, f, indent=2)
        # Read back
        with open(path, "r") as f:
            loaded = json.load(f)
        assert loaded["concepts"]["interest_income"]["chain"][0] == "InterestIncomeExpense"

    def test_all_derived_ratios(self):
        derived = list_all_derived_ratios()
        assert isinstance(derived, list)
        assert "tier1_ratio" in derived or "tier1_ratio" in [r.to_concept for r in ALL_SECTOR_RELATIONS if r.relation_type == RelationType.DERIVED]


# ═══════════════════════════════════════════════════════════════════════════════
# ENUMERATION COVERAGE
# ═══════════════════════════════════════════════════════════════════════════════


class TestEnumCoverage:
    def test_all_subsectors_have_concepts(self):
        for sub in SubSector:
            concepts = get_concepts_by_subsector(sub)
            assert len(concepts) > 0, f"SubSector {sub.value} has no concepts"

    def test_all_sectors_have_concepts(self):
        for sec in Sector:
            concepts = get_concepts_by_sector(sec)
            assert len(concepts) > 0, f"Sector {sec.value} has no concepts"

    def test_all_relation_types_used(self):
        used_types = {r.relation_type for r in ALL_SECTOR_RELATIONS}
        assert RelationType.CALC_SUM in used_types
        assert RelationType.DERIVED in used_types
        assert RelationType.DEFINITION_MEMBER_OF in used_types
