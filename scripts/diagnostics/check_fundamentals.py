import polars as pl
df = pl.read_parquet('data/pit_fundamentals/valuation_fundamentals.parquet')
for ticker in ['AAPL', 'MSFT', 'GOOGL']:
    ticker_df = df.filter(pl.col('ticker') == ticker).sort('filed_date', descending=True)
    if len(ticker_df) > 0:
        row = ticker_df.row(0, named=True)
        print(f'{ticker}: roic={row.get("roic")}, icr={row.get("interest_coverage")}, fcf_yield={row.get("fcf_yield")}, wacc_estimate={row.get("wacc_estimate")}, fcf_margin={row.get("fcf_margin")}')