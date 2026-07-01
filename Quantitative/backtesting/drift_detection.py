import yaml
import os
import json
from db.connection import get_db_connection
from backtesting.backtest import run_walk_forward_backtest
from optimization.optuna_search import run_bayesian_optimization, save_optimized_weights_as_challenger

CONFIG_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config", "reddit_weights.yaml"))

def check_ic_drift_and_reoptimize(decay_threshold: float = 0.20, recent_window_days: int = 60) -> dict:
    """
    Checks if the trailing IC of the active weight configuration has decayed by more than decay_threshold.
    If decayed, triggers Optuna re-optimization and saves the output.
    
    Args:
        decay_threshold: Relative decay threshold (default 20%)
        recent_window_days: Lookback window for trailing IC calculation (default 60 days)
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. Get the current active version's baseline metrics
    cursor.execute("""
        SELECT version_id, ic_score, category_weights, subreddit_weights, lookback_days
        FROM weight_versions 
        WHERE is_active = 1 
        ORDER BY version_id DESC LIMIT 1
    """)
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        print("No active weight version found in database to monitor drift.")
        return {"drift_detected": False, "reason": "No active version"}
        
    version_id, baseline_ic, cat_w_str, sub_w_str, baseline_lookback = row[0], row[1], row[2], row[3], row[4]
    
    if baseline_ic is None or baseline_ic <= 0.05:
        # Avoid division by zero or triggers on noisy low baseline values
        print(f"Baseline IC ({baseline_ic}) is too low to reliably measure relative decay.")
        return {"drift_detected": False, "reason": "Baseline IC too low"}
        
    cat_weights = json.loads(cat_w_str)
    sub_weights = json.loads(sub_w_str)
    
    # 2. Run recent backtest over a shorter window for trailing IC
    recent_perf = run_walk_forward_backtest(cat_weights, sub_weights, lookback_days=recent_window_days)
    recent_ic = recent_perf["ic"]
    
    # 3. Compare for decay
    decay = (baseline_ic - recent_ic) / baseline_ic
    print(f"Checking drift: Baseline IC={baseline_ic:.4f} (lookback={baseline_lookback}d), Trailing IC={recent_ic:.4f} (lookback={recent_window_days}d), Decay={decay:.2%}")
    
    if decay > decay_threshold:
        print(f"⚠️ IC Drift Detected ({decay:.2%} decay > {decay_threshold:.2%}). Triggering weight re-optimization...")
        
        # Trigger Bayesian weight optimization
        opt_results = run_bayesian_optimization(trials=25)
        save_optimized_weights_as_challenger(opt_results)
        
        return {
            "drift_detected": True,
            "baseline_ic": baseline_ic,
            "recent_ic": recent_ic,
            "decay": decay,
            "reoptimization_triggered": True,
            "new_challenger_sharpe": opt_results["metrics"]["sharpe"]
        }
        
    return {
        "drift_detected": False,
        "baseline_ic": baseline_ic,
        "recent_ic": recent_ic,
        "decay": decay,
        "reoptimization_triggered": False
    }

if __name__ == "__main__":
    check_ic_drift_and_reoptimize()
