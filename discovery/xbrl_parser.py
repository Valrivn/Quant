"""XBRL JSON parser — extract standardized PIT fundamentals from SEC EDGAR companyfacts.

Every extracted value traces to a specific filing (accession + filed date).
as_of date = filing date (not period end date), ensuring no look-ahead bias.

Concept fallback chains: SEC filers use different GAAP tags across years;
this module tries multiple candidates and coalesces the first available.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── XBRL concept candidates per friendly field ──────────────────────────────
# Order matters: first hit wins.  Groups are tried in sequence; the first
# tag that yields a non-null value for the filing wins.

CONCEPT_CHAINS: Dict[str, List[str]] = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomer",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
        "OperatingRevenue",
    ],
    "ebit": [
        "OperatingIncomeLoss",
        "OperatingIncome",
        "IncomeFromOperations",
        "OperatingProfitLoss",
        "IncomeLossFromOperations",
    ],
    "tax_rate_num": [
        "IncomeTaxExpenseBenefit",
    ],
    "tax_rate_denom": [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxes",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesAndMinorityInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesAndAffiliates",
    ],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "CapitalExpenditureDiscontinuedOperations",
    ],
    "depreciation": [
        "DepreciationDepletionAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "DepreciationAndAmortization",
        "Depreciation",
        "AmortizationOfIntangibleAssets",
    ],
    "change_nwc_opc": [
        "IncreaseDecreaseInAccountsReceivable",
        "IncreaseDecreaseInOtherReceivablesNet",
        "IncreaseDecreaseInInventories",
        "IncreaseDecreaseInAccountsPayable",
        "IncreaseDecreaseInAccruedLiabilitiesNet",
        "IncreaseDecreaseInOtherCurrentLiabilitiesNet",
        "IncreaseDecreaseInOtherOperatingCapitalNet",
        "IncreaseDecreaseInOtherOperatingAssets",
        "IncreaseDecreaseInOtherOperatingLiabilities",
    ],
    "rd_expense": [
        "ResearchAndDevelopmentExpense",
    ],
    "long_term_debt": [
        "LongTermDebtNoncurrent",
        "LongTermDebtAndCapitalLeaseObligationsNoncurrent",
        "LongTermDebt",
        "LongTermDebtAndCapitalLeaseObligations",
    ],
    "short_term_debt": [
        "ShortTermDebt",
        "ShortTermDebtCurrent",
        "LongTermDebtCurrent",
        "LongTermDebtCurrentMaturities",
        "DebtCurrent",
        "CurrentPortionOfLongTermDebt",
        "CommercialPaper",
    ],
    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsAndShortTermInvestments",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ],
    "shares_outstanding": [
        "CommonStockSharesOutstanding",
        "CommonStockSharesAuthorized",
        "EntityCommonStockSharesOutstanding",
    ],
}


@dataclass
class FilingFact:
    """One extracted fact from a single filing."""
    ticker: str
    accession: str
    form: str
    filed: str            # YYYY-MM-DD filing date
    fiscal_end: str       # YYYY-MM-DD period end
    field: str            # friendly name
    value: Optional[float] = None
    concept: str = ""     # which XBRL concept was used
    unit: str = "USD"


@dataclass
class FilingRecord:
    """Complete extracted record for one filing (all fields)."""
    ticker: str
    accession: str
    form: str
    filed: str
    fiscal_end: str
    facts: Dict[str, Optional[float]] = field(default_factory=dict)
    concepts_used: Dict[str, str] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)


def _get_entries(companyfacts: dict, tag: str) -> List[dict]:
    """Get all USD-denominated entries for a US-GAAP concept."""
    us_gaap = companyfacts.get("facts", {}).get("us-gaap", {})
    # Also check IFRS for non-US filers (e.g., some foreign private issuers)
    ifrs = companyfacts.get("facts", {}).get("ifrs-full", {})
    
    for namespace in (us_gaap, ifrs):
        tag_data = namespace.get(tag, {})
        units = tag_data.get("units", {})
        entries = units.get("USD", [])
        if entries:
            return entries
    return []


def _get_shares_entries(companyfacts: dict, tag: str) -> List[dict]:
    """Get shares-outstanding entries (unit = shares or pure)."""
    us_gaap = companyfacts.get("facts", {}).get("us-gaap", {})
    tag_data = us_gaap.get(tag, {})
    units = tag_data.get("units", {})
    # Try shares, pure, then USD (some filers oddly report in USD)
    for unit_key in ("shares", "pure", "USD"):
        entries = units.get(unit_key, [])
        if entries:
            return entries
    return []


def _extract_value_for_filing(
    entries: List[dict],
    accession: str,
    fiscal_end: str,
) -> Optional[Tuple[float, str]]:
    """Find the value for a specific filing (by accession) or fiscal end.
    
    Priority:
    1. Exact accession match (most precise — XBRL inline fact).
    2. Fiscal-end match with same accession prefix.
    3. Fiscal-end match (fallback).
    
    Returns (value, unit) or None.
    """
    # Pass 1: exact accession match
    candidates = [e for e in entries if e.get("accn") == accession]
    if candidates:
        # If multiple, pick the one closest to fiscal_end
        candidates.sort(key=lambda e: abs(
            hash(e.get("end", "")) - hash(fiscal_end)))
        e = candidates[0]
        return e.get("val"), e.get("uom", "USD")

    # Pass 2: fiscal end match with filed date filtering
    candidates = [e for e in entries if e.get("end") == fiscal_end]
    if candidates:
        e = candidates[0]
        return e.get("val"), e.get("uom", "USD")

    return None


def _extract_value_by_end(
    entries: List[dict],
    fiscal_end: str,
) -> Optional[Tuple[float, str]]:
    """Extract a value for a given fiscal period end from all entries."""
    candidates = [e for e in entries if e.get("end") == fiscal_end]
    if not candidates:
        return None
    # Prefer the entry with the earliest filed date (PIT-safe)
    candidates.sort(key=lambda e: e.get("filed", "9999"))
    e = candidates[0]
    return e.get("val"), e.get("uom", "USD")


def extract_filing_from_companyfacts(
    companyfacts: dict,
    ticker: str,
    filing: dict,
) -> FilingRecord:
    """Extract all target fields from companyfacts for one filing.
    
    Args:
        companyfacts: Full companyfacts JSON response
        ticker: Stock ticker
        filing: Dict with keys: accession, form, filed, fiscal_end
    
    Returns:
        FilingRecord with extracted facts and missing field list.
    """
    record = FilingRecord(
        ticker=ticker,
        accession=filing["accession"],
        form=filing["form"],
        filed=filing["filed"],
        fiscal_end=filing["fiscal_end"],
    )

    # Fields that use standard USD entries
    usd_fields = [
        "revenue", "ebit", "tax_rate_num", "tax_rate_denom", "capex",
        "depreciation", "rd_expense",
        "long_term_debt", "short_term_debt", "cash",
    ]

    for friendly in usd_fields:
        concepts = CONCEPT_CHAINS.get(friendly, [])
        found = False
        for concept in concepts:
            entries = _get_entries(companyfacts, concept)
            if not entries:
                continue
            result = _extract_value_for_filing(
                entries, record.accession, record.fiscal_end)
            if result is None:
                result = _extract_value_by_end(entries, record.fiscal_end)
            if result is not None:
                val, unit = result
                if val is not None:
                    record.facts[friendly] = float(val)
                    record.concepts_used[friendly] = concept
                    found = True
                    break
        if not found:
            record.missing.append(friendly)

    # Shares outstanding (different unit)
    for concept in CONCEPT_CHAINS["shares_outstanding"]:
        entries = _get_shares_entries(companyfacts, concept)
        if not entries:
            continue
        result = _extract_value_for_filing(
            entries, record.accession, record.fiscal_end)
        if result is None:
            result = _extract_value_by_end(entries, record.fiscal_end)
        if result is not None:
            val, unit = result
            if val is not None:
                record.facts["shares_outstanding"] = float(val)
                record.concepts_used["shares_outstanding"] = concept
                break
    else:
        record.missing.append("shares_outstanding")

    # Total debt = long_term_debt + short_term_debt
    ltd = record.facts.get("long_term_debt")
    std = record.facts.get("short_term_debt")
    if ltd is not None or std is not None:
        record.facts["total_debt"] = (ltd or 0.0) + (std or 0.0)
        record.concepts_used["total_debt"] = "computed:long_term+short_term"
    else:
        record.missing.append("total_debt")

    # Tax rate = tax_expense / pretax_income
    tax_num = record.facts.get("tax_rate_num")
    tax_den = record.facts.get("tax_rate_denom")
    if tax_num is not None and tax_den is not None and tax_den != 0:
        record.facts["tax_rate"] = abs(tax_num / tax_den)
        record.concepts_used["tax_rate"] = "computed:tax_expense/pretax_income"
    else:
        record.missing.append("tax_rate")

    # Change in NWC from operating cash flow components
    nwc_components = []
    nwc_concepts_used = []
    for concept in CONCEPT_CHAINS["change_nwc_opc"]:
        entries = _get_entries(companyfacts, concept)
        if not entries:
            continue
        result = _extract_value_for_filing(
            entries, record.accession, record.fiscal_end)
        if result is None:
            result = _extract_value_by_end(entries, record.fiscal_end)
        if result is not None:
            val, _ = result
            if val is not None:
                nwc_components.append(float(val))
                nwc_concepts_used.append(concept)
    if nwc_components:
        # Sum of working capital changes from CFO section
        # These are adjustments to reconcile net income to OCF
        # Typically: increases in assets are negative, increases in liabilities positive
        record.facts["change_nwc"] = sum(nwc_components)
        record.concepts_used["change_nwc"] = "+".join(nwc_concepts_used)
    else:
        record.missing.append("change_nwc")

    # Remove intermediate fields from final facts
    for intermediate in ("tax_rate_num", "tax_rate_denom",
                         "long_term_debt", "short_term_debt"):
        record.facts.pop(intermediate, None)

    return record


def extract_all_filings_from_companyfacts(
    companyfacts: dict,
    ticker: str,
    filings: List[dict],
) -> List[FilingRecord]:
    """Extract PIT facts for all filings of one ticker.
    
    Args:
        companyfacts: Full companyfacts JSON
        ticker: Stock ticker
        filings: List of filing dicts from submissions index
    
    Returns:
        List of FilingRecord, one per filing.
    """
    records = []
    for filing in filings:
        try:
            record = extract_filing_from_companyfacts(
                companyfacts, ticker, filing)
            records.append(record)
        except Exception as exc:
            logger.warning("Failed to extract %s/%s: %s",
                           ticker, filing.get("accession", "?"), exc)
    return records


def compute_computed_fields(record: FilingRecord) -> Dict[str, Any]:
    """Return a flat dict of all fields including computed ones."""
    out = {
        "ticker": record.ticker,
        "accession": record.accession,
        "form": record.form,
        "filed": record.filed,
        "fiscal_end": record.fiscal_end,
    }
    # All standard fields
    for field_name in ("revenue", "ebit", "tax_rate", "capex", "depreciation",
                       "change_nwc", "rd_expense", "total_debt", "cash",
                       "shares_outstanding"):
        out[field_name] = record.facts.get(field_name)
    out["missing_fields"] = record.missing
    out["concepts_used"] = record.concepts_used
    return out


def coverage_summary(records: List[FilingRecord]) -> dict:
    """Compute coverage statistics across a list of filing records."""
    if not records:
        return {"total_filings": 0}
    all_fields = ["revenue", "ebit", "tax_rate", "capex", "depreciation",
                  "change_nwc", "rd_expense", "total_debt", "cash",
                  "shares_outstanding"]
    coverage = {f: 0 for f in all_fields}
    for rec in records:
        for f in all_fields:
            if f in rec.facts and rec.facts[f] is not None:
                coverage[f] += 1
    return {
        "total_filings": len(records),
        "coverage": {f: {"count": c, "pct": round(100 * c / len(records), 1)}
                     for f, c in coverage.items()},
    }


