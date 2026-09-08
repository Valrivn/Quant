"""EDGAR/XBRL Ingestion Pipeline — Expanded Sector XBRL Chains.

Fetches latest 10-K and 4x 10-Q per company, parses with sector-specific
XBRL concept chains, applies validators, and stores results in data lake.

Usage:
    python -m discovery.edgar_ingestion.pipeline

Requires: DISCOVERY_LIVE=1 for network access.
"""

from __future__ import annotations

import json
import os
import sys
import time
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from discovery.sector_xbrl_chains import (
    Sector, SubSector, ConceptNode, ALL_SECTOR_CONCEPTS,
    get_concepts_by_sector, get_concepts_by_subsector, build_xbrl_concept_map,
)
from discovery.sector_validators import validate_facts, ValidationResult

logger = logging.getLogger(__name__)

# -- Configuration ------------------------------------------------------------

USER_AGENT = "Quant-research backtest/1.0 (data integrity work; contact hayden@quant.local)"
SEC_RATE_LIMIT_DELAY = 1.0  # 1 req/s (SEC allows 10, being conservative)

# Company definitions with sector classifications
COMPANIES = {
    # Semiconductors
    "NVDA": {"sector": Sector.TECHNOLOGY, "subsector": SubSector.SEMICONDUCTORS, "peers": ["AMD", "INTC", "AVGO", "QCOM"]},
    "AMD": {"sector": Sector.TECHNOLOGY, "subsector": SubSector.SEMICONDUCTORS, "peers": []},
    "INTC": {"sector": Sector.TECHNOLOGY, "subsector": SubSector.SEMICONDUCTORS, "peers": []},
    "AVGO": {"sector": Sector.TECHNOLOGY, "subsector": SubSector.SEMICONDUCTORS, "peers": []},
    "QCOM": {"sector": Sector.TECHNOLOGY, "subsector": SubSector.SEMICONDUCTORS, "peers": []},
    # Consumer Electronics
    "AAPL": {"sector": Sector.CONSUMER_ELECTRONICS, "subsector": SubSector.CONSUMER_ELECTRONICS, "peers": ["MSFT", "GOOGL", "META"]},
    "MSFT": {"sector": Sector.TECHNOLOGY, "subsector": SubSector.SOFTWARE_CLOUD, "peers": ["ORCL", "CRM", "ADBE", "NOW"]},
    "GOOGL": {"sector": Sector.TECHNOLOGY, "subsector": SubSector.SOFTWARE_CLOUD, "peers": []},
    "META": {"sector": Sector.TECHNOLOGY, "subsector": SubSector.SOFTWARE_CLOUD, "peers": []},
    # Automotive/EV
    "TSLA": {"sector": Sector.AUTOMOTIVE, "subsector": SubSector.AUTOMOTIVE_EV, "peers": ["F", "GM", "RIVN", "LCID"]},
    "F": {"sector": Sector.AUTOMOTIVE, "subsector": SubSector.AUTOMOTIVE_EV, "peers": []},
    "GM": {"sector": Sector.AUTOMOTIVE, "subsector": SubSector.AUTOMOTIVE_EV, "peers": []},
    "RIVN": {"sector": Sector.AUTOMOTIVE, "subsector": SubSector.AUTOMOTIVE_EV, "peers": []},
    "LCID": {"sector": Sector.AUTOMOTIVE, "subsector": SubSector.AUTOMOTIVE_EV, "peers": []},
    # Software/Cloud (additional peers)
    "ORCL": {"sector": Sector.TECHNOLOGY, "subsector": SubSector.SOFTWARE_CLOUD, "peers": []},
    "CRM": {"sector": Sector.TECHNOLOGY, "subsector": SubSector.SOFTWARE_CLOUD, "peers": []},
    "ADBE": {"sector": Sector.TECHNOLOGY, "subsector": SubSector.SOFTWARE_CLOUD, "peers": []},
    "NOW": {"sector": Sector.TECHNOLOGY, "subsector": SubSector.SOFTWARE_CLOUD, "peers": []},
    # E-Commerce
    "AMZN": {"sector": Sector.ECOMMERCE, "subsector": SubSector.ECOMMERCE, "peers": ["WMT", "SHOP", "EBAY", "MELI"]},
    "WMT": {"sector": Sector.ECOMMERCE, "subsector": SubSector.ECOMMERCE, "peers": []},
    "SHOP": {"sector": Sector.ECOMMERCE, "subsector": SubSector.ECOMMERCE, "peers": []},
    "EBAY": {"sector": Sector.ECOMMERCE, "subsector": SubSector.ECOMMERCE, "peers": []},
    "MELI": {"sector": Sector.ECOMMERCE, "subsector": SubSector.ECOMMERCE, "peers": []},
}

# Deduplicated ticker list
ALL_TICKERS = sorted(set(COMPANIES.keys()))

# Number of 10-Q filings to fetch (in addition to 10-K)
NUM_10Q = 4


# -- Data structures ----------------------------------------------------------

@dataclass
class Filing:
    """A single SEC filing to process."""
    ticker: str
    cik: str
    accession: str
    form: str
    filed: str
    fiscal_end: str
    url: str = ""


@dataclass
class ParsedFiling:
    """Parsed result for a single filing."""
    ticker: str
    cik: str
    accession: str
    form: str
    filed: str
    fiscal_end: str
    sector: str
    subsector: str
    facts: Dict[str, Optional[float]] = field(default_factory=dict)
    concepts_used: Dict[str, str] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)
    validation: Optional[dict] = None
    parse_errors: List[str] = field(default_factory=list)


@dataclass
class IngestionSummary:
    """Summary of the ingestion run."""
    started_at: str = ""
    completed_at: str = ""
    tickers_targeted: int = 0
    ciks_resolved: int = 0
    ciks_failed: List[str] = field(default_factory=list)
    filings_fetched: int = 0
    filings_parsed: int = 0
    filings_failed: int = 0
    total_concepts_extracted: int = 0
    total_validation_flags: int = 0
    sector_breakdown: Dict[str, int] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)


# -- CIK Resolution -----------------------------------------------------------

def resolve_ciks(tickers: List[str]) -> Dict[str, Optional[str]]:
    """Resolve tickers to SEC CIKs using the cik_resolver module."""
    try:
        from valuation_alpha.universe.cik_resolver import resolve_ciks as _resolve
        return _resolve(tickers)
    except Exception as exc:
        logger.warning("CIK resolver failed: %s — using manual fallback", exc)
        # Manual fallback for known tickers
        MANUAL_CIKS = {
            "NVDA": "0001045810", "AMD": "0000002488", "INTC": "0000050863",
            "AVGO": "0001054374", "QCOM": "0000804328",
            "AAPL": "0000320193", "MSFT": "0000789019", "GOOGL": "0001652044",
            "META": "0001326801",
            "TSLA": "0001318605", "F": "0000037996", "GM": "0001467858",
            "RIVN": "0001874178", "LCID": "0001811306",
            "ORCL": "0001341439", "CRM": "0001108524", "ADBE": "0000796343",
            "NOW": "0001374571",
            "AMZN": "0001018724", "WMT": "0000093751", "SHOP": "0001708114",
            "EBAY": "0001065679", "MELI": "0001099590",
        }
        return {t: MANUAL_CIKS.get(t) for t in tickers}


# -- EDGAR Fetching -----------------------------------------------------------

def _sec_get(url: str, max_retries: int = 3) -> Optional[dict]:
    """GET from SEC EDGAR with retries and rate limiting."""
    import requests
    for attempt in range(max_retries):
        try:
            time.sleep(SEC_RATE_LIMIT_DELAY)
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
            if resp.status_code == 429:
                wait = 2 ** (attempt + 1)
                logger.warning("SEC rate limited, waiting %ds", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            if attempt == max_retries - 1:
                logger.error("SEC fetch failed after %d attempts: %s", max_retries, exc)
                return None
    return None


def fetch_submissions(cik: str) -> List[dict]:
    """Fetch recent filings from SEC EDGAR submissions endpoint."""
    cik_padded = cik.zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    data = _sec_get(url)
    if not data:
        return []

    recent = data.get("filings", {}).get("recent", {})
    if not recent:
        return []

    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    filed_dates = recent.get("filingDate", [])
    primary_docs = recent.get("primaryDocument", [])

    filings = []
    for i, form in enumerate(forms):
        if form in ("10-K", "10-Q"):
            accn = accessions[i]
            accn_dashed = accn.replace("-", "")
            filings.append({
                "accession": accn,
                "accession_dashed": accn_dashed,
                "form": form,
                "filed": filed_dates[i],
                "primary_doc": primary_docs[i],
                "url": f"https://www.sec.gov/Archives/edgar/data/{cik_padded}/{accn_dashed}/{primary_docs[i]}",
            })

    return filings


def fetch_companyfacts(cik: str) -> Optional[dict]:
    """Fetch companyfacts XBRL data from SEC EDGAR."""
    cik_padded = cik.zfill(10)
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_padded}.json"
    return _sec_get(url)


def select_filings(submissions: List[dict], num_10q: int = NUM_10Q) -> List[dict]:
    """Select latest 10-K and up to num_10q 10-Q filings."""
    k_filings = [f for f in submissions if f["form"] == "10-K"]
    q_filings = [f for f in submissions if f["form"] == "10-Q"]

    # Sort by filing date descending
    k_filings.sort(key=lambda f: f["filed"], reverse=True)
    q_filings.sort(key=lambda f: f["filed"], reverse=True)

    selected = []
    if k_filings:
        selected.append(k_filings[0])  # Latest 10-K
    selected.extend(q_filings[:num_10q])  # Latest N 10-Q

    return selected


# -- XBRL Parsing with Sector Chains -----------------------------------------

def extract_with_sector_chains(
    companyfacts: dict,
    ticker: str,
    filing: dict,
    sector: Sector,
    subsector: SubSector,
) -> ParsedFiling:
    """Extract facts using both base and sector-specific XBRL concept chains."""
    from discovery.xbrl_parser import (
        CONCEPT_CHAINS, _get_entries, _get_shares_entries,
        _extract_value_for_filing, _extract_value_by_end,
    )

    info = COMPANIES.get(ticker, {})
    parsed = ParsedFiling(
        ticker=ticker,
        cik=filing.get("cik", ""),
        accession=filing["accession"],
        form=filing["form"],
        filed=filing["filed"],
        fiscal_end=filing.get("fiscal_end", filing["filed"]),
        sector=sector.value,
        subsector=subsector.value,
    )

    # Step 1: Extract base concepts (revenue, ebit, capex, etc.)
    base_fields = [
        "revenue", "ebit", "tax_rate_num", "tax_rate_denom", "capex",
        "depreciation", "rd_expense", "long_term_debt", "short_term_debt", "cash",
    ]

    for friendly in base_fields:
        concepts = CONCEPT_CHAINS.get(friendly, [])
        found = False
        for concept in concepts:
            entries = _get_entries(companyfacts, concept)
            if not entries:
                continue
            result = _extract_value_for_filing(entries, filing["accession"], parsed.fiscal_end)
            if result is None:
                result = _extract_value_by_end(entries, parsed.fiscal_end)
            if result is not None:
                val, unit = result
                if val is not None:
                    parsed.facts[friendly] = float(val)
                    parsed.concepts_used[friendly] = concept
                    found = True
                    break
        if not found:
            parsed.missing.append(friendly)

    # Shares outstanding
    for concept in CONCEPT_CHAINS.get("shares_outstanding", []):
        entries = _get_shares_entries(companyfacts, concept)
        if not entries:
            continue
        result = _extract_value_for_filing(entries, filing["accession"], parsed.fiscal_end)
        if result is None:
            result = _extract_value_by_end(entries, parsed.fiscal_end)
        if result is not None:
            val, unit = result
            if val is not None:
                parsed.facts["shares_outstanding"] = float(val)
                parsed.concepts_used["shares_outstanding"] = concept
                break
    else:
        parsed.missing.append("shares_outstanding")

    # Total debt
    ltd = parsed.facts.get("long_term_debt")
    std = parsed.facts.get("short_term_debt")
    if ltd is not None or std is not None:
        parsed.facts["total_debt"] = (ltd or 0.0) + (std or 0.0)
        parsed.concepts_used["total_debt"] = "computed:long_term+short_term"

    # Tax rate
    tax_num = parsed.facts.get("tax_rate_num")
    tax_den = parsed.facts.get("tax_rate_denom")
    if tax_num is not None and tax_den is not None and tax_den != 0:
        parsed.facts["tax_rate"] = abs(tax_num / tax_den)
        parsed.concepts_used["tax_rate"] = "computed:tax_expense/pretax_income"

    # Remove intermediate fields
    for intermediate in ("tax_rate_num", "tax_rate_denom"):
        parsed.facts.pop(intermediate, None)

    # Step 2: Extract sector-specific concepts
    sector_concepts = get_concepts_by_subsector(subsector)
    for concept_key, node in sector_concepts.items():
        if node.is_computed or not node.chain:
            continue
        if concept_key in parsed.facts:
            continue  # Already extracted
        found = False
        for xbrl_tag in node.chain:
            entries = _get_entries(companyfacts, xbrl_tag)
            if not entries:
                continue
            result = _extract_value_for_filing(entries, filing["accession"], parsed.fiscal_end)
            if result is None:
                result = _extract_value_by_end(entries, parsed.fiscal_end)
            if result is not None:
                val, unit = result
                if val is not None:
                    parsed.facts[concept_key] = float(val)
                    parsed.concepts_used[concept_key] = xbrl_tag
                    found = True
                    break
        if not found:
            parsed.missing.append(concept_key)

    # Step 3: Compute sector-specific derived fields
    _compute_sector_derived(parsed, subsector)

    return parsed


def _compute_sector_derived(parsed: ParsedFiling, subsector: SubSector) -> None:
    """Compute sector-specific derived metrics."""
    facts = parsed.facts

    if subsector == SubSector.SEMICONDUCTORS:
        rev = facts.get("semiconductor_revenue") or facts.get("revenue")
        cogs = facts.get("cost_of_revenue_semi")
        if rev and cogs is not None:
            facts["semiconductor_gross_margin"] = (rev - cogs) / rev
            parsed.concepts_used["semiconductor_gross_margin"] = "computed"
        rd = facts.get("semiconductor_rd_expense") or facts.get("rd_expense")
        if rev and rd is not None:
            facts["semiconductor_rd_intensity"] = rd / rev
            parsed.concepts_used["semiconductor_rd_intensity"] = "computed"

    elif subsector == SubSector.SOFTWARE_CLOUD:
        rev = facts.get("software_revenue") or facts.get("revenue")
        cogs = facts.get("cost_of_revenue_sw")
        if rev and cogs is not None:
            facts["software_gross_margin"] = (rev - cogs) / rev
            parsed.concepts_used["software_gross_margin"] = "computed"

    elif subsector == SubSector.AUTOMOTIVE_EV:
        rev = facts.get("automotive_revenue") or facts.get("revenue")
        cogs = facts.get("cost_of_revenue_auto")
        if rev and cogs is not None:
            facts["automotive_gross_margin"] = (rev - cogs) / rev
            parsed.concepts_used["automotive_gross_margin"] = "computed"

    elif subsector == SubSector.CONSUMER_ELECTRONICS:
        rev = facts.get("product_revenue_ce") or facts.get("revenue")
        cogs = facts.get("cost_of_goods_ce")
        if rev and cogs is not None:
            facts["ce_gross_margin"] = (rev - cogs) / rev
            parsed.concepts_used["ce_gross_margin"] = "computed"

    elif subsector == SubSector.ECOMMERCE:
        rev = facts.get("ecommerce_revenue") or facts.get("revenue")
        cogs = facts.get("cost_of_revenue_ecom")
        if rev and cogs is not None:
            facts["ecom_gross_margin"] = (rev - cogs) / rev
            parsed.concepts_used["ecom_gross_margin"] = "computed"


# -- Validation ---------------------------------------------------------------

def validate_parsed_filing(parsed: ParsedFiling) -> ValidationResult:
    """Apply sector-specific validation to a parsed filing."""
    try:
        subsector = SubSector(parsed.subsector)
        return validate_facts(parsed.facts, subsector)
    except Exception as exc:
        logger.warning("Validation failed for %s/%s: %s", parsed.ticker, parsed.accession, exc)
        return ValidationResult()


# -- Storage ------------------------------------------------------------------

def store_results(
    parsed_filings: List[ParsedFiling],
    summary: IngestionSummary,
    output_dir: Path,
) -> None:
    """Store parsed results in the data lake."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # Store individual filing results
    for parsed in parsed_filings:
        filename = f"{parsed.ticker}_{parsed.accession.replace('-', '')}.json"
        filepath = output_dir / filename
        data = {
            "ticker": parsed.ticker,
            "cik": parsed.cik,
            "accession": parsed.accession,
            "form": parsed.form,
            "filed": parsed.filed,
            "fiscal_end": parsed.fiscal_end,
            "sector": parsed.sector,
            "subsector": parsed.subsector,
            "facts": parsed.facts,
            "concepts_used": parsed.concepts_used,
            "missing": parsed.missing,
            "validation": parsed.validation,
            "parse_errors": parsed.parse_errors,
        }
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2, default=str)

    # Store summary
    summary_path = output_dir / "_ingestion_summary.json"
    with open(summary_path, "w") as f:
        json.dump(asdict(summary), f, indent=2, default=str)

    # Store concept chain mapping
    concept_map = {}
    for ticker, info in COMPANIES.items():
        subsector = info["subsector"]
        concepts = get_concepts_by_subsector(subsector)
        concept_map[ticker] = {
            "sector": info["sector"].value,
            "subsector": subsector.value,
            "concepts": {
                k: {"chain": list(v.chain), "unit": v.unit, "is_computed": v.is_computed}
                for k, v in concepts.items()
            },
        }
    concept_map_path = output_dir / "_concept_chain_map.json"
    with open(concept_map_path, "w") as f:
        json.dump(concept_map, f, indent=2)


# -- Main Pipeline ------------------------------------------------------------

def run_pipeline() -> IngestionSummary:
    """Execute the full EDGAR/XBRL ingestion pipeline."""
    summary = IngestionSummary(
        started_at=datetime.utcnow().isoformat(),
        tickers_targeted=len(ALL_TICKERS),
    )

    logger.info("Starting EDGAR ingestion for %d tickers", len(ALL_TICKERS))

    # Step 1: Resolve CIKs
    logger.info("Step 1: Resolving CIKs...")
    cik_map = resolve_ciks(ALL_TICKERS)

    resolved = {t: c for t, c in cik_map.items() if c}
    failed = [t for t, c in cik_map.items() if not c]
    summary.ciks_resolved = len(resolved)
    summary.ciks_failed = failed

    logger.info("CIKs resolved: %d/%d", len(resolved), len(ALL_TICKERS))
    if failed:
        logger.warning("Failed CIKs: %s", failed)

    # Step 2: Fetch submissions and select filings
    logger.info("Step 2: Fetching submissions...")
    all_parsed: List[ParsedFiling] = []

    for ticker in sorted(resolved.keys()):
        cik = resolved[ticker]
        info = COMPANIES[ticker]
        sector = info["sector"]
        subsector = info["subsector"]

        logger.info("Processing %s (CIK=%s, sector=%s)...", ticker, cik, sector.value)

        # Fetch submissions
        submissions = fetch_submissions(cik)
        if not submissions:
            logger.warning("No submissions found for %s", ticker)
            summary.filings_failed += 1
            continue

        selected = select_filings(submissions)
        logger.info("  Selected %d filings for %s", len(selected), ticker)

        # Fetch companyfacts
        companyfacts = fetch_companyfacts(cik)
        if not companyfacts:
            logger.warning("No companyfacts for %s", ticker)
            summary.filings_failed += 1
            continue

        # Parse each filing
        for filing in selected:
            filing["cik"] = cik
            try:
                parsed = extract_with_sector_chains(
                    companyfacts, ticker, filing, sector, subsector
                )

                # Validate
                validation = validate_parsed_filing(parsed)
                parsed.validation = validation.to_dict()

                all_parsed.append(parsed)
                summary.filings_parsed += 1
                summary.total_concepts_extracted += len(parsed.facts)
                summary.total_validation_flags += len(validation.issues)

                # Track sector breakdown
                sector_key = f"{sector.value}:{subsector.value}"
                summary.sector_breakdown[sector_key] = summary.sector_breakdown.get(sector_key, 0) + 1

            except Exception as exc:
                logger.error("Failed to parse %s/%s: %s", ticker, filing["accession"], exc)
                summary.filings_failed += 1
                summary.errors.append(f"{ticker}/{filing['accession']}: {exc}")

    summary.filings_fetched = summary.filings_parsed + summary.filings_failed
    summary.completed_at = datetime.utcnow().isoformat()

    # Step 3: Store results
    logger.info("Step 3: Storing results...")
    output_dir = PROJECT_ROOT / "data" / "edgar_xbrl_ingestion"
    store_results(all_parsed, summary, output_dir)

    logger.info("Pipeline complete. Results stored to %s", output_dir)
    return summary


# -- CLI Entry Point ----------------------------------------------------------

def main():
    """CLI entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    summary = run_pipeline()

    # Print summary
    print("\n" + "=" * 70)
    print("EDGAR/XBRL INGESTION SUMMARY")
    print("=" * 70)
    print(f"  Tickers targeted:     {summary.tickers_targeted}")
    print(f"  CIKs resolved:        {summary.ciks_resolved}/{summary.tickers_targeted}")
    if summary.ciks_failed:
        print(f"  CIKs failed:          {', '.join(summary.ciks_failed)}")
    print(f"  Filings fetched:      {summary.filings_fetched}")
    print(f"  Filings parsed:       {summary.filings_parsed}")
    print(f"  Filings failed:       {summary.filings_failed}")
    print(f"  Concepts extracted:   {summary.total_concepts_extracted}")
    print(f"  Validation flags:     {summary.total_validation_flags}")
    print()
    print("  Sector breakdown:")
    for sector, count in sorted(summary.sector_breakdown.items()):
        print(f"    {sector}: {count} filings")
    if summary.errors:
        print()
        print("  Errors:")
        for err in summary.errors[:10]:
            print(f"    - {err}")
    print("=" * 70)


if __name__ == "__main__":
    main()
