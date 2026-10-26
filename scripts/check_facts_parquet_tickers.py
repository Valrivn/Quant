import sqlite3
import pandas as pd
import numpy as np
import os

df_facts = pd.read_parquet("data/sec_facts/facts/facts.parquet")
df_filings = pd.read_parquet("data/sec_facts/filings/filings.parquet")
df_concepts = pd.read_parquet("data/sec_facts/concepts/concepts.parquet")

conn = sqlite3.connect("reddit_quant.db")
sec_cik_map = pd.read_sql_query("SELECT * FROM sec_cik_map", conn)
conn.close()

df_facts['cik_padded'] = df_facts['cik'].astype(str).str.zfill(10)
sec_cik_map['cik_padded'] = sec_cik_map['cik'].astype(str).str.zfill(10)

facts_with_ticker = df_facts.merge(sec_cik_map[['cik_padded', 'ticker']], on='cik_padded', how='left')

print("=== FACTS.PARQUET METRICS ===")
print("Total rows:", len(df_facts))
print("Unique CIKs:", df_facts['cik_padded'].nunique())
unique_tickers = facts_with_ticker['ticker'].dropna().nunique()
print("Unique mapped tickers:", unique_tickers)

# Check number of filings per CIK/ticker
filing_counts = df_filings.groupby('cik')['accession'].nunique()
print("\nFilings per CIK (distribution):")
print(filing_counts.describe())

# Check filing years per CIK/ticker
df_facts['year'] = df_facts['frame'].str.extract(r'CY(\d{4})')
print("\nFiling years present in facts.parquet:")
print(df_facts['year'].value_counts().sort_index())

# Check continuous years per ticker/CIK
cik_years = df_facts.groupby('cik_padded')['year'].nunique()
print("\nUnique calendar years per CIK:")
print(cik_years.value_counts().sort_index())

ciks_with_3plus_years = cik_years[cik_years >= 3].index
print(f"CIKs with >= 3 years of 10-K data: {len(ciks_with_3plus_years)}")

tickers_3plus = facts_with_ticker[facts_with_ticker['cik_padded'].isin(ciks_with_3plus_years)]['ticker'].dropna().nunique()
print(f"Mapped tickers with >= 3 years of 10-K data: {tickers_3plus}")

# Check Statement Completeness for these 100 CIKs
print("\n=== STATEMENT COMPLETENESS FOR THE 100 CIKS ===")
# Income statement concepts: Revenue, OperatingIncome/EBIT, NetIncome (or EBT), Tax, Interest
income_concepts = [
    'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax',
    'us-gaap:OperatingIncomeLoss',
    'us-gaap:EarningsBeforeInterestAndTaxes',
    'us-gaap:InterestExpense',
    'us-gaap:IncomeTaxExpenseBenefit'
]

# Balance sheet concepts: Assets, Liabilities, StockholdersEquity, Cash, Debt
bs_concepts = [
    'us-gaap:Assets',
    'us-gaap:Liabilities',
    'us-gaap:StockholdersEquity',
    'us-gaap:CashAndCashEquivalentsAtCarryingValue',
    'us-gaap:LongTermDebtNoncurrent',
    'us-gaap:DebtAndCapitalLeaseObligations'
]

# Cash flow concepts: NetCashProvidedByUsedInOperatingActivities, PaymentsToAcquirePropertyPlantAndEquipment (CapEx)
cf_concepts = [
    'us-gaap:NetCashProvidedByUsedInOperatingActivities',
    'us-gaap:PaymentsToAcquirePropertyPlantAndEquipment'
]

all_concepts_in_facts = df_facts['concept'].unique()
print("All 22 concepts present in facts.parquet:")
for c in sorted(all_concepts_in_facts):
    print(" -", c)

print("\nCompleteness check across concepts in facts.parquet:")
for name, c_list in [("Income Statement", income_concepts), ("Balance Sheet", bs_concepts), ("Cash Flow Statement", cf_concepts)]:
    present = [c for c in c_list if c in all_concepts_in_facts]
    print(f"  {name}: {len(present)} / {len(c_list)} key concepts present in dataset ({len(present)/len(c_list)*100:.1f}%)")

