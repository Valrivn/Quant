import json
from backtesting.chi_square import run_standard_backtest, load_factors

try:
    factors = load_factors()
    res = run_standard_backtest('ig-llm', factors=factors)
    print("BACKTEST_SUCCESS")
    
    def safe_serialize(obj):
        if isinstance(obj, float):
            import math
            if math.isnan(obj): return None
            if math.isinf(obj): return "inf"
        return obj
    
    print(json.dumps(res, default=safe_serialize, indent=2))
except Exception as e:
    import traceback
    print("BACKTEST_FAILED")
    traceback.print_exc()
