import optuna
import yaml
import os
import time
from db.connection import get_db_connection
from backtesting.backtest import run_walk_forward_backtest
from config import HYBRID_SOURCE_WEIGHTS, HYBRID_OPTIMIZATION_CONFIG

# Disable Optuna logging output unless wanted
optuna.logging.set_verbosity(optuna.logging.WARNING)

CONFIG_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config", "reddit_weights.yaml"))
HYBRID_WEIGHTS_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config", "hybrid_weights.yaml"))

def load_weights_config():
    with open(CONFIG_PATH, 'r') as f:
        return yaml.safe_load(f)

def load_hybrid_weights_config():
    with open(HYBRID_WEIGHTS_PATH, 'r') as f:
        return yaml.safe_load(f)

def run_bayesian_optimization(trials: int = 50, objective_metric: str = "sharpe", optimize_source_weights: bool = True) -> dict:
    """
    Runs Optuna to optimize subreddit, category, and source weights.
    Returns the best weights and their corresponding backtest performance.
    """
    config = load_weights_config()
    hybrid_config = load_hybrid_weights_config()
    sub_taxonomy = config["subreddit_weights"]
    
    categories = list(sub_taxonomy.keys())
    sources = list(HYBRID_SOURCE_WEIGHTS.keys())
    source_bounds = HYBRID_OPTIMIZATION_CONFIG.get("source_weight_bounds", {})
    
    def objective(trial: optuna.Trial):
        # 1. Suggest category weights
        cat_weights_raw = {}
        for cat in categories:
            cat_weights_raw[cat] = trial.suggest_float(f"cat_{cat}", 0.01, 1.0)
            
        # Normalize category weights to sum to 1.0
        sum_raw = sum(cat_weights_raw.values())
        cat_weights = {k: v / sum_raw for k, v in cat_weights_raw.items()}
        
        # 2. Suggest subreddit weights (normalized within each category)
        sub_weights = {}
        for cat, subs in sub_taxonomy.items():
            sub_weights[cat] = {}
            sub_names = list(subs.keys())
            sub_raw = {}
            for sub in sub_names:
                sub_raw[sub] = trial.suggest_float(f"sub_{cat}_{sub}", 0.01, 1.0)
            # Normalize within category
            sum_sub = sum(sub_raw.values())
            for sub in sub_names:
                sub_weights[cat][sub] = sub_raw[sub] / sum_sub
        
        # 3. Suggest source weights (if enabled)
        source_weights = {}
        if optimize_source_weights:
            src_raw = {}
            for src in sources:
                bounds = source_bounds.get(src, [0.0, 1.0])
                src_raw[src] = trial.suggest_float(f"src_{src}", bounds[0], bounds[1])
            sum_src = sum(src_raw.values())
            source_weights = {k: v / sum_src for k, v in src_raw.items()}
        else:
            source_weights = HYBRID_SOURCE_WEIGHTS
                
        # Run backtest
        results = run_walk_forward_backtest(
            category_weights=cat_weights,
            subreddit_weights=sub_weights,
            source_weights=source_weights,
            objective=objective_metric
        )
        
        # Return objective value to maximize
        if objective_metric == "sharpe":
            return results["sharpe"]
        elif objective_metric == "information_coefficient":
            return results["ic"]
        else:
            return results["hit_rate"]

    study = optuna.create_study(direction="maximize")
    study.optimize(objective, n_trials=trials)
    
    best_params = study.best_params
    
    # Reconstruct optimal weights from best parameters
    best_cat_weights_raw = {cat: best_params[f"cat_{cat}"] for cat in categories}
    sum_best_raw = sum(best_cat_weights_raw.values())
    best_cat_weights = {k: v / sum_best_raw for k, v in best_cat_weights_raw.items()}
    
    best_sub_weights = {}
    for cat, subs in sub_taxonomy.items():
        best_sub_weights[cat] = {}
        for sub in subs.keys():
            best_sub_weights[cat][sub] = best_params[f"sub_{cat}_{sub}"]
    
    best_source_weights = {}
    if optimize_source_weights:
        best_src_raw = {src: best_params[f"src_{src}"] for src in sources}
        sum_best_src = sum(best_src_raw.values())
        best_source_weights = {k: v / sum_best_src for k, v in best_src_raw.items()}
    else:
        best_source_weights = HYBRID_SOURCE_WEIGHTS
            
    # Run a final backtest to fetch all performance metrics
    final_perf = run_walk_forward_backtest(best_cat_weights, best_sub_weights, best_source_weights)
    
    return {
        "category_weights": best_cat_weights,
        "subreddit_weights": best_sub_weights,
        "source_weights": best_source_weights,
        "metrics": final_perf
    }

def save_optimized_weights_as_challenger(opt_results: dict):
    """
    Saves optimized weights to the weight_versions table as a Challenger (is_active = 0).
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Save parameters as YAML
    config_yaml = yaml.dump({
        "category_weights": opt_results["category_weights"],
        "subreddit_weights": opt_results["subreddit_weights"]
    })
    
    import json
    cursor.execute("""
        INSERT INTO weight_versions (
            config_yaml, category_weights, subreddit_weights, ic_score, sharpe_ratio, 
            hit_rate, lookback_days, optimization_method, promoted_at, is_active, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        config_yaml, 
        json.dumps(opt_results["category_weights"]), 
        json.dumps(opt_results["subreddit_weights"]),
        opt_results["metrics"]["ic"],
        opt_results["metrics"]["sharpe"],
        opt_results["metrics"]["hit_rate"],
        180, # Lookback days default
        "bayesian_optimization",
        None, # Not promoted yet
        0, # is_active = False (Challenger status)
        int(time.time())
    ))
    
    conn.commit()
    conn.close()
    print("Optimized weights saved as Challenger configuration.")

if __name__ == "__main__":
    res = run_bayesian_optimization(trials=20)
    save_optimized_weights_as_challenger(res)
