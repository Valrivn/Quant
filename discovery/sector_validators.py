"""Sector-specific validators for XBRL-derived fundamentals.

These validators apply domain knowledge *after* extraction: they check that a
set of extracted facts is internally consistent and economically plausible for
the target sector, and they compute the sector-specific derived ratios that
the base parser cannot know about (MLR, combined ratio, FFO/AFFO, NOI, etc.).

Division of labor with ``xbrl_parser.py``:
  - ``xbrl_parser`` resolves raw XBRL facts (concept chains, PIT handling).
  - This module validates plausibility and computes derived sector ratios.

All functions are pure; no network, no RNG, no mutation of inputs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from discovery.sector_xbrl_chains import (
    Sector,
    SubSector,
)

logger = logging.getLogger(__name__)

# ── Plausibility bounds ───────────────────────────────────────────────────────
# Bag-of-words ranges used by validate_ranges(). Rationale for the bound
# choices is documented in the sector taxonomy doc. Units are:
#   ratios            -> fraction (0..1) unless percentages are stated
#   money             -> absolute USD magnitude warnings (no hard fail)


_RATIO_BOUNDS: Dict[str, Tuple[Optional[float], Optional[float]]] = {
    # Banking
    "tier1_ratio": (0.0, 1.0),            # fraction; Basel III floor ~4.5% CET1
    "net_interest_margin": (0.0, 1.0),    # fraction of earning assets
    "efficiency_ratio": (0.0, 3.0),       # noninterest exp / revenue; healthy < 0.7
    # Insurance
    "loss_ratio": (0.0, 2.0),
    "combined_ratio": (0.0, 3.0),         # <1.0 profitable underwriting
    "expense_ratio": (0.0, 1.0),
    # Asset management
    "pre_tax_margin_am": (0.0, 5.0),
    # REITs
    "occupancy_rate": (0.0, 1.0),
    "cap_rate": (0.0, 1.0),
    # Pharma / biotech
    "gross_margin_pharma": (0.0, 1.0),
    "rd_intensity": (0.0, 5.0),           # R&D / revenue; biotech can exceed 1
    # Healthcare services
    "operating_margin_hs": (-1.0, 1.0),
    # Managed care
    "medical_loss_ratio": (0.0, 1.5),     # ACA federal floor ~0.80 (0.85 large group)
    "administrative_ratio": (0.0, 1.0),
    "admin_plus_mlr": (0.0, 2.0),
    # Technology
    "gross_margin_tech": (0.0, 1.0),
    "rd_intensity_tech": (0.0, 5.0),      # R&D / Revenue; biotech-like can exceed 1
    "operating_margin_tech": (-1.0, 1.0),
    "sbc_intensity": (0.0, 1.0),          # SBC / Revenue
    # Automotive
    "gross_margin_auto": (0.0, 1.0),
    "operating_margin_auto": (-1.0, 1.0),
    "rd_intensity_auto": (0.0, 1.0),
    # Consumer Electronics
    "gross_margin_ce": (0.0, 1.0),
    "services_mix_ce": (0.0, 1.0),        # services / total revenue
    "operating_margin_ce": (-1.0, 1.0),
    # E-Commerce
    "gross_margin_ecom": (0.0, 1.0),
    "fulfillment_ratio": (0.0, 1.0),      # fulfillment / revenue
    "operating_margin_ecom": (-1.0, 1.0),
}


@dataclass
class ValidationIssue:
    """One validator finding."""
    code: str                 # machine-readable code
    level: str                # "error" | "warning" | "info"
    concept: str              # the concept involved
    message: str
    value: Optional[float] = None
    expected_range: Optional[Tuple[Optional[float], Optional[float]]] = None


@dataclass
class ValidationResult:
    """Result of running validators over a set of facts."""
    sector: Optional[Sector] = None
    subsector: Optional[SubSector] = None
    issues: List[ValidationIssue] = field(default_factory=list)

    def errors(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.level == "error"]

    def warnings(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.level == "warning"]

    @property
    def valid(self) -> bool:
        return not self.errors()

    def to_dict(self) -> dict:
        return {
            "sector": self.sector.value if self.sector else None,
            "subsector": self.subsector.value if self.subsector else None,
            "valid": self.valid,
            "issues": [
                {
                    "code": i.code,
                    "level": i.level,
                    "concept": i.concept,
                    "message": i.message,
                    "value": i.value,
                    "expected_range": i.expected_range,
                }
                for i in self.issues
            ],
        }


# ── Core validation helpers ───────────────────────────────────────────────────

def _add(
    result: ValidationResult,
    code: str,
    level: str,
    concept: str,
    message: str,
    value: Optional[float] = None,
    expected_range: Optional[Tuple[Optional[float], Optional[float]]] = None,
) -> None:
    result.issues.append(ValidationIssue(
        code=code, level=level, concept=concept, message=message,
        value=value, expected_range=expected_range,
    ))


def _num(d: Dict[str, float], *names: str) -> Optional[float]:
    """First non-None numeric value for the given names."""
    for n in names:
        v = d.get(n)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _div(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    if numerator is None or denominator is None or denominator == 0:
        return None
    try:
        return float(numerator) / float(denominator)
    except (TypeError, ZeroDivisionError):
        return None


def _check_range(
    result: ValidationResult,
    concept: str,
    value: Optional[float],
    bounds: Optional[Tuple[Optional[float], Optional[float]]],
    hard: bool = True,
    label: str = "",
) -> None:
    """Check a value against bounds and record an issue if outside."""
    if value is None or bounds is None:
        return
    lo, hi = bounds
    out_of_range = False
    if lo is not None and value < lo:
        out_of_range = True
    if hi is not None and value > hi:
        out_of_range = True
    if out_of_range:
        code = "ratio_out_of_range" if not label else f"{label}_out_of_range"
        _add(
            result, code, "error" if hard else "warning", concept,
            f"{concept} = {value:.4f} outside plausible range {lo}..{hi}",
            value=value, expected_range=(lo, hi),
        )


# ── Sector validators ─────────────────────────────────────────────────────────

def validate_banking(facts: Dict[str, float]) -> ValidationResult:
    """Validate banking fundamentals and compute Basel III-style derived ratios.

    Expects facts with keys: interest_income, interest_expense, earning_assets,
    tier1_capital, risk_weighted_assets, provision_for_loan_losses, loans, etc.
    """
    result = ValidationResult(sector=Sector.FINANCIAL, subsector=SubSector.BANKING)

    interest_income = _num(facts, "interest_income", "net_interest_income")
    interest_expense = _num(facts, "interest_expense")
    earning_assets = _num(facts, "earning_assets", "earning_assets_avg")

    # Net interest income (recompute if only components given)
    nii = _num(facts, "net_interest_income")
    if nii is None and interest_income is not None and interest_expense is not None:
        nii = interest_income - interest_expense

    if nii is not None and earning_assets:
        nim = _div(nii, earning_assets)
        _check_range(result, "net_interest_margin", nim,
                     _RATIO_BOUNDS.get("net_interest_margin"))
        if nim is not None:
            facts_copy = dict(facts)
            facts_copy["net_interest_margin"] = nim
            result.issues.append(ValidationIssue(
                code="derived_nim",
                level="info",
                concept="net_interest_margin",
                message="net_interest_margin = net_interest_income / earning_assets",
                value=nim,
            ))

    # Basel III Tier 1 ratio
    tier1 = _num(facts, "tier1_capital", "common_equity_tier1")
    rwa = _num(facts, "risk_weighted_assets", "total_rwa")
    tier1_ratio = _div(tier1, rwa)
    _check_range(result, "tier1_ratio", tier1_ratio, _RATIO_BOUNDS.get("tier1_ratio"))
    if tier1_ratio is not None and tier1_ratio < 0.045:
        _add(result, "tier1_below_regulatory_floor", "error",
             "tier1_ratio",
             f"Tier 1 ratio {tier1_ratio:.4f} below Basel III 4.5% CET1 floor",
             value=tier1_ratio)

    # Provision intensity vs loans
    provision = _num(facts, "provision_for_loan_losses", "provision_for_credit_losses")
    loans = _num(facts, "total_loans", "loans")
    if provision is not None and loans:
        provision_rate = _div(provision, loans)
        if provision_rate is not None and provision_rate > 0.10:
            _add(result, "provision_rate_high", "warning", "provision_for_loan_losses",
                 f"Provision rate {provision_rate:.4f} of loans > 10% (stress signal)",
                 value=provision_rate)

    # Efficiency ratio
    nonint_exp = _num(facts, "noninterest_expense")
    net_rev = _num(facts, "net_revenue")
    if net_rev is None:
        net_rev = nii
        if _num(facts, "noninterest_income") is not None:
            net_rev = (nii or 0.0) + _num(facts, "noninterest_income")
    if nonint_exp is not None and net_rev:
        eff = _div(nonint_exp, net_rev)
        _check_range(result, "efficiency_ratio", eff, _RATIO_BOUNDS.get("efficiency_ratio"))

    return result


def validate_insurance(facts: Dict[str, float]) -> ValidationResult:
    """Validate insurance fundamentals; derive loss/combined ratios."""
    result = ValidationResult(sector=Sector.FINANCIAL, subsector=SubSector.INSURANCE)

    premiums = _num(facts, "premiums_earned", "premium_revenue")
    losses = _num(facts, "losses_incurred", "claims_incurred")
    underwriting_exp = _num(facts, "underwriting_expenses")

    loss_ratio = _div(losses, premiums)
    _check_range(result, "loss_ratio", loss_ratio, _RATIO_BOUNDS.get("loss_ratio"))
    if loss_ratio is not None:
        result.issues.append(ValidationIssue(
            code="derived_loss_ratio", level="info", concept="loss_ratio",
            message="loss_ratio = losses_incurred / premiums_earned",
            value=loss_ratio,
        ))

    if underwriting_exp is not None and premiums:
        exp_ratio = _div(underwriting_exp, premiums)
        _check_range(result, "expense_ratio", exp_ratio, _RATIO_BOUNDS.get("expense_ratio"))
    else:
        exp_ratio = None

    combined = None
    if loss_ratio is not None and exp_ratio is not None:
        combined = loss_ratio + exp_ratio
    elif loss_ratio is not None:
        # Fall back to (losses + UW exp) / premiums when exp is embedded
        combined = _div((losses or 0.0) + (underwriting_exp or 0.0), premiums)
    _check_range(result, "combined_ratio", combined, _RATIO_BOUNDS.get("combined_ratio"))
    if combined is not None:
        result.issues.append(ValidationIssue(
            code="derived_combined_ratio", level="info", concept="combined_ratio",
            message="combined_ratio = loss_ratio + expense_ratio",
            value=combined,
        ))
        if combined > 1.0:
            _add(result, "underwriting_loss", "warning", "combined_ratio",
                 f"Combined ratio {combined:.4f} > 100% => underwriting loss",
                 value=combined)

    # Reserve adequacy sanity: loss_reserves vs premiums written
    reserves = _num(facts, "loss_reserves", "unpaid_losses")
    premiums_written = _num(facts, "premiums_written")
    if reserves is not None and premiums_written:
        reserve_ratio = _div(reserves, premiums_written)
        if reserve_ratio is not None and (reserve_ratio < 0.5 or reserve_ratio > 10.0):
            _add(result, "reserve_ratio_unusual", "warning", "loss_reserves",
                 f"Loss reserves {reserve_ratio:.2f}x premiums written (unusual)",
                 value=reserve_ratio)

    return result


def validate_asset_management(facts: Dict[str, float]) -> ValidationResult:
    """Validate asset-manager fundamentals (fee revenue, expense ratio)."""
    result = ValidationResult(sector=Sector.FINANCIAL, subsector=SubSector.ASSET_MANAGEMENT)

    aum = _num(facts, "aum", "assets_under_management")
    mgmt_fees = _num(facts, "management_fee_revenue", "management_fees")
    perf_fees = _num(facts, "performance_fee_revenue", "performance_fees")
    total_fees = _num(facts, "total_fee_revenue", "total_investment_fee_revenue")
    if total_fees is None and (mgmt_fees is not None or perf_fees is not None):
        total_fees = (mgmt_fees or 0.0) + (perf_fees or 0.0)

    # Implied management fee rate (annualized) — typically 0.1%-2%
    if mgmt_fees is not None and aum:
        implied = _div(mgmt_fees, aum)
        if implied is not None and (implied < 0.001 or implied > 0.10):
            _add(result, "implied_fee_rate_unusual", "warning",
                 "management_fee_revenue",
                 f"Implied management fee rate {implied:.4f} of AUM outside 0.1%..10%",
                 value=implied)

    # Expense ratio = fund op expenses / AUM
    fund_exp = _num(facts, "fund_operating_expenses", "operating_expenses")
    expense_ratio = _div(fund_exp, aum)
    _check_range(result, "expense_ratio", expense_ratio,
                 _RATIO_BOUNDS.get("expense_ratio"))

    return result


def validate_reits(facts: Dict[str, float]) -> ValidationResult:
    """Validate REIT fundamentals; derive NOI, FFO, AFFO, occupancy."""
    result = ValidationResult(sector=Sector.FINANCIAL, subsector=SubSector.REITS)

    rental = _num(facts, "rental_revenue", "rental_income")
    prop_ops = _num(facts, "property_operations_expense", "property_operating_expenses")
    prop_taxes = _num(facts, "real_estate_taxes", "property_taxes")
    noi = _num(facts, "noi")
    if noi is None and rental is not None:
        noi = rental - (prop_ops or 0.0) - (prop_taxes or 0.0)
        if noi is not None:
            result.issues.append(ValidationIssue(
                code="derived_noi", level="info", concept="noi",
                message="noi = rental_revenue - property_ops - property_taxes",
                value=noi,
            ))

    # NOI margin sanity
    if noi is not None and rental:
        noi_margin = _div(noi, rental)
        if noi_margin is not None and (noi_margin < -0.5 or noi_margin > 1.0):
            _add(result, "noi_margin_unusual", "warning", "noi",
                 f"NOI margin {noi_margin:.4f} of rental revenue unusual",
                 value=noi_margin)

    # FFO = net income + depreciation - gains_on_sale
    net_income = _num(facts, "net_income_common", "net_income", "net_income_loss")
    depr = _num(facts, "ffo_depreciation_adjustment", "depreciation")
    gains = _num(facts, "ffo_gains_adjustment", "gain_on_sale")
    ffo = _num(facts, "ffo")
    if ffo is None and net_income is not None and depr is not None:
        ffo = net_income + depr - (gains or 0.0)
        if ffo is not None:
            result.issues.append(ValidationIssue(
                code="derived_ffo", level="info", concept="ffo",
                message="ffo = net_income + depreciation - gains_on_sale",
                value=ffo,
            ))

    # Occupancy
    occupied = _num(facts, "occupied_sqft")
    total = _num(facts, "total_leasable_sqft")
    occupancy = _div(occupied, total)
    if occupancy is not None:
        result.issues.append(ValidationIssue(
            code="derived_occupancy", level="info", concept="occupancy_rate",
            message="occupancy_rate = occupied_sqft / total_leasable_sqft",
            value=occupancy,
        ))
    _check_range(result, "occupancy_rate", occupancy,
                 _RATIO_BOUNDS.get("occupancy_rate"))

    # Cap rate (external, informational)
    cap_rate = _num(facts, "cap_rate")
    _check_range(result, "cap_rate", cap_rate, _RATIO_BOUNDS.get("cap_rate"), hard=False)

    return result


def validate_pharma_biotech(facts: Dict[str, float]) -> ValidationResult:
    """Validate pharma/biotech fundamentals (R&D intensity, gross margin)."""
    result = ValidationResult(sector=Sector.HEALTHCARE, subsector=SubSector.PHARMA_BIOTECH)

    revenue = _num(facts, "product_revenue", "total_revenue_pharma", "revenue")
    cogs = _num(facts, "cost_of_goods_sold_pharma", "cost_of_goods_sold")
    rd = _num(facts, "pharma_rd_expense", "rd_expense", "research_development")

    gross_margin = _div(revenue - (cogs or 0.0), revenue) if revenue is not None else None
    _check_range(result, "gross_margin_pharma", gross_margin,
                 _RATIO_BOUNDS.get("gross_margin_pharma"))
    if gross_margin is not None:
        result.issues.append(ValidationIssue(
            code="derived_gross_margin", level="info", concept="gross_margin_pharma",
            message="gross_margin = (revenue - cogs) / revenue", value=gross_margin,
        ))

    rd_intensity = _div(rd, revenue)
    _check_range(result, "rd_intensity", rd_intensity, _RATIO_BOUNDS.get("rd_intensity"))
    if rd_intensity is not None and rd_intensity > 0.30:
        _add(result, "high_rd_intensity", "info", "pharma_rd_expense",
             f"R&D intensity {rd_intensity:.4f} of revenue > 30% (typical for mid/late biotech)")

    # Royalty revenue sanity: should be non-negative
    royalty = _num(facts, "royalty_revenue")
    if royalty is not None and royalty < 0:
        _add(result, "negative_royalty", "error", "royalty_revenue",
             f"Royalty revenue negative: {royalty}")

    return result


def validate_medical_devices(facts: Dict[str, float]) -> ValidationResult:
    """Validate medical-device fundamentals."""
    result = ValidationResult(sector=Sector.HEALTHCARE, subsector=SubSector.MEDICAL_DEVICES)

    revenue = _num(facts, "device_product_revenue", "product_revenue")
    cogs = _num(facts, "device_cogs", "cost_of_goods_sold")
    if revenue is not None and cogs is not None:
        gross = _div(revenue - cogs, revenue)
        if gross is not None and (gross < 0.0 or gross > 1.0):
            _add(result, "gross_margin_out_of_range", "error",
                 "device_product_revenue",
                 f"Gross margin {gross:.4f} outside [0,1]", value=gross)

    # Deferred revenue should be non-negative
    deferred = _num(facts, "deferred_revenue_devices", "deferred_revenue")
    if deferred is not None and deferred < 0:
        _add(result, "negative_deferred_revenue", "error",
             "deferred_revenue_devices", f"Deferred revenue negative: {deferred}")

    # Warranty reserve sanity
    warranty = _num(facts, "warranty_reserve")
    if warranty is not None and revenue and _div(warranty, revenue) > 0.10:
        _add(result, "warranty_reserves_high", "warning", "warranty_reserve",
             f"Warranty reserves {_div(warranty, revenue):.4f}x revenue > 10%")

    return result


def validate_healthcare_services(facts: Dict[str, float]) -> ValidationResult:
    """Validate healthcare-services fundamentals (revenue per visit, margins)."""
    result = ValidationResult(sector=Sector.HEALTHCARE, subsector=SubSector.HEALTHCARE_SERVICES)

    revenue = _num(facts, "patient_revenue", "net_patient_revenue")
    discharges = _num(facts, "discharges", "patient_days")

    revenue_per_visit = _div(revenue, discharges)
    if revenue_per_visit is not None:
        result.issues.append(ValidationIssue(
            code="derived_revenue_per_visit", level="info",
            concept="revenue_per_visit",
            message="revenue_per_visit = patient_revenue / visits", value=revenue_per_visit,
        ))
        if revenue_per_visit < 0:
            _add(result, "negative_revenue_per_visit", "error", "revenue_per_visit",
                 f"Revenue per visit negative: {revenue_per_visit}")

    # Operating margin from aggregated expenses
    op_exp = _num(facts, "operating_expenses_healthcare", "operating_expenses")
    if op_exp is not None and revenue:
        margin = _div(revenue - op_exp, revenue)
        _check_range(result, "operating_margin_hs", margin,
                     _RATIO_BOUNDS.get("operating_margin_hs"))

    return result


def validate_managed_care(facts: Dict[str, float]) -> ValidationResult:
    """Validate managed-care fundamentals (MLR, admin ratio, payer mix)."""
    result = ValidationResult(sector=Sector.HEALTHCARE, subsector=SubSector.MANAGED_CARE)

    premium = _num(facts, "premium_revenue_mc", "premium_revenue")
    medical = _num(facts, "medical_expenses", "medical_costs")
    admin = _num(facts, "administrative_expenses_mc", "administrative_expenses")

    mlr = _div(medical, premium)
    _check_range(result, "medical_loss_ratio", mlr, _RATIO_BOUNDS.get("medical_loss_ratio"))
    if mlr is not None:
        result.issues.append(ValidationIssue(
            code="derived_mlr", level="info", concept="medical_loss_ratio",
            message="medical_loss_ratio = medical_expenses / premium_revenue",
            value=mlr,
        ))
        # ACA federal floors: 80% (individual/small group) / 85% (large group)
        if mlr is not None and mlr < 0.80:
            _add(result, "mlr_below_aca_floor", "warning", "medical_loss_ratio",
                 f"MLR {mlr:.4f} < 80% ACA rebate floor (may indicate rebates due)")

    admin_ratio = _div(admin, premium)
    _check_range(result, "administrative_ratio", admin_ratio,
                 _RATIO_BOUNDS.get("administrative_ratio"))

    # Premium yield = premium / membership (annualized, external data)
    members = _num(facts, "total_members")
    premium_yield = _div(premium, members)
    if premium_yield is not None:
        result.issues.append(ValidationIssue(
            code="derived_premium_yield", level="info", concept="premium_yield",
            message="premium_yield = premium_revenue / membership",
            value=premium_yield,
        ))

    # Payer mix: shares must sum to ~1 when all segments present
    segments = [
        _num(facts, "commercial_members"),
        _num(facts, "medicare_advantage_members"),
        _num(facts, "medicaid_members"),
    ]
    if members and all(s is not None for s in segments):
        share_total = sum(s for s in segments if s is not None) / members
        if share_total < 0.80 or share_total > 1.05:
            _add(result, "payer_mix_does_not_reconcile", "warning",
                 "total_members",
                 f"Payer segments ({share_total:.2f}) do not reconcile to total membership")

    return result


# ── Dispatch ──────────────────────────────────────────────────────────────────

# ── Technology validators ────────────────────────────────────────────────────

def validate_semiconductors(facts: Dict[str, float]) -> ValidationResult:
    """Validate semiconductor fundamentals."""
    result = ValidationResult(sector=Sector.TECHNOLOGY, subsector=SubSector.SEMICONDUCTORS)
    revenue = _num(facts, "semiconductor_revenue", "revenue")
    cogs = _num(facts, "cost_of_revenue_semi", "cost_of_goods_sold")
    rd = _num(facts, "semiconductor_rd_expense", "rd_expense")
    sbc = _num(facts, "stock_based_comp_semi", "stock_based_compensation")
    gross_margin = _div(revenue - (cogs or 0.0), revenue) if revenue else None
    _check_range(result, "gross_margin_tech", gross_margin, _RATIO_BOUNDS.get("gross_margin_tech"))
    if gross_margin is not None:
        result.issues.append(ValidationIssue(code="derived_gross_margin", level="info",
            concept="semiconductor_gross_margin", message="gross_margin = (revenue - cogs) / revenue", value=gross_margin))
    rd_intensity = _div(rd, revenue)
    _check_range(result, "rd_intensity_tech", rd_intensity, _RATIO_BOUNDS.get("rd_intensity_tech"))
    sbc_intensity = _div(sbc, revenue)
    _check_range(result, "sbc_intensity", sbc_intensity, _RATIO_BOUNDS.get("sbc_intensity"))
    return result


def validate_software_cloud(facts: Dict[str, float]) -> ValidationResult:
    """Validate software/cloud fundamentals."""
    result = ValidationResult(sector=Sector.TECHNOLOGY, subsector=SubSector.SOFTWARE_CLOUD)
    revenue = _num(facts, "software_revenue", "revenue")
    cogs = _num(facts, "cost_of_revenue_sw", "cost_of_goods_sold")
    deferred = _num(facts, "deferred_revenue_sw", "deferred_revenue")
    sbc = _num(facts, "stock_based_comp_sw", "stock_based_compensation")
    gross_margin = _div(revenue - (cogs or 0.0), revenue) if revenue else None
    _check_range(result, "gross_margin_tech", gross_margin, _RATIO_BOUNDS.get("gross_margin_tech"))
    if deferred is not None and deferred < 0:
        _add(result, "negative_deferred_revenue", "error", "deferred_revenue_sw",
             f"Deferred revenue negative: {deferred}")
    sbc_intensity = _div(sbc, revenue)
    _check_range(result, "sbc_intensity", sbc_intensity, _RATIO_BOUNDS.get("sbc_intensity"))
    return result


# ── Automotive validators ────────────────────────────────────────────────────

def validate_automotive_ev(facts: Dict[str, float]) -> ValidationResult:
    """Validate automotive/EV fundamentals."""
    result = ValidationResult(sector=Sector.AUTOMOTIVE, subsector=SubSector.AUTOMOTIVE_EV)
    revenue = _num(facts, "automotive_revenue", "revenue")
    cogs = _num(facts, "cost_of_revenue_auto", "cost_of_goods_sold")
    rd = _num(facts, "automotive_rd_expense", "rd_expense")
    op_income = _num(facts, "operating_income_auto", "operating_income")
    gross_margin = _div(revenue - (cogs or 0.0), revenue) if revenue else None
    _check_range(result, "gross_margin_auto", gross_margin, _RATIO_BOUNDS.get("gross_margin_auto"))
    if gross_margin is not None:
        result.issues.append(ValidationIssue(code="derived_gross_margin", level="info",
            concept="automotive_gross_margin", message="gross_margin = (revenue - cogs) / revenue", value=gross_margin))
    operating_margin = _div(op_income, revenue)
    _check_range(result, "operating_margin_auto", operating_margin, _RATIO_BOUNDS.get("operating_margin_auto"))
    rd_intensity = _div(rd, revenue)
    _check_range(result, "rd_intensity_auto", rd_intensity, _RATIO_BOUNDS.get("rd_intensity_auto"))
    return result


# ── Consumer Electronics validators ──────────────────────────────────────────

def validate_consumer_electronics(facts: Dict[str, float]) -> ValidationResult:
    """Validate consumer electronics fundamentals."""
    result = ValidationResult(sector=Sector.CONSUMER_ELECTRONICS, subsector=SubSector.CONSUMER_ELECTRONICS)
    revenue = _num(facts, "product_revenue_ce", "revenue")
    cogs = _num(facts, "cost_of_goods_ce", "cost_of_goods_sold")
    services = _num(facts, "services_revenue_ce", "services_revenue")
    op_income = _num(facts, "operating_income_ce", "operating_income")
    gross_margin = _div(revenue - (cogs or 0.0), revenue) if revenue else None
    _check_range(result, "gross_margin_ce", gross_margin, _RATIO_BOUNDS.get("gross_margin_ce"))
    if services is not None and revenue:
        services_mix = _div(services, revenue)
        _check_range(result, "services_mix_ce", services_mix, _RATIO_BOUNDS.get("services_mix_ce"))
    operating_margin = _div(op_income, revenue)
    _check_range(result, "operating_margin_ce", operating_margin, _RATIO_BOUNDS.get("operating_margin_ce"))
    return result


# ── E-Commerce validators ────────────────────────────────────────────────────

def validate_ecommerce(facts: Dict[str, float]) -> ValidationResult:
    """Validate e-commerce fundamentals."""
    result = ValidationResult(sector=Sector.ECOMMERCE, subsector=SubSector.ECOMMERCE)
    revenue = _num(facts, "ecommerce_revenue", "revenue")
    cogs = _num(facts, "cost_of_revenue_ecom", "cost_of_goods_sold")
    fulfillment = _num(facts, "fulfillment_expense", "fulfillment_cost")
    op_income = _num(facts, "operating_income_ecom", "operating_income")
    gross_margin = _div(revenue - (cogs or 0.0), revenue) if revenue else None
    _check_range(result, "gross_margin_ecom", gross_margin, _RATIO_BOUNDS.get("gross_margin_ecom"))
    if gross_margin is not None:
        result.issues.append(ValidationIssue(code="derived_gross_margin", level="info",
            concept="ecom_gross_margin", message="gross_margin = (revenue - cogs) / revenue", value=gross_margin))
    fulfillment_ratio = _div(fulfillment, revenue)
    _check_range(result, "fulfillment_ratio", fulfillment_ratio, _RATIO_BOUNDS.get("fulfillment_ratio"))
    operating_margin = _div(op_income, revenue)
    _check_range(result, "operating_margin_ecom", operating_margin, _RATIO_BOUNDS.get("operating_margin_ecom"))
    return result




_VALIDATORS = {
    SubSector.BANKING: validate_banking,
    SubSector.INSURANCE: validate_insurance,
    SubSector.ASSET_MANAGEMENT: validate_asset_management,
    SubSector.REITS: validate_reits,
    SubSector.PHARMA_BIOTECH: validate_pharma_biotech,
    SubSector.MEDICAL_DEVICES: validate_medical_devices,
    SubSector.HEALTHCARE_SERVICES: validate_healthcare_services,
    SubSector.MANAGED_CARE: validate_managed_care,
    SubSector.SEMICONDUCTORS: validate_semiconductors,
    SubSector.SOFTWARE_CLOUD: validate_software_cloud,
    SubSector.AUTOMOTIVE_EV: validate_automotive_ev,
    SubSector.CONSUMER_ELECTRONICS: validate_consumer_electronics,
    SubSector.ECOMMERCE: validate_ecommerce,
}


def validate_facts(
    facts: Dict[str, float],
    subsector: SubSector,
) -> ValidationResult:
    """Validate a dict of extracted facts against a sub-sector's domain rules.

    Args:
        facts: friendly-name -> numeric value dict (from FilingRecord.facts).
        subsector: which sub-sector rules to apply.

    Returns:
        ValidationResult with issues (errors/warnings/info) plus any derived
        ratios appended as info issues.
    """
    validator = _VALIDATORS.get(subsector)
    if validator is None:
        return ValidationResult(subsector=subsector, issues=[
            ValidationIssue("unknown_subsector", "error", "",
                            f"No validator registered for {subsector.value}")
        ])
    return validator(dict(facts))


def validate_filings(
    records,
    subsector: SubSector,
    scope: str = "annual",
) -> Dict[str, ValidationResult]:
    """Validate multiple FilingRecords (e.g. from ``extract_all_filings_from_companyfacts``).

    Args:
        records: iterable of objects with a ``.facts`` dict (FilingRecord-compatible).
        subsector: sub-sector rules to apply.
        scope: "annual" or "quarterly" (informational only).

    Returns:
        Dict keyed by accession (falls back to index) of ValidationResult.
    """
    out: Dict[str, ValidationResult] = {}
    for i, rec in enumerate(records):
        key = getattr(rec, "accession", None) or f"record_{i}"
        out[key] = validate_facts(dict(rec.facts), subsector)
    return out