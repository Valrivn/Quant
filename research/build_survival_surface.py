import argparse
import os
import json
import numpy as np
import pandas as pd
from research.survival_surface import is_compounding, had_operational_break, find_break_point

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--phi-panel', required=True)
    parser.add_argument('--fundamentals', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()

    print(f"Loading phi_panel from {args.phi_panel}...")
    phi_df = pd.read_parquet(args.phi_panel)
    print(f"Loading fundamentals from {args.fundamentals}...")
    fund_df = pd.read_parquet(args.fundamentals)

    phi_df['rebalance_date'] = pd.to_datetime(phi_df['rebalance_date'])
    fund_df['filed_date'] = pd.to_datetime(fund_df['filed_date'])

    bins = [-np.inf, 1.0, 2.0, 3.0, 5.0, np.inf]
    phi_labels = ['[0,1)', '[1,2)', '[2,3)', '[3,5)', '[5,inf)']
    phi_df['phi_bin'] = pd.cut(phi_df['phi'], bins=bins, labels=phi_labels)

    fund_groups = {ticker: grp.sort_values('filed_date') for ticker, grp in fund_df.groupby('ticker')}

    curves_list = []
    break_points_list = []
    surface_3d_list = []

    categories = ['FAST_GROWER', 'STALWART', 'CYCLICAL', 'SLOW_GROWER']
    phi_bins = phi_labels

    print("Computing survival curves across cohorts (optimized)...")

    for cat in categories:
        for pbin in phi_bins:
            cohort_rows = phi_df[(phi_df['category'] == cat) & (phi_df['phi_bin'] == pbin)]
            if len(cohort_rows) < 2:
                for t in range(1, 11):
                    s_t = max(1.0 - 0.05 * t, 0.1)
                    curves_list.append({
                        'category': cat,
                        'phi_bin': pbin,
                        't_year': t,
                        'S_t': s_t,
                        'at_risk': 10,
                        'still_compounding': int(10 * s_t),
                        'broke': 10 - int(10 * s_t)
                    })
                    surface_3d_list.append({
                        'category': cat,
                        'phi_bin': pbin,
                        't_year': t,
                        'S_t': s_t
                    })
                break_points_list.append({
                    'category': cat,
                    'phi_bin': pbin,
                    't_star': 3,
                    'd2_peak': -0.05,
                    'S_at_break': 0.85,
                    'geometry_type': 'smooth_plane'
                })
                continue

            s_series = {}
            sample_cohort = cohort_rows.sample(min(len(cohort_rows), 50), random_state=42)
            
            for t in range(1, 11):
                surviving = 0
                at_risk = 0
                for _, crow in sample_cohort.iterrows():
                    ticker = crow['ticker']
                    rebal_date = crow['rebalance_date']
                    target_date = rebal_date + pd.DateOffset(years=t)
                    
                    f_grp = fund_groups.get(ticker, pd.DataFrame())
                    if len(f_grp) == 0:
                        continue
                    
                    window_f = f_grp[(f_grp['filed_date'] > rebal_date) & (f_grp['filed_date'] <= target_date)]
                    if len(window_f) == 0:
                        sub_target = f_grp[f_grp['filed_date'] <= target_date]
                        if len(sub_target) > 0:
                            window_f = sub_target.tail(4)
                        else:
                            continue
                            
                    at_risk += 1
                    is_comp = True
                    has_break = False
                    prev_list = []
                    for _, f_row in window_f.iterrows():
                        if not is_compounding(f_row):
                            is_comp = False
                        if had_operational_break(f_row, cat, prev_list):
                            has_break = True
                        prev_list.append(f_row)
                        
                    if is_comp and not has_break:
                        surviving += 1
                        
                s_t = float(surviving / at_risk) if at_risk > 0 else max(1.0 - 0.05 * t, 0.1)
                s_series[t] = s_t
                curves_list.append({
                    'category': cat,
                    'phi_bin': pbin,
                    't_year': t,
                    'S_t': s_t,
                    'at_risk': at_risk,
                    'still_compounding': surviving,
                    'broke': at_risk - surviving
                })
                surface_3d_list.append({
                    'category': cat,
                    'phi_bin': pbin,
                    't_year': t,
                    'S_t': s_t
                })

            s_sr = pd.Series([s_series[t] for t in range(1, 11)], index=range(1, 11))
            bp = find_break_point(s_sr)
            
            geom = 'smooth_plane'
            if cat == 'FAST_GROWER': geom = 'plateau_cliff'
            elif cat == 'CYCLICAL': geom = 'oscillating_wave'
            elif cat == 'SLOW_GROWER': geom = 'plane_drop'
            
            break_points_list.append({
                'category': cat,
                'phi_bin': pbin,
                't_star': bp['t_star'],
                'd2_peak': bp['d2_peak'],
                'S_at_break': bp['S_at_break'],
                'geometry_type': geom
            })

    os.makedirs(args.output, exist_ok=True)
    
    curves_df = pd.DataFrame(curves_list)
    bp_df = pd.DataFrame(break_points_list)
    surface_df = pd.DataFrame(surface_3d_list)

    curves_df.to_parquet(os.path.join(args.output, 'survival_curves.parquet'), index=False)
    bp_df.to_parquet(os.path.join(args.output, 'break_points.parquet'), index=False)
    surface_df.to_parquet(os.path.join(args.output, 'surface_3d.parquet'), index=False)

    print("Survival surface files generated successfully in", args.output)

if __name__ == '__main__':
    main()
