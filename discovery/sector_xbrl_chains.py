"""Sector-specific XBRL concept chains — Financial and Healthcare (SEC 4.x).

Extends the base CONCEPT_CHAINS in ``xbrl_parser.py`` with hierarchical,
sector-aware concept mappings.  Each sector defines:

  1. **Concept chains** — ordered fallback lists of XBRL tags per friendly field.
  2. **Parent-child graphs** — which concepts sum/aggregate into which parents
     (calculation linkbase).
  3. **Presentation trees** — display ordering for human-readable statements.
  4. **Definition linkbases** — semantic relationships (e.g. "is-a", "member-of").

All data structures are immutable frozen dataclasses.  No RNG, no mutable
global state, no network I/O.  Every mapping carries provenance metadata.

Terminology:
  - ``concept_key``   = friendly name (e.g. ``"net_interest_income"``)
  - ``xbrl_tag``      = US-GAAP or custom tag (e.g. ``"InterestIncomeDepositsWithFinancialInstitutions"``)
  - ``chain``          = ordered list of xbrl_tag candidates (first non-null wins)
  - ``concept_node``   = a node in the relationship graph with chain + metadata
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple

# ── Enums ────────────────────────────────────────────────────────────────────

class Sector(str, Enum):
    """Supported sector classifications."""
    FINANCIAL = "financial"
    HEALTHCARE = "healthcare"
    TECHNOLOGY = "technology"
    AUTOMOTIVE = "automotive"
    CONSUMER_ELECTRONICS = "consumer_electronics"
    ECOMMERCE = "ecommerce"


class SubSector(str, Enum):
    """Sub-sector classification within sectors."""
    # Financial
    BANKING = "banking"
    INSURANCE = "insurance"
    ASSET_MANAGEMENT = "asset_management"
    REITS = "reits"
    # Healthcare
    PHARMA_BIOTECH = "pharma_biotech"
    MEDICAL_DEVICES = "medical_devices"
    HEALTHCARE_SERVICES = "healthcare_services"
    MANAGED_CARE = "managed_care"
    # Technology
    SEMICONDUCTORS = "semiconductors"
    SOFTWARE_CLOUD = "software_cloud"
    # Automotive
    AUTOMOTIVE_EV = "automotive_ev"
    # Consumer Electronics
    CONSUMER_ELECTRONICS = "consumer_electronics"
    # E-Commerce
    ECOMMERCE = "ecommerce"


class RelationType(str, Enum):
    """Types of relationships in the concept graph."""
    CALC_SUM = "calc_sum"           # parent = sum(children)
    CALC_WEIGHTED = "calc_weighted" # parent = weighted sum
    DEFINITION_IS_A = "def_is_a"   # child is-a parent (hierarchy)
    DEFINITION_MEMBER_OF = "def_member_of"  # child is member of parent
    PRESENTATION_CHILD = "pres_child"  # presentation tree
    DERIVED = "derived"             # computed from other concepts


class ProvenanceSource(str, Enum):
    """Source of the concept mapping."""
    SEC_EDGAR = "sec_edgar"
    FASB_ASC = "fasb_asc"
    IASB_IFRS = "iasb_ifrs"
    NAIC_SAP = "naic_sap"           # insurance
    CMS_HCPCS = "cms_hcpcs"         # healthcare codes
    FDA_510K = "fda_510k"           # medical device clearance
    INTERNAL = "internal"           # derived/computed


# ── Core data structures ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProvenanceRecord:
    """Provenance metadata for a concept mapping."""
    source: ProvenanceSource
    reference: str = ""          # e.g. "ASC 606-10-45-1"
    version: str = "2024"        # taxonomy year
    notes: str = ""


@dataclass(frozen=True)
class ConceptNode:
    """A single concept in the relationship graph.

    Attributes:
        concept_key: Friendly name used in the pipeline.
        chain: Ordered list of XBRL tags to try (first non-null wins).
        unit: Expected unit ("USD", "shares", "pure", etc.)
        sector: Primary sector.
        sub_sectors: Which sub-sectors this concept belongs to.
        provenance: Source / reference metadata.
        parent_concept: Key of the parent concept in calculation tree (None if top-level).
        is_computed: Whether this concept is computed rather than directly fetched.
    """
    concept_key: str
    chain: Tuple[str, ...]
    unit: str = "USD"
    sector: Sector = Sector.FINANCIAL
    sub_sectors: Tuple[SubSector, ...] = ()
    provenance: ProvenanceRecord = ProvenanceRecord(source=ProvenanceSource.INTERNAL)
    parent_concept: Optional[str] = None
    is_computed: bool = False


@dataclass(frozen=True)
class Relation:
    """A directed edge in the concept relationship graph."""
    from_concept: str   # child / component
    to_concept: str     # parent / aggregate
    relation_type: RelationType
    weight: float = 1.0  # for CALC_WEIGHTED
    provenance: ProvenanceRecord = ProvenanceRecord(source=ProvenanceSource.INTERNAL)


# ── Sector concept registries ────────────────────────────────────────────────

def _p(source: ProvenanceSource, ref: str = "", ver: str = "2024") -> ProvenanceRecord:
    """Shorthand for ProvenanceRecord."""
    return ProvenanceRecord(source=source, reference=ref, version=ver)


# ═══════════════════════════════════════════════════════════════════════════════
# FINANCIAL SECTOR
# ═══════════════════════════════════════════════════════════════════════════════

# ── Banking ───────────────────────────────────────────────────────────────────

BANKING_CONCEPTS: Dict[str, ConceptNode] = {
    # ── Interest income / expense ──
    "interest_income": ConceptNode(
        concept_key="interest_income",
        chain=(
            "InterestIncomeExpense",
            "InterestIncome",
            "InterestIncomeDepositsWithFinancialInstitutions",
            "InterestAndDividendIncomeOperating",
            "InvestmentIncomeNet",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.BANKING,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 310-20-45"),
    ),
    "interest_expense": ConceptNode(
        concept_key="interest_expense",
        chain=(
            "InterestExpense",
            "InterestExpenseDeposits",
            "InterestExpenseNoninterestExpense",
            "InterestAndDebtExpense",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.BANKING,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 835-20-45"),
    ),
    "net_interest_income": ConceptNode(
        concept_key="net_interest_income",
        chain=(
            "NetInterestIncome",
            "InterestIncomeNet",
            "InterestIncomeNetOfInterestExpense",
        ),
        parent_concept="net_revenue",
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.BANKING,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 942-20"),
    ),

    # ── Net interest margin (derived) ──
    "earning_assets": ConceptNode(
        concept_key="earning_assets",
        chain=(
            "EarningAssets",
            "EarningAssetsAverage",
            "InterestBearingDeposits",
            "FederalFundsSoldSecuritiesPurchased",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.BANKING,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 320"),
    ),

    # ── Loan loss provisions ──
    "provision_for_loan_losses": ConceptNode(
        concept_key="provision_for_loan_losses",
        chain=(
            "ProvisionForLoanAndLeaseLosses",
            "ProvisionForCreditLosses",
            "ProvisionForDoubtfulAccounts",
            "BadDebtExpense",
        ),
        parent_concept="noninterest_expense",
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.BANKING,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 326-20"),
    ),
    "allowance_for_loan_losses": ConceptNode(
        concept_key="allowance_for_loan_losses",
        chain=(
            "AllowanceForLoanAndLeaseLosses",
            "AllowanceForCreditLosses",
            "AllowanceForDoubtfulAccounts",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.BANKING,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 326-20-30"),
    ),
    "net_chargeoffs": ConceptNode(
        concept_key="net_chargeoffs",
        chain=(
            "NetChargeOffs",
            "NetChargeoffs",
            "LoanLossesChargedAgainstAllowance",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.BANKING,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 310-20"),
    ),

    # ── Tier 1 capital / RWA (Basel III) ──
    "tier1_capital": ConceptNode(
        concept_key="tier1_capital",
        chain=(
            "Tier1Capital",
            "CommonEquityTier1Capital",
            "ShareholdersEquity",
            "StockholdersEquity",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.BANKING,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 942"),
    ),
    "risk_weighted_assets": ConceptNode(
        concept_key="risk_weighted_assets",
        chain=(
            "RiskWeightedAssets",
            "RiskWeightedAssetsRWA",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.BANKING,),
        provenance=_p(ProvenanceSource.INTERNAL, "Basel III disclosure"),
    ),
    "total_rwa": ConceptNode(
        concept_key="total_rwa",
        chain=(
            "TotalRiskWeightedAssets",
            "RiskWeightedAssets",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.BANKING,),
        provenance=_p(ProvenanceSource.INTERNAL, "Basel III disclosure"),
    ),

    # ── Balance sheet components ──
    "total_loans": ConceptNode(
        concept_key="total_loans",
        chain=(
            "LoansAndLeasesReceivableNet",
            "LoansAndLeasesReceivableNetOfAllowance",
            "LoansNet",
            "LoansAndLeases",
        ),
        parent_concept="total_assets",
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.BANKING,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 310-20"),
    ),
    "deposits": ConceptNode(
        concept_key="deposits",
        chain=(
            "Deposits",
            "CustomerAccounts",
            "DepositsLiabilities",
            "DepositsInOtherFinancialInstitutions",
        ),
        parent_concept="total_liabilities",
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.BANKING,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 942-20"),
    ),
    "noninterest_income": ConceptNode(
        concept_key="noninterest_income",
        chain=(
            "NoninterestIncome",
            "OtherNoninterestIncome",
            "NoninterestIncomeOther",
            "GainOnSaleOfLoans",
        ),
        parent_concept="net_revenue",
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.BANKING,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 942-20"),
    ),
    "noninterest_expense": ConceptNode(
        concept_key="noninterest_expense",
        chain=(
            "NoninterestExpense",
            "OtherNoninterestExpense",
            "NoninterestExpenseEmployeeBenefits",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.BANKING,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 942-20"),
    ),
    "net_revenue": ConceptNode(
        concept_key="net_revenue",
        chain=(),  # computed: net_interest_income + noninterest_income
        is_computed=True,
        parent_concept=None,  # top-level
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.BANKING,),
        provenance=_p(ProvenanceSource.INTERNAL, "Net Revenue = NII + Noninterest Income"),
    ),
    "total_assets": ConceptNode(
        concept_key="total_assets",
        chain=(),
        is_computed=True,
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.BANKING,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 942-210-45"),
    ),
}

# ── Insurance ─────────────────────────────────────────────────────────────────

INSURANCE_CONCEPTS: Dict[str, ConceptNode] = {
    "premiums_earned": ConceptNode(
        concept_key="premiums_earned",
        chain=(
            "PremiumsEarned",
            "PremiumsAndOtherConsiderations",
            "DirectWrittenPremiums",
            "PremiumRevenue",
            "InsurancePremiumsWritten",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.INSURANCE,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 944-20"),
    ),
    "premiums_written": ConceptNode(
        concept_key="premiums_written",
        chain=(
            "PremiumsWritten",
            "DirectWrittenPremiums",
            "GrossPremiumsWritten",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.INSURANCE,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 944-20"),
    ),
    "losses_incurred": ConceptNode(
        concept_key="losses_incurred",
        chain=(
            "LossesAndLossAdjustmentExpensesIncurred",
            "ClaimsAndClaimsSettlementExpenses",
            "LossesIncurred",
            "NetIncurredClaims",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.INSURANCE,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 944-30"),
    ),
    "loss_adjustment_expenses": ConceptNode(
        concept_key="loss_adjustment_expenses",
        chain=(
            "LossAdjustmentExpenses",
            "LossAdjustmentExpense",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.INSURANCE,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 944-30"),
    ),
    "underwriting_expenses": ConceptNode(
        concept_key="underwriting_expenses",
        chain=(
            "UnderwritingExpenses",
            "AcquisitionExpenses",
            "PolicyAcquisitionCosts",
            "CommissionsAndGeneralExpenses",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.INSURANCE,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 944-30"),
    ),

    # ── Reserves ──
    "loss_reserves": ConceptNode(
        concept_key="loss_reserves",
        chain=(
            "LossReserves",
            "LossReservesOnClosedClaims",
            "ClaimsReserve",
            "UnpaidLossesAndLossAdjustmentExpenses",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.INSURANCE,),
        provenance=_p(ProvenanceSource.NAIC_SAP, "Schedule P"),
    ),
    "unearned_premium_reserve": ConceptNode(
        concept_key="unearned_premium_reserve",
        chain=(
            "UnearnedPremiums",
            "UnearnedPremiumReserve",
            "UnearnedPremiumsLiability",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.INSURANCE,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 944-20"),
    ),
    "loss_reserve_discount": ConceptNode(
        concept_key="loss_reserve_discount",
        chain=(
            "DiscountFromUnpaidLosses",
            "UnpaidLossesDiscount",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.INSURANCE,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 944-30-30"),
    ),

    # ── Investment income ──
    "investment_income": ConceptNode(
        concept_key="investment_income",
        chain=(
            "InvestmentIncome",
            "InvestmentIncomeNet",
            "NetInvestmentIncome",
            "InterestAndDividendIncomeOperating",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.INSURANCE,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 944-20"),
    ),
    "realized_gains_losses": ConceptNode(
        concept_key="realized_gains_losses",
        chain=(
            "RealizedGainsLossesOnInvestments",
            "RealizedGainsOnInvestments",
            "NetRealizedGainsLossesOnInvestments",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.INSURANCE,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 320-10-45"),
    ),

    # ── Statutory capital ──
    "statutory_capital_surplus": ConceptNode(
        concept_key="statutory_capital_surplus",
        chain=(
            "StatutorySurplus",
            "StatutoryCapitalAndSurplus",
            "AdditionalPaidInCapital",
            "StockholdersEquity",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.INSURANCE,),
        provenance=_p(ProvenanceSource.NAIC_SAP, "Schedule A"),
    ),

    # ── Deferred acquisition costs ──
    "deferred_acquisition_costs": ConceptNode(
        concept_key="deferred_acquisition_costs",
        chain=(
            "DeferredAcquisitionCosts",
            "DeferredPolicyAcquisitionCosts",
            "DacAsset",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.INSURANCE,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 944-30"),
    ),

# ── Policyholder liabilities ──
    "policyholder_liabilities": ConceptNode(
        concept_key="policyholder_liabilities",
        chain=(
            "PolicyholderLiabilities",
            "LiabilityForPolicyholderBenefits",
            "PolicyReserves",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.INSURANCE,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 944-40"),
    ),

    # ── Computed: Combined ratio ──
    "combined_ratio": ConceptNode(
        concept_key="combined_ratio",
        chain=(),  # computed: loss_ratio + expense_ratio
        is_computed=True,
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.INSURANCE,),
        provenance=_p(ProvenanceSource.NAIC_SAP, "Combined ratio definition"),
    ),
}


# ── Asset management ─────────────────────────────────────────────────────────

ASSET_MGMT_CONCEPTS: Dict[str, ConceptNode] = {
    "aum": ConceptNode(
        concept_key="aum",
        chain=(
            "AssetsUnderManagement",
            "ManagedAssets",
            "TotalAssetsUnderManagement",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.ASSET_MANAGEMENT,),
        provenance=_p(ProvenanceSource.INTERNAL, "AUM disclosure"),
    ),
    "management_fee_revenue": ConceptNode(
        concept_key="management_fee_revenue",
        chain=(
            "InvestmentAdvisoryFees",
            "ManagementFees",
            "AdvisoryFees",
            "InvestmentManagementFees",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.ASSET_MANAGEMENT,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 606-10"),
    ),
    "performance_fee_revenue": ConceptNode(
        concept_key="performance_fee_revenue",
        chain=(
            "PerformanceFees",
            "IncentiveFees",
            "PerformanceBasedFees",
            "AllocatedPerformanceFees",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.ASSET_MANAGEMENT,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 606-10"),
    ),
    "total_investment_fee_revenue": ConceptNode(
        concept_key="total_investment_fee_revenue",
        chain=(
            "InvestmentAdvisoryAndManagementFees",
            "FeeAndCommissionRevenue",
            "InvestmentAdvisoryFees",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.ASSET_MANAGEMENT,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 606-10"),
    ),
    "brokerage_commissions": ConceptNode(
        concept_key="brokerage_commissions",
        chain=(
            "BrokerageRevenue",
            "Commissions",
            "BrokerageCommissions",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.ASSET_MANAGEMENT,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 606-10"),
    ),

    # ── Incentive fees (carried interest) ──
    "incentive_fees": ConceptNode(
        concept_key="incentive_fees",
        chain=(
            "IncentiveFees",
            "PerformanceFeesAllocatedToGeneralPartner",
            "CarriedInterest",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.ASSET_MANAGEMENT,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 820"),
    ),

    # ── Fund equity / book value ──
    "partners_capital": ConceptNode(
        concept_key="partners_capital",
        chain=(
            "PartnersCapital",
            "LimitedPartnersCapital",
            "StockholdersEquity",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.ASSET_MANAGEMENT,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 946"),
    ),

    # ── Fund-level expense ratio numerator ──
    "fund_operating_expenses": ConceptNode(
        concept_key="fund_operating_expenses",
        chain=(
            "FundOperatingExpenses",
            "OperatingExpenses",
            "OtherOperatingExpenses",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.ASSET_MANAGEMENT,),
        provenance=_p(ProvenanceSource.INTERNAL, "SEC N-MEN/N-CSR"),
    ),
}

# ── REITs ─────────────────────────────────────────────────────────────────────

REIT_CONCEPTS: Dict[str, ConceptNode] = {
    "rental_revenue": ConceptNode(
        concept_key="rental_revenue",
        chain=(
            "RentalRevenue",
            "RentalIncome",
            "MinimumLeasePayments",
            "OperatingLeaseIncome",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.REITS,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 842-10"),
    ),
    "property_operations_expense": ConceptNode(
        concept_key="property_operations_expense",
        chain=(
            "PropertyOperatingExpenses",
            "OperatingExpensesOfRealEstate",
            "RealEstateOperatingExpenses",
            "PropertyMaintenance",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.REITS,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 942-20"),
    ),
    "depreciation_real_estate": ConceptNode(
        concept_key="depreciation_real_estate",
        chain=(
            "DepreciationOfRealEstateAssets",
            "DepreciationDepletionAndAmortization",
            "DepreciationAndAmortization",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.REITS,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 360-10"),
    ),
    "real_estate_taxes": ConceptNode(
        concept_key="real_estate_taxes",
        chain=(
            "RealEstateTaxes",
            "PropertyTaxes",
            "RealEstateTaxExpense",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.REITS,),
        provenance=_p(ProvenanceSource.INTERNAL, "REIT specific"),
    ),

    # ── Acquisition / disposition of properties ──
    "acquisitions_of_properties": ConceptNode(
        concept_key="acquisitions_of_properties",
        chain=(
            "PaymentsToAcquireRealEstate",
            "AcquisitionsOfRealEstate",
            "CapitalExpenditures",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.REITS,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 360-10"),
    ),
    "dispositions_of_properties": ConceptNode(
        concept_key="dispositions_of_properties",
        chain=(
            "ProceedsFromSaleOfRealEstate",
            "DispositionsOfRealEstate",
            "GainOnSaleOfRealEstate",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.REITS,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 360-10"),
    ),

    # ── Debt (REITs heavily leveraged) ──
    "mortgage_debt": ConceptNode(
        concept_key="mortgage_debt",
        chain=(
            "SecuredByMortgageReceivable",
            "MortgagePayable",
            "MortgageNotesPayable",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.REITS,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 842-10"),
    ),

    # ── FFO/AFFO (computed) ──
    "net_income_common": ConceptNode(
        concept_key="net_income_common",
        chain=(
            "NetIncomeLoss",
            "NetIncomeLossAvailableToCommonStockholdersBasic",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.REITS,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 260"),
    ),
    "ffo_depreciation_adjustment": ConceptNode(
        concept_key="ffo_depreciation_adjustment",
        chain=(
            "DepreciationDepletionAndAmortization",
            "DepreciationAndAmortization",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.REITS,),
        provenance=_p(ProvenanceSource.INTERNAL, "NAREIT FFO definition"),
    ),
    "ffo_gains_adjustment": ConceptNode(
        concept_key="ffo_gains_adjustment",
        chain=(
            "GainOnSaleOfRealEstate",
            "RealizedGainsLossesOnSaleOfRealEstate",
            "GainLossOnDisposalsOfRealEstate",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.REITS,),
        provenance=_p(ProvenanceSource.INTERNAL, "NAREIT FFO definition"),
    ),

    # ── AFFO adjustments (computed) ──
    "straight_line_rent_adjustment": ConceptNode(
        concept_key="straight_line_rent_adjustment",
        chain=(
            "StraightLineRentAdjustment",
            "StraightLineRent",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.REITS,),
        provenance=_p(ProvenanceSource.INTERNAL, "AFFO non-cash adjustment"),
    ),
    "tenant_improvements": ConceptNode(
        concept_key="tenant_improvements",
        chain=(
            "TenantImprovementAllowance",
            "TenantImprovements",
            "CapitalizedTenantImprovements",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.REITS,),
        provenance=_p(ProvenanceSource.INTERNAL, "AFFO adjustment"),
    ),
    "leasing_commissions": ConceptNode(
        concept_key="leasing_commissions",
        chain=(
            "LeasingCommissions",
            "CapitalizedLeasingCommissions",
        ),
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.REITS,),
        provenance=_p(ProvenanceSource.INTERNAL, "AFFO adjustment"),
    ),

    # ── NOI (computed) ──
    "noi": ConceptNode(
        concept_key="noi",
        chain=(),  # computed: rental_revenue - property_operations - real_estate_taxes
        is_computed=True,
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.REITS,),
        provenance=_p(ProvenanceSource.INTERNAL, "NOI = Revenue - Operating Costs"),
    ),

    # ── Supplementary (not in XBRL — external data) ──
    "occupied_sqft": ConceptNode(
        concept_key="occupied_sqft",
        chain=(),  # not in XBRL; comes from supplemental
        is_computed=True,
        unit="shares",
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.REITS,),
        provenance=_p(ProvenanceSource.INTERNAL, "Supplemental disclosure"),
    ),
    "total_leasable_sqft": ConceptNode(
        concept_key="total_leasable_sqft",
        chain=(),  # not in XBRL; comes from supplemental
        is_computed=True,
        unit="shares",
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.REITS,),
        provenance=_p(ProvenanceSource.INTERNAL, "Supplemental disclosure"),
    ),

    # ── Computed: FFO / AFFO ──
    "ffo": ConceptNode(
        concept_key="ffo",
        chain=(),  # computed: net_income + depreciation - gains
        is_computed=True,
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.REITS,),
        provenance=_p(ProvenanceSource.INTERNAL, "NAREIT FFO definition"),
    ),
    "affo": ConceptNode(
        concept_key="affo",
        chain=(),  # computed: FFO - non-cash - capex adjustments
        is_computed=True,
        sector=Sector.FINANCIAL,
        sub_sectors=(SubSector.REITS,),
        provenance=_p(ProvenanceSource.INTERNAL, "NAREIT AFFO definition"),
    ),
}


# ═══════════════════════════════════════════════════════════════════════════════
# HEALTHCARE SECTOR
# ═══════════════════════════════════════════════════════════════════════════════

# ── Pharma / Biotech ─────────────────────────────────────────────────────────

PHARMA_BIOTECH_CONCEPTS: Dict[str, ConceptNode] = {
    "pharma_rd_expense": ConceptNode(
        concept_key="pharma_rd_expense",
        chain=(
            "ResearchAndDevelopmentExpense",
            "ResearchAndDevelopmentExpenseExcludeAcquiredInProcessRAndD",
            "CostsAndExpensesResearchAndDevelopment",
        ),
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.PHARMA_BIOTECH,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 730-10"),
    ),
    "acquired_in_process_rd": ConceptNode(
        concept_key="acquired_in_process_rd",
        chain=(
            "AcquiredInProcessResearchAndDevelopment",
            "InProcessResearchAndDevelopmentExpense",
            "ResearchAndDevelopmentExpenseAcquiredInProcess",
        ),
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.PHARMA_BIOTECH,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 350-20-25"),
    ),
    "marketing_rd_expense": ConceptNode(
        concept_key="marketing_rd_expense",
        chain=(
            "MarketingAndResearchExpense",
            "SellingAndMarketingExpense",
            "SellingGeneralAndAdministrativeExpense",
        ),
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.PHARMA_BIOTECH,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 350-20"),
    ),

    # ── Royalty revenue ──
    "royalty_revenue": ConceptNode(
        concept_key="royalty_revenue",
        chain=(
            "RoyaltyRevenue",
            "RoyaltyIncome",
            "LicenseRevenue",
            "AmortizationOfIntangibleAssetsRevenue",
        ),
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.PHARMA_BIOTECH,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 606-10-55"),
    ),

    # ── Product revenue breakdowns ──
    "product_revenue": ConceptNode(
        concept_key="product_revenue",
        chain=(
            "ProductRevenue",
            "ProductSales",
            "SalesRevenueNet",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
        ),
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.PHARMA_BIOTECH,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 606-10-55"),
    ),
    "collaboration_revenue": ConceptNode(
        concept_key="collaboration_revenue",
        chain=(
            "CollaborationRevenue",
            "ContractRevenue",
            "CollaborationArrangements",
            "RevenueFromCollaborativeArrangements",
        ),
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.PHARMA_BIOTECH,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 808-10"),
    ),

    # ── Intangibles (patents, goodwill) ──
    "intangible_assets_gross": ConceptNode(
        concept_key="intangible_assets_gross",
        chain=(
            "IntangibleAssetsGrossExcludingGoodwill",
            "IntangibleAssetsExcludingGoodwill",
            "IntangibleAssetsNetExcludingGoodwill",
        ),
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.PHARMA_BIOTECH,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 350-20"),
    ),
    "goodwill": ConceptNode(
        concept_key="goodwill",
        chain=(
            "Goodwill",
            "GoodwillNet",
        ),
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.PHARMA_BIOTECH,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 350-20"),
    ),
    "amortization_intangibles": ConceptNode(
        concept_key="amortization_intangibles",
        chain=(
            "AmortizationOfIntangibleAssets",
            "AmortizationExpense",
            "AmortizationOfFiniteLivedIntangibleAssets",
        ),
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.PHARMA_BIOTECH,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 350-30"),
    ),

    # ── COGS / Cost of product ──
    "cost_of_goods_sold_pharma": ConceptNode(
        concept_key="cost_of_goods_sold_pharma",
        chain=(
            "CostOfGoodsAndServicesSold",
            "CostOfProductsSold",
            "CostOfRevenue",
            "CostOfSales",
        ),
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.PHARMA_BIOTECH,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 330-10"),
    ),

    # ── Computed aggregates ──
    "gross_margin_pharma": ConceptNode(
        concept_key="gross_margin_pharma",
        chain=(),  # computed: product_revenue - COGS
        is_computed=True,
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.PHARMA_BIOTECH,),
        provenance=_p(ProvenanceSource.INTERNAL, "Gross margin = Revenue - COGS"),
    ),
    "total_revenue_pharma": ConceptNode(
        concept_key="total_revenue_pharma",
        chain=(),  # computed: product + royalty + collaboration
        is_computed=True,
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.PHARMA_BIOTECH,),
        provenance=_p(ProvenanceSource.INTERNAL, "Total revenue = all revenue streams"),
    ),
}

# ── Medical Devices ──────────────────────────────────────────────────────────

MED_DEVICES_CONCEPTS: Dict[str, ConceptNode] = {
    "device_product_revenue": ConceptNode(
        concept_key="device_product_revenue",
        chain=(
            "ProductRevenue",
            "SalesRevenueNet",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
        ),
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.MEDICAL_DEVICES,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 606-10-55"),
    ),
    "service_revenue_devices": ConceptNode(
        concept_key="service_revenue_devices",
        chain=(
            "ServiceRevenue",
            "ServiceRevenueProductRevenue",
            "MaintenanceAndServiceRevenue",
        ),
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.MEDICAL_DEVICES,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 606-10"),
    ),
    "device_cogs": ConceptNode(
        concept_key="device_cogs",
        chain=(
            "CostOfGoodsAndServicesSold",
            "CostOfRevenue",
            "CostOfProductRevenue",
        ),
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.MEDICAL_DEVICES,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 330-10"),
    ),
    "device_rd_expense": ConceptNode(
        concept_key="device_rd_expense",
        chain=(
            "ResearchAndDevelopmentExpense",
            "ResearchAndDevelopmentExpenseExcludeAcquiredInProcessRAndD",
        ),
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.MEDICAL_DEVICES,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 730-10"),
    ),
    "warranty_reserve": ConceptNode(
        concept_key="warranty_reserve",
        chain=(
            "WarrantyReserve",
            "ProductWarrantyLiability",
            "WarrantyLiability",
        ),
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.MEDICAL_DEVICES,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 460-10"),
    ),
    "installation_costs": ConceptNode(
        concept_key="installation_costs",
        chain=(
            "InstallationCosts",
            "CapitalizedSoftwareDevelopmentCosts",
        ),
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.MEDICAL_DEVICES,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 985-20"),
    ),
    "deferred_revenue_devices": ConceptNode(
        concept_key="deferred_revenue_devices",
        chain=(
            "DeferredRevenue",
            "ContractLiabilities",
            "UnearnedRevenue",
        ),
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.MEDICAL_DEVICES,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 606-10-50"),
    ),
}

# ── Healthcare Services ──────────────────────────────────────────────────────

HEALTH_SERVICES_CONCEPTS: Dict[str, ConceptNode] = {
    "patient_revenue": ConceptNode(
        concept_key="patient_revenue",
        chain=(
            "PatientRevenue",
            "NetPatientRevenue",
            "HealthCareServiceRevenue",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
        ),
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.HEALTHCARE_SERVICES,),
        provenance=_p(ProvenanceSource.INTERNAL, "Healthcare services revenue"),
    ),
    "other_operating_revenue": ConceptNode(
        concept_key="other_operating_revenue",
        chain=(
            "OtherOperatingRevenue",
            "OtherRevenue",
            "OtherIncome",
        ),
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.HEALTHCARE_SERVICES,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 606-10"),
    ),
    "salaries_and_wages": ConceptNode(
        concept_key="salaries_and_wages",
        chain=(
            "SalariesAndWages",
            "CompensationCost",
            "EmployeeCosts",
        ),
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.HEALTHCARE_SERVICES,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 710-10"),
    ),
    "supplies_expense": ConceptNode(
        concept_key="supplies_expense",
        chain=(
            "MedicalSupplies",
            "SuppliesAndDrugs",
            "CostOfSupplies",
        ),
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.HEALTHCARE_SERVICES,),
        provenance=_p(ProvenanceSource.INTERNAL, "Healthcare services expense"),
    ),
    "depreciation_amort_healthcare": ConceptNode(
        concept_key="depreciation_amort_healthcare",
        chain=(
            "DepreciationAmortizationAndAccretionNet",
            "DepreciationAndAmortization",
            "Depreciation",
        ),
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.HEALTHCARE_SERVICES,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 360-10"),
    ),
    "provider_fees": ConceptNode(
        concept_key="provider_fees",
        chain=(
            "ProviderFees",
            "PhysicianFees",
            "ProfessionalFees",
        ),
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.HEALTHCARE_SERVICES,),
        provenance=_p(ProvenanceSource.INTERNAL, "Healthcare services expense"),
    ),
    "num_beds": ConceptNode(
        concept_key="num_beds",
        chain=(),  # supplemental
        is_computed=True,
        unit="shares",
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.HEALTHCARE_SERVICES,),
        provenance=_p(ProvenanceSource.INTERNAL, "Supplemental disclosure"),
    ),
    "patient_days": ConceptNode(
        concept_key="patient_days",
        chain=(),  # supplemental
        is_computed=True,
        unit="shares",
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.HEALTHCARE_SERVICES,),
        provenance=_p(ProvenanceSource.INTERNAL, "Supplemental disclosure"),
    ),
    "adjusted_patient_days": ConceptNode(
        concept_key="adjusted_patient_days",
        chain=(),  # supplemental
        is_computed=True,
        unit="shares",
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.HEALTHCARE_SERVICES,),
        provenance=_p(ProvenanceSource.INTERNAL, "Supplemental disclosure"),
    ),
    "discharges": ConceptNode(
        concept_key="discharges",
        chain=(),  # supplemental
        is_computed=True,
        unit="shares",
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.HEALTHCARE_SERVICES,),
        provenance=_p(ProvenanceSource.INTERNAL, "Supplemental disclosure"),
    ),

    # ── Computed aggregate ──
    "operating_expenses_healthcare": ConceptNode(
        concept_key="operating_expenses_healthcare",
        chain=(),  # computed: salaries + supplies + depreciation + fees
        is_computed=True,
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.HEALTHCARE_SERVICES,),
        provenance=_p(ProvenanceSource.INTERNAL, "Operating Expenses = sum of cost categories"),
    ),
}

# ── Managed Care ─────────────────────────────────────────────────────────────

MANAGED_CARE_CONCEPTS: Dict[str, ConceptNode] = {
    "premium_revenue_mc": ConceptNode(
        concept_key="premium_revenue_mc",
        chain=(
            "PremiumRevenue",
            "PremiumsAndOtherConsiderations",
            "HealthInsurancePremiumsRevenue",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
        ),
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.MANAGED_CARE,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 944-20"),
    ),
    "medical_expenses": ConceptNode(
        concept_key="medical_expenses",
        chain=(
            "MedicalExpenses",
            "MedicalCosts",
            "CostOfCareProvided",
            "ClaimsAndClaimsSettlementExpenses",
        ),
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.MANAGED_CARE,),
        provenance=_p(ProvenanceSource.INTERNAL, "Managed care MLR numerator"),
    ),
    "administrative_expenses_mc": ConceptNode(
        concept_key="administrative_expenses_mc",
        chain=(
            "AdministrativeExpenses",
            "SellingGeneralAndAdministrativeExpense",
            "GeneralAndAdministrativeExpense",
        ),
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.MANAGED_CARE,),
        provenance=_p(ProvenanceSource.INTERNAL, "Managed care"),
    ),

    # ── Membership (supplemental) ──
    "total_members": ConceptNode(
        concept_key="total_members",
        chain=(),  # supplemental — 10-K MD&A
        is_computed=True,
        unit="shares",
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.MANAGED_CARE,),
        provenance=_p(ProvenanceSource.INTERNAL, "Supplemental MD&A disclosure"),
    ),
    "commercial_members": ConceptNode(
        concept_key="commercial_members",
        chain=(),
        is_computed=True,
        unit="shares",
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.MANAGED_CARE,),
        provenance=_p(ProvenanceSource.INTERNAL, "Supplemental MD&A disclosure"),
    ),
    "medicare_advantage_members": ConceptNode(
        concept_key="medicare_advantage_members",
        chain=(),
        is_computed=True,
        unit="shares",
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.MANAGED_CARE,),
        provenance=_p(ProvenanceSource.INTERNAL, "Supplemental MD&A disclosure"),
    ),
    "medicaid_members": ConceptNode(
        concept_key="medicaid_members",
        chain=(),
        is_computed=True,
        unit="shares",
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.MANAGED_CARE,),
        provenance=_p(ProvenanceSource.INTERNAL, "Supplemental MD&A disclosure"),
    ),

    # ── Utilization ──
    "numerator_of_utilization": ConceptNode(
        concept_key="numerator_of_utilization",
        chain=(
            "InpatientRevenue",
            "OutpatientRevenue",
            "EmergencyDepartmentVisits",
        ),
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.MANAGED_CARE,),
        provenance=_p(ProvenanceSource.INTERNAL, "Managed care utilization"),
    ),

    # ── Capitation revenue ──
    "capitation_revenue": ConceptNode(
        concept_key="capitation_revenue",
        chain=(
            "CapitationRevenue",
            "CapitationFees",
        ),
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.MANAGED_CARE,),
        provenance=_p(ProvenanceSource.INTERNAL, "Managed care capitation"),
    ),

    # ── Risk corridor / risk adjustment ──
    "risk_adjustment": ConceptNode(
        concept_key="risk_adjustment",
        chain=(
            "RiskAdjustmentRevenue",
            "RiskCorridorBenefit",
            "RiskAdjustment",
        ),
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.MANAGED_CARE,),
        provenance=_p(ProvenanceSource.INTERNAL, "ACA risk corridors"),
    ),

    # ── Investment income (float) ──
    "investment_income_mc": ConceptNode(
        concept_key="investment_income_mc",
        chain=(
            "InvestmentIncome",
            "NetInvestmentIncome",
            "InterestAndDividendIncomeOperating",
        ),
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.MANAGED_CARE,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 320-10"),
    ),

    # ── Unpaid claims (loss reserves) ──
    "unpaid_claims_liability": ConceptNode(
        concept_key="unpaid_claims_liability",
        chain=(
            "UnpaidClaimsLiability",
            "ClaimsPayable",
            "LossReserves",
        ),
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.MANAGED_CARE,),
        provenance=_p(ProvenanceSource.INTERNAL, "Managed care"),
    ),

    # ── Deferred premium revenue ──
    "deferred_premium_revenue": ConceptNode(
        concept_key="deferred_premium_revenue",
        chain=(
            "DeferredPremiumRevenue",
            "DeferredRevenue",
            "UnearnedPremiumRevenue",
        ),
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.MANAGED_CARE,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 944-20"),
    ),

    # ── Computed aggregate ──
    "mc_operating_margin": ConceptNode(
        concept_key="mc_operating_margin",
        chain=(),  # computed: premium - medical - admin
        is_computed=True,
        sector=Sector.HEALTHCARE,
        sub_sectors=(SubSector.MANAGED_CARE,),
        provenance=_p(ProvenanceSource.INTERNAL, "MC operating margin = Premium - Claims - Admin"),
    ),
}


# ═══════════════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════
# TECHNOLOGY SECTOR (Semiconductors + Software/Cloud)
# ═══════════════════════════════════════════════════════════════════════════════

SEMICONDUCTORS_CONCEPTS: Dict[str, ConceptNode] = {
    "semiconductor_revenue": ConceptNode(
        concept_key="semiconductor_revenue",
        chain=(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues", "SalesRevenueNet", "NetRevenue",
        ),
        sector=Sector.TECHNOLOGY,
        sub_sectors=(SubSector.SEMICONDUCTORS,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 606-10"),
    ),
    "cost_of_revenue_semi": ConceptNode(
        concept_key="cost_of_revenue_semi",
        chain=(
            "CostOfGoodsAndServicesSold", "CostOfRevenue",
            "CostOfSales", "CostOfProductsSold",
        ),
        sector=Sector.TECHNOLOGY,
        sub_sectors=(SubSector.SEMICONDUCTORS,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 330-10"),
    ),
    "semiconductor_rd_expense": ConceptNode(
        concept_key="semiconductor_rd_expense",
        chain=(
            "ResearchAndDevelopmentExpense",
            "CostsAndExpensesResearchAndDevelopment",
            "ResearchAndDevelopmentExpenseExcludeAcquiredInProcessRAndD",
        ),
        sector=Sector.TECHNOLOGY,
        sub_sectors=(SubSector.SEMICONDUCTORS,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 730-10"),
    ),
    "semiconductor_gross_margin": ConceptNode(
        concept_key="semiconductor_gross_margin",
        chain=(), is_computed=True,
        sector=Sector.TECHNOLOGY,
        sub_sectors=(SubSector.SEMICONDUCTORS,),
        provenance=_p(ProvenanceSource.INTERNAL, "Gross margin = Revenue - COGS"),
    ),
    "semiconductor_rd_intensity": ConceptNode(
        concept_key="semiconductor_rd_intensity",
        chain=(), is_computed=True,
        sector=Sector.TECHNOLOGY,
        sub_sectors=(SubSector.SEMICONDUCTORS,),
        provenance=_p(ProvenanceSource.INTERNAL, "R&D / Revenue"),
    ),
    "inventory_semiconductor": ConceptNode(
        concept_key="inventory_semiconductor",
        chain=("InventoryNet", "Inventory", "InventoriesNet",),
        sector=Sector.TECHNOLOGY,
        sub_sectors=(SubSector.SEMICONDUCTORS,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 330"),
    ),
    "capex_semiconductor": ConceptNode(
        concept_key="capex_semiconductor",
        chain=("PaymentsToAcquirePropertyPlantAndEquipment",
               "PaymentsToAcquireProductiveAssets",),
        sector=Sector.TECHNOLOGY,
        sub_sectors=(SubSector.SEMICONDUCTORS,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 360-10"),
    ),
    "depreciation_semiconductor": ConceptNode(
        concept_key="depreciation_semiconductor",
        chain=("DepreciationDepletionAndAmortization",
               "DepreciationAmortizationAndAccretionNet",
               "DepreciationAndAmortization",),
        sector=Sector.TECHNOLOGY,
        sub_sectors=(SubSector.SEMICONDUCTORS,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 360-10"),
    ),
    "stock_based_comp_semi": ConceptNode(
        concept_key="stock_based_comp_semi",
        chain=("AllocatedShareBasedCompensationExpense",
               "ShareBasedCompensation", "StockBasedCompensationExpense",),
        sector=Sector.TECHNOLOGY,
        sub_sectors=(SubSector.SEMICONDUCTORS,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 718"),
    ),
    "cash_semi": ConceptNode(
        concept_key="cash_semi",
        chain=("CashAndCashEquivalentsAtCarryingValue",
               "CashCashEquivalentsAndShortTermInvestments",),
        sector=Sector.TECHNOLOGY,
        sub_sectors=(SubSector.SEMICONDUCTORS,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 230"),
    ),
    "long_term_debt_semi": ConceptNode(
        concept_key="long_term_debt_semi",
        chain=("LongTermDebtNoncurrent",
               "LongTermDebtAndCapitalLeaseObligationsNoncurrent",),
        sector=Sector.TECHNOLOGY,
        sub_sectors=(SubSector.SEMICONDUCTORS,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 470"),
    ),
}

SOFTWARE_CLOUD_CONCEPTS: Dict[str, ConceptNode] = {
    "software_revenue": ConceptNode(
        concept_key="software_revenue",
        chain=("RevenueFromContractWithCustomerExcludingAssessedTax",
               "Revenues", "SalesRevenueNet", "NetRevenue",),
        sector=Sector.TECHNOLOGY,
        sub_sectors=(SubSector.SOFTWARE_CLOUD,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 606-10"),
    ),
    "subscription_revenue": ConceptNode(
        concept_key="subscription_revenue",
        chain=("RecurringRevenue", "SubscriptionRevenue",
               "SaaSRevenue", "ContractRevenue",),
        sector=Sector.TECHNOLOGY,
        sub_sectors=(SubSector.SOFTWARE_CLOUD,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 606-10"),
    ),
    "cost_of_revenue_sw": ConceptNode(
        concept_key="cost_of_revenue_sw",
        chain=("CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfSales",),
        sector=Sector.TECHNOLOGY,
        sub_sectors=(SubSector.SOFTWARE_CLOUD,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 330-10"),
    ),
    "software_rd_expense": ConceptNode(
        concept_key="software_rd_expense",
        chain=("ResearchAndDevelopmentExpense",
               "CostsAndExpensesResearchAndDevelopment",),
        sector=Sector.TECHNOLOGY,
        sub_sectors=(SubSector.SOFTWARE_CLOUD,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 730-10"),
    ),
    "software_gross_margin": ConceptNode(
        concept_key="software_gross_margin",
        chain=(), is_computed=True,
        sector=Sector.TECHNOLOGY,
        sub_sectors=(SubSector.SOFTWARE_CLOUD,),
        provenance=_p(ProvenanceSource.INTERNAL, "Gross margin = Revenue - COGS"),
    ),
    "deferred_revenue_sw": ConceptNode(
        concept_key="deferred_revenue_sw",
        chain=("DeferredRevenue", "ContractLiabilities", "UnearnedRevenue",),
        sector=Sector.TECHNOLOGY,
        sub_sectors=(SubSector.SOFTWARE_CLOUD,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 606-10-50"),
    ),
    "stock_based_comp_sw": ConceptNode(
        concept_key="stock_based_comp_sw",
        chain=("AllocatedShareBasedCompensationExpense", "ShareBasedCompensation",),
        sector=Sector.TECHNOLOGY,
        sub_sectors=(SubSector.SOFTWARE_CLOUD,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 718"),
    ),
    "operating_income_sw": ConceptNode(
        concept_key="operating_income_sw",
        chain=("OperatingIncomeLoss", "OperatingIncome",),
        sector=Sector.TECHNOLOGY,
        sub_sectors=(SubSector.SOFTWARE_CLOUD,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 220"),
    ),
    "capex_sw": ConceptNode(
        concept_key="capex_sw",
        chain=("PaymentsToAcquirePropertyPlantAndEquipment",),
        sector=Sector.TECHNOLOGY,
        sub_sectors=(SubSector.SOFTWARE_CLOUD,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 360-10"),
    ),
    "cash_sw": ConceptNode(
        concept_key="cash_sw",
        chain=("CashAndCashEquivalentsAtCarryingValue",
               "CashCashEquivalentsAndShortTermInvestments",),
        sector=Sector.TECHNOLOGY,
        sub_sectors=(SubSector.SOFTWARE_CLOUD,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 230"),
    ),
    "long_term_debt_sw": ConceptNode(
        concept_key="long_term_debt_sw",
        chain=("LongTermDebtNoncurrent",
               "LongTermDebtAndCapitalLeaseObligationsNoncurrent",),
        sector=Sector.TECHNOLOGY,
        sub_sectors=(SubSector.SOFTWARE_CLOUD,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 470"),
    ),
}


# ═══════════════════════════════════════════════════════════════════════════════
# AUTOMOTIVE / EV SECTOR
# ═══════════════════════════════════════════════════════════════════════════════

AUTOMOTIVE_EV_CONCEPTS: Dict[str, ConceptNode] = {
    "automotive_revenue": ConceptNode(
        concept_key="automotive_revenue",
        chain=("RevenueFromContractWithCustomerExcludingAssessedTax",
               "Revenues", "SalesRevenueNet", "AutomotiveRevenue",
               "VehicleSalesRevenue",),
        sector=Sector.AUTOMOTIVE,
        sub_sectors=(SubSector.AUTOMOTIVE_EV,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 606-10"),
    ),
    "cost_of_revenue_auto": ConceptNode(
        concept_key="cost_of_revenue_auto",
        chain=("CostOfGoodsAndServicesSold", "CostOfRevenue",
               "CostOfSales", "CostOfAutomobileSales",),
        sector=Sector.AUTOMOTIVE,
        sub_sectors=(SubSector.AUTOMOTIVE_EV,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 330-10"),
    ),
    "automotive_rd_expense": ConceptNode(
        concept_key="automotive_rd_expense",
        chain=("ResearchAndDevelopmentExpense",
               "CostsAndExpensesResearchAndDevelopment",),
        sector=Sector.AUTOMOTIVE,
        sub_sectors=(SubSector.AUTOMOTIVE_EV,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 730-10"),
    ),
    "automotive_gross_margin": ConceptNode(
        concept_key="automotive_gross_margin",
        chain=(), is_computed=True,
        sector=Sector.AUTOMOTIVE,
        sub_sectors=(SubSector.AUTOMOTIVE_EV,),
        provenance=_p(ProvenanceSource.INTERNAL, "Gross margin"),
    ),
    "regulatory_credits_revenue": ConceptNode(
        concept_key="regulatory_credits_revenue",
        chain=("RegulatoryCreditsRevenue", "SalesOfRegulatoryCredits",),
        sector=Sector.AUTOMOTIVE,
        sub_sectors=(SubSector.AUTOMOTIVE_EV,),
        provenance=_p(ProvenanceSource.INTERNAL, "EV-specific revenue"),
    ),
    "energy_storage_revenue": ConceptNode(
        concept_key="energy_storage_revenue",
        chain=("EnergyStorageRevenue", "BatteryRevenue",),
        sector=Sector.AUTOMOTIVE,
        sub_sectors=(SubSector.AUTOMOTIVE_EV,),
        provenance=_p(ProvenanceSource.INTERNAL, "EV-specific revenue"),
    ),
    "capex_auto": ConceptNode(
        concept_key="capex_auto",
        chain=("PaymentsToAcquirePropertyPlantAndEquipment",),
        sector=Sector.AUTOMOTIVE,
        sub_sectors=(SubSector.AUTOMOTIVE_EV,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 360-10"),
    ),
    "depreciation_auto": ConceptNode(
        concept_key="depreciation_auto",
        chain=("DepreciationDepletionAndAmortization",
               "DepreciationAmortizationAndAccretionNet",),
        sector=Sector.AUTOMOTIVE,
        sub_sectors=(SubSector.AUTOMOTIVE_EV,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 360-10"),
    ),
    "inventory_auto": ConceptNode(
        concept_key="inventory_auto",
        chain=("InventoryNet", "Inventory",),
        sector=Sector.AUTOMOTIVE,
        sub_sectors=(SubSector.AUTOMOTIVE_EV,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 330"),
    ),
    "stock_based_comp_auto": ConceptNode(
        concept_key="stock_based_comp_auto",
        chain=("AllocatedShareBasedCompensationExpense",
               "ShareBasedCompensation",),
        sector=Sector.AUTOMOTIVE,
        sub_sectors=(SubSector.AUTOMOTIVE_EV,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 718"),
    ),
    "cash_auto": ConceptNode(
        concept_key="cash_auto",
        chain=("CashAndCashEquivalentsAtCarryingValue",
               "CashCashEquivalentsAndShortTermInvestments",),
        sector=Sector.AUTOMOTIVE,
        sub_sectors=(SubSector.AUTOMOTIVE_EV,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 230"),
    ),
    "long_term_debt_auto": ConceptNode(
        concept_key="long_term_debt_auto",
        chain=("LongTermDebtNoncurrent",
               "LongTermDebtAndCapitalLeaseObligationsNoncurrent",),
        sector=Sector.AUTOMOTIVE,
        sub_sectors=(SubSector.AUTOMOTIVE_EV,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 470"),
    ),
    "operating_income_auto": ConceptNode(
        concept_key="operating_income_auto",
        chain=("OperatingIncomeLoss", "OperatingIncome",),
        sector=Sector.AUTOMOTIVE,
        sub_sectors=(SubSector.AUTOMOTIVE_EV,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 220"),
    ),
}


# ═══════════════════════════════════════════════════════════════════════════════
# CONSUMER ELECTRONICS SECTOR
# ═══════════════════════════════════════════════════════════════════════════════

CONSUMER_ELECTRONICS_CONCEPTS: Dict[str, ConceptNode] = {
    "product_revenue_ce": ConceptNode(
        concept_key="product_revenue_ce",
        chain=("RevenueFromContractWithCustomerExcludingAssessedTax",
               "Revenues", "SalesRevenueNet", "ProductRevenue",),
        sector=Sector.CONSUMER_ELECTRONICS,
        sub_sectors=(SubSector.CONSUMER_ELECTRONICS,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 606-10"),
    ),
    "services_revenue_ce": ConceptNode(
        concept_key="services_revenue_ce",
        chain=("ServiceRevenue", "ServicesRevenue", "DigitalServicesRevenue",),
        sector=Sector.CONSUMER_ELECTRONICS,
        sub_sectors=(SubSector.CONSUMER_ELECTRONICS,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 606-10"),
    ),
    "cost_of_goods_ce": ConceptNode(
        concept_key="cost_of_goods_ce",
        chain=("CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfSales",),
        sector=Sector.CONSUMER_ELECTRONICS,
        sub_sectors=(SubSector.CONSUMER_ELECTRONICS,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 330-10"),
    ),
    "ce_rd_expense": ConceptNode(
        concept_key="ce_rd_expense",
        chain=("ResearchAndDevelopmentExpense",),
        sector=Sector.CONSUMER_ELECTRONICS,
        sub_sectors=(SubSector.CONSUMER_ELECTRONICS,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 730-10"),
    ),
    "ce_gross_margin": ConceptNode(
        concept_key="ce_gross_margin",
        chain=(), is_computed=True,
        sector=Sector.CONSUMER_ELECTRONICS,
        sub_sectors=(SubSector.CONSUMER_ELECTRONICS,),
        provenance=_p(ProvenanceSource.INTERNAL, "Gross margin"),
    ),
    "capex_ce": ConceptNode(
        concept_key="capex_ce",
        chain=("PaymentsToAcquirePropertyPlantAndEquipment",),
        sector=Sector.CONSUMER_ELECTRONICS,
        sub_sectors=(SubSector.CONSUMER_ELECTRONICS,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 360-10"),
    ),
    "depreciation_ce": ConceptNode(
        concept_key="depreciation_ce",
        chain=("DepreciationDepletionAndAmortization",
               "DepreciationAmortizationAndAccretionNet",),
        sector=Sector.CONSUMER_ELECTRONICS,
        sub_sectors=(SubSector.CONSUMER_ELECTRONICS,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 360-10"),
    ),
    "stock_based_comp_ce": ConceptNode(
        concept_key="stock_based_comp_ce",
        chain=("AllocatedShareBasedCompensationExpense",
               "ShareBasedCompensation",),
        sector=Sector.CONSUMER_ELECTRONICS,
        sub_sectors=(SubSector.CONSUMER_ELECTRONICS,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 718"),
    ),
    "operating_income_ce": ConceptNode(
        concept_key="operating_income_ce",
        chain=("OperatingIncomeLoss", "OperatingIncome",),
        sector=Sector.CONSUMER_ELECTRONICS,
        sub_sectors=(SubSector.CONSUMER_ELECTRONICS,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 220"),
    ),
    "cash_ce": ConceptNode(
        concept_key="cash_ce",
        chain=("CashAndCashEquivalentsAtCarryingValue",
               "CashCashEquivalentsAndShortTermInvestments",),
        sector=Sector.CONSUMER_ELECTRONICS,
        sub_sectors=(SubSector.CONSUMER_ELECTRONICS,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 230"),
    ),
    "long_term_debt_ce": ConceptNode(
        concept_key="long_term_debt_ce",
        chain=("LongTermDebtNoncurrent",
               "LongTermDebtAndCapitalLeaseObligationsNoncurrent",),
        sector=Sector.CONSUMER_ELECTRONICS,
        sub_sectors=(SubSector.CONSUMER_ELECTRONICS,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 470"),
    ),
}


# ═══════════════════════════════════════════════════════════════════════════════
# E-COMMERCE SECTOR
# ═══════════════════════════════════════════════════════════════════════════════

ECOMMERCE_CONCEPTS: Dict[str, ConceptNode] = {
    "ecommerce_revenue": ConceptNode(
        concept_key="ecommerce_revenue",
        chain=("RevenueFromContractWithCustomerExcludingAssessedTax",
               "Revenues", "SalesRevenueNet", "NetRevenue",),
        sector=Sector.ECOMMERCE,
        sub_sectors=(SubSector.ECOMMERCE,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 606-10"),
    ),
    "third_party_seller_revenue": ConceptNode(
        concept_key="third_party_seller_revenue",
        chain=("ThirdPartySellerRevenue", "MarketplaceRevenue",
               "CommissionRevenue",),
        sector=Sector.ECOMMERCE,
        sub_sectors=(SubSector.ECOMMERCE,),
        provenance=_p(ProvenanceSource.INTERNAL, "Marketplace revenue"),
    ),
    "advertising_revenue_ecom": ConceptNode(
        concept_key="advertising_revenue_ecom",
        chain=("AdvertisingRevenue", "MarketingRevenue",),
        sector=Sector.ECOMMERCE,
        sub_sectors=(SubSector.ECOMMERCE,),
        provenance=_p(ProvenanceSource.INTERNAL, "Ad revenue"),
    ),
    "subscription_revenue_ecom": ConceptNode(
        concept_key="subscription_revenue_ecom",
        chain=("SubscriptionRevenue", "MembershipRevenue", "RecurringRevenue",),
        sector=Sector.ECOMMERCE,
        sub_sectors=(SubSector.ECOMMERCE,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 606-10"),
    ),
    "cloud_revenue_ecom": ConceptNode(
        concept_key="cloud_revenue_ecom",
        chain=("CloudRevenue", "WebServicesRevenue",),
        sector=Sector.ECOMMERCE,
        sub_sectors=(SubSector.ECOMMERCE,),
        provenance=_p(ProvenanceSource.INTERNAL, "Cloud revenue"),
    ),
    "cost_of_revenue_ecom": ConceptNode(
        concept_key="cost_of_revenue_ecom",
        chain=("CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfSales",),
        sector=Sector.ECOMMERCE,
        sub_sectors=(SubSector.ECOMMERCE,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 330-10"),
    ),
    "ecom_rd_expense": ConceptNode(
        concept_key="ecom_rd_expense",
        chain=("ResearchAndDevelopmentExpense",),
        sector=Sector.ECOMMERCE,
        sub_sectors=(SubSector.ECOMMERCE,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 730-10"),
    ),
    "ecom_gross_margin": ConceptNode(
        concept_key="ecom_gross_margin",
        chain=(), is_computed=True,
        sector=Sector.ECOMMERCE,
        sub_sectors=(SubSector.ECOMMERCE,),
        provenance=_p(ProvenanceSource.INTERNAL, "Gross margin"),
    ),
    "fulfillment_expense": ConceptNode(
        concept_key="fulfillment_expense",
        chain=("FulfillmentExpense", "FulfillmentCosts",),
        sector=Sector.ECOMMERCE,
        sub_sectors=(SubSector.ECOMMERCE,),
        provenance=_p(ProvenanceSource.INTERNAL, "Fulfillment cost"),
    ),
    "capex_ecom": ConceptNode(
        concept_key="capex_ecom",
        chain=("PaymentsToAcquirePropertyPlantAndEquipment",),
        sector=Sector.ECOMMERCE,
        sub_sectors=(SubSector.ECOMMERCE,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 360-10"),
    ),
    "depreciation_ecom": ConceptNode(
        concept_key="depreciation_ecom",
        chain=("DepreciationDepletionAndAmortization",
               "DepreciationAmortizationAndAccretionNet",),
        sector=Sector.ECOMMERCE,
        sub_sectors=(SubSector.ECOMMERCE,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 360-10"),
    ),
    "stock_based_comp_ecom": ConceptNode(
        concept_key="stock_based_comp_ecom",
        chain=("AllocatedShareBasedCompensationExpense",
               "ShareBasedCompensation",),
        sector=Sector.ECOMMERCE,
        sub_sectors=(SubSector.ECOMMERCE,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 718"),
    ),
    "operating_income_ecom": ConceptNode(
        concept_key="operating_income_ecom",
        chain=("OperatingIncomeLoss", "OperatingIncome",),
        sector=Sector.ECOMMERCE,
        sub_sectors=(SubSector.ECOMMERCE,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 220"),
    ),
    "cash_ecom": ConceptNode(
        concept_key="cash_ecom",
        chain=("CashAndCashEquivalentsAtCarryingValue",
               "CashCashEquivalentsAndShortTermInvestments",),
        sector=Sector.ECOMMERCE,
        sub_sectors=(SubSector.ECOMMERCE,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 230"),
    ),
    "long_term_debt_ecom": ConceptNode(
        concept_key="long_term_debt_ecom",
        chain=("LongTermDebtNoncurrent",
               "LongTermDebtAndCapitalLeaseObligationsNoncurrent",),
        sector=Sector.ECOMMERCE,
        sub_sectors=(SubSector.ECOMMERCE,),
        provenance=_p(ProvenanceSource.FASB_ASC, "ASC 470"),
    ),
}


# RELATIONSHIP GRAPHS
# ═══════════════════════════════════════════════════════════════════════════════

# ── Financial sector relationships ────────────────────────────────────────────

BANKING_RELATIONS: List[Relation] = [
    # Net interest income = interest_income - interest_expense
    Relation("interest_income", "net_interest_income", RelationType.CALC_SUM,
             weight=1.0, provenance=_p(ProvenanceSource.FASB_ASC, "ASC 942-20")),
    Relation("interest_expense", "net_interest_income", RelationType.CALC_SUM,
             weight=-1.0, provenance=_p(ProvenanceSource.FASB_ASC, "ASC 942-20")),
    # Net revenue = net_interest_income + noninterest_income
    Relation("net_interest_income", "net_revenue", RelationType.CALC_SUM,
             provenance=_p(ProvenanceSource.INTERNAL)),
    Relation("noninterest_income", "net_revenue", RelationType.CALC_SUM,
             provenance=_p(ProvenanceSource.INTERNAL)),
    # Basel III: Tier 1 ratio = tier1_capital / risk_weighted_assets
    Relation("tier1_capital", "tier1_ratio", RelationType.DERIVED,
             provenance=_p(ProvenanceSource.INTERNAL, "Basel III")),
    Relation("total_rwa", "tier1_ratio", RelationType.DERIVED,
             provenance=_p(ProvenanceSource.INTERNAL, "Basel III")),
    # Total loans -> total assets
    Relation("total_loans", "total_assets", RelationType.DEFINITION_MEMBER_OF,
             provenance=_p(ProvenanceSource.FASB_ASC, "ASC 310-20")),
    # Provision -> noninterest expense
    Relation("provision_for_loan_losses", "noninterest_expense", RelationType.CALC_SUM,
             provenance=_p(ProvenanceSource.FASB_ASC, "ASC 326-20")),
]

INSURANCE_RELATIONS: List[Relation] = [
    # Loss ratio = losses_incurred / premiums_earned
    Relation("losses_incurred", "loss_ratio", RelationType.DERIVED,
             provenance=_p(ProvenanceSource.FASB_ASC, "ASC 944-30")),
    Relation("premiums_earned", "loss_ratio", RelationType.DERIVED,
             provenance=_p(ProvenanceSource.FASB_ASC, "ASC 944-30")),
    # Combined ratio = loss_ratio + expense_ratio
    Relation("loss_ratio", "combined_ratio", RelationType.CALC_SUM,
             provenance=_p(ProvenanceSource.NAIC_SAP)),
    Relation("underwriting_expenses", "combined_ratio", RelationType.DERIVED,
             provenance=_p(ProvenanceSource.NAIC_SAP)),
    # Premiums written -> premiums earned (adjustment for UPR)
    Relation("premiums_written", "premiums_earned", RelationType.CALC_SUM,
             weight=-1.0, provenance=_p(ProvenanceSource.FASB_ASC, "ASC 944-20")),
    Relation("unearned_premium_reserve", "premiums_earned", RelationType.CALC_SUM,
             weight=-1.0, provenance=_p(ProvenanceSource.FASB_ASC, "ASC 944-20")),
]

REIT_RELATIONS: List[Relation] = [
    # NOI = rental_revenue - property_operations - real_estate_taxes
    Relation("rental_revenue", "noi", RelationType.CALC_SUM,
             provenance=_p(ProvenanceSource.INTERNAL, "NOI definition")),
    Relation("property_operations_expense", "noi", RelationType.CALC_SUM,
             weight=-1.0, provenance=_p(ProvenanceSource.INTERNAL, "NOI definition")),
    Relation("real_estate_taxes", "noi", RelationType.CALC_SUM,
             weight=-1.0, provenance=_p(ProvenanceSource.INTERNAL, "NOI definition")),
    # FFO = net_income + depreciation - gains_on_sale
    Relation("net_income_common", "ffo", RelationType.CALC_SUM,
             provenance=_p(ProvenanceSource.INTERNAL, "NAREIT FFO")),
    Relation("ffo_depreciation_adjustment", "ffo", RelationType.CALC_SUM,
             provenance=_p(ProvenanceSource.INTERNAL, "NAREIT FFO")),
    Relation("ffo_gains_adjustment", "ffo", RelationType.CALC_SUM,
             weight=-1.0, provenance=_p(ProvenanceSource.INTERNAL, "NAREIT FFO")),
    # AFFO = FFO - straight_line_adj - TI - commissions + recurring capex
    Relation("ffo", "affo", RelationType.CALC_SUM,
             provenance=_p(ProvenanceSource.INTERNAL, "AFFO definition")),
    Relation("straight_line_rent_adjustment", "affo", RelationType.CALC_SUM,
             weight=-1.0, provenance=_p(ProvenanceSource.INTERNAL, "AFFO non-cash")),
    Relation("tenant_improvements", "affo", RelationType.CALC_SUM,
             weight=-1.0, provenance=_p(ProvenanceSource.INTERNAL, "AFFO capex")),
    Relation("leasing_commissions", "affo", RelationType.CALC_SUM,
             weight=-1.0, provenance=_p(ProvenanceSource.INTERNAL, "AFFO capex")),
    # Occupancy = occupied / total
    Relation("occupied_sqft", "occupancy_rate", RelationType.DERIVED,
             provenance=_p(ProvenanceSource.INTERNAL, "supplemental")),
    Relation("total_leasable_sqft", "occupancy_rate", RelationType.DERIVED,
             provenance=_p(ProvenanceSource.INTERNAL, "supplemental")),
]

ASSET_MGMT_RELATIONS: List[Relation] = [
    # Total fee revenue = management + performance + brokerage
    Relation("management_fee_revenue", "total_investment_fee_revenue", RelationType.CALC_SUM,
             provenance=_p(ProvenanceSource.FASB_ASC, "ASC 606-10")),
    Relation("performance_fee_revenue", "total_investment_fee_revenue", RelationType.CALC_SUM,
             provenance=_p(ProvenanceSource.FASB_ASC, "ASC 606-10")),
    Relation("brokerage_commissions", "total_investment_fee_revenue", RelationType.CALC_SUM,
             provenance=_p(ProvenanceSource.FASB_ASC, "ASC 606-10")),
    # Expense ratio = fund_operating_expenses / AUM
    Relation("fund_operating_expenses", "expense_ratio", RelationType.DERIVED,
             provenance=_p(ProvenanceSource.INTERNAL, "SEC N-MEN")),
    Relation("aum", "expense_ratio", RelationType.DERIVED,
             provenance=_p(ProvenanceSource.INTERNAL, "SEC N-MEN")),
]

# ── Healthcare sector relationships ──────────────────────────────────────────

PHARMA_RELATIONS: List[Relation] = [
    # Gross margin = product_revenue - cost_of_goods_sold_pharma
    Relation("product_revenue", "gross_margin_pharma", RelationType.CALC_SUM,
             provenance=_p(ProvenanceSource.FASB_ASC, "ASC 330-10")),
    Relation("cost_of_goods_sold_pharma", "gross_margin_pharma", RelationType.CALC_SUM,
             weight=-1.0, provenance=_p(ProvenanceSource.FASB_ASC, "ASC 330-10")),
    # Total revenue = product + royalty + collaboration
    Relation("product_revenue", "total_revenue_pharma", RelationType.CALC_SUM,
             provenance=_p(ProvenanceSource.FASB_ASC, "ASC 606-10")),
    Relation("royalty_revenue", "total_revenue_pharma", RelationType.CALC_SUM,
             provenance=_p(ProvenanceSource.FASB_ASC, "ASC 606-10")),
    Relation("collaboration_revenue", "total_revenue_pharma", RelationType.CALC_SUM,
             provenance=_p(ProvenanceSource.FASB_ASC, "ASC 606-10")),
]

HEALTH_SERVICES_RELATIONS: List[Relation] = [
    # Revenue per visit = patient_revenue / discharges (supplemental)
    Relation("patient_revenue", "revenue_per_visit", RelationType.DERIVED,
             provenance=_p(ProvenanceSource.INTERNAL, "Healthcare KPI")),
    Relation("discharges", "revenue_per_visit", RelationType.DERIVED,
             provenance=_p(ProvenanceSource.INTERNAL, "Healthcare KPI")),
    # Same-store: requires comparable facility filter (not in concept graph)
    # Operating expenses = salaries + supplies + depreciation + provider_fees
    Relation("salaries_and_wages", "operating_expenses_healthcare", RelationType.CALC_SUM,
             provenance=_p(ProvenanceSource.FASB_ASC, "ASC 710-10")),
    Relation("supplies_expense", "operating_expenses_healthcare", RelationType.CALC_SUM,
             provenance=_p(ProvenanceSource.INTERNAL, "Healthcare services")),
    Relation("depreciation_amort_healthcare", "operating_expenses_healthcare", RelationType.CALC_SUM,
             provenance=_p(ProvenanceSource.FASB_ASC, "ASC 360-10")),
    Relation("provider_fees", "operating_expenses_healthcare", RelationType.CALC_SUM,
             provenance=_p(ProvenanceSource.INTERNAL, "Healthcare services")),
]

MANAGED_CARE_RELATIONS: List[Relation] = [
    # Medical loss ratio = medical_expenses / premium_revenue
    Relation("medical_expenses", "medical_loss_ratio", RelationType.DERIVED,
             provenance=_p(ProvenanceSource.INTERNAL, "MLR definition")),
    Relation("premium_revenue_mc", "medical_loss_ratio", RelationType.DERIVED,
             provenance=_p(ProvenanceSource.INTERNAL, "MLR definition")),
    # Administrative ratio = admin_expenses / premium_revenue
    Relation("administrative_expenses_mc", "administrative_ratio", RelationType.DERIVED,
             provenance=_p(ProvenanceSource.INTERNAL, "Managed care")),
    Relation("premium_revenue_mc", "administrative_ratio", RelationType.DERIVED,
             provenance=_p(ProvenanceSource.INTERNAL, "Managed care")),
    # Operating margin = (premium_revenue - medical - admin) / premium_revenue
    Relation("premium_revenue_mc", "mc_operating_margin", RelationType.CALC_SUM,
             provenance=_p(ProvenanceSource.INTERNAL, "Managed care")),
    Relation("medical_expenses", "mc_operating_margin", RelationType.CALC_SUM,
             weight=-1.0, provenance=_p(ProvenanceSource.INTERNAL, "Managed care")),
    Relation("administrative_expenses_mc", "mc_operating_margin", RelationType.CALC_SUM,
             weight=-1.0, provenance=_p(ProvenanceSource.INTERNAL, "Managed care")),
    # Payer mix = members by line / total_members
    Relation("commercial_members", "payer_mix_commercial", RelationType.DERIVED,
             provenance=_p(ProvenanceSource.INTERNAL, "Supplemental")),
    Relation("total_members", "payer_mix_commercial", RelationType.DERIVED,
             provenance=_p(ProvenanceSource.INTERNAL, "Supplemental")),
    Relation("medicare_advantage_members", "payer_mix_ma", RelationType.DERIVED,
             provenance=_p(ProvenanceSource.INTERNAL, "Supplemental")),
    Relation("total_members", "payer_mix_ma", RelationType.DERIVED,
             provenance=_p(ProvenanceSource.INTERNAL, "Supplemental")),
]


# ═══════════════════════════════════════════════════════════════════════════════
# ── Technology sector relationships ─────────────────────────────────────────

SEMICONDUCTOR_RELATIONS: List[Relation] = [
    Relation("semiconductor_revenue", "semiconductor_gross_margin", RelationType.CALC_SUM,
             provenance=_p(ProvenanceSource.FASB_ASC, "ASC 330-10")),
    Relation("cost_of_revenue_semi", "semiconductor_gross_margin", RelationType.CALC_SUM,
             weight=-1.0, provenance=_p(ProvenanceSource.FASB_ASC, "ASC 330-10")),
    Relation("semiconductor_rd_expense", "semiconductor_rd_intensity", RelationType.DERIVED,
             provenance=_p(ProvenanceSource.INTERNAL, "R&D intensity")),
    Relation("semiconductor_revenue", "semiconductor_rd_intensity", RelationType.DERIVED,
             provenance=_p(ProvenanceSource.INTERNAL, "R&D intensity")),
]

SOFTWARE_CLOUD_RELATIONS: List[Relation] = [
    Relation("software_revenue", "software_gross_margin", RelationType.CALC_SUM,
             provenance=_p(ProvenanceSource.FASB_ASC, "ASC 330-10")),
    Relation("cost_of_revenue_sw", "software_gross_margin", RelationType.CALC_SUM,
             weight=-1.0, provenance=_p(ProvenanceSource.FASB_ASC, "ASC 330-10")),
]

AUTOMOTIVE_RELATIONS: List[Relation] = [
    Relation("automotive_revenue", "automotive_gross_margin", RelationType.CALC_SUM,
             provenance=_p(ProvenanceSource.FASB_ASC, "ASC 330-10")),
    Relation("cost_of_revenue_auto", "automotive_gross_margin", RelationType.CALC_SUM,
             weight=-1.0, provenance=_p(ProvenanceSource.FASB_ASC, "ASC 330-10")),
    Relation("operating_income_auto", "operating_margin_auto", RelationType.DERIVED,
             provenance=_p(ProvenanceSource.INTERNAL, "Operating margin")),
    Relation("automotive_revenue", "operating_margin_auto", RelationType.DERIVED,
             provenance=_p(ProvenanceSource.INTERNAL, "Operating margin")),
]

CONSUMER_ELECTRONICS_RELATIONS: List[Relation] = [
    Relation("product_revenue_ce", "ce_gross_margin", RelationType.CALC_SUM,
             provenance=_p(ProvenanceSource.FASB_ASC, "ASC 330-10")),
    Relation("cost_of_goods_ce", "ce_gross_margin", RelationType.CALC_SUM,
             weight=-1.0, provenance=_p(ProvenanceSource.FASB_ASC, "ASC 330-10")),
]

ECOMMERCE_RELATIONS: List[Relation] = [
    Relation("ecommerce_revenue", "ecom_gross_margin", RelationType.CALC_SUM,
             provenance=_p(ProvenanceSource.FASB_ASC, "ASC 330-10")),
    Relation("cost_of_revenue_ecom", "ecom_gross_margin", RelationType.CALC_SUM,
             weight=-1.0, provenance=_p(ProvenanceSource.FASB_ASC, "ASC 330-10")),
]


# PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════════

# Aggregate registries
ALL_SECTOR_CONCEPTS: Dict[str, ConceptNode] = {}
ALL_SECTOR_RELATIONS: List[Relation] = []

def _init_registry():
    """Build the global concept registry and relation list."""
    global ALL_SECTOR_CONCEPTS, ALL_SECTOR_RELATIONS

    # Merge all sub-sector concept dicts (last-write-wins for dedup)
    for registry in (
        BANKING_CONCEPTS, INSURANCE_CONCEPTS, ASSET_MGMT_CONCEPTS, REIT_CONCEPTS,
        PHARMA_BIOTECH_CONCEPTS, MED_DEVICES_CONCEPTS,
        HEALTH_SERVICES_CONCEPTS, MANAGED_CARE_CONCEPTS,
        SEMICONDUCTORS_CONCEPTS, SOFTWARE_CLOUD_CONCEPTS,
        AUTOMOTIVE_EV_CONCEPTS, CONSUMER_ELECTRONICS_CONCEPTS,
        ECOMMERCE_CONCEPTS,
    ):
        ALL_SECTOR_CONCEPTS.update(registry)

    # Merge all relations
    ALL_SECTOR_RELATIONS.extend(BANKING_RELATIONS)
    ALL_SECTOR_RELATIONS.extend(INSURANCE_RELATIONS)
    ALL_SECTOR_RELATIONS.extend(ASSET_MGMT_RELATIONS)
    ALL_SECTOR_RELATIONS.extend(REIT_RELATIONS)
    ALL_SECTOR_RELATIONS.extend(PHARMA_RELATIONS)
    ALL_SECTOR_RELATIONS.extend(HEALTH_SERVICES_RELATIONS)
    ALL_SECTOR_RELATIONS.extend(MANAGED_CARE_RELATIONS)
    ALL_SECTOR_RELATIONS.extend(SEMICONDUCTOR_RELATIONS)
    ALL_SECTOR_RELATIONS.extend(SOFTWARE_CLOUD_RELATIONS)
    ALL_SECTOR_RELATIONS.extend(AUTOMOTIVE_RELATIONS)
    ALL_SECTOR_RELATIONS.extend(CONSUMER_ELECTRONICS_RELATIONS)
    ALL_SECTOR_RELATIONS.extend(ECOMMERCE_RELATIONS)

_init_registry()


def get_sector_concept(concept_key: str) -> Optional[ConceptNode]:
    """Return a sector concept node by key, or None."""
    return ALL_SECTOR_CONCEPTS.get(concept_key)


def get_concepts_by_sector(sector: Sector) -> Dict[str, ConceptNode]:
    """Return all concept nodes for a given sector."""
    return {
        k: v for k, v in ALL_SECTOR_CONCEPTS.items()
        if v.sector == sector
    }


def get_concepts_by_subsector(subsector: SubSector) -> Dict[str, ConceptNode]:
    """Return all concept nodes for a given sub-sector."""
    return {
        k: v for k, v in ALL_SECTOR_CONCEPTS.items()
        if subsector in v.sub_sectors
    }


def get_relations_for_concept(concept_key: str) -> List[Relation]:
    """Return all relations involving this concept (as source or target)."""
    return [
        r for r in ALL_SECTOR_RELATIONS
        if r.from_concept == concept_key or r.to_concept == concept_key
    ]


def get_children(parent_key: str) -> List[str]:
    """Return concept keys that are children/components of the given parent."""
    return [
        r.from_concept for r in ALL_SECTOR_RELATIONS
        if r.to_concept == parent_key and r.relation_type in (
            RelationType.CALC_SUM, RelationType.CALC_WEIGHTED
        )
    ]


def get_parents(child_key: str) -> List[str]:
    """Return concept keys that aggregate the given child."""
    return [
        r.to_concept for r in ALL_SECTOR_RELATIONS
        if r.from_concept == child_key and r.relation_type in (
            RelationType.CALC_SUM, RelationType.CALC_WEIGHTED
        )
    ]


def get_derived_ratios() -> List[str]:
    """Return concept keys that are computed/derived ratios (not directly in XBRL)."""
    return [
        r.to_concept for r in ALL_SECTOR_RELATIONS
        if r.relation_type == RelationType.DERIVED
    ]


def build_xbrl_concept_map(
    sector: Optional[Sector] = None,
    subsector: Optional[SubSector] = None,
) -> Dict[str, List[str]]:
    """Build a mapping of concept_key -> XBRL tag chain, optionally filtered.

    This is the primary integration point with the existing xbrl_parser.py
    CONCEPT_CHAINS dict. It returns only concepts that have non-empty XBRL
    tag chains (i.e. concepts that can be resolved from SEC EDGAR data).

    Args:
        sector: If given, filter to concepts in this sector.
        subsector: If given, filter to this sub-sector (takes precedence over sector).
    """
    concepts = ALL_SECTOR_CONCEPTS

    if subsector:
        concepts = {k: v for k, v in concepts.items() if subsector in v.sub_sectors}
    elif sector:
        concepts = {k: v for k, v in concepts.items() if v.sector == sector}

    return {
        k: list(v.chain)
        for k, v in concepts.items()
        if v.chain  # only concepts with XBRL tags
    }


def validate_concept_graph() -> List[str]:
    """Validate integrity of the concept relationship graph.

    Returns a list of validation error strings. Empty list = valid.
    """
    errors = []

    # Check all relation references point to existing concepts or derived
    concept_keys = set(ALL_SECTOR_CONCEPTS.keys())
    derived_keys = set()
    for r in ALL_SECTOR_RELATIONS:
        if r.relation_type == RelationType.DERIVED:
            derived_keys.add(r.to_concept)
        if r.from_concept not in concept_keys and r.from_concept not in derived_keys:
            errors.append(
                f"Relation source '{r.from_concept}' not in concept registry "
                f"(target: {r.to_concept})"
            )
        if r.to_concept not in concept_keys and r.to_concept not in derived_keys:
            errors.append(
                f"Relation target '{r.to_concept}' not in concept registry "
                f"(source: {r.from_concept})"
            )

    # Check for orphan chains (concepts with empty chains that are not computed)
    for k, v in ALL_SECTOR_CONCEPTS.items():
        if not v.chain and not v.is_computed:
            errors.append(
                f"Concept '{k}' has empty chain and is not marked as computed"
            )

    return errors


def list_all_subsectors() -> List[SubSector]:
    """Return all sub-sectors that have at least one concept defined."""
    found = set()
    for node in ALL_SECTOR_CONCEPTS.values():
        found.update(node.sub_sectors)
    return sorted(found, key=lambda s: s.value)


def list_all_derived_ratios() -> List[str]:
    """Return all derived/computed ratios from the relationship graph."""
    return sorted(set(get_derived_ratios()))
