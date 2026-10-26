import json, numpy as np, pandas as pd

df = pd.read_csv("data/backtests/three_arm_comparison/three_arm_monthly_performance.csv")
df["rebalance_date"] = pd.to_datetime(df["rebalance_date"])

def metrics(r, bench, rf=0.02):
    T = len(r)
    rf_m = (1.0 + rf)**(1/12) - 1.0
    cum = float(np.prod(1.0 + r) - 1.0)
    cagr = float((1.0 + cum)**(12.0/T) - 1.0) if T > 0 else 0.0
    vol = float(np.std(r, ddof=1) * np.sqrt(12.0)) if T > 1 else 0.0
    ex = r - rf_m
    sharpe = float(np.mean(ex) / np.std(r, ddof=1) * np.sqrt(12.0)) if vol > 0 else 0.0
    d_ex = np.where(ex < 0, ex, 0.0)
    d_vol = float(np.sqrt(np.mean(d_ex**2)) * np.sqrt(12.0))
    sortino = float(np.mean(ex) / d_vol * np.sqrt(12.0)) if d_vol > 0 else 0.0
    w = 10000.0 * np.cumprod(1.0 + r)
    pk = np.maximum.accumulate(w)
    mdd = float(np.min((w - pk) / pk))
    calmar = float(cagr / abs(mdd)) if abs(mdd) > 0 else 0.0
    cov = np.cov(r, bench)
    beta = float(cov[0, 1] / (cov[1, 1] + 1e-9)) if cov[1, 1] > 0 else 1.0
    b_cum = float(np.prod(1.0 + bench) - 1.0)
    b_cagr = float((1.0 + b_cum)**(12.0/T) - 1.0) if T > 0 else 0.0
    alpha = float(cagr - (rf + beta * (b_cagr - rf)))
    act = r - bench
    te = float(np.std(act, ddof=1) * np.sqrt(12.0)) if T > 1 else 0.0
    ir = float((np.mean(act) * 12.0) / te) if te > 0 else 0.0
    up = bench > 0
    dn = bench < 0
    up_cap = float(np.mean(r[up]) / np.mean(bench[up])) if np.sum(up) > 0 else 1.0
    dn_cap = float(np.mean(r[dn]) / np.mean(bench[dn])) if np.sum(dn) > 0 else 1.0
    return {
        "cum": cum, "cagr": cagr, "vol": vol, "sharpe": sharpe, "sortino": sortino,
        "max_dd": mdd, "calmar": calmar, "beta": beta, "alpha": alpha,
        "te": te, "ir": ir, "up_cap": up_cap, "dn_cap": dn_cap
    }

print("=== REGIME PERFORMANCE SUMMARY ===")
regs = [
    ("Full Backtest (2018-2024, 83M)", df),
    ("2020 COVID Crash & Recovery (Mar 2020 - Dec 2020, 10M)", df[(df["rebalance_date"] >= "2020-03-01") & (df["rebalance_date"] <= "2020-12-31")]),
    ("2022 Fed Rate Hiking Bear (Jan 2022 - Dec 2022, 12M)", df[(df["rebalance_date"] >= "2022-01-01") & (df["rebalance_date"] <= "2022-12-31")]),
    ("2023-2024 Tech & AI Bull Run (Jan 2023 - Nov 2024, 23M)", df[df["rebalance_date"] >= "2023-01-01"]),
    ("2018 Market Volatility (Jan 2018 - Dec 2018, 11M)", df[(df["rebalance_date"] >= "2018-01-01") & (df["rebalance_date"] <= "2018-12-31")]),
    ("2019 Expansion (Jan 2019 - Dec 2019, 12M)", df[(df["rebalance_date"] >= "2019-01-01") & (df["rebalance_date"] <= "2019-12-31")]),
    ("2021 Post-COVID Reopening (Jan 2021 - Dec 2021, 12M)", df[(df["rebalance_date"] >= "2021-01-01") & (df["rebalance_date"] <= "2021-12-31")])
]

for name, sub in regs:
    print(f"\n--- {name} ---")
    b = sub["spy_ret"].values
    for arm_label, col in [("Arm 1 (Full Quant)", "arm1_net_ret"),
                           ("Arm 2 (Qual Moat)", "arm2_net_ret"),
                           ("Arm 3 (Hybrid)", "arm3_net_ret"),
                           ("SPY Benchmark", "spy_ret")]:
        m = metrics(sub[col].values, b)
        print(f"{arm_label:20s} | Cum: {m['cum']*100:6.2f}% | Ann: {m['cagr']*100:6.2f}% | Vol: {m['vol']*100:5.2f}% | Sharpe: {m['sharpe']:5.2f} | Sortino: {m['sortino']:5.2f} | MaxDD: {m['max_dd']*100:6.2f}% | Beta: {m['beta']:4.2f} | Alpha: {m['alpha']*100:6.2f}% | Up: {m['up_cap']*100:5.1f}% | Dn: {m['dn_cap']*100:5.1f}%")

print("\n=== TRANSACTION COST STRESS TEST ===")
for tcost in [15.0, 30.0, 50.0, 75.0, 100.0]:
    net1 = df["arm1_gross_ret"] - df["arm1_turnover"] * (tcost / 10000.0)
    net2 = df["arm2_gross_ret"] - df["arm2_turnover"] * (tcost / 10000.0)
    net3 = df["arm3_gross_ret"] - df["arm3_turnover"] * (tcost / 10000.0)
    b = df["spy_ret"].values
    m1 = metrics(net1.values, b)
    m2 = metrics(net2.values, b)
    m3 = metrics(net3.values, b)
    print(f"T-Cost = {tcost:5.1f} bps:")
    print(f"  Arm 1: CAGR={m1['cagr']*100:5.2f}%, Sharpe={m1['sharpe']:4.2f}, Alpha={m1['alpha']*100:5.2f}%, Sortino={m1['sortino']:4.2f}, MaxDD={m1['max_dd']*100:5.2f}%, AnnDrag={df['arm1_turnover'].mean()*12*tcost:5.1f} bps")
    print(f"  Arm 2: CAGR={m2['cagr']*100:5.2f}%, Sharpe={m2['sharpe']:4.2f}, Alpha={m2['alpha']*100:5.2f}%, Sortino={m2['sortino']:4.2f}, MaxDD={m2['max_dd']*100:5.2f}%, AnnDrag={df['arm2_turnover'].mean()*12*tcost:5.1f} bps")
    print(f"  Arm 3: CAGR={m3['cagr']*100:5.2f}%, Sharpe={m3['sharpe']:4.2f}, Alpha={m3['alpha']*100:5.2f}%, Sortino={m3['sortino']:4.2f}, MaxDD={m3['max_dd']*100:5.2f}%, AnnDrag={df['arm3_turnover'].mean()*12*tcost:5.1f} bps")

print("\n=== CORRELATION ANALYSIS ===")
rets = pd.DataFrame({"Arm 1": df["arm1_net_ret"], "Arm 2": df["arm2_net_ret"], "Arm 3": df["arm3_net_ret"], "SPY": df["spy_ret"]})
print("Total Return Correlation:\n", rets.corr().round(4))
acts = pd.DataFrame({"Arm 1 Act": df["arm1_net_ret"] - df["spy_ret"], "Arm 2 Act": df["arm2_net_ret"] - df["spy_ret"], "Arm 3 Act": df["arm3_net_ret"] - df["spy_ret"]})
print("Active Return Correlation:\n", acts.corr().round(4))
