#!/usr/bin/env python
"""Re-parse latest 10-K for affected tickers with updated concept chains."""

import sys
sys.path.insert(0, '.')

import json
from pathlib import Path
from discovery.xbrl_parser import extract_filing_from_companyfacts

def reparse_ticker(ticker: str):
    parsed_path = Path(f"data/pit_fundamentals/{ticker}/parsed_filings.json")
    if not parsed_path.exists():
        print(f"{ticker}: No parsed_filings.json")
        return
    
    with parsed_path.open() as f:
        filings = json.load(f)
    
    # Find latest 10-K
    k_filings = [f for f in filings if f.get("form") == "10-K"]
    if not k_filings:
        print(f"{ticker}: No 10-K filings")
        return
    
    k_filings.sort(key=lambda f: f["filed"], reverse=True)
    latest_10k = k_filings[0]
    
    # Re-extract facts using updated concept chains
    # We need the raw companyfacts for this ticker
    cf_path = Path(f"data/pit_fundamentals/{ticker}/companyfacts.json")
    if not cf_path.exists():
        print(f"{ticker}: No companyfacts.json")
        return
    
    with cf_path.open() as f:
        companyfacts = json.load(f)
    
    # Re-extract
    from discovery.xbrl_parser import extract_filing_from_companyfacts
    new_record = extract_filing_from_companyfacts(companyfacts, ticker, latest_10k)
    
    # Update the filing in the list
    for i, f in enumerate(filings):
        if f.get("accession") == latest_10k["accession"]:
            filings[i] = {
                "ticker": new_record.ticker,
                "accession": new_record.accession,
                "form": new_record.form,
                "filed": new_record.filed,
                "fiscal_end": new_record.fiscal_end,
                "facts": new_record.facts,
                "concepts_used": new_record.concepts_used,
                "missing": new_record.missing,
            }
            break
    
    # Save back
    with parsed_path.open("w") as f:
        json.dump(filings, f, indent=2)
    
    print(f"{ticker}: Updated latest 10-K ({latest_10k['filed']})")
    print(f"  New facts: {list(new_record.facts.keys())}")
    print(f"  Missing: {new_record.missing}")

if __name__ == "__main__":
    # Tickers with missing fields in latest 10-K
    affected = ['JNJ', 'V', 'AMZN', 'PFE', 'JPM', 'MA', 'UNH', 'HD', 'DIS', 'PYPL', 'ADBE', 'NFLX', 'CRM', 'INTC', 'TMO', 'ABBV', 'ACN', 'COST']
    
    for ticker in affected:
        try:
            reparse_ticker(ticker)
        except Exception as e:
            print(f"{ticker}: Error - {e}")
    
    print("Done!")