import os
import json
import hashlib
import time
from datetime import datetime
import numpy as np
import pandas as pd
from research.lifecycle_sieve import (
    compute_growth, compute_margin_vol, compute_payout, compute_reinvestment,
    classify_category, compute_icr, compute_tax_rate, compute_de, compute_phi
)

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--fundamentals', required=True)
    parser.add_argument('--prices', required=True)
    parser.add_argument('--universe', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()

    print(f"Loading fundamentals from {args.fundamentals}...")
    fund_df = pd.read_parquet(args.fundamentals)
    print(f"Loading prices from {args.prices}...")
    prices_df = pd.read_parquet(args.prices)
    print(f"Loading universe from {args.universe}...")
    univ_df = pd.read_parquet(args.universe)

    univ_df['rebalance_date'] = pd.to_datetime(univ_df['rebalance_date'])
    fund_df['filed_date'] = pd.to_datetime(fund_df['filed_date'])
    prices_df['date'] = pd.to_datetime(prices_df['date'])

    fund_groups = {ticker: grp for ticker, grp in fund_df.groupby('ticker')}
    price_groups = {ticker: grp for ticker, grp in prices_df.groupby('ticker')}
    
    market_ticker = list(price_groups.keys())[0] if len(price_groups) > 0 else None
    market_df = price_groups.get(market_ticker, pd.DataFrame()) if market_ticker else pd.DataFrame()
    if len(market_df) > 0:
        market_monthly_series = market_df.set_index('date')['close'].resample('ME').last().pct_change().dropna()
    else:
        market_monthly_series = pd.Series(dtype=float)

    results = []
    total_rows = len(univ_df)
    print(f"Processing {total_rows} universe rows...")

    for idx, row in univ_df.iterrows():
        ticker = row['ticker']
        cik = row.get('cik', None)
        rebal_date = row['rebalance_date']
        market_cap = row.get('market_cap', np.nan)
        
        fund_ticker = fund_groups.get(ticker, pd.DataFrame())
        
        sub_fund = fund_ticker[fund_ticker['filed_date'] <= rebal_date].sort_values('filed_date')
        latest_f = sub_fund.iloc[-1] if len(sub_fund) > 0 else pd.Series()
            
        g = compute_growth(fund_ticker, rebal_date)
        margin_vol = compute_margin_vol(fund_ticker, rebal_date)
        payout = compute_payout(fund_ticker, rebal_date)
        reinvest = compute_reinvestment(fund_ticker, rebal_date)
        
        category = classify_category(g, margin_vol, market_cap, payout, reinvest)
        
        icr = compute_icr(latest_f)
        t_rate = compute_tax_rate(latest_f)
        de, neg_eq = compute_de(latest_f)
        
        ticker_prices = price_groups.get(ticker, pd.DataFrame())
        if len(ticker_prices) == 0:
            beta_lev = 1.0
        else:
            sub_p = ticker_prices[ticker_prices['date'] <= rebal_date]
            if len(sub_p) < 60:
                beta_lev = 1.0
            else:
                monthly = sub_p.set_index('date')['close'].resample('ME').last().pct_change().dropna()
                market_sub = market_monthly_series[market_monthly_series.index <= rebal_date]
                if len(market_sub) < 60:
                    beta_lev = 1.0
                else:
                    aligned = pd.concat([monthly, market_sub], axis=1, join='inner').dropna()
                    if len(aligned) < 30:
                        beta_lev = 1.0
                    else:
                        cov = np.cov(aligned.iloc[:, 0], aligned.iloc[:, 1])[0, 1]
                        var_m = np.var(aligned.iloc[:, 1], ddof=1)
                        beta_lev = float(cov / var_m) if var_m > 0 else 1.0
        
        denom = 1.0 + (1.0 - t_rate) * de
        beta_u = max(beta_lev / denom if denom > 0 else beta_lev, 0.1)
        
        phi = compute_phi(icr, beta_u, t_rate, de)
        
        components = [g, margin_vol, payout, reinvest, icr, beta_lev, beta_u, t_rate, de, phi]
        nan_count = sum(1 for c in components if pd.isna(c))
        
        results.append({
            'ticker': ticker,
            'cik': cik,
            'rebalance_date': rebal_date.date(),
            'category': category,
            'g_3yr': g,
            'margin_vol_10yr': margin_vol,
            'payout_3yr': payout,
            'reinvestment_3yr': reinvest,
            'icr': icr,
            'beta_levered': beta_lev,
            'beta_unlevered': beta_u,
            'tax_rate': t_rate,
            'debt_to_equity': de,
            'phi': phi,
            'negative_equity_flag': neg_eq,
            'component_nan_count': nan_count
        })
        
        if (idx + 1) % 1000 == 0 or (idx + 1) == total_rows:
            print(f"Processed {idx + 1} / {total_rows} rows...")

    res_df = pd.DataFrame(results)
    
    timestamp_str = datetime.utcnow().isoformat() + "Z"
    timestamp_ns = time.time_ns()
    
    csv_bytes = res_df.to_csv(index=False).encode('utf-8')
    sha256_val = hashlib.sha256(csv_bytes).hexdigest()
    uuid_val = hashlib.md5(csv_bytes).hexdigest()
    
    res_df['provenance_sha256'] = sha256_val
    res_df['provenance_uuid'] = uuid_val
    res_df['provenance_timestamp_ns'] = timestamp_ns

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    res_df.to_parquet(args.output, index=False)
    print(f"Saved phi_panel to {args.output} with {len(res_df)} rows.")

    sidecar_path = args.output + ".provenance.json"
    provenance_meta = {
        "source": "IN_HOUSE_DENSE_FUNDAMENTALS",
        "retrieval_timestamp": timestamp_str,
        "source_version": "dense_fundamentals_20261010",
        "pit_timestamp_column": "filed_date",
        "entity_key": "ticker",
        "transformations": ["growth_cagr_3yr","margin_vol_10yr","phi_formula","category_classify","kaplan_meier_survival","second_derivative_break"],
        "sha256": sha256_val,
        "row_count": len(res_df),
        "date_range": {"min": str(res_df['rebalance_date'].min()), "max": str(res_df['rebalance_date'].max())},
        "mapping_confidence": "high"
    }
    with open(sidecar_path, 'w') as f:
        json.dump(provenance_meta, f, indent=2)
    print(f"Saved sidecar provenance to {sidecar_path}")

if __name__ == '__main__':
    main()
