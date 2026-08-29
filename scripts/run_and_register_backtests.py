#!/usr/bin/env python3
import os
import sys
import datetime
import numpy as np
import pandas as pd
from pathlib import Path

# Add project root to paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backtesting.chi_square import run_standard_backtest, load_factors

def format_val(v, fmt="{:.3f}"):
    if v is None:
        return "n/a"
    try:
        # handle np.float64, etc.
        val = float(v)
        if np.isnan(val):
            return "n/a"
        return fmt.format(val)
    except Exception:
        return str(v)

def run_method(method):
    print(f"[*] Running backtest for method: {method}...")
    
    # Try loading factors for FF5 alpha
    factors = None
    try:
        factors = load_factors()
    except Exception as e:
        print(f"[!] Warning: factors could not be loaded: {e}")
        
    res = run_standard_backtest(method, factors=factors)
    
    date_str = datetime.datetime.now().strftime("%Y%m%d")
    artifact_filename = f"{date_str}-{method}.md"
    artifact_rel_path = f".agents/project/org/backtests/{artifact_filename}"
    artifact_abs_path = PROJECT_ROOT / artifact_rel_path
    
    # Generate Artifact Markdown Content
    lines = [
        f"# Backtest Artifact: {method}",
        "",
        f"**Date Run:** {datetime.date.today().isoformat()}",
        f"**Engine:** {res['engine']}",
        f"**Strategy Label:** {res['strategy_label']}",
        f"**Initial Capital:** ${res['initial']}",
        f"**Audit Status:** {res['audit_status']}",
        "",
        "## Chi-Square Verdict (FULL Window)",
        f"* **Method:** {res['chi_square']['method']}",
        f"* **Statistic:** {res['chi_square']['statistic']:.4f}",
        f"* **p-value:** {res['chi_square']['p_value']:.4f}",
        f"* **Verdict:** {res['chi_square']['verdict']}",
        f"* **Alpha:** {res['chi_square']['alpha']}",
        "",
        "## Regimes"
    ]
    
    for rname, rmeta in res['regimes'].items():
        win_pct = rmeta['win_rate'] * 100
        lines.append(f"* **{rname.capitalize()}:** {rmeta['wins']} wins, {rmeta['losses']} losses ({win_pct:.1f}% win rate)")
        
    lines.extend([
        "",
        "## Returns by Regime"
    ])
    
    for rperiod, rret in res['regime_returns'].items():
        lines.append(f"* **{rperiod}:** {rret * 100:.2f}%")
        
    for row in res['rows']:
        wname = row['window'].split(":")[0]
        wrange = row['window'].split(":")[1]
        lines.extend([
            "",
            f"## Window {1 if wname == 'FULL' else 2} ({wname}:{wrange})",
            f"* **Sharpe:** {format_val(row['sharpe'])}",
            f"* **Sortino:** {format_val(row['sortino'])}",
            f"* **Calmar:** {format_val(row['calmar'])}",
            f"* **Win Rate:** {row['win_rate']*100:.2f}%",
            f"* **Max Drawdown:** {row['maxdd']*100:.2f}%",
            f"* **Excess vs SPY:** {row['excess_sp500']*100:.2f}%",
        ])
        if row.get('alpha_annualized') is not None:
            lines.append(f"* **Alpha (Ann):** {row['alpha_annualized']*100:.2f}% (CI: {row['alpha_ci_lower']*100:.2f}% - {row['alpha_ci_upper']*100:.2f}%)")
        else:
            lines.append(f"* **Alpha (Ann):** n/a")
        lines.extend([
            f"* **Fees Paid:** ${row['fees']:.2f} ({row['fees_pct_of_gain']*100:.2f}% of gains)" if row['gain'] > 0 else f"* **Fees Paid:** ${row['fees']:.2f} (n/a)",
            f"* **Trades:** {row['trades']}"
        ])
        
    os.makedirs(artifact_abs_path.parent, exist_ok=True)
    with open(artifact_abs_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[+] Saved backtest artifact to {artifact_rel_path}")
    
    # Append to Registry
    registry_path = PROJECT_ROOT / ".agents/project/org/backtest-registry.md"
    
    # Read registry
    with open(registry_path, "r") as f:
        reg_content = f.read()
        
    # Check if section header exists for today's run
    section_header = f"## Run {date_str}-{method} (backtest-agent)"
    
    registry_rows = []
    for row in res['rows']:
        wname = row['window'].split(":")[0]
        # map row values to registry schema
        # schema: run_id | window | method | end_value | gain | total_return | ann_return | ann_vol | sharpe | sortino | calmar | win_rate | maxdd | excess_sp500 | info_ratio | alpha_ff5 | alpha_ci_lower | alpha_ci_upper | fees | fees_pct_of_gain | trades | chi2_verdict | chi2_method | bull_return | bear_return | audit_status | artifact
        
        # regimes mapping
        bull_ret_str = "/".join(f"{k}:{v*100:.1f}%" for k, v in res['regime_returns'].items() if "bull" in k)
        bear_ret_str = "/".join(f"{k}:{v*100:.1f}%" for k, v in res['regime_returns'].items() if "bear" in k)
        
        reg_row = f"| {date_str}-{method} | {wname} | {method} | {format_val(row['end_value'], '{:.2f}')} | {format_val(row['gain'], '{:.2f}')} | {format_val(row['total_return'], '{:.3f}')} | {format_val(row['ann_return'], '{:.3f}')} | {format_val(row['ann_vol'], '{:.3f}')} | {format_val(row['sharpe'], '{:.3f}')} | {format_val(row['sortino'], '{:.3f}')} | {format_val(row['calmar'], '{:.3f}')} | {format_val(row['win_rate'], '{:.3f}')} | {format_val(row['maxdd'], '{:.3f}')} | {format_val(row['excess_sp500'], '{:.3f}')} | {format_val(row['info_ratio'], '{:.3f}')} | {format_val(row.get('alpha_annualized'), '{:.3f}')} | {format_val(row.get('alpha_ci_lower'), '{:.3f}')} | {format_val(row.get('alpha_ci_upper'), '{:.3f}')} | {format_val(row['fees'], '{:.2f}')} | {format_val(row['fees_pct_of_gain'], '{:.3f}')} | {row['trades']} | {res['chi_square']['verdict']} | {res['chi_square']['method']} | {bull_ret_str} | {bear_ret_str} | {res['audit_status']} | {artifact_rel_path} |"
        registry_rows.append(reg_row)
        
    append_text = f"\n\n{section_header}\n\n| run_id | window | method | end_value | gain | total_return | ann_return | ann_vol | sharpe | sortino | calmar | win_rate | maxdd | excess_sp500 | info_ratio | alpha_ff5 | alpha_ci_lower | alpha_ci_upper | fees | fees_pct_of_gain | trades | chi2_verdict | chi2_method | bull_return | bear_return | audit_status | artifact |\n|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n" + "\n".join(registry_rows)
    
    with open(registry_path, "a") as f:
        f.write(append_text)
    print(f"[+] Appended backtest results for {method} to registry.")

def main():
    methods = ["ig-llm"]
    for m in methods:
        try:
            run_method(m)
        except Exception as e:
            print(f"[!] Error running backtest for {m}: {e}")
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    main()
