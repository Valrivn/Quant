import polars as pl

df = pl.read_parquet("data/pit_fundamentals/valuation_fundamentals.parquet")

# Check AAPL recent data
ticker_df = df.filter(pl.col('ticker') == 'AAPL').sort('filed_date', descending=True)
if len(ticker_df) > 0:
    row = ticker_df.row(0, named=True)
    # Check all fields we might use
    for key in ['roic_cap_rd', 'ebit', 'invested_capital', 'interest_coverage', 'interest_expense', 
                'fcf', 'wacc_estimate', 'fcf_margin', 'sector']:
        print(f'{key}: {row.get(key)}')

print()

# Check if roic_cap_rd is 0 or None for recent data
for ticker in ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'META', 'NVDA', 'TSLA', 'JPM', 'JNJ', 'V']:
    ticker_df = df.filter(pl.col('ticker') == ticker).sort('filed_date', descending=True)
    if len(ticker_df) > 0:
        row = ticker_df.row(0, named=True)
        roic = row.get('roic_cap_rd')
        fcf = row.get('fcf')
        print(f'{ticker}: roic_cap_rd={row.get("roic_cap_rd")}, fcf={row.get("fcf")}, wacc_estimate={row.get("wacc_estimate")}, fcf_margin={row.get("fcf_margin")}')