# Fix real_backtest.py imports to use new v3 modules

with open('real_backtest.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Fix imports
content = content.replace(
    'from Quantitative.regime_shield_v3 import StickyHMMWithDwell',
    'from Quantitative.regime_shield_v3 import FactorHMMv3 as StickyHMMWithDwell'
)
content = content.replace(
    'from Quantitative.swap_barrier_v2 import BidirectionalSwapBarrier, SwapHolding, SwapCandidate, PortfolioStateMock',
    'from Quantitative.swap_barrier_v3 import SwapBarrierV3 as BidirectionalSwapBarrier'
)
content = content.replace(
    'from Quantitative.dcf_nowcaster import FCFMatrix, WACCEngine, DurationNowcaster',
    'from Quantitative.dcf_nowcaster import FCFMatrix, WACCEngine, DurationNowcaster'
)
content = content.replace(
    'from Quantitative.exit_scheduler import ExitScheduler',
    'from Quantitative.exit_scheduler import ExitScheduler'
)

# Also need to update the usage of SwapBarrierV3 API
# The old BidirectionalSwapBarrier had different API than SwapBarrierV3
# Let me check what the real_backtest.py does with it

with open('real_backtest.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('Updated real_backtest.py imports')