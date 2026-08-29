import sqlite3
import pandas as pd
import numpy as np
from pathlib import Path
import os
import glob
from datetime import datetime

# Import school schedule trading calendar
from diversification.trading_calendar import is_trading_allowed, get_allowed_trading_dates

# Paths
DB_PATH = "reddit_quant.db"
DATA_DIR = Path("C:/Users/Hayden/Quant/data/nasdaq")

def load_data():
    print("Loading historical daily closes from local CSV cache...")
    prices_list = []
    for filepath in glob.glob(str(DATA_DIR / "*.csv")):
        ticker = Path(filepath).stem
        df = pd.read_csv(filepath, index_col=0, parse_dates=True)
        df = df.rename(columns={"close": ticker})
        prices_list.append(df)
        
    if not prices_list:
        raise RuntimeError("No local CSV files found in data/nasdaq")
        
    prices = pd.concat(prices_list, axis=1).sort_index().ffill().dropna()
    print(f"Loaded price data. Range: {prices.index[0].strftime('%Y-%m-%d')} to {prices.index[-1].strftime('%Y-%m-%d')}. Shape: {prices.shape}")
    print("Assets loaded:", list(prices.columns))
    
    # Load credit spreads from database
    conn = sqlite3.connect(DB_PATH)
    try:
        df_fred = pd.read_sql_query("SELECT date, value FROM risk_signals WHERE ticker = 'BAA10Y'", conn)
        df_fred['date'] = pd.to_datetime(df_fred['date'])
        df_fred = df_fred.set_index('date').sort_index()
        baa10y = df_fred['value']
        print(f"Loaded {len(baa10y)} BAA10Y credit spread observations from database.")
    except Exception as e:
        print("Using proxy for credit spreads from asset ratios:", e)
        baa10y = (prices["VCSH"] / prices["VCIT"]) * 200.0
        
    # Load mentions for IG (category = 'hype') and Reddit (category = 'retail_options')
    df_mentions = pd.read_sql_query(
        "SELECT ticker, date, category, mention_count FROM daily_aggregations", conn
    )
    df_mentions['date'] = pd.to_datetime(df_mentions['date'])
    print(f"Loaded {len(df_mentions)} daily mention observations across categories.")
    
    return prices, baa10y, df_mentions

def run_backtest_with_sigmoid(prices, baa10y, df_mentions, strategy_mode="sigmoid_risk_off"):
    """
    Simulates a chronological walk-forward portfolio.
    
    strategy_mode:
      - 'baseline': standard binary macro-state target weights
      - 'sigmoid_standard': smooth sigmoid interpolation between bull and bear macro weights
      - 'sigmoid_risk_off': smooth sigmoid rotation from equity to short-term bills/gold on stress acceleration
    """
    idx = prices.index
    # Only run from 2021-06-01 to 2026-06-30
    idx = idx[(idx >= '2021-06-01') & (idx <= '2026-06-30')]
    
    # Find allowed rebalance dates based on user's schedule constraints
    # Filter dates where school calendar permits trading
    allowed_idx = get_allowed_trading_dates(idx)
    
    # We want to rebalance roughly monthly, but only on dates when trading is allowed.
    # Group by year-month and select the first allowed trading date of each month.
    monthly_rebal_dates = []
    df_temp = pd.DataFrame(index=allowed_idx)
    df_temp['year'] = df_temp.index.year
    df_temp['month'] = df_temp.index.month
    for (y, m), group in df_temp.groupby(['year', 'month']):
        monthly_rebal_dates.append(group.index[0])
    
    # Pre-calculate credit spread derivatives
    spread_series = baa10y.reindex(idx).ffill()
    spread_series = spread_series.fillna(200.0)
    
    # First derivative (velocity) - 5-day difference
    spread_vel = spread_series.diff(5).fillna(0.0)
    # Second derivative (acceleration) - 5-day difference of velocity
    spread_accel = spread_vel.diff(5).fillna(0.0)
    
    portfolio_value = 100.0
    vpath = []
    dates = []
    
    assets = list(prices.columns)
    current_weights = {t: 1.0 / len(assets) for t in assets}
    last_prices = prices.loc[idx[0]].to_dict()
    
    for d in idx:
        # Rebalance check (restricted to allowed schedule dates)
        if d in monthly_rebal_dates or d == idx[0]:
            spread_val = float(spread_series.loc[d])
            vel_val = float(spread_vel.loc[d])
            accel_val = float(spread_accel.loc[d])
            
            # Effective stress incorporating acceleration (second derivative)
            # Scaling factor: if spreads are accelerating upwards, increase the effective stress metric
            accel_factor = 2.0
            x_eff = spread_val + accel_factor * max(0.0, accel_val)
            
            # Logistic Sigmoid function
            k = 0.05
            x_0 = 250.0
            w_defensive = 1.0 / (1.0 + np.exp(-k * (x_eff - x_0)))
            
            # 2. Select dynamic equity cohort from mentions (trailing 30 days)
            lookback_start = d - pd.Timedelta(days=30)
            recent_mentions = df_mentions[(df_mentions['date'] >= lookback_start) & (df_mentions['date'] < d)]
            
            # Separating IG (hype) and Reddit (retail_options) mentions
            ig_mentions = recent_mentions[recent_mentions['category'] == 'hype']
            reddit_mentions = recent_mentions[recent_mentions['category'] == 'retail_options']
            
            ig_sums = ig_mentions.groupby('ticker')['mention_count'].sum().to_dict()
            reddit_sums = reddit_mentions.groupby('ticker')['mention_count'].sum().to_dict()
            
            sorted_ig = [t[0] for t in sorted(ig_sums.items(), key=lambda x: x[1], reverse=True)[:3] if t[0] in assets]
            sorted_reddit = [t[0] for t in sorted(reddit_sums.items(), key=lambda x: x[1], reverse=True)[:3] if t[0] in assets]
            
            # Fallbacks
            if not sorted_ig:
                sorted_ig = ["SPY"]
            if not sorted_reddit:
                sorted_reddit = ["SPY"]
                
            # Set target sleeve weights based on strategy mode
            if strategy_mode == "baseline":
                if spread_val < 200.0:
                    target_sleeves = {"equity": 0.30, "bonds": 0.30, "bills": 0.25, "gold": 0.15}
                elif spread_val > 300.0:
                    target_sleeves = {"equity": 0.55, "bonds": 0.15, "bills": 0.20, "gold": 0.10}
                else:
                    target_sleeves = {"equity": 0.40, "bonds": 0.20, "bills": 0.20, "gold": 0.20}
            elif strategy_mode == "sigmoid_standard":
                bull_s = {"equity": 0.30, "bonds": 0.30, "bills": 0.25, "gold": 0.15}
                bear_s = {"equity": 0.55, "bonds": 0.15, "bills": 0.20, "gold": 0.10}
                target_sleeves = {}
                for key in bull_s.keys():
                    target_sleeves[key] = (1.0 - w_defensive) * bull_s[key] + w_defensive * bear_s[key]
            else: # sigmoid_risk_off
                # Defensive shift: higher stress rotates from equity to bills and gold
                bull_s = {"equity": 0.60, "bonds": 0.20, "bills": 0.10, "gold": 0.10}
                bear_s = {"equity": 0.20, "bonds": 0.10, "bills": 0.50, "gold": 0.20}
                target_sleeves = {}
                for key in bull_s.keys():
                    target_sleeves[key] = (1.0 - w_defensive) * bull_s[key] + w_defensive * bear_s[key]
            
            # Map sleeve targets to asset weights
            new_target = {t: 0.0 for t in assets}
            
            # Equity split: 70% weight to IG tickers, 30% weight to Reddit tickers
            eq_w = target_sleeves["equity"]
            
            # Allocate 70% of equity sleeve to IG candidates
            for s in sorted_ig:
                new_target[s] = new_target.get(s, 0.0) + (eq_w * 0.70 / len(sorted_ig))
                
            # Allocate 30% of equity sleeve to Reddit candidates
            for s in sorted_reddit:
                new_target[s] = new_target.get(s, 0.0) + (eq_w * 0.30 / len(sorted_reddit))
                
            # Bonds: VCSH, VCIT
            bond_w = target_sleeves["bonds"]
            if "VCSH" in assets:
                new_target["VCSH"] = bond_w * 0.5
            if "VCIT" in assets:
                new_target["VCIT"] = bond_w * 0.5
                
            # Bills: BIL, SHY, SGOV
            bills_w = target_sleeves["bills"]
            bills_assets = [b for b in ["BIL", "SHY", "SGOV"] if b in assets]
            if bills_assets:
                for b in bills_assets:
                    new_target[b] = bills_w / len(bills_assets)
                    
            # Gold: GLD, IAU
            gold_w = target_sleeves["gold"]
            gold_assets = [g for g in ["GLD", "IAU"] if g in assets]
            if gold_assets:
                for g in gold_assets:
                    new_target[g] = gold_w / len(gold_assets)
            
            # Normalize target weights to sum to 1.0
            tot_w = sum(new_target.values())
            current_weights = {t: w / tot_w for t, w in new_target.items()} if tot_w > 0 else current_weights
            last_prices = prices.loc[d].to_dict()
            
        # Daily return update
        day_prices = prices.loc[d].to_dict()
        day_ret = 0.0
        for t in assets:
            p_prev = last_prices.get(t, 0)
            p_curr = day_prices.get(t, 0)
            if p_prev > 0 and p_curr > 0:
                ret = (p_curr / p_prev) - 1.0
                day_ret += current_weights.get(t, 0.0) * ret
                
        portfolio_value *= (1.0 + day_ret)
        
        # Drift weights
        total_w = 0.0
        new_w = {}
        for t in assets:
            p_prev = last_prices.get(t, 0)
            p_curr = day_prices.get(t, 0)
            ret = (p_curr / p_prev) if p_prev > 0 else 1.0
            new_w[t] = current_weights.get(t, 0.0) * ret
            total_w += new_w[t]
            
        current_weights = {t: w / total_w for t, w in new_w.items()} if total_w > 0 else current_weights
        last_prices = day_prices
        
        vpath.append(portfolio_value)
        dates.append(d)
        
    res = pd.Series(vpath, index=dates)
    return res

def compute_metrics(series, label):
    rets = series.pct_change().dropna()
    total_ret = (series.iloc[-1] / series.iloc[0]) - 1.0
    vol = rets.std() * np.sqrt(252)
    sharpe = total_ret / vol if vol > 0 else 0.0
    dd = (series / series.cummax()) - 1.0
    max_dd = dd.min()
    return {
        "label": label,
        "return": total_ret,
        "volatility": vol,
        "sharpe": sharpe,
        "max_dd": max_dd
    }

def print_performance_table(metrics_list):
    print("\n" + "="*80)
    print(f"{'Strategy / Segment':40s} | {'Return':>10s} | {'Vol':>8s} | {'Sharpe':>6s} | {'MaxDD':>8s}")
    print("="*80)
    for m in metrics_list:
        print(f"{m['label']:40s} | {m['return']*100:8.2f}% | {m['volatility']*100:6.2f}% | {m['sharpe']:6.2f} | {m['max_dd']*100:7.2f}%")
    print("="*80)

def main():
    prices, baa10y, df_mentions = load_data()
    
    # Run backtests
    print("\nRunning chronological walk-forward simulations (under Grade 12 Trading Schedule Constraints)...")
    res_baseline = run_backtest_with_sigmoid(prices, baa10y, df_mentions, "baseline")
    res_sigmoid = run_backtest_with_sigmoid(prices, baa10y, df_mentions, "sigmoid_standard")
    res_riskoff = run_backtest_with_sigmoid(prices, baa10y, df_mentions, "sigmoid_risk_off")
    
    spy = prices["SPY"]
    
    # Slice dates
    # Bear market: 2022 calendar year
    bear_start, bear_end = '2022-01-03', '2022-12-30'
    # Bull market: 2024-01-02 to 2026-06-29
    bull_start, bull_end = '2024-01-02', '2026-06-29'
    
    # Overall Period (2021-06 to 2026-06)
    overall_start = prices.index[prices.index >= '2021-06-01'][0]
    overall_end = prices.index[prices.index <= '2026-06-30'][-1]
    
    # Metric lists
    overall_metrics = []
    bear_metrics = []
    bull_metrics = []
    
    # 1. Overall Metrics
    overall_metrics.append(compute_metrics(res_baseline.loc[overall_start:overall_end], "Baseline (Binary Macro Allocator)"))
    overall_metrics.append(compute_metrics(res_sigmoid.loc[overall_start:overall_end], "Sigmoid (Standard Smooth Allocator)"))
    overall_metrics.append(compute_metrics(res_riskoff.loc[overall_start:overall_end], "Sigmoid Risk-Off (70/30 IG/Reddit)"))
    overall_metrics.append(compute_metrics(spy.loc[overall_start:overall_end], "SPY Benchmark"))
    
    # 2. Bear Market Metrics (2022)
    bear_metrics.append(compute_metrics(res_baseline.loc[bear_start:bear_end], "Baseline (Binary Macro Allocator)"))
    bear_metrics.append(compute_metrics(res_sigmoid.loc[bear_start:bear_end], "Sigmoid (Standard Smooth Allocator)"))
    bear_metrics.append(compute_metrics(res_riskoff.loc[bear_start:bear_end], "Sigmoid Risk-Off (70/30 IG/Reddit)"))
    bear_metrics.append(compute_metrics(spy.loc[bear_start:bear_end], "SPY Benchmark"))
    
    # 3. Bull Market Metrics (2024-2026)
    bull_metrics.append(compute_metrics(res_baseline.loc[bull_start:bull_end], "Baseline (Binary Macro Allocator)"))
    bull_metrics.append(compute_metrics(res_sigmoid.loc[bull_start:bull_end], "Sigmoid (Standard Smooth Allocator)"))
    bull_metrics.append(compute_metrics(res_riskoff.loc[bull_start:bull_end], "Sigmoid Risk-Off (70/30 IG/Reddit)"))
    bull_metrics.append(compute_metrics(spy.loc[bull_start:bull_end], "SPY Benchmark"))
    
    print("\nOVERALL PORTFOLIO METRICS (2021-06 to 2026-06):")
    print_performance_table(overall_metrics)
    
    print("\n2022 BEAR MARKET STRESS-TEST METRICS:")
    print_performance_table(bear_metrics)
    
    print("\n2024-2026 BULL MARKET METRICS:")
    print_performance_table(bull_metrics)
    
    # 4. Overall individual Asset Class/Sleeve performance
    print("\n=== INDIVIDUAL COMPONENT CLASS RETURNS (2021-06 to 2026-06) ===")
    print("="*60)
    print(f"{'Sleeve Class':20s} | {'Asset Proxy':15s} | {'Total Return':>12s} | {'Max Drawdown':>12s}")
    print("="*60)
    
    for name, assets in [("Stocks", ["SPY"]), ("Bonds", ["VCSH", "VCIT"]), ("Gold", ["GLD", "IAU"]), ("Bills", ["BIL", "SHY", "SGOV"])]:
        for asset in assets:
            if asset in prices.columns:
                series = prices[asset].loc[overall_start:overall_end]
                ret = (series.iloc[-1] / series.iloc[0]) - 1.0
                dd = (series / series.cummax()) - 1.0
                print(f"{name:20s} | {asset:15s} | {ret*100:11.2f}% | {dd.min()*100:11.2f}%")
    print("="*60)

if __name__ == "__main__":
    main()
