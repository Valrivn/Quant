import json
from datetime import datetime
import os

try:
    with open('rm_final_result.json', 'r', encoding='utf-16') as f:
        res = json.load(f)
except Exception:
    with open('rm_final_result.json', 'r', encoding='utf-8') as f:
        res = json.load(f)

date_str = '20260820'
method = 'rm-final'
run_id = f'{date_str}-{method}'
artifact_path = f'.agents/project/org/backtests/{run_id}.md'
registry_path = '.agents/project/org/backtest-registry.md'

markdown = f"""# Backtest Artifact: {method} (Run {date_str})

**Run ID**: {run_id}
**Engine**: {res['engine']} ({res['strategy_label']})
**Audit Status**: {res['audit_status']}

## Data Gap Note
The rm-final engine's signal history starts in 2021 (the Reddit panel window) rather than 2018. The engine handles the pre-2021 gap by holding a neutral/baseline state or skipping active allocation until the valid panel data begins in 2021.

## Chi-Square Verdict (FULL Window)
- **Verdict**: {res['chi_square']['verdict']}
- **Method**: {res['chi_square']['method']}
- **Statistic**: {res['chi_square']['statistic']:.2f} (p = {res['chi_square']['p_value']:.4f})
- **Expected Table**: {res['chi_square']['expected']}

## Regimes
- **Bull (2023-24)**: {res['regimes']['bull']['win_rate']*100:.1f}% win rate ({res['regimes']['bull']['wins']}W / {res['regimes']['bull']['losses']}L), Return: {res['regime_returns']['bull:2023-01..2024-12']*100:.1f}%
- **Bear (2020, 2022)**: {res['regimes']['bear']['win_rate']*100:.1f}% win rate ({res['regimes']['bear']['wins']}W / {res['regimes']['bear']['losses']}L), Return: {res['regime_returns']['bear:2020-02..2020-03']*100:.1f}% (2020) / {res['regime_returns']['bear:2022-01..2022-12']*100:.1f}% (2022)

## Windows

### Window 1 (FULL)
- **Sharpe**: {res['rows'][0]['sharpe']:.3f} | **Sortino**: {res['rows'][0]['sortino']:.3f} | **Calmar**: {res['rows'][0]['calmar']:.3f}
- **Win Rate**: {res['rows'][0]['win_rate']*100:.1f}%
- **Alpha (FF5)**: {res['rows'][0]['alpha_annualized']:.3f} (95% CI: {res['rows'][0]['alpha_ci_lower']:.3f} to {res['rows'][0]['alpha_ci_upper']:.3f})
- **Excess vs SPY**: {res['rows'][0]['excess_sp500']:.3f}
- **Max Drawdown**: {res['rows'][0]['maxdd']*100:.1f}%
- **Fees**: ${res['rows'][0]['fees']:.2f} ({res['rows'][0]['fees_pct_of_gain']:.1f}% of gains)
- **Trades**: {res['rows'][0]['trades']}

### Window 2 (RECENT)
- **Sharpe**: {res['rows'][1]['sharpe']:.3f} | **Sortino**: {res['rows'][1]['sortino']:.3f} | **Calmar**: {res['rows'][1]['calmar']:.3f}
- **Win Rate**: {res['rows'][1]['win_rate']*100:.1f}%
- **Alpha (FF5)**: {res['rows'][1]['alpha_annualized']:.3f} (95% CI: {res['rows'][1]['alpha_ci_lower']:.3f} to {res['rows'][1]['alpha_ci_upper']:.3f})
- **Excess vs SPY**: {res['rows'][1]['excess_sp500']:.3f}
- **Max Drawdown**: {res['rows'][1]['maxdd']*100:.1f}%
- **Fees**: ${res['rows'][1]['fees']:.2f} ({res['rows'][1]['fees_pct_of_gain']:.1f}% of gains)
- **Trades**: {res['rows'][1]['trades']}
"""

os.makedirs(os.path.dirname(artifact_path), exist_ok=True)
with open(artifact_path, 'w') as f:
    f.write(markdown)

# Write to registry
registry_lines = []
for row in res['rows']:
    window = 'FULL' if 'FULL' in row['window'] else 'RECENT'
    
    alpha = f"{row['alpha_annualized']:.3f}" if row['alpha_annualized'] is not None else 'n/a'
    lower = f"{row['alpha_ci_lower']:.3f}" if row['alpha_ci_lower'] is not None else 'n/a'
    upper = f"{row['alpha_ci_upper']:.3f}" if row['alpha_ci_upper'] is not None else 'n/a'
    
    bull_ret = f"bull:2023-01..2024-12:{res['regime_returns']['bull:2023-01..2024-12']*100:.1f}%"
    bear_ret = f"bear:2020-02..2020-03:{res['regime_returns']['bear:2020-02..2020-03']*100:.1f}%/bear:2022-01..2022-12:{res['regime_returns']['bear:2022-01..2022-12']*100:.1f}%"
    
    r = f"| {run_id} | {window} | {method} | {row['end_value']:.2f} | {row['gain']:.2f} | {row['total_return']:.3f} | {row['ann_return']:.3f} | {row['ann_vol']:.3f} | {row['sharpe']:.3f} | {row['sortino']:.3f} | {row['calmar']:.3f} | {row['win_rate']:.3f} | {row['maxdd']:.3f} | {row['excess_sp500']:.3f} | {row['info_ratio']:.3f} | {alpha} | {lower} | {upper} | {row['fees']:.2f} | {row['fees_pct_of_gain']:.3f} | {row['trades']} | {res['chi_square']['verdict']} | {res['chi_square']['method']} | {bull_ret} | {bear_ret} | {res['audit_status']} | {artifact_path} |"
    registry_lines.append(r)

with open(registry_path, 'r') as f:
    content = f.read()

if run_id not in content:
    with open(registry_path, 'a') as f:
        f.write(f'\n## Run {run_id} (backtest-agent)\n\n')
        f.write('| run_id | window | method | end_value | gain | total_return | ann_return | ann_vol | sharpe | sortino | calmar | win_rate | maxdd | excess_sp500 | info_ratio | alpha_ff5 | alpha_ci_lower | alpha_ci_upper | fees | fees_pct_of_gain | trades | chi2_verdict | chi2_method | bull_return | bear_return | audit_status | artifact |\n')
        f.write('|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n')
        for r in registry_lines:
            f.write(r + '\n')
