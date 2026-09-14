import json
import sys
print("starting", flush=True)

try:
    from backtesting.chi_square import run_standard_backtest
    print("imported", flush=True)
    res = run_standard_backtest('rm-final', factors=None)
    print("finished backtest", flush=True)
    with open('bt_result.json', 'w') as f:
        json.dump(res, f, default=str, indent=2)
    print("SUCCESS", flush=True)
except Exception as e:
    import traceback
    traceback.print_exc(file=sys.stdout)
