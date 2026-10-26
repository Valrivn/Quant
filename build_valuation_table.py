"""Build comprehensive PIT fundamental table with all derived valuation fields."""

import polars as pl
from pathlib import Path

# Load raw XBRL facts
df = pl.read_parquet('data/pit_fundamentals/raw_xbrl_facts.parquet')

# Parse dates
df = df.with_columns([
    pl.col('fiscal_end').str.to_date().alias('fiscal_end_date'),
    pl.col('filed').str.to_date().alias('filed_date'),
    pl.col('fiscal_end').str.to_date().dt.year().alias('fiscal_year'),
])

# Pivot to wide format: one row per ticker-filing (filed_date)
# Each filing may have multiple concepts - we need to coalesce by field
# For each field, take the value from the earliest filed date for that fiscal_end (PIT-safe)

# First, for each ticker, fiscal_end, field -> get the earliest filed value
pivoted = df.group_by(['ticker', 'fiscal_end_date', 'field']).agg([
    pl.col('value').first().alias('value'),  # first by filed_date since sorted
    pl.col('filed_date').first().alias('filed_date'),
    pl.col('accession').first().alias('accession'),
    pl.col('form').first().alias('form'),
    pl.col('fiscal_year').first().alias('fiscal_year'),
    pl.col('concept').first().alias('concept'),
]).sort(['ticker', 'fiscal_end_date', 'field'])

# Now pivot to wide: one row per ticker-filing
wide = pivoted.pivot(
    values='value',
    index=['ticker', 'fiscal_end_date', 'filed_date', 'accession', 'form', 'fiscal_year'],
    on='field',
    aggregate_function='first'
)

# Rename columns to clean names
wide = wide.rename({
    'revenue': 'revenue',
    'revenue_alt': 'revenue_alt',
    'operating_income': 'operating_income',
    'ebit': 'ebit',
    'interest_expense': 'interest_expense',
    'income_tax_expense': 'income_tax_expense',
    'net_income': 'net_income',
    'total_assets': 'total_assets',
    'total_liabilities': 'total_liabilities',
    'stockholders_equity': 'stockholders_equity',
    'cash': 'cash',
    'long_term_debt': 'long_term_debt',
    'total_debt': 'total_debt',
    'operating_cash_flow': 'operating_cash_flow',
    'capex': 'capex',
    'depreciation_amortization': 'depreciation_amortization',
    'rd_expense': 'rd_expense',
    'sbc_expense': 'sbc_expense',
    'shares_outstanding': 'shares_outstanding',
    'current_assets': 'current_assets',
    'current_liabilities': 'current_liabilities',
    'inventory': 'inventory',
    'receivables': 'receivables',
    'payables': 'payables',
})

# Coalesce revenue: use revenue if available, else revenue_alt
if 'revenue_alt' in wide.columns:
    wide = wide.with_columns([
        pl.coalesce([pl.col('revenue'), pl.col('revenue_alt')]).alias('revenue')
    ]).drop('revenue_alt')

# Coalesce EBIT: use ebit if available, else operating_income
if 'ebit' not in wide.columns and 'operating_income' in wide.columns:
    wide = wide.with_columns([
        pl.col('operating_income').alias('ebit')
    ])
elif 'ebit' in wide.columns and 'operating_income' in wide.columns:
    wide = wide.with_columns([
        pl.coalesce([pl.col('ebit'), pl.col('operating_income')]).alias('ebit')
    ])
elif 'ebit' not in wide.columns and 'operating_income' not in wide.columns:
    # Neither exists - create null column
    wide = wide.with_columns([pl.lit(None).cast(pl.Float64).alias('ebit')])

# Coalesce total_debt: use total_debt if available, else long_term_debt
if 'total_debt' not in wide.columns and 'long_term_debt' in wide.columns:
    wide = wide.with_columns([
        pl.col('long_term_debt').alias('total_debt')
    ])
elif 'total_debt' in wide.columns and 'long_term_debt' in wide.columns:
    wide = wide.with_columns([
        pl.coalesce([pl.col('total_debt'), pl.col('long_term_debt')]).alias('total_debt')
    ])
elif 'total_debt' not in wide.columns and 'long_term_debt' not in wide.columns:
    wide = wide.with_columns([pl.lit(None).cast(pl.Float64).alias('total_debt')])

# ============================================================
# DERIVED VALUATION FIELDS
# ============================================================

# 1. NOPAT = EBIT * (1 - tax_rate)
# tax_rate = income_tax_expense / pretax_income
# pretax_income = EBIT - interest_expense (approx) or use net_income + tax + interest
wide = wide.with_columns([
    # Pretax income approximation
    (pl.col('ebit') - pl.col('interest_expense')).alias('pretax_income'),
])

wide = wide.with_columns([
    # Tax rate
    (pl.col('income_tax_expense') / pl.col('pretax_income')).alias('tax_rate'),
])

# NOPAT
wide = wide.with_columns([
    (pl.col('ebit') * (1 - pl.col('tax_rate'))).alias('nopat'),
])

# 2. Invested Capital = Total Assets - Current Liabilities - Cash
# Or: Stockholders Equity + Total Debt - Cash
wide = wide.with_columns([
    (pl.col('stockholders_equity') + pl.col('total_debt') - pl.col('cash')).alias('invested_capital'),
    (pl.col('total_assets') - pl.col('current_liabilities') - pl.col('cash')).alias('invested_capital_alt'),
])

# Use invested_capital_alt if primary is null
wide = wide.with_columns([
    pl.coalesce([pl.col('invested_capital'), pl.col('invested_capital_alt')]).alias('invested_capital')
]).drop('invested_capital_alt')

# 3. ROIC = NOPAT / Invested Capital
wide = wide.with_columns([
    (pl.col('nopat') / pl.col('invested_capital')).alias('roic'),
])

# 4. Free Cash Flow = Operating Cash Flow - Capex
wide = wide.with_columns([
    (pl.col('operating_cash_flow') - pl.col('capex')).alias('fcf'),
])

# 5. FCF Yield = FCF / Market Cap (need price - will join later)
# 6. Revenue Growth (YoY)
wide = wide.with_columns([
    (pl.col('revenue') / pl.col('revenue').shift(1) - 1).over('ticker').alias('revenue_yoy'),
])

# 7. EBIT Growth (YoY)
wide = wide.with_columns([
    (pl.col('ebit') / pl.col('ebit').shift(1) - 1).over('ticker').alias('ebit_yoy'),
])

# 8. FCF Margin = FCF / Revenue
wide = wide.with_columns([
    (pl.col('fcf') / pl.col('revenue')).alias('fcf_margin'),
])

# 9. Operating Margin = EBIT / Revenue
wide = wide.with_columns([
    (pl.col('ebit') / pl.col('revenue')).alias('operating_margin'),
])

# 10. Debt/Equity
wide = wide.with_columns([
    (pl.col('total_debt') / pl.col('stockholders_equity')).alias('debt_to_equity'),
])

# 11. Interest Coverage = EBIT / Interest Expense
wide = wide.with_columns([
    (pl.col('ebit') / pl.col('interest_expense')).alias('interest_coverage'),
])

# 12. R&D / Revenue
wide = wide.with_columns([
    (pl.col('rd_expense') / pl.col('revenue')).alias('rd_to_revenue'),
])

# 13. SBC / Revenue
wide = wide.with_columns([
    (pl.col('sbc_expense') / pl.col('revenue')).alias('sbc_to_revenue'),
])

# 14. Capital Intensity = Capex / Revenue
wide = wide.with_columns([
    (pl.col('capex') / pl.col('revenue')).alias('capex_to_revenue'),
])

# 15. WACC Components
# Cost of Debt = Interest Expense / Total Debt
wide = wide.with_columns([
    (pl.col('interest_expense') / pl.col('total_debt')).alias('cost_of_debt'),
])

# Cost of Equity via CAPM (will need risk-free rate and beta - placeholder)
# For now, we'll compute a simplified WACC using cost_of_debt and assumed cost_of_equity
# WACC = (E/V) * Re + (D/V) * Rd * (1 - t)
wide = wide.with_columns([
    (pl.col('stockholders_equity') / (pl.col('stockholders_equity') + pl.col('total_debt'))).alias('weight_equity'),
    (pl.col('total_debt') / (pl.col('stockholders_equity') + pl.col('total_debt'))).alias('weight_debt'),
])

# 16. Reinvestment Rate = (Capex - Depreciation + R&D) / NOPAT
# Using: Reinvestment = Capex - Depreciation + Change in NWC + R&D
# Simplified: (Capex - Depreciation + R&D) / NOPAT
wide = wide.with_columns([
    ((pl.col('capex') - pl.col('depreciation_amortization') + pl.col('rd_expense')) / pl.col('nopat')).alias('reinvestment_rate'),
])

# 17. Expected Growth g = Reinvestment Rate * ROIC
wide = wide.with_columns([
    (pl.col('reinvestment_rate') * pl.col('roic')).alias('expected_growth'),
])

# 18. FCF / Invested Capital (Return on Invested Capital - cash flow version)
wide = wide.with_columns([
    (pl.col('fcf') / pl.col('invested_capital')).alias('fcf_roic'),
])

# 19. Net Debt = Total Debt - Cash
wide = wide.with_columns([
    (pl.col('total_debt') - pl.col('cash')).alias('net_debt'),
])

# 20. EV approximation = Market Cap + Net Debt (need market cap from prices)

# Drop intermediate columns
cols_to_drop = ['pretax_income', 'tax_rate', 'invested_capital_alt']
existing_to_drop = [c for c in cols_to_drop if c in wide.columns]
wide = wide.drop(existing_to_drop)

# Sort for PIT access: by ticker, filed_date
wide = wide.sort(['ticker', 'filed_date'])

print(f"Final table shape: {wide.shape}")
print(f"Columns: {wide.columns}")

# Save
out_path = Path('data/pit_fundamentals/valuation_fundamentals.parquet')
wide.write_parquet(out_path)
print(f"Saved to {out_path}")

# Coverage summary
print("\n=== DERIVED FIELD COVERAGE (tickers with non-null) ===")
derived_fields = ['nopat', 'invested_capital', 'roic', 'fcf', 'revenue_yoy', 'ebit_yoy',
                  'fcf_margin', 'operating_margin', 'debt_to_equity', 'interest_coverage',
                  'rd_to_revenue', 'sbc_to_revenue', 'capex_to_revenue', 'cost_of_debt',
                  'reinvestment_rate', 'expected_growth', 'fcf_roic', 'net_debt']

for field in derived_fields:
    if field in wide.columns:
        cnt = wide.filter(pl.col(field).is_not_null())['ticker'].n_unique()
        print(f"  {field}: {cnt} tickers")

# Tickers with 3+ years of ROIC
print("\n=== TICKERS WITH 3+ YEARS OF ROIC ===")
roic_df = wide.filter(pl.col('roic').is_not_null())
roic_years = roic_df.group_by('ticker').agg([
    pl.col('fiscal_year').n_unique().alias('num_years')
])
print(f"Tickers with 3+ years ROIC: {len(roic_years.filter(pl.col('num_years') >= 3))}")

# Tickers with 3+ years of FCF
fcf_df = wide.filter(pl.col('fcf').is_not_null())
fcf_years = fcf_df.group_by('ticker').agg([
    pl.col('fiscal_year').n_unique().alias('num_years')
])
print(f"Tickers with 3+ years FCF: {len(fcf_years.filter(pl.col('num_years') >= 3))}")