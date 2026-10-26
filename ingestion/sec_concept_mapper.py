"""US-GAAP taxonomy concept mapper.

Maps the raw, namespaced XBRL tags emitted by
``ingestion.sec_xbrl_full_extractor`` onto a canonical factor vocabulary, then
annotates every raw fact row with ``canonical`` (or null) and ``category``.

Outputs
-------
``config/xbrl_concept_map.json``
    ``{raw_concept: {canonical_name, category, unit, formula_hint, taxonomy,
    taxonomy_type, period_type, balance, needs_footnote, priority}}``.
``data/pit_fundamentals/xbrl_full/mapped_concepts.parquet``
    The raw facts plus ``canonical`` + ``category`` columns (streamed).

Taxonomy
--------
The US-GAAP 2024 element definitions are downloaded (cached under
``config/taxonomy/us-gaap-2024/``) from the FASB XBRL taxonomy service to
validate tag existence and infer an element type.  Network is best-effort:
if the download fails the curated synonym dictionary is still fully usable.

CLI
---
    python -m ingestion.sec_concept_mapper \
        --input data/pit_fundamentals/xbrl_full/raw_xbrl_facts_full.parquet \
        --map config/xbrl_concept_map.json \
        --output data/pit_fundamentals/xbrl_full/mapped_concepts.parquet
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import re
import sys
import urllib.request
import zipfile
from collections import OrderedDict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import polars as pl

from ingestion.pit_provenance import write_provenance

log = logging.getLogger("sec_concept_mapper")

ROOT = Path(__file__).resolve().parents[1]
SOURCE_VERSION = "companyfacts_20261010"
TAXONOMY_VERSION = "us-gaap-2024"

TAXONOMY_URLS = [
    "https://xbrl.fasb.org/us-gaap/2024/us-gaap-2024.zip",
    "https://www.sec.gov/files/taxonomies/us-gaap-2024.zip",
]
TAXONOMY_ELEMENT_MEMBER = "us-gaap-2024/elts/us-gaap-2024.xsd"

# ---------------------------------------------------------------------------
# Curated canonical vocabulary.
#
# Each entry: canonical_name -> (category, unit_family, formula_hint,
#                                needs_footnote, [synonym, ...])
#
# A synonym may be ``Tag`` (us-gaap by default) or ``taxonomy:Tag``.  Order is
# priority: earlier tags win when several are present for the same period.
# unit_family in {USD, shares, per_share, pure, other}.
# ---------------------------------------------------------------------------
CuratedSpec = Tuple[str, str, Optional[str], bool, List[str]]

CURATED: "OrderedDict[str, CuratedSpec]" = OrderedDict([
    # ----- income statement (duration) -------------------------------------
    ("revenue", ("revenue", "USD", "top-line revenue", False, [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
        "SalesRevenueServicesNet",
        "RevenuesNetOfInterestExpense",
        "HealthCareOrganizationRevenue",
        "RegulatedAndUnregulatedOperatingRevenue",
        "OilAndGasRevenue",
        "RealEstateRevenueNet",
        "InterestAndDividendIncomeOperating",
    ])),
    ("cogs", ("cogs", "USD", "cost of revenue", False, [
        "CostOfRevenue",
        "CostOfGoodsAndServicesSold",
        "CostOfGoodsSold",
        "CostOfServices",
        "CostOfSales",
        "CostOfGoodsSoldExcludingDepreciationDepletionAndAmortization",
    ])),
    ("gross_profit", ("gross_profit", "USD", "revenue - cogs", False, [
        "GrossProfit",
    ])),
    ("operating_expenses", ("opex", "USD", None, False, [
        "OperatingExpenses",
        "CostsAndExpenses",
        "OperatingCostsAndExpenses",
    ])),
    ("sga", ("opex", "USD", None, False, [
        "SellingGeneralAndAdministrativeExpense",
        "GeneralAndAdministrativeExpense",
        "SellingAndMarketingExpense",
        "SellingExpense",
        "MarketingExpense",
    ])),
    ("rd_expense", ("opex", "USD", "R&D expense", False, [
        "ResearchAndDevelopmentExpense",
        "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
        "ResearchAndDevelopmentExpenseSoftwareExcludingAcquiredInProcessCost",
    ])),
    ("operating_income", ("operating_income", "USD", "EBIT proxy", False, [
        "OperatingIncomeLoss",
    ])),
    ("pretax_income", ("income", "USD", "EBT", False, [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxes",
    ])),
    ("interest_expense", ("interest", "USD", "cost of debt numerator", False, [
        "InterestExpense",
        "InterestExpenseNonoperating",
        "InterestExpenseDebt",
        "InterestAndDebtExpense",
        "InterestExpenseBorrowings",
    ])),
    # Component interest tags.  Many utilities / banks / lessees tag interest
    # only as a breakdown; the dense builder sums these when no total exists.
    ("interest_expense_finance_lease", ("interest", "USD", "interest component", False, [
        "FinanceLeaseInterestExpense",
    ])),
    ("interest_expense_deposit", ("interest", "USD", "interest component", False, [
        "InterestExpenseDepositLiability",
    ])),
    ("interest_expense_longterm_debt", ("interest", "USD", "interest component", False, [
        "InterestExpenseLongTermDebt",
    ])),
    ("interest_expense_shortterm_debt", ("interest", "USD", "interest component", False, [
        "InterestExpenseShortTermBorrowings",
    ])),
    ("interest_expense_other", ("interest", "USD", "interest component", False, [
        "InterestExpenseOther",
    ])),
    ("interest_income", ("interest", "USD", None, False, [
        "InterestIncomeOperating",
        "InvestmentIncomeInterest",
    ])),
    ("income_tax_expense", ("tax", "USD", None, False, [
        "IncomeTaxExpenseBenefit",
    ])),
    ("net_income", ("net_income", "USD", None, False, [
        "NetIncomeLoss",
        "ProfitLoss",
    ])),
    ("net_income_common", ("net_income", "USD", "Net income to common", False, [
        "NetIncomeLossAvailableToCommonStockholdersBasic",
        "NetIncomeLossAvailableToCommonStockholdersDiluted",
    ])),
    ("eps_basic", ("per_share", "per_share", None, False, [
        "EarningsPerShareBasic",
    ])),
    ("eps_diluted", ("per_share", "per_share", None, False, [
        "EarningsPerShareDiluted",
    ])),
    ("shares_basic", ("shares", "shares", None, False, [
        "WeightedAverageNumberOfSharesOutstandingBasic",
    ])),
    ("shares_diluted", ("shares", "shares", None, False, [
        "WeightedAverageNumberOfDilutedSharesOutstanding",
    ])),
    ("depreciation_amortization", ("cash", "USD", "D&A for EBITDA", False, [
        "DepreciationDepletionAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "DepreciationAndAmortization",
        "DepreciationDepletionAndAmortizationExcludingAmortizationOfDeferredFinancingCosts",
        "Depreciation",
        "AmortizationOfIntangibleAssets",
    ])),
    ("sbc_expense", ("cash", "USD", "stock-based comp", False, [
        "ShareBasedCompensation",
        "AllocatedShareBasedCompensationExpense",
        "ShareBasedCompensationArrangementByShareBasedPaymentAwardCompensationCost",
    ])),
    ("dividends_paid", ("cash", "USD", "dividends", False, [
        "PaymentsOfDividends",
        "PaymentsOfDividendsCommonStock",
        "PaymentsOfDividendsMinorityInterest",
    ])),
    ("buybacks", ("cash", "USD", "share repurchases", False, [
        "PaymentsForRepurchaseOfCommonStock",
        "PaymentsForRepurchaseOfEquity",
    ])),
    ("acquisitions", ("cash", "USD", "M&A cash", False, [
        "PaymentsToAcquireBusinessesNetOfCashAcquired",
        "BusinessCombinationConsiderationTransferred1",
        "PaymentsToAcquireBusinessesGross",
    ])),
    # ----- cash flow (duration) -------------------------------------------
    ("operating_cash_flow", ("cashflow", "USD", "FCF numerator", False, [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ])),
    ("investing_cash_flow", ("cashflow", "USD", None, False, [
        "NetCashProvidedByUsedInInvestingActivities",
        "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations",
    ])),
    ("financing_cash_flow", ("cashflow", "USD", None, False, [
        "NetCashProvidedByUsedInFinancingActivities",
        "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations",
    ])),
    ("capex", ("capex", "USD", "FCF deduction", False, [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "PaymentsForCapitalImprovements",
        "PaymentsToAcquireOtherPropertyPlantAndEquipment",
        "PaymentsToAcquireMachineryAndEquipment",
    ])),
    ("change_nwc", ("cashflow", "USD", None, False, [
        "IncreaseDecreaseInOperatingCapitalNet",
        "IncreaseDecreaseInOperatingAssetsAndLiabilitiesNet",
    ])),
    # ----- balance sheet (instant) ----------------------------------------
    ("cash", ("cash", "USD", None, False, [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "CashAndCashEquivalents",
    ])),
    ("marketable_securities", ("investments", "USD", None, False, [
        "MarketableSecuritiesCurrent",
        "ShortTermInvestments",
        "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
        "AvailableForSaleSecurities",
        "OtherShortTermInvestments",
    ])),
    ("receivables", ("receivables", "USD", None, False, [
        "AccountsReceivableNetCurrent",
        "AccountsReceivableNet",
        "ReceivablesNetCurrent",
    ])),
    ("inventory", ("inventory", "USD", None, False, [
        "InventoryNet",
        "InventoryFinishedGoods",
        "InventoryFinishedGoodsNetOfReserves",
        "InventoryRawMaterials",
        "InventoryWorkInProcess",
    ])),
    ("ppe_net", ("ppe", "USD", None, False, [
        "PropertyPlantAndEquipmentNet",
    ])),
    ("goodwill", ("intangibles", "USD", None, False, [
        "Goodwill",
    ])),
    ("intangibles", ("intangibles", "USD", None, False, [
        "FiniteLivedIntangibleAssetsNet",
        "IntangibleAssetsNetExcludingGoodwill",
        "IndefiniteLivedIntangibleAssetsExcludingGoodwill",
        "FiniteLivedIntangibleAssetsGross",
    ])),
    ("total_assets", ("balance", "USD", None, False, [
        "Assets",
    ])),
    ("total_liabilities", ("balance", "USD", None, False, [
        "Liabilities",
    ])),
    ("current_assets", ("balance", "USD", None, False, [
        "AssetsCurrent",
    ])),
    ("current_liabilities", ("balance", "USD", None, False, [
        "LiabilitiesCurrent",
    ])),
    ("total_debt", ("debt", "USD", "WACC D numerator", False, [
        "LongTermDebt",
        "LongTermDebtAndCapitalLeaseObligations",
        "DebtLongtermAndShorttermCombinedAmount",
        "DebtAndCapitalLeaseObligations",
    ])),
    ("long_term_debt", ("debt", "USD", None, False, [
        "LongTermDebtNoncurrent",
        "LongTermDebtAndCapitalLeaseObligations",
        "LongTermDebt",
    ])),
    ("short_term_debt", ("debt", "USD", None, False, [
        "ShortTermBorrowings",
        "LongTermDebtCurrent",
        "DebtCurrent",
        "CommercialPaper",
        "LongTermDebtAndCapitalLeaseObligationsCurrent",
    ])),
    ("operating_lease_liability", ("debt", "USD", None, True, [
        "OperatingLeaseLiability",
    ])),
    ("operating_lease_liability_current", ("debt", "USD", None, True, [
        "OperatingLeaseLiabilityCurrent",
    ])),
    ("operating_lease_liability_noncurrent", ("debt", "USD", None, True, [
        "OperatingLeaseLiabilityNoncurrent",
    ])),
    ("equity", ("equity", "USD", "WACC E numerator", False, [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ])),
    ("retained_earnings", ("equity", "USD", None, False, [
        "RetainedEarningsAccumulatedDeficit",
    ])),
    ("accumulated_oci", ("equity", "USD", None, False, [
        "AccumulatedOtherComprehensiveIncomeLossNetOfTax",
    ])),
    ("treasury_stock", ("equity", "USD", None, False, [
        "TreasuryStockCommonValue",
        "TreasuryStockValue",
    ])),
    ("preferred_stock", ("equity", "USD", None, False, [
        "PreferredStockValue",
    ])),
    ("minority_interest", ("equity", "USD", None, False, [
        "MinorityInterest",
    ])),
    ("deferred_revenue", ("deferred_revenue", "USD", None, False, [
        "DeferredRevenueCurrent",
        "DeferredRevenueNoncurrent",
        "DeferredRevenue",
    ])),
    ("deferred_revenue_current", ("deferred_revenue", "USD", None, False, [
        "DeferredRevenueCurrent",
        "ContractWithCustomerLiabilityCurrent",
    ])),
    ("deferred_revenue_noncurrent", ("deferred_revenue", "USD", None, False, [
        "DeferredRevenueNoncurrent",
        "ContractWithCustomerLiabilityNoncurrent",
    ])),
    ("contract_liabilities", ("contract_liabilities", "USD", "RPO balance", False, [
        "ContractWithCustomerLiabilityCurrent",
        "ContractWithCustomerLiabilityNoncurrent",
        "ContractWithCustomerLiability",
    ])),
    ("contract_liability_current", ("contract_liabilities", "USD", None, False, [
        "ContractWithCustomerLiabilityCurrent",
    ])),
    ("contract_liability_noncurrent", ("contract_liabilities", "USD", None, False, [
        "ContractWithCustomerLiabilityNoncurrent",
    ])),
    ("rpo", ("rpo", "USD", "remaining performance obligation", True, [
        "RevenueRemainingPerformanceObligation",
    ])),
    ("payables", ("payables", "USD", None, False, [
        "AccountsPayableCurrent",
        "AccountsPayableAndAccruedLiabilitiesCurrent",
        "AccountsPayableTradeCurrent",
    ])),
    ("deferred_tax_assets", ("balance", "USD", None, False, [
        "DeferredIncomeTaxAssetsNet",
        "DeferredTaxAssetsNet",
    ])),
    ("deferred_tax_liabilities", ("balance", "USD", None, False, [
        "DeferredIncomeTaxLiabilitiesNet",
        "DeferredTaxLiabilities",
    ])),
    ("long_term_investments", ("investments", "USD", None, False, [
        "LongTermInvestments",
        "EquityMethodInvestments",
    ])),
    ("shares_outstanding", ("shares", "shares", None, False, [
        "dei:EntityCommonStockSharesOutstanding",
        "CommonStockSharesOutstanding",
        "CommonStockSharesIssued",
    ])),
])

# Derived factors live in the dense builder; declared here for documentation
# and so the audit can reason about expected coverage.
DERIVED_FACTORS = [
    "gross_margin", "operating_margin", "net_margin", "ebit_margin",
    "ebitda", "ebitda_margin", "fcf", "fcf_margin", "fcf_to_assets",
    "nopat", "invested_capital", "roic", "roe", "roa", "asset_turnover",
    "net_debt", "debt_to_equity", "debt_to_assets", "net_debt_to_equity",
    "net_debt_to_ebitda", "interest_coverage", "ebitda_interest_coverage",
    "current_ratio", "quick_ratio", "cash_ratio", "working_capital",
    "inventory_turnover", "receivables_turnover", "days_sales_outstanding",
    "days_inventory", "cash_conversion_cycle", "effective_tax_rate",
    "rd_to_revenue", "sbc_to_revenue", "capex_to_revenue",
    "depreciation_to_revenue", "sga_to_revenue", "reinvestment_rate",
    "expected_growth", "fcf_roic", "payout_ratio", "book_value_per_share",
    "cost_of_debt", "after_tax_cost_of_debt", "weight_debt", "weight_equity",
    "cost_of_equity_proxy", "wacc_proxy", "revenue_yoy", "ebit_yoy",
    "net_income_yoy", "fcf_yoy", "shares_yoy", "accruals", "asset_growth",
    "capex_to_depreciation",
]

# ---------------------------------------------------------------------------
# Taxonomy download / parse
# ---------------------------------------------------------------------------


def download_taxonomy(cache_dir: Path, *, force: bool = False) -> Optional[Path]:
    """Download the US-GAAP 2024 taxonomy zip and extract the element XSD.

    Returns the path to the extracted element XSD, or ``None`` on failure.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    xsd_path = cache_dir / "us-gaap-2024-elts.xsd"
    if xsd_path.exists() and not force:
        return xsd_path

    last_err: Optional[Exception] = None
    for url in TAXONOMY_URLS:
        try:
            log.info("Downloading taxonomy: %s", url)
            req = urllib.request.Request(
                url, headers={"User-Agent": "QuantResearch research@example.com"}
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = resp.read()
            zf = zipfile.ZipFile(io.BytesIO(payload))
            member = next(
                (n for n in zf.namelist() if n.endswith(TAXONOMY_ELEMENT_MEMBER)),
                None,
            )
            if member is None:
                member = next(
                    (n for n in zf.namelist() if n.endswith("us-gaap-2024.xsd")),
                    None,
                )
            if member is None:
                raise RuntimeError("element xsd not found in taxonomy zip")
            with open(xsd_path, "wb") as fh:
                fh.write(zf.read(member))
            log.info("Cached taxonomy element xsd -> %s (%d bytes)", xsd_path, xsd_path.stat().st_size)
            return xsd_path
        except Exception as exc:  # pragma: no cover - network dependent
            last_err = exc
            log.warning("Taxonomy download failed from %s: %s", url, exc)
    log.warning("Could not download US-GAAP taxonomy (%s); using curated map only", last_err)
    return None


def parse_taxonomy_xsd(xsd_path: Path) -> Dict[str, Dict[str, object]]:
    """Parse ``<xs:element ...>`` definitions into {name: metadata}.

    Note: the FASB XSD uses single-quoted attributes, so we parse tolerant of
    both quote styles.
    """
    txt = xsd_path.read_text(encoding="utf-8", errors="ignore")
    elements: Dict[str, Dict[str, object]] = {}
    for m in re.finditer(r"<xs:element\b([^>]*?)/?>", txt, flags=re.DOTALL):
        attrs = dict(re.findall(r"([\w:-]+)\s*=\s*[\"']([^\"']*)[\"']", m.group(1)))
        name = attrs.get("name")
        if not name:
            continue
        elem_type = attrs.get("type", "")
        elements[name] = {
            "type": elem_type,
            "period_type": attrs.get("xbrli:periodType"),
            "balance": attrs.get("xbrli:balance"),
            "abstract": attrs.get("abstract", "false").lower() == "true",
        }
    return elements


# ---------------------------------------------------------------------------
# Mapping construction
# ---------------------------------------------------------------------------

UNIT_BY_TYPE = {
    "monetaryItemType": "USD",
    "sharesItemType": "shares",
    "perShareItemType": "per_share",
    "pureItemType": "pure",
    "percentItemType": "pure",
    "decimalItemType": "pure",
    "integerItemType": "other",
}

_CATEGORY_KEYWORDS = [
    ("Revenue", "revenue"), ("Sales", "revenue"),
    ("CostOf", "cogs"), ("Cost", "expense"),
    ("Expense", "expense"), ("Income", "income"),
    ("Cash", "cash"), ("Debt", "debt"), ("Borrow", "debt"), ("Notes", "debt"),
    ("Asset", "asset"), ("Liabilit", "liability"), ("Equity", "equity"),
    ("Stock", "equity"), ("Inventory", "inventory"),
    ("Receivab", "receivables"), ("Payable", "payables"),
    ("Goodwill", "intangibles"), ("Intangible", "intangibles"),
    ("PropertyPlant", "ppe"),
]


def _infer_category(tag: str, elem_type: str) -> str:
    t = elem_type.split(":")[-1]
    if "textBlock" in t:
        return "disclosure"
    if t == "domainItemType":
        return "axis_member"
    if t in ("monetaryItemType",):
        for kw, cat in _CATEGORY_KEYWORDS:
            if kw.lower() in tag.lower():
                return cat
        return "monetary_other"
    if t == "sharesItemType":
        return "shares"
    if t in ("perShareItemType",):
        return "per_share"
    if t in ("pureItemType", "percentItemType", "decimalItemType"):
        return "ratio"
    return "other"


def build_concept_map(
    concepts: List[str],
    elements: Optional[Dict[str, Dict[str, object]]] = None,
) -> "OrderedDict[str, dict]":
    """Build the ``raw_concept -> spec`` mapping.

    ``concepts`` are namespaced (``us-gaap:Assets``).  Curated synonyms win;
    everything else gets an inferred category with ``canonical_name=None``.
    """
    elements = elements or {}

    # tag (optionally tax:Tag) -> (canonical, spec, priority)
    curated_index: Dict[str, Tuple[str, CuratedSpec, int]] = {}
    for canonical, spec in CURATED.items():
        for prio, syn in enumerate(spec[4]):
            key = syn if ":" in syn else f"us-gaap:{syn}"
            curated_index.setdefault(key, (canonical, spec, prio))

    out: "OrderedDict[str, dict]" = OrderedDict()
    for concept in sorted(concepts):
        tax, _, tag = concept.partition(":")
        hit = curated_index.get(concept)
        elem = elements.get(tag, {}) if tax == "us-gaap" else {}
        elem_type = str(elem.get("type", "")) or (hit[1][1] if hit else "")
        if hit:
            canonical, spec, prio = hit
            category, unit_family, formula_hint, needs_footnote, _ = spec
            out[concept] = {
                "canonical_name": canonical,
                "category": category,
                "unit": unit_family,
                "formula_hint": formula_hint,
                "taxonomy": tax,
                "taxonomy_type": elem_type or None,
                "period_type": elem.get("period_type"),
                "balance": elem.get("balance"),
                "needs_footnote": bool(needs_footnote),
                "priority": prio,
            }
        else:
            out[concept] = {
                "canonical_name": None,
                "category": _infer_category(tag, elem_type),
                "unit": UNIT_BY_TYPE.get(elem_type.split(":")[-1], "other"),
                "formula_hint": None,
                "taxonomy": tax,
                "taxonomy_type": elem_type or None,
                "period_type": elem.get("period_type"),
                "balance": elem.get("balance"),
                "needs_footnote": "textBlock" in elem_type,
                "priority": None,
            }
    return out


def _map_to_dataframe(concept_map: "OrderedDict[str, dict]") -> pl.DataFrame:
    rows = []
    for concept, spec in concept_map.items():
        rows.append({
            "concept": concept,
            "canonical": spec["canonical_name"],
            "category": spec["category"],
            "unit_family": spec.get("unit"),
            "period_type": spec.get("period_type"),
            "priority": spec.get("priority"),
        })
    return pl.DataFrame(
        rows,
        schema={
            "concept": pl.Utf8,
            "canonical": pl.Utf8,
            "category": pl.Utf8,
            "unit_family": pl.Utf8,
            "period_type": pl.Utf8,
            "priority": pl.Int64,
        },
    )


def map_parquet(
    raw_path: Path,
    out_path: Path,
    concept_map: "OrderedDict[str, dict]",
) -> Dict[str, object]:
    """Stream-annotate the raw facts parquet with canonical + category."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    map_df = _map_to_dataframe(concept_map)
    lf = (
        pl.scan_parquet(raw_path)
        .join(map_df.lazy(), on="concept", how="left")
        .select([
            "ticker", "cik", "concept", "canonical", "category",
            "unit_family", "period_type", "priority",
            "field", "value", "unit", "fiscal_end", "filed", "accession",
            "form", "frame", "start",
        ])
    )
    lf.sink_parquet(out_path, compression="zstd")
    stats = (
        pl.scan_parquet(out_path)
        .select([
            pl.len().alias("rows"),
            pl.col("canonical").is_not_null().sum().alias("mapped_rows"),
            pl.col("concept").n_unique().alias("concepts"),
        ])
        .collect()
        .row(0)
    )
    return {"rows": stats[0], "mapped_rows": stats[1], "concepts": stats[2]}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        default="data/pit_fundamentals/xbrl_full/raw_xbrl_facts_full.parquet",
    )
    parser.add_argument("--map", default="config/xbrl_concept_map.json")
    parser.add_argument(
        "--output",
        default="data/pit_fundamentals/xbrl_full/mapped_concepts.parquet",
    )
    parser.add_argument("--taxonomy-cache", default="config/taxonomy/us-gaap-2024")
    parser.add_argument("--no-download", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    def _abs(p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else ROOT / path

    raw_path = _abs(args.input)
    map_path = _abs(args.map)
    out_path = _abs(args.output)
    if not raw_path.exists():
        log.error("Raw facts parquet not found: %s", raw_path)
        return 2

    # 1. taxonomy elements (best-effort).  ``--no-download`` still uses a
    # cached XSD if one is present; it only forbids network access.
    elements: Dict[str, Dict[str, object]] = {}
    cache_dir = _abs(args.taxonomy_cache)
    cached_xsd = cache_dir / "us-gaap-2024-elts.xsd"
    cached_json = cache_dir / "us-gaap-2024-elements.json"
    xsd: Optional[Path] = None
    if not args.no_download:
        xsd = download_taxonomy(_abs(args.taxonomy_cache))
    elif cached_xsd.exists():
        xsd = cached_xsd

    if xsd is not None:
        elements = parse_taxonomy_xsd(xsd)
        log.info("Parsed %d US-GAAP element definitions", len(elements))
    elif cached_json.exists():
        with open(cached_json, "r", encoding="utf-8") as fh:
            elements = json.load(fh)
        log.info("Loaded %d cached US-GAAP element definitions", len(elements))

    # 2. unique raw concepts
    concepts = (
        pl.scan_parquet(raw_path)
        .select(pl.col("concept").unique())
        .collect()["concept"]
        .to_list()
    )
    log.info("Mapping %d unique raw concepts", len(concepts))

    concept_map = build_concept_map(concepts, elements)
    mapped = sum(1 for v in concept_map.values() if v["canonical_name"])
    log.info("Curated mappings: %d concepts -> canonical factors", mapped)

    # 3. write map JSON + taxonomy element cache
    map_path.parent.mkdir(parents=True, exist_ok=True)
    with open(map_path, "w", encoding="utf-8") as fh:
        json.dump(concept_map, fh, indent=2)
    if elements:
        elem_out = _abs(args.taxonomy_cache) / "us-gaap-2024-elements.json"
        with open(elem_out, "w", encoding="utf-8") as fh:
            json.dump(elements, fh)
        log.info("Wrote taxonomy element cache -> %s", elem_out)

    # 4. annotate parquet
    stats = map_parquet(raw_path, out_path, concept_map)
    log.info(
        "Mapped parquet: %s rows (%s canonical, %.1f%%), %d concepts",
        f"{stats['rows']:,}",
        f"{stats['mapped_rows']:,}",
        100.0 * stats["mapped_rows"] / max(stats["rows"], 1),
        stats["concepts"],
    )

    # 5. provenance
    write_provenance(
        map_path,
        source="SEC_EDGAR",
        source_version=SOURCE_VERSION,
        pit_timestamp_column="filed",
        entity_key="cik",
        transformations=["concept_map_usgaap2024", "synonym_reconciliation"],
        row_count=len(concept_map),
        date_range={"min": None, "max": None},
        mapping_confidence="high",
        extra={
            "raw_input": str(raw_path).replace("\\", "/"),
            "canonical_concepts": mapped,
            "taxonomy_version": TAXONOMY_VERSION,
            "taxonomy_elements": len(elements),
        },
    )
    write_provenance(
        out_path,
        source="SEC_EDGAR",
        source_version=SOURCE_VERSION,
        pit_timestamp_column="filed",
        entity_key="cik",
        transformations=["concept_map_usgaap2024", "row_annotation"],
        row_count=stats["rows"],
        date_range={"min": None, "max": None},
        mapping_confidence="high",
        extra={
            "concept_map": str(map_path).replace("\\", "/"),
            "mapped_rows": stats["mapped_rows"],
            "mapped_concepts": mapped,
        },
    )
    print(json.dumps({"concepts": len(concept_map), "canonical": mapped, **stats}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
