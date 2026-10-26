import polars as pl

df = pl.read_parquet("data/pit_fundamentals/valuation_fundamentals.parquet")

# Check a recent AAPL filing
ticker_df = df.filter(pl.col('ticker') == 'AAPL').sort('filed_date', descending=True)
row = ticker_df.row(0, named=True)

# Print all keys and values
for k, v in sorted(row.items()):
    print(f'{k}: {v} ({type(v).__name__})')