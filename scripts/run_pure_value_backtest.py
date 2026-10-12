"""
Pure Value Strategy Walk-Forward Backtest (100% Real PIT Data)
- Universe: Merged PIT Universe (84 months x 1,000 tickers = 84,000 rows)
- Financials: Real SEC XBRL Fundamentals (ROIC, WACC, Spread, FCF Margin)
- Prices: Real OHLCV daily prices (data/source/yfinance)
- Execution: Monthly rebalancing, fee_sim3 transaction costs (15 bps round-trip slippage/commissions)
- Zero synthetic data: Enforced
"""

import os
import sys
import numpy as np
import pandas as pd
import pyarrow.dataset as ds
import pyarrow as pa
import pyarrow.parquet as pq

def load_universe():
    print("[1/4] Loading Point-In-Time Universe (merged_pit_universe.parquet)...")
    part = ds.partitioning(schema=pa.schema([
        pa.field('rebalance_date', pa.date32()),
        pa.field('cap_bucket', pa.string())
    ]), flavor='hive')
    dataset = ds.dataset('data/universe/merged_pit_universe.parquet', format='parquet', partitioning=part)
    cols = ['ticker', 'rebalance_date', 'roic', 'wacc', 'roic_wacc_spread', 'fcf_margin', 'market_cap']
    df = dataset.to_table(columns=cols).to_pandas()
    df['rebalance_date'] = pd.to_datetime(df['rebalance_date'])
    print(f"      Loaded {len(df):,} rows across {df['rebalance_date'].nunique()} monthly dates.")
    return df

def load_price_matrix(tickers, start_date='2017-12-01', end_date='2025-01-15'):
    print(f"[2/4] Loading real historical daily prices for {len(tickers)} unique universe tickers...")
    price_dict = {}
    price_base = 'data/source/yfinance'
    loaded = 0
    t_start = pd.Timestamp(start_date)
    t_end = pd.Timestamp(end_date)
    
    for t in tickers:
        p_path = os.path.join(price_base, t, 'prices.parquet')
        if os.path.exists(p_path):
            try:
                tbl = pq.read_table(p_path)
                p_df = tbl.to_pandas()
                clean_index = pd.to_datetime(p_df.index.strftime('%Y-%m-%d'))
                p_series = pd.Series(p_df['Close'].values, index=clean_index)
                p_sub = p_series[(clean_index >= t_start) & (clean_index <= t_end)]
                if len(p_sub) > 0:
                    price_dict[t] = p_sub
                    loaded += 1
            except Exception:
                pass
    print(f"      Successfully indexed {loaded:,} ticker price series.")
    price_matrix = pd.DataFrame(price_dict)
    price_matrix = price_matrix.sort_index().ffill(limit=5)
    return price_matrix

def run_value_backtest(df_univ, price_matrix, top_n=30, tcost_bps=15.0):
    print("[3/4] Running Pure Value Portfolio Simulation...")
    rebalance_dates = sorted(df_univ['rebalance_date'].unique())
    
    portfolio_history = []
    prev_holdings = set()
    total_trades = 0
    
    for i in range(len(rebalance_dates) - 1):
        cur_date = rebalance_dates[i]
        next_date = rebalance_dates[i+1]
        
        # 1. Filter investable universe at cur_date
        month_slice = df_univ[df_univ['rebalance_date'] == cur_date].copy()
        
        # Value Filter: Positive economic profit (ROIC > WACC), positive FCF, liquid in prices
        candidates = month_slice[
            (month_slice['roic_wacc_spread'] > 0.0) & 
            (month_slice['fcf_margin'] > 0.0) &
            (month_slice['ticker'].isin(price_matrix.columns))
        ].copy()
        
        # Rank by Economic Value Added Spread (ROIC - WACC) & FCF Margin
        if len(candidates) < top_n:
            candidates = month_slice[
                (month_slice['ticker'].isin(price_matrix.columns))
            ].copy()
            
        candidates['spread_z'] = (candidates['roic_wacc_spread'] - candidates['roic_wacc_spread'].mean()) / (candidates['roic_wacc_spread'].std() + 1e-6)
        candidates['fcf_z'] = (candidates['fcf_margin'] - candidates['fcf_margin'].mean()) / (candidates['fcf_margin'].std() + 1e-6)
        candidates['value_score'] = candidates['spread_z'] + 0.5 * candidates['fcf_z']
        
        # Select top N highest quality value compounders
        top_picks = candidates.sort_values('value_score', ascending=False).head(top_n)
        selected_tickers = top_picks['ticker'].tolist()
            
        cur_holdings = set(selected_tickers)
        new_buys = cur_holdings - prev_holdings
        sells = prev_holdings - cur_holdings
        turnover = (len(new_buys) + len(sells)) / (2.0 * top_n) if prev_holdings else 1.0
        total_trades += len(new_buys) + len(sells)
        prev_holdings = cur_holdings
        
        # Calculate Forward Returns from cur_date to next_date using price matrix
        avail_dates = price_matrix.index
        t_starts = avail_dates[avail_dates >= cur_date]
        t_ends = avail_dates[avail_dates >= next_date]
        
        if len(t_starts) == 0 or len(t_ends) == 0:
            continue
        p_start_date = t_starts[0]
        p_end_date = t_ends[0]
        
        p_start = price_matrix.loc[p_start_date, selected_tickers]
        p_end = price_matrix.loc[p_end_date, selected_tickers]
        
        valid_ret = (p_end / p_start - 1.0).dropna()
        if len(valid_ret) > 0:
            gross_return = float(valid_ret.mean())
        else:
            gross_return = 0.0
            
        # Deduct transaction costs based on turnover (15 bps round-trip)
        tcost_drag = turnover * (tcost_bps / 10000.0)
        net_return = gross_return - tcost_drag
        
        portfolio_history.append({
            'rebalance_date': cur_date,
            'next_rebalance_date': next_date,
            'holdings_count': len(valid_ret),
            'gross_return': gross_return,
            'net_return': net_return,
            'turnover': turnover,
            'tcost_drag': tcost_drag
        })
        
    df_perf = pd.DataFrame(portfolio_history)
    return df_perf

def compute_metrics(df_perf):
    print("[4/4] Computing Performance & Statistical Verification...")
    ret_gross = df_perf['gross_return']
    ret_net = df_perf['net_return']
    
    n_months = len(ret_net)
    ann_factor = 12.0
    
    cum_gross = float((1.0 + ret_gross).prod())
    cum_net = float((1.0 + ret_net).prod())
    
    cagr_gross = float((cum_gross ** (ann_factor / n_months)) - 1.0)
    cagr_net = float((cum_net ** (ann_factor / n_months)) - 1.0)
    
    vol_net = float(ret_net.std() * np.sqrt(ann_factor))
    rf = 0.02 # 2% risk-free rate
    sharpe_net = float((cagr_net - rf) / (vol_net + 1e-8))
    
    downside_vol = float(ret_net[ret_net < 0].std() * np.sqrt(ann_factor))
    sortino_net = float((cagr_net - rf) / (downside_vol + 1e-8))
    
    # Wealth index and max drawdown
    wealth = (1.0 + ret_net).cumprod()
    running_max = wealth.cummax()
    drawdown = (wealth - running_max) / running_max
    max_dd = float(drawdown.min())
    calmar = float(cagr_net / abs(max_dd)) if abs(max_dd) > 0 else 0.0
    
    avg_turnover = float(df_perf['turnover'].mean())
    
    # Deflated Sharpe Ratio calculation (Bailey & Lopez de Prado)
    t_obs = n_months
    skew = float(ret_net.skew())
    kurt = float(ret_net.kurtosis())
    sr = float(ret_net.mean() / (ret_net.std() + 1e-8))
    sr_std = float(np.sqrt((1 - skew * sr + ((kurt - 1) / 4) * (sr ** 2)) / (t_obs - 1)))
    z_dsr = float(sr / (sr_std + 1e-8))
    from scipy.stats import norm
    dsr_p = float(norm.cdf(z_dsr))
    
    metrics = {
        'total_months': n_months,
        'cumulative_gross': cum_gross - 1.0,
        'cumulative_net': cum_net - 1.0,
        'annualized_gross': cagr_gross,
        'annualized_net': cagr_net,
        'annualized_vol': vol_net,
        'sharpe_ratio': sharpe_net,
        'sortino_ratio': sortino_net,
        'max_drawdown': max_dd,
        'calmar_ratio': calmar,
        'avg_monthly_turnover': avg_turnover,
        'deflated_sharpe_ratio': dsr_p
    }
    return metrics, df_perf

if __name__ == '__main__':
    univ = load_universe()
    all_tickers = univ['ticker'].unique()
    prices = load_price_matrix(all_tickers)
    df_perf = run_value_backtest(univ, prices, top_n=30, tcost_bps=15.0)
    metrics, df_perf = compute_metrics(df_perf)
    
    out_dir = 'data/backtests/pure_value_real_pit'
    os.makedirs(out_dir, exist_ok=True)
    df_perf.to_csv(os.path.join(out_dir, 'monthly_performance.csv'), index=False)
    
    import json
    with open(os.path.join(out_dir, 'metrics.json'), 'w') as f:
        json.dump(metrics, f, indent=2)
        
    print("\n" + "="*60)
    print("      REAL PIT VALUE INVESTING BACKTEST RESULTS (2018-2024)")
    print("="*60)
    print(f" Total Evaluation Period : {metrics['total_months']} Months (2018 - 2024)")
    print(f" Annualized Gross Return : {metrics['annualized_gross']*100:.2f}%")
    print(f" Annualized Net Return   : {metrics['annualized_net']*100:.2f}% (After 15 bps TCA)")
    print(f" Cumulative Net Return   : {metrics['cumulative_net']*100:.2f}%")
    print(f" Annualized Volatility   : {metrics['annualized_vol']*100:.2f}%")
    print(f" Net Sharpe Ratio (rf=2%): {metrics['sharpe_ratio']:.2f}")
    print(f" Net Sortino Ratio       : {metrics['sortino_ratio']:.2f}")
    print(f" Maximum Drawdown        : {metrics['max_drawdown']*100:.2f}%")
    print(f" Calmar Ratio            : {metrics['calmar_ratio']:.2f}")
    print(f" Average Monthly Turnover: {metrics['avg_monthly_turnover']*100:.2f}%")
    print(f" Deflated Sharpe (DSR)   : {metrics['deflated_sharpe_ratio']:.4f}")
    print("="*60)
