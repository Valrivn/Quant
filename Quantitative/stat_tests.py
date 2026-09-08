"""
Statistical Test Suite for Backtest Harness
Pure functions, type hints. No global state.

Tests:
- ANOVA: Sector variance in max drawdown drops
- Welch's t-test: Dot-Com (1999-2003) vs AI Boom (2020-2026) drawdown distributions
- Chi-square: Win/Loss contingency (Static -10% vs Dynamic IV_P25 vs Dynamic IV_P10)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy import stats

logger = logging.getLogger(__name__)

RESULTS_DIR = Path("data/checkpoints/backtest/stats")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

@dataclass
class ANOVAResult:
    """ANOVA test result."""
    f_statistic: float
    p_value: float
    significant: bool
    alpha: float
    sectors_tested: List[str]
    sector_means: Dict[str, float]
    sector_vars: Dict[str, float]
    between_group_var: float
    within_group_var: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "f_statistic": self.f_statistic,
            "p_value": self.p_value,
            "significant": self.significant,
            "alpha": self.alpha,
            "sectors_tested": self.sectors_tested,
            "sector_means": self.sector_means,
            "sector_vars": self.sector_vars,
            "between_group_var": self.between_group_var,
            "within_group_var": self.within_group_var,
        }

@dataclass
class WelchTTestResult:
    """Welch's t-test result."""
    t_statistic: float
    p_value: float
    significant: bool
    alpha: float
    group1_name: str
    group2_name: str
    group1_mean: float
    group2_mean: float
    group1_std: float
    group2_std: float
    group1_n: int
    group2_n: int
    degrees_of_freedom: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "t_statistic": self.t_statistic,
            "p_value": self.p_value,
            "significant": self.significant,
            "alpha": self.alpha,
            "group1_name": self.group1_name,
            "group2_name": self.group2_name,
            "group1_mean": self.group1_mean,
            "group2_mean": self.group2_mean,
            "group1_std": self.group1_std,
            "group2_std": self.group2_std,
            "group1_n": self.group1_n,
            "group2_n": self.group2_n,
            "degrees_of_freedom": self.degrees_of_freedom,
        }

@dataclass
class ChiSquareResult:
    """Chi-square test result."""
    chi2_statistic: float
    p_value: float
    significant: bool
    alpha: float
    dof: int
    contingency_table: List[List[int]]
    expected_table: List[List[float]]
    strategies: List[str]
    outcomes: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chi2_statistic": self.chi2_statistic,
            "p_value": self.p_value,
            "significant": self.significant,
            "alpha": self.alpha,
            "dof": self.dof,
            "contingency_table": self.contingency_table,
            "expected_table": self.expected_table,
            "strategies": self.strategies,
            "outcomes": self.outcomes,
        }

@dataclass
class DeflatedSharpeResult:
    """Deflated Sharpe Ratio (DSR) result."""
    sharpe_ratio: float
    dsr: float
    p_value: float
    n_trials: int
    significant: bool
    alpha: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sharpe_ratio": self.sharpe_ratio,
            "dsr": self.dsr,
            "p_value": self.p_value,
            "n_trials": self.n_trials,
            "significant": self.significant,
            "alpha": self.alpha,
        }

def anova_sector_variance(
    drawdowns_by_sector: Dict[str, List[float]],
    alpha: float = 0.05,
) -> ANOVAResult:
    """ANOVA: Test if sector max drawdown means differ significantly.
    
    H0: All sector mean drawdowns are equal
    H1: At least one sector has different mean drawdown
    
    Args:
        drawdowns_by_sector: Dict of sector -> list of max drawdowns (as decimals, negative)
        alpha: Significance level
    
    Returns:
        ANOVAResult with F-statistic, p-value, and significance
    """
    sectors = list(drawdowns_by_sector.keys())
    groups = [drawdowns_by_sector[s] for s in sectors]
    
    # Filter out empty groups
    valid_sectors = []
    valid_groups = []
    for s, g in zip(sectors, groups):
        if len(g) > 0:
            valid_sectors.append(s)
            valid_groups.append(g)
    
    if len(valid_groups) < 2:
        return ANOVAResult(
            f_statistic=0.0,
            p_value=1.0,
            significant=False,
            alpha=alpha,
            sectors_tested=valid_sectors,
            sector_means={},
            sector_vars={},
            between_group_var=0.0,
            within_group_var=0.0,
        )
    
    # Perform one-way ANOVA
    f_stat, p_value = stats.f_oneway(*valid_groups)
    
    # Compute group statistics
    sector_means = {s: float(np.mean(g)) for s, g in zip(valid_sectors, valid_groups)}
    sector_vars = {s: float(np.var(g, ddof=1)) for s, g in zip(valid_sectors, valid_groups)}
    
    # Between and within group variance
    all_values = np.concatenate(valid_groups)
    grand_mean = np.mean(all_values)
    between_var = sum(len(g) * (np.mean(g) - grand_mean)**2 for g in valid_groups) / (len(valid_groups) - 1)
    within_var = sum((len(g) - 1) * np.var(g, ddof=1) for g in valid_groups) / (len(all_values) - len(valid_groups))
    
    result = ANOVAResult(
        f_statistic=float(f_stat),
        p_value=float(p_value),
        significant=p_value < alpha,
        alpha=alpha,
        sectors_tested=valid_sectors,
        sector_means=sector_means,
        sector_vars=sector_vars,
        between_group_var=float(between_var),
        within_group_var=float(within_var),
    )
    
    logger.info(f"ANOVA Sector Variance: F={f_stat:.4f}, p={p_value:.4f}, significant={result.significant}")
    
    return result

def welch_t_test_regimes(
    drawdowns_dotcom: List[float],
    drawdowns_ai_boom: List[float],
    alpha: float = 0.05,
) -> WelchTTestResult:
    """Welch's t-test: Dot-Com (1999-2003) vs AI Boom (2020-2026) drawdown distributions.
    
    H0: Mean drawdowns are equal between regimes
    H1: Mean drawdowns differ (two-sided)
    
    Args:
        drawdowns_dotcom: Max drawdowns from 1999-2003 period
        drawdowns_ai_boom: Max drawdowns from 2020-2026 period
        alpha: Significance level
    
    Returns:
        WelchTTestResult
    """
    # Welch's t-test (does not assume equal variances)
    t_stat, p_value = stats.ttest_ind(
        drawdowns_dotcom,
        drawdowns_ai_boom,
        equal_var=False,
        alternative='two-sided',
    )
    
    # Degrees of freedom for Welch's test
    n1, n2 = len(drawdowns_dotcom), len(drawdowns_ai_boom)
    var1, var2 = np.var(drawdowns_dotcom, ddof=1), np.var(drawdowns_ai_boom, ddof=1)
    dof = (var1/n1 + var2/n2)**2 / ((var1/n1)**2/(n1-1) + (var2/n2)**2/(n2-1))
    
    result = WelchTTestResult(
        t_statistic=float(t_stat),
        p_value=float(p_value),
        significant=p_value < alpha,
        alpha=alpha,
        group1_name="Dot-Com (1999-2003)",
        group2_name="AI Boom (2020-2026)",
        group1_mean=float(np.mean(drawdowns_dotcom)) if n1 > 0 else 0.0,
        group2_mean=float(np.mean(drawdowns_ai_boom)) if n2 > 0 else 0.0,
        group1_std=float(np.std(drawdowns_dotcom, ddof=1)) if n1 > 1 else 0.0,
        group2_std=float(np.std(drawdowns_ai_boom, ddof=1)) if n2 > 1 else 0.0,
        group1_n=n1,
        group2_n=n2,
        degrees_of_freedom=float(dof),
    )
    
    logger.info(
        f"Welch's t-test DotCom vs AI: t={t_stat:.4f}, p={p_value:.4f}, "
        f"dof={dof:.1f}, significant={result.significant}"
    )
    
    return result

def chi_square_winloss(
    strategy_outcomes: Dict[str, Dict[str, int]],
    alpha: float = 0.05,
) -> ChiSquareResult:
    """Chi-square: Win/Loss contingency across strategies.
    
    Strategies: Static -10% stop, Dynamic IV_P25, Dynamic IV_P10
    Outcomes: Win, Loss
    
    H0: Win/Loss distribution is independent of strategy
    H1: Win/Loss distribution depends on strategy
    
    Args:
        strategy_outcomes: Dict of strategy -> {"win": count, "loss": count}
        alpha: Significance level
    
    Returns:
        ChiSquareResult
    """
    strategies = list(strategy_outcomes.keys())
    outcomes = ["win", "loss"]
    
    # Build contingency table
    contingency = []
    for s in strategies:
        row = [
            strategy_outcomes[s].get("win", 0),
            strategy_outcomes[s].get("loss", 0),
        ]
        contingency.append(row)
    
    contingency = np.array(contingency, dtype=float)
    
    # Chi-square test
    chi2, p_value, dof, expected = stats.chi2_contingency(contingency, correction=False)
    
    result = ChiSquareResult(
        chi2_statistic=float(chi2),
        p_value=float(p_value),
        significant=p_value < alpha,
        alpha=alpha,
        dof=int(dof),
        contingency_table=contingency.astype(int).tolist(),
        expected_table=expected.tolist(),
        strategies=strategies,
        outcomes=outcomes,
    )
    
    logger.info(
        f"Chi-square Win/Loss: chi2={chi2:.4f}, p={p_value:.4f}, "
        f"dof={dof}, significant={result.significant}"
    )
    
    return result

def deflated_sharpe_ratio(
    returns: np.ndarray,
    n_trials: int,
    alpha: float = 0.05,
) -> DeflatedSharpeResult:
    """Compute Deflated Sharpe Ratio (DSR) per Bailey & Lopez de Prado (2014).
    
    DSR adjusts Sharpe ratio for multiple testing / selection bias.
    
    Args:
        returns: Strategy returns array
        n_trials: Number of strategy configurations tested (multiple testing correction)
        alpha: Significance level
    
    Returns:
        DeflatedSharpeResult with SR, DSR, p-value
    """
    if len(returns) < 2:
        return DeflatedSharpeResult(
            sharpe_ratio=0.0, dsr=0.0, p_value=1.0,
            n_trials=n_trials, significant=False, alpha=alpha,
        )
    
    sr = np.mean(returns) / np.std(returns, ddof=1) * np.sqrt(252)  # Annualized
    
    # DSR formula: P(SR* <= sr) where SR* is max of n_trials Sharpe ratios
    # Under null, SR ~ N(0, 1/sqrt(N))
    # Using normal approximation for DSR
    T = len(returns)
    sr_var = (1 + 0.5 * sr**2) / T  # Variance of Sharpe ratio estimator
    sr_std = np.sqrt(sr_var)
    
    # Expected maximum of n_trials standard normals
    # Approximation: E[max] ≈ sqrt(2 * log(n_trials))
    if n_trials > 1:
        emc = np.sqrt(2 * np.log(n_trials))
    else:
        emc = 0.0
    
    # DSR = P(SR* <= sr_observed) = Φ((sr - emc * sr_std) / sr_std)
    z = (sr - emc * sr_std) / sr_std if sr_std > 0 else 0
    from scipy.stats import norm
    p_value = float(norm.cdf(z))
    dsr = float(p_value)  # DSR is the probability that true SR > 0 after correction
    
    result = DeflatedSharpeResult(
        sharpe_ratio=float(sr),
        dsr=dsr,
        p_value=p_value,
        n_trials=n_trials,
        significant=p_value < alpha,
        alpha=alpha,
    )
    
    logger.info(f"Deflated Sharpe: SR={sr:.4f}, DSR={dsr:.4f}, p={p_value:.4f}, trials={n_trials}")
    
    return result

def fama_french_5factor_alpha(
    returns: np.ndarray,
    factor_returns: Dict[str, np.ndarray],
    slippage_bps: float = 50,
    alpha: float = 0.05,
) -> Dict[str, Any]:
    """Compute FF5 alpha net of slippage.
    
    Args:
        returns: Strategy excess returns (over RF)
        factor_returns: Dict with keys Mkt-RF, SMB, HML, RMW, CMA
        slippage_bps: Round-trip slippage in bps
        alpha: Significance level for t-test
    
    Returns:
        Dict with alpha, t-stat, p-value, R2, factor loadings
    """
    # Net returns after slippage
    net_returns = returns - (slippage_bps / 10000)  # Daily slippage drag
    
    # Build factor matrix
    factors = np.column_stack([
        factor_returns["Mkt-RF"],
        factor_returns["SMB"],
        factor_returns["HML"],
        factor_returns["RMW"],
        factor_returns["CMA"],
    ])
    
    # Add intercept
    X = np.column_stack([np.ones(len(factors)), factors])
    y = net_returns
    
    # OLS
    try:
        beta, residuals, rank, s = np.linalg.lstsq(X, y, rcond=None)
        alpha_val = beta[0] * 252  # Annualized alpha
        
        # T-stat for alpha
        n, k = X.shape
        mse = np.sum(residuals**2) / (n - k) if n > k else 0
        var_beta = mse * np.linalg.inv(X.T @ X)[0, 0] if mse > 0 else 0
        t_stat = alpha_val / np.sqrt(var_beta) if var_beta > 0 else 0
        
        # P-value (two-sided)
        from scipy.stats import t
        p_value = 2 * t.sf(abs(t_stat), n - k) if n > k else 1.0
        
        # R-squared
        ss_total = np.sum((y - np.mean(y))**2)
        ss_res = np.sum(residuals**2)
        r2 = 1 - ss_res / ss_total if ss_total > 0 else 0
        
        return {
            "alpha_annualized": float(alpha_val),
            "t_stat": float(t_stat),
            "p_value": float(p_value),
            "significant": p_value < alpha,
            "r_squared": float(r2),
            "factor_loadings": {
                "Mkt-RF": float(beta[1]),
                "SMB": float(beta[2]),
                "HML": float(beta[3]),
                "RMW": float(beta[4]),
                "CMA": float(beta[5]),
            },
            "slippage_bps": slippage_bps,
        }
    except Exception as e:
        logger.warning(f"FF5 regression failed: {e}")
        return {
            "alpha_annualized": 0.0,
            "t_stat": 0.0,
            "p_value": 1.0,
            "significant": False,
            "r_squared": 0.0,
            "factor_loadings": {},
            "slippage_bps": slippage_bps,
        }

def max_drawdown(returns: np.ndarray) -> float:
    """Compute maximum drawdown from returns."""
    cumulative = np.cumprod(1 + returns)
    running_max = np.maximum.accumulate(cumulative)
    drawdown = (cumulative - running_max) / running_max
    return float(np.min(drawdown))

def run_full_stat_suite(
    backtest_results: Dict[str, Any],
    factor_data: Optional[Dict[str, np.ndarray]] = None,
    n_trials: int = 100,
) -> Dict[str, Any]:
    """Run complete statistical test suite on backtest results.
    
    Args:
        backtest_results: Dict with keys:
            - "drawdowns_by_sector": Dict[sector, List[drawdowns]]
            - "drawdowns_dotcom": List[float]
            - "drawdowns_ai_boom": List[float]
            - "strategy_outcomes": Dict[strategy, {"win": int, "loss": int}]
            - "returns": np.ndarray of strategy returns
        factor_data: Optional FF5 factor returns for alpha test
        n_trials: Number of trials for DSR
    
    Returns:
        Dict with all test results
    """
    results = {}
    
    # ANOVA: Sector variance in max drawdown
    if "drawdowns_by_sector" in backtest_results:
        results["anova_sector"] = anova_sector_variance(
            backtest_results["drawdowns_by_sector"]
        ).to_dict()
    
    # Welch's t-test: Dot-Com vs AI Boom
    if "drawdowns_dotcom" in backtest_results and "drawdowns_ai_boom" in backtest_results:
        results["welch_dotcom_vs_ai"] = welch_t_test_regimes(
            backtest_results["drawdowns_dotcom"],
            backtest_results["drawdowns_ai_boom"],
        ).to_dict()
    
    # Chi-square: Win/Loss by strategy
    if "strategy_outcomes" in backtest_results:
        results["chi_square_winloss"] = chi_square_winloss(
            backtest_results["strategy_outcomes"]
        ).to_dict()
    
    # Deflated Sharpe Ratio
    if "returns" in backtest_results:
        results["deflated_sharpe"] = deflated_sharpe_ratio(
            backtest_results["returns"],
            n_trials=n_trials,
        ).to_dict()
    
    # FF5 Alpha
    if "returns" in backtest_results and factor_data:
        results["ff5_alpha"] = fama_french_5factor_alpha(
            backtest_results["returns"],
            factor_data,
        )
    
    # Max Drawdown
    if "returns" in backtest_results:
        results["max_drawdown"] = max_drawdown(backtest_results["returns"])
    
    # Save results
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = RESULTS_DIR / f"stat_tests_{timestamp}.json"
    with path.open("w") as f:
        json.dump(results, f, indent=2, default=str)
    
    return results

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    # Synthetic test data
    np.random.seed(42)
    
    # ANOVA: 4 sectors, different drawdown distributions
    drawdowns_by_sector = {
        "Technology": list(np.random.normal(-0.25, 0.08, 20)),
        "Healthcare": list(np.random.normal(-0.15, 0.05, 20)),
        "Financials": list(np.random.normal(-0.30, 0.10, 20)),
        "Consumer": list(np.random.normal(-0.18, 0.06, 20)),
    }
    
    anova_res = anova_sector_variance(drawdowns_by_sector)
    print(f"ANOVA: F={anova_res.f_statistic:.4f}, p={anova_res.p_value:.4f}, sig={anova_res.significant}")
    
    # Welch: Dot-Com vs AI Boom
    dotcom_dd = list(np.random.normal(-0.40, 0.12, 50))
    ai_dd = list(np.random.normal(-0.25, 0.08, 50))
    welch_res = welch_t_test_regimes(dotcom_dd, ai_dd)
    print(f"Welch: t={welch_res.t_statistic:.4f}, p={welch_res.p_value:.4f}, sig={welch_res.significant}")
    
    # Chi-square: 3 strategies
    outcomes = {
        "Static_-10%": {"win": 45, "loss": 55},
        "Dynamic_IV_P25": {"win": 60, "loss": 40},
        "Dynamic_IV_P10": {"win": 55, "loss": 45},
    }
    chi_res = chi_square_winloss(outcomes)
    print(f"Chi-square: chi2={chi_res.chi2_statistic:.4f}, p={chi_res.p_value:.4f}, sig={chi_res.significant}")
    
    # DSR
    returns = np.random.normal(0.0005, 0.01, 500)
    dsr_res = deflated_sharpe_ratio(returns, n_trials=100)
    print(f"DSR: SR={dsr_res.sharpe_ratio:.4f}, DSR={dsr_res.dsr:.4f}, p={dsr_res.p_value:.4f}")
    
    # FF5 Alpha (mock)
    factor_data = {
        "Mkt-RF": np.random.normal(0.0004, 0.01, 500),
        "SMB": np.random.normal(0.0001, 0.005, 500),
        "HML": np.random.normal(0.0001, 0.005, 500),
        "RMW": np.random.normal(0.0001, 0.005, 500),
        "CMA": np.random.normal(0.0001, 0.005, 500),
    }
    ff5_res = fama_french_5factor_alpha(returns, factor_data)
    print(f"FF5 Alpha: {ff5_res['alpha_annualized']:.4f}, t={ff5_res['t_stat']:.4f}, p={ff5_res['p_value']:.4f}")