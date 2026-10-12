import json
import datetime
from backtesting.chi_square import run_standard_backtest, load_factors

try:
    factors = load_factors()
except Exception:
    factors = None

res = run_standard_backtest('spy', factors=factors)

date_str = '20260821'
run_id = f'{date_str}-spy'
artifact_path = f'.agents/project/org/backtests/{run_id}.md'

bull_returns = []
bear_returns = []
for k, v in res.get('regime_returns', {}).items():
    if k.startswith('bull'):
        bull_returns.append(f'{k}:{v*100:.1f}%')
    elif k.startswith('bear'):
        bear_returns.append(f'{k}:{v*100:.1f}%')

bull_str = '/'.join(bull_returns)
bear_str = '/'.join(bear_returns)

md = f'# Backtest Run: {run_id}\n\n'
md += f'- **Method**: spy\n'
md += f'- **Strategy**: {res["strategy_label"]}\n'
md += f'- **Audit Status**: {res["audit_status"]}\n'
md += f'- **Initial Capital**: {res["initial"]}\n'
if "chi_square" in res:
    chi = res["chi_square"]
    md += f'- **Chi-Square Verdict**: {chi["verdict"]} (stat={chi["statistic"]:.3f}, p={chi["p_value"]:.4f}, method={chi["method"]})\n'
md += f'\n## Results\n\n'
md += '| window | end_value | gain | total_return | ann_return | ann_vol | sharpe | sortino | calmar | win_rate | maxdd | excess_sp500 | info_ratio | alpha_ff5 | fees | trades |\n'
md += '|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n'

registry_lines = []
registry_lines.append(f'## Run {run_id} (backtest-agent)\n')
registry_lines.append('| run_id | window | method | end_value | gain | total_return | ann_return | ann_vol | sharpe | sortino | calmar | win_rate | maxdd | excess_sp500 | info_ratio | alpha_ff5 | alpha_ci_lower | alpha_ci_upper | fees | fees_pct_of_gain | trades | chi2_verdict | chi2_method | bull_return | bear_return | audit_status | artifact |')
registry_lines.append('|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|')

for row in res['rows']:
    window_short = row['window'].split(':')[0]
    
    alpha = row['alpha_annualized']
    ci_l = row['alpha_ci_lower']
    ci_u = row['alpha_ci_upper']
    
    alpha_str = f'{alpha:.3f}' if alpha is not None else 'n/a'
    cil_str = f'{ci_l:.3f}' if ci_l is not None else 'n/a'
    ciu_str = f'{ci_u:.3f}' if ci_u is not None else 'n/a'
    
    md += f'| {window_short} | {row["end_value"]:.2f} | {row["gain"]:.2f} | {row["total_return"]:.3f} | {row["ann_return"]:.3f} | {row["ann_vol"]:.3f} | {row["sharpe"]:.3f} | {row["sortino"]:.3f} | {row["calmar"]:.3f} | {row["win_rate"]:.3f} | {row["maxdd"]:.3f} | {row["excess_sp500"]:.3f} | {row["info_ratio"]:.3f} | {alpha_str} | {row["fees"]:.2f} | {row["trades"]} |\n'
    
    cv = res['chi_square']['verdict'] if 'chi_square' in res else 'n/a'
    cm = res['chi_square']['method'] if 'chi_square' in res else 'n/a'
    
    reg_row = f'| {run_id} | {window_short} | spy | {row["end_value"]:.2f} | {row["gain"]:.2f} | {row["total_return"]:.3f} | {row["ann_return"]:.3f} | {row["ann_vol"]:.3f} | {row["sharpe"]:.3f} | {row["sortino"]:.3f} | {row["calmar"]:.3f} | {row["win_rate"]:.3f} | {row["maxdd"]:.3f} | {row["excess_sp500"]:.3f} | {row["info_ratio"]:.3f} | {alpha_str} | {cil_str} | {ciu_str} | {row["fees"]:.2f} | {row["fees_pct_of_gain"]:.3f} | {row["trades"]} | {cv} | {cm} | {bull_str} | {bear_str} | {res["audit_status"]} | {artifact_path} |'
    registry_lines.append(reg_row)

import os
with open(artifact_path, 'w') as f:
    f.write(md)

with open('.agents/project/org/backtest-registry.md', 'a') as f:
    f.write('\n' + '\n'.join(registry_lines) + '\n')

print('Success')
