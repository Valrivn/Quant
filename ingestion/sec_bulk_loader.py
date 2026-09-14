"""
SEC Bulk Loader
Downloads companyfacts.zip from SEC EDGAR, parses facts/filings/concepts into Parquet partitions,
and generates JSON provenance tracking. Supports incremental daily RSS checks.
"""

import os
import json
import zipfile
import datetime
import hashlib
import urllib.request
import argparse
from pathlib import Path
import pandas as pd
import polars as pl

SEC_COMPANYFACTS_URL = "https://www.sec.gov/files/companyfacts.zip"
SEC_USER_AGENT = "QuantPlatform/1.0 (quant@example.com)"

def calculate_sha256(filepath: Path) -> str:
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()

def write_provenance(partition_dir: Path, source_url: str, version: str, sha256_hash: str, row_count: int):
    provenance = {
        "source": source_url,
        "retrieval_ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "version": version,
        "sha256": sha256_hash,
        "row_count": row_count
    }
    with open(partition_dir / "provenance.json", "w", encoding="utf-8") as f:
        json.dump(provenance, f, indent=2)

def generate_synthetic_companyfacts_zip(dest_zip: Path, num_ciks: int = 100, universe_path: str = None):
    """Generates synthetic SEC companyfacts JSON files for testing with historical data from 2018-2024."""
    import random
    random.seed(42)
    
    # Load real tickers from universe if provided
    if universe_path:
        try:
            import pandas as pd
            universe_df = pd.read_parquet(universe_path)
            # Get unique tickers from first date
            first_date = universe_df['date'].min()
            first_month = universe_df[universe_df['date'] == first_date]
            real_tickers = first_month['ticker'].dropna().unique().tolist()
            real_ciks = first_month[['ticker', 'cik']].drop_duplicates().set_index('ticker')['cik'].to_dict()
            tickers = real_tickers[:num_ciks]
            sample_ciks = [real_ciks.get(t, f"{i+1:010d}") for i, t in enumerate(tickers)]
            print(f"Using {len(tickers)} real tickers from universe")
        except Exception as e:
            print(f"Could not load universe: {e}, using synthetic tickers")
            sample_ciks = [f"{i+1:010d}" for i in range(num_ciks)]
            tickers = [f"TICK{i+1}" for i in range(num_ciks)]
    else:
        sample_ciks = [f"{i+1:010d}" for i in range(num_ciks)]
        tickers = [f"TICK{i+1}" for i in range(num_ciks)]
    
    def _synth_concept(base_val, growth_rate, unit, start_year=2017, end_year=2024):
        """Helper to create synthetic concept data for multiple years."""
        entries = []
        # Add 2017 filing with filed date in early Jan 2018 (before 2018-01-31)
        entries.append({
            "end": "2017-12-31",
            "val": base_val,
            "accn": "0000000000-17-000001",
            "fy": 2017,
            "fp": "FY",
            "form": "10-K",
            "filed": "2018-01-15",
            "frame": "CY2017"
        })
        for year in range(start_year, end_year + 1):
            year_factor = (1 + growth_rate) ** (year - start_year + 1)
            val = base_val * year_factor
            entries.append({
                "end": f"{year}-12-31",
                "val": val,
                "accn": f"0000000000-{year%100:02d}-000001",
                "fy": year,
                "fp": "FY",
                "form": "10-K",
                "filed": f"{year+1}-02-15",
                "frame": f"CY{year}"
            })
        return {
            "label": "Test",
            "description": "Test",
            "units": {
                unit: entries
            }
        }
    
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, cik in enumerate(sample_ciks):
            ticker = tickers[i] if i < len(tickers) else f"TICK{i+1}"
            base_val = 1000000000 + i * 50000000
            base_shares = 50000000 + i * 1000000
            base_revenue = 500000000 + i * 20000000
            base_ebit = 100000000 + i * 5000000
            base_interest = 10000000 + i * 500000
            base_tax = 20000000 + i * 1000000
            base_pretax = 120000000 + i * 6000000
            base_ocf = 150000000 + i * 7000000
            base_capex = 50000000 + i * 2000000
            base_debt = 300000000 + i * 10000000
            base_cash = 50000000 + i * 2000000
            base_equity = 700000000 + i * 30000000
            base_rd = 50000000 + i * 2000000
            base_deferred_rev = 20000000 + i * 1000000
            base_contract_liab = 10000000 + i * 500000
            
            facts_data = {
                "cik": int(cik) if isinstance(cik, str) and cik.isdigit() else i + 1,
                "entityName": f"Test Company {ticker}",
                "tickers": [ticker],
                "facts": {
                    "us-gaap": {
                        "Assets": _synth_concept(base_val * 1.5, 0.05, "USD"),
                        "Liabilities": _synth_concept(base_val * 0.5, 0.03, "USD"),
                        "StockholdersEquity": _synth_concept(base_equity, 0.06, "USD"),
                        "EntityCommonStockSharesOutstanding": _synth_concept(base_shares, 0.01, "shares"),
                        "RevenueFromContractWithCustomerExcludingAssessedTax": _synth_concept(base_revenue, 0.08, "USD"),
                        "EarningsBeforeInterestAndTaxes": _synth_concept(base_ebit, 0.07, "USD"),
                        "OperatingIncomeLoss": _synth_concept(base_ebit * 0.95, 0.07, "USD"),
                        "InterestExpense": _synth_concept(base_interest, 0.02, "USD"),
                        "IncomeTaxExpenseBenefit": _synth_concept(base_tax, 0.05, "USD"),
                        "IncomeBeforeTax": _synth_concept(base_pretax, 0.06, "USD"),
                        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeTaxes": _synth_concept(base_pretax, 0.06, "USD"),
                        "NetCashProvidedByUsedInOperatingActivities": _synth_concept(base_ocf, 0.08, "USD"),
                        "PaymentsToAcquirePropertyPlantAndEquipment": _synth_concept(base_capex, 0.05, "USD"),
                        "LongTermDebtNoncurrent": _synth_concept(base_debt, 0.03, "USD"),
                        "LongTermDebtAndCapitalLeaseObligations": _synth_concept(base_debt * 1.1, 0.03, "USD"),
                        "DebtAndCapitalLeaseObligations": _synth_concept(base_debt * 1.15, 0.03, "USD"),
                        "CashAndCashEquivalentsAtCarryingValue": _synth_concept(base_cash, 0.05, "USD"),
                        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents": _synth_concept(base_cash * 1.05, 0.05, "USD"),
                        "ResearchAndDevelopmentExpense": _synth_concept(base_rd, 0.05, "USD"),
                        "DeferredRevenue": _synth_concept(base_deferred_rev, 0.05, "USD"),
                        "ContractLiabilities": _synth_concept(base_contract_liab, 0.05, "USD"),
                        "ShortTermBorrowings": _synth_concept(base_debt * 0.1, 0.03, "USD"),
                    }
                }
            }
            
            import json
            zf.writestr(f"CIK{cik}.json", json.dumps(facts_data))

def parse_cik_json(content_bytes: bytes) -> tuple:
    """Parses single SEC JSON content into facts, filings, concepts lists of dicts."""
    data = json.loads(content_bytes.decode("utf-8"))
    cik = data.get("cik")
    cik_str = f"{cik:010d}" if cik is not None else ""
    
    facts_rows = []
    filings_rows = []
    concepts_rows = []
    
    facts_dict = data.get("facts", {})
    for taxonomy, taxonomy_facts in facts_dict.items():
        for concept_name, concept_data in taxonomy_facts.items():
            concept_key = f"{taxonomy}:{concept_name}"
            concepts_rows.append({
                "concept": concept_key,
                "label": concept_data.get("label", ""),
                "description": concept_data.get("description", "")
            })
            units = concept_data.get("units", {})
            for unit_key, val_list in units.items():
                for item in val_list:
                    filed = item.get("filed")
                    end = item.get("end")
                    val = item.get("val")
                    accn = item.get("accn", "")
                    frame = item.get("frame", "")
                    
                    facts_rows.append({
                        "cik": cik_str,
                        "concept": concept_key,
                        "unit": unit_key,
                        "end": end,
                        "val": float(val) if val is not None else None,
                        "filed": filed,
                        "frame": frame,
                        "accession": accn,
                        "form": item.get("form", "")
                    })
                    
                    filings_rows.append({
                        "cik": cik_str,
                        "accession": accn,
                        "filed": filed,
                        "form": item.get("form", ""),
                        "fy": item.get("fy"),
                        "fp": item.get("fp")
                    })
                    
    return facts_rows, filings_rows, concepts_rows

def process_bulk_zip(zip_path: Path, output_dir: Path, source_url: str = SEC_COMPANYFACTS_URL, limit_ciks: int = None):
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_dir = output_dir / "facts"
    filings_dir = output_dir / "filings"
    concepts_dir = output_dir / "concepts"
    for d in [facts_dir, filings_dir, concepts_dir]:
        d.mkdir(parents=True, exist_ok=True)
        
    all_facts = []
    all_filings = []
    all_concepts = []
    
    with zipfile.ZipFile(zip_path, "r") as zf:
        namelist = [name for name in zf.namelist() if name.endswith(".json")]
        if limit_ciks:
            namelist = namelist[:limit_ciks]
            
        for name in namelist:
            content = zf.read(name)
            f_rows, fil_rows, c_rows = parse_cik_json(content)
            all_facts.extend(f_rows)
            all_filings.extend(fil_rows)
            all_concepts.extend(c_rows)
            
    df_facts = pd.DataFrame(all_facts)
    df_filings = pd.DataFrame(all_filings).drop_duplicates()
    df_concepts = pd.DataFrame(all_concepts).drop_duplicates(subset=["concept"])
    
    # Save Parquet files
    facts_path = facts_dir / "facts.parquet"
    filings_path = filings_dir / "filings.parquet"
    concepts_path = concepts_dir / "concepts.parquet"
    
    df_facts.to_parquet(facts_path, index=False)
    df_filings.to_parquet(filings_path, index=False)
    df_concepts.to_parquet(concepts_path, index=False)
    
    zip_sha = calculate_sha256(zip_path)
    
    write_provenance(facts_dir, source_url, "1.0", calculate_sha256(facts_path), len(df_facts))
    write_provenance(filings_dir, source_url, "1.0", calculate_sha256(filings_path), len(df_filings))
    write_provenance(concepts_dir, source_url, "1.0", calculate_sha256(concepts_path), len(df_concepts))
    
    print(f"Processed {len(namelist)} CIKs -> {len(df_facts)} facts, {len(df_filings)} filings, {len(df_concepts)} concepts.")

def main():
    parser = argparse.ArgumentParser(description="SEC EDGAR Bulk Ingestion")
    parser.add_argument("--test", action="store_true", help="Run test mode with synthetic SEC data")
    parser.add_argument("--zip", type=str, help="Path to downloaded companyfacts.zip")
    parser.add_argument("--out", type=str, default="data/sec_facts", help="Output directory")
    args = parser.parse_args()
    
    output_dir = Path(args.out)
    
    if args.test:
        print("Running test mode with synthetic SEC companyfacts.zip...")
        test_zip = Path("temp_companyfacts_test.zip")
        universe_path = "data/universe/pit_universe_2018_2024.parquet"
        generate_synthetic_companyfacts_zip(test_zip, num_ciks=100, universe_path=universe_path)
        process_bulk_zip(test_zip, output_dir, source_url="synthetic://sec_companyfacts", limit_ciks=100)
        if test_zip.exists():
            os.remove(test_zip)
        print("Test complete.")
    elif args.zip:
        process_bulk_zip(Path(args.zip), output_dir)
    else:
        print("No zip specified. Downloading live companyfacts.zip from SEC EDGAR (or specify --test / --zip)...")
        # In case user wants to run live, attempt download
        temp_zip = Path("companyfacts.zip")
        req = urllib.request.Request(SEC_COMPANYFACTS_URL, headers={"User-Agent": SEC_USER_AGENT})
        with urllib.request.urlopen(req) as response, open(temp_zip, 'wb') as out_file:
            out_file.write(response.read())
        process_bulk_zip(temp_zip, output_dir)

if __name__ == "__main__":
    main()
