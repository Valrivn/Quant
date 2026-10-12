import sys
import os
sys.path.insert(0, os.getcwd())
import json
from backtesting.chi_square import run_standard_backtest, load_factors

try:
    factors = load_factors()
except Exception as e:
    factors = None
    print(f'Could not load factors: {e}')

result = run_standard_backtest('minvar', factors=factors)
print(json.dumps(result, indent=2, default=str))
