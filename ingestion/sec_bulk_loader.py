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

def generate_synthetic_companyfacts_zip(dest_zip: Path, num_ciks: int = 10):
    """Generates synthetic SEC companyfacts JSON files for testing."""
    sample_ciks = [f"{i+1:010d}" for i in range(num_ciks)]
    tickers = [f"TICK{i+1}" for i in range(num_ciks)]
    
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, cik in enumerate(sample_ciks):
            facts_data = {
                "cik": int(cik),
                "entityName": f"Test Company {i+1}",
                "facts": {
                    "us-gaap": {
                        "Assets": {
                            "label": "Assets",
                            "description": "Total Assets",
                            "units": {
                                "USD": [
                                    {
                                        "end": "2020-12-31",
                                        "val": 1000000000 + i * 50000000,
                                        "accn": f"0000000000-21-{i+1:06d}",
                                        "fy": 2020,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2021-02-15",
                                        "frame": "CY2020"
                                    },
                                    {
                                        "end": "2021-12-31",
                                        "val": 1200000000 + i * 50000000,
                                        "accn": f"0000000000-22-{i+1:06d}",
                                        "fy": 2021,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2022-02-16",
                                        "frame": "CY2021"
                                    }
                                ]
                            }
                        },
                        "EntityCommonStockSharesOutstanding": {
                            "label": "Shares Outstanding",
                            "units": {
                                "shares": [
                                    {
                                        "end": "2021-12-31",
                                        "val": 50000000 + i * 1000000,
                                        "accn": f"0000000000-22-{i+1:06d}",
                                        "fy": 2021,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2022-02-16"
                                    }
                                ]
                            }
                        }
                    }
                }
            }
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
                        "accession": accn
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
        generate_synthetic_companyfacts_zip(test_zip, num_ciks=10)
        process_bulk_zip(test_zip, output_dir, source_url="synthetic://sec_companyfacts", limit_ciks=10)
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
