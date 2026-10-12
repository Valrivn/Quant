import pprint
import sys

try:
    from backtesting.chi_square import load_factors, run_standard_backtest
except ImportError as e:
    print('Import Error:', e)
    sys.exit(1)

try:
    factors = load_factors()
except Exception as e:
    print('Factors load failed:', e)
    factors = None

res = run_standard_backtest('dividend', factors=factors)
pprint.pprint(res)
