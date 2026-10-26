"""Extract raw US-GAAP XBRL facts from cached companyfacts.json for backtest-ready fundamentals."""

import json
import polars as pl
from pathlib import Path
from typing import Dict, List, Optional

# Core US-GAAP concepts needed for valuation backtest
# These are the raw SEC XBRL tags
RAW_CONCEPTS = {
    # Income Statement
    "revenue": "RevenueFromContractWithCustomerExcludingAssessedTax",
    "revenue_alt": "Revenues",
    "operating_income": "OperatingIncomeLoss",
    "ebit": "EarningsBeforeInterestAndTaxes",
    "interest_expense": "InterestExpense",
    "income_tax_expense": "IncomeTaxExpenseBenefit",
    "net_income": "NetIncomeLoss",
    "pretax_income": "IncomeLossFromContinuingOperationsBeforeIncomeTaxes",
    
    # Balance Sheet
    "total_assets": "Assets",
    "total_liabilities": "Liabilities",
    "stockholders_equity": "StockholdersEquity",
    "cash": "CashAndCashEquivalentsAtCarryingValue",
    "long_term_debt": "LongTermDebtNoncurrent",
    "total_debt": "DebtAndCapitalLeaseObligations",
    "short_term_debt": "ShortTermDebt",
    "current_assets": "AssetsCurrent",
    "current_liabilities": "LiabilitiesCurrent",
    "inventory": "InventoryNet",
    "receivables": "AccountsReceivableNetCurrent",
    "payables": "AccountsPayableCurrent",
    
    # Cash Flow
    "operating_cash_flow": "NetCashProvidedByUsedInOperatingActivities",
    "capex": "PaymentsToAcquirePropertyPlantAndEquipment",
    "depreciation_amortization": "DepreciationDepletionAndAmortization",
    "rd_expense": "ResearchAndDevelopmentExpense",
    "sbc_expense": "ShareBasedCompensation",
    "change_nwc": "IncreaseDecreaseInOperatingCapitalNet",
    
    # Shares
    "shares_outstanding": "CommonStockSharesOutstanding",
    "shares_diluted": "WeightedAverageNumberOfSharesOutstandingDiluted",
}

def extract_raw_facts_from_companyfacts(companyfacts: dict, ticker: str) -> List[dict]:
    """Extract all raw USD facts for target concepts from companyfacts JSON."""
    rows = []
    facts_usgaap = companyfacts.get("facts", {}).get("us-gaap", {})
    facts_ifrs = companyfacts.get("facts", {}).get("ifrs-full", {})
    
    for field_name, concept in RAW_CONCEPTS.items():
        entries = []
        # Try us-gaap first
        if concept in facts_usgaap:
            tag_data = facts_usgaap[concept]
            units = tag_data.get("units", {})
            entries = units.get("USD", [])
        # Fallback to IFRS
        if not entries and concept in facts_ifrs:
            tag_data = facts_ifrs[concept]
            units = tag_data.get("units", {})
            entries = units.get("USD", [])
        
        if not entries:
            continue
            
        for entry in entries:
            # Only keep entries with required fields
            if not all(k in entry for k in ("val", "end", "filed", "accn")):
                continue
            if entry["val"] is None:
                continue
                
            rows.append({
                "ticker": ticker,
                "cik": companyfacts.get("cik", ""),
                "concept": concept,
                "field": field_name,
                "value": float(entry["val"]),
                "unit": entry.get("uom", "USD"),
                "fiscal_end": entry["end"],
                "filed": entry["filed"],
                "accession": entry["accn"],
                "form": entry.get("form", ""),
                "frame": entry.get("frame", ""),
            })
    
    return rows

def main():
    data_dir = Path("data/pit_fundamentals")
    all_rows = []
    
    # Get all ticker directories
    ticker_dirs = [d for d in data_dir.iterdir() if d.is_dir()]
    print(f"Found {len(ticker_dirs)} ticker directories")
    
    for ticker_dir in ticker_dirs:
        ticker = ticker_dir.name
        cf_file = ticker_dir / "companyfacts.json"
        if not cf_file.exists():
            continue
            
        try:
            with open(cf_file, "r") as f:
                companyfacts = json.load(f)
            
            rows = extract_raw_facts_from_companyfacts(companyfacts, ticker)
            all_rows.extend(rows)
            
            if len(all_rows) % 50000 == 0:
                print(f"  Processed {ticker}: {len(rows)} rows, total {len(all_rows)}")
                
        except Exception as e:
            print(f"Error processing {ticker}: {e}")
    
    if not all_rows:
        print("No data extracted!")
        return
    
    df = pl.DataFrame(all_rows)
    print(f"\nTotal raw fact rows: {len(df)}")
    print(f"Unique tickers: {df['ticker'].n_unique()}")
    print(f"Unique concepts: {df['concept'].n_unique()}")
    
    # Deduplicate: same ticker, concept, fiscal_end, filed -> keep first
    df = df.unique(subset=["ticker", "concept", "fiscal_end", "filed"], keep="first")
    print(f"After dedup: {len(df)} rows")
    
    # Sort for PIT access
    df = df.sort(["ticker", "filed", "concept"])
    
    # Save
    out_path = Path("data/pit_fundamentals/raw_xbrl_facts.parquet")
    df.write_parquet(out_path)
    print(f"Saved to {out_path}")
    
    # Summary by concept
    print("\nConcept coverage (ticker count):")
    for concept in sorted(df["concept"].unique().to_list()):
        cnt = df.filter(pl.col("concept") == concept)["ticker"].n_unique()
        print(f"  {concept}: {cnt} tickers")
    
    # Summary by field
    print("\nField coverage (ticker count):")
    for field in sorted(df["field"].unique().to_list()):
        cnt = df.filter(pl.col("field") == field)["ticker"].n_unique()
        print(f"  {field}: {cnt} tickers")

if __name__ == "__main__":
    main()