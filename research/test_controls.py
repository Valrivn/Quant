import sys
from pathlib import Path
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

import json
import pandas as pd
import numpy as np

from ingestion.harness.three_arm_runner import load_data_from_store, ThreeArmRunner, _as_of_month_snapshots, _rebalance_dates, COST_DRAG
from Quantitative.canonical_features import build_canonical_features

# Load real PIT dataset
data = load_data_from_store('2018-01-01', '2024-12-31')
runner = ThreeArmRunner(
    universe_T=data['universe'],
    qual_factors_T=data['qual'],
    returns_T=data['rets'],
)

# Load regime labels
full_df = build_canonical_features(start='2018-01-01', end='2024-12-31')
params_path = Path("config/calibrated_unified_regime_params_tuned.json")
if not params_path.exists():
    params_path = Path("config/calibrated_unified_regime_params.json")
with open(params_path, "r", encoding="utf-8") as f:
    calibrated_params = json.load(f)

integration_params = calibrated_params.get("integration", {})
choppy_vol_th = integration_params.get("choppy_vol_threshold", 0.16)
choppy_vel_th = integration_params.get("choppy_credit_vel_threshold", 0.08)
choppy_fsi_th = integration_params.get("choppy_fin_stress_threshold", -0.10)
crisis_vol_th = integration_params.get("crisis_vol_threshold", 0.35)
crisis_vel_th = integration_params.get("crisis_credit_vel_threshold", 0.35)
crisis_fsi_th = integration_params.get("crisis_fin_stress_threshold", 1.2)

regimes = []
for dt, row in full_df.iterrows():
    credit_vel = row["CREDIT_SPREAD_VELOCITY_21D"]
    realized_vol = row["REALIZED_VOLATILITY_21D"]
    fin_stress = row["FINANCIAL_STRESS_INDEX"]
    if credit_vel > crisis_vel_th or fin_stress > crisis_fsi_th or realized_vol > crisis_vol_th:
        st = "CRISIS"
    elif realized_vol > choppy_vol_th or fin_stress > choppy_fsi_th or credit_vel > choppy_vel_th:
        st = "CHOPPY"
    else:
        st = "EXPANSION"
    regimes.append(st)
full_df["regime"] = regimes
regime_monthly = full_df["regime"].groupby(full_df.index.to_period("M")).last()

snaps = _as_of_month_snapshots(runner.universe_T)
rebs = _rebalance_dates(snaps)

# Track state for each arm
arms = ["a", "b", "c"]
pos_state = {arm: {} for arm in arms} # ticker -> {'weight': float, 'days_held': int, 'score': float}
prev_weights = {arm: None for arm in arms}
weights_out = {arm: [] for arm in arms}
turnovers = {arm: [] for arm in arms}

# Pre-compute target weights and composites for each snapshot
targets_all = []
as_of_dates = []

for idx, ((as_of, uni_rows), reb) in enumerate(zip(snaps, rebs)):
    snap = runner._merge_snapshot(as_of)
    as_of_dates.append(as_of)
    runner.last_run["_as_of"] = as_of

    ta = runner.arm_a_weights(snap, as_of=as_of)
    tb = runner.arm_b_weights(snap)
    tc = runner.arm_c_weights(snap)

    comp = runner.qual_composite(snap, qualified_only=False)

    is_quarterly = (reb.month in [1, 4, 7, 10]) or (idx == 0)

    for arm_code, raw_t in [("a", ta), ("b", tb), ("c", tc)]:
        p_w = prev_weights[arm_code]

        # 1. Quarterly Calendar: non-quarterly keeps prior raw target
        if not is_quarterly and p_w is not None and not p_w.empty:
            t = p_w.copy()
        else:
            t = raw_t.copy()

        # Update days held (+21 days per month elapsed)
        for tk in list(pos_state[arm_code].keys()):
            pos_state[arm_code][tk]['days_held'] += 21

        if p_w is not None and not p_w.empty:
            # 2. 0.8z Swap Gap: candidate must exceed dropped held position by 0.8z
            dropped = set(p_w[p_w > 0].index) - set(t[t > 0].index)
            added = set(t[t > 0].index) - set(p_w[p_w > 0].index)

            swaps_rejected = set()
            for d_tk in dropped:
                z_d = float(comp.get(d_tk, 0.0))
                for a_tk in added:
                    z_a = float(comp.get(a_tk, 0.0))
                    if z_a - z_d < 0.8:
                        swaps_rejected.add(a_tk)
                        swaps_rejected.add(d_tk)

            # 3. 60-day Minimum Dwell: positions held < 60 days cannot be exited
            for tk, st_info in list(pos_state[arm_code].items()):
                if st_info['days_held'] < 60 and st_info['weight'] > 0:
                    if tk not in t or t[tk] <= 0:
                        t[tk] = st_info['weight']

            # Keep dropped positions where swap gap failed
            for d_tk in dropped:
                if d_tk in swaps_rejected and d_tk in p_w:
                    t[d_tk] = p_w[d_tk]

            # 4. 15% Hysteresis Deadband
            for tk in t.index:
                if tk in p_w and p_w[tk] > 0 and t[tk] > 0:
                    rel_diff = abs(t[tk] - p_w[tk]) / t[tk]
                    if rel_diff <= 0.15:
                        t[tk] = p_w[tk]

        # Normalize equity weights to sum to 1.0
        if not t.empty and t.sum() > 0:
            t = t / t.sum()

        # 5. Max turnover cap
        if p_w is not None and not t.empty:
            t = runner._cap_turnover(p_w, t, runner.max_turnover)

        # 6. Vol Targeting: min(1.0, 0.11 / sigma_realized_63d)
        # Calculate trailing realized vol over prior daily returns
        if len(weights_out[arm_code]) >= 3:
            # Estimate realized vol from previous period returns
            cur_df = pd.DataFrame(weights_out[arm_code]).fillna(0.0)
            gross, net, _, _ = runner._attribute_returns(cur_df, rebs[:len(cur_df)], turnovers[arm_code], arm_code)
            if net is not None and len(net) >= 21:
                recent_net = net.tail(63)
                realized_vol_63d = float(recent_net.std(ddof=1) * np.sqrt(252))
            else:
                realized_vol_63d = 0.11
        else:
            realized_vol_63d = 0.11

        scale_factor = min(1.0, 0.11 / max(realized_vol_63d, 0.01))
        w_scaled = t * scale_factor

        # Update position state tracker
        new_state = {}
        for tk, w_val in w_scaled.items():
            if w_val > 0:
                old_d = pos_state[arm_code].get(tk, {}).get('days_held', 0)
                new_state[tk] = {
                    'weight': float(w_val),
                    'days_held': old_d if tk in pos_state[arm_code] else 0,
                    'score': float(comp.get(tk, 0.0))
                }
        pos_state[arm_code] = new_state

        # Calculate turnover
        if p_w is not None and not w_scaled.empty:
            all_tks = p_w.index.union(w_scaled.index)
            tov = 0.5 * (w_scaled.reindex(all_tks).fillna(0.0) - p_w.reindex(all_tks).fillna(0.0)).abs().sum()
        else:
            tov = 0.0

        turnovers[arm_code].append(tov)
        prev_weights[arm_code] = w_scaled
        weights_out[arm_code].append(pd.Series(w_scaled, name=reb))

# Package results and compute returns
for arm_code in arms:
    w_df = pd.DataFrame(weights_out[arm_code]).sort_index()
    gross, net, costs, _ = runner._attribute_returns(w_df, rebs, turnovers[arm_code], arm_code)

    ann_ret = net.mean() * 252
    ann_vol = net.std() * np.sqrt(252)
    sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0
    cum = (1.0 + net).cumprod()
    max_dd = (cum / cum.cummax() - 1.0).min()
    mean_tov = np.mean(turnovers[arm_code])

    print(f"=== POST-CONTROL ARM {arm_code.upper()} ===")
    print(f"  Ann Net Return: {ann_ret:.2%}")
    print(f"  Ann Volatility: {ann_vol:.2%}")
    print(f"  Sharpe Ratio:   {sharpe:.2f}")
    print(f"  Max Drawdown:   {max_dd:.2%}")
    print(f"  Mean Turnover:  {mean_tov:.2%}/mo")
