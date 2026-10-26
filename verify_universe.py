import polars as pl

facts = pl.read_parquet('data/pit_fundamentals/pit_fundamentals.parquet')
print(f'Total facts: {len(facts)}')
print(f'Unique tickers: {facts["ticker"].n_unique()}')

facts_years = facts.with_columns([
    pl.col('fiscal_end').str.to_date().dt.year().alias('fiscal_year')
]).filter(pl.col('fiscal_year').is_not_null())

ticker_years = facts_years.group_by('ticker').agg([
    pl.col('fiscal_year').n_unique().alias('num_years'),
    pl.col('fiscal_year').min().alias('min_year'),
    pl.col('fiscal_year').max().alias('max_year')
])

ticker_3plus = ticker_years.filter(pl.col('num_years') >= 3)
print(f'Tickers with 3+ fiscal years: {len(ticker_3plus)}')
print(f'Tickers with 2+ fiscal years: {len(ticker_years.filter(pl.col("num_years") >= 2))}')

print(f'Year range for 3+ tickers: {ticker_3plus["min_year"].min()} - {ticker_3plus["max_year"].max()}')

core_concepts = ['RevenueFromContractWithCustomerExcludingAssessedTax', 'OperatingIncomeLoss', 'EBIT', 'InterestExpense', 
                 'IncomeTaxExpenseBenefit', 'Assets', 'Liabilities', 'StockholdersEquity', 
                 'CashAndCashEquivalentsAtCarryingValue', 'LongTermDebtNoncurrent', 
                 'DebtAndCapitalLeaseObligations', 'NetCashProvidedByUsedInOperatingActivities', 
                 'PaymentsToAcquirePropertyPlantAndEquipment']

for concept in core_concepts:
    count = facts.filter(pl.col('concept') == concept)['ticker'].n_unique()
    print(f'{concept}: {count} tickers')