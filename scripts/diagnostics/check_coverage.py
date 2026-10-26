import polars as pl
import pandas as pd

df = pl.read_parquet('data/pit_fundamentals/raw_xbrl_facts.parquet')

# Parse fiscal year
df = df.with_columns([
    pl.col('fiscal_end').str.to_date().dt.year().alias('fiscal_year')
]).filter(pl.col('fiscal_year').is_not_null())

# Count unique fiscal years per ticker (any field)
ticker_years = df.group_by('ticker').agg([
    pl.col('fiscal_year').n_unique().alias('num_years'),
    pl.col('fiscal_year').min().alias('min_year'),
    pl.col('fiscal_year').max().alias('max_year'),
])

print(f'Total tickers in raw facts: {len(ticker_years)}')
print(f'Tickers with 3+ fiscal years (any data): {len(ticker_years.filter(pl.col("num_years") >= 3))}')
print(f'Tickers with 2+ fiscal years (any data): {len(ticker_years.filter(pl.col("num_years") >= 2))}')

# Check year range
t3 = ticker_years.filter(pl.col('num_years') >= 3)
print(f'Year range for 3+ tickers: {t3["min_year"].min()} - {t3["max_year"].max()}')

# Check how many tickers in monthly reconstitution but NOT in raw facts
univ = pd.read_parquet('data/universe/monthly_reconstitution.parquet')
univ_tickers = set(univ['ticker'].dropna().unique())
raw_tickers = set(ticker_years['ticker'].to_list())
missing = univ_tickers - raw_tickers
print(f'\nUniverse tickers: {len(univ_tickers)}')
print(f'Raw facts tickers: {len(raw_tickers)}')
print(f'Missing from raw facts: {len(missing)}')
print(f'Missing sample: {sorted(list(missing))[:20]}')