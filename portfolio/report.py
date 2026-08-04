"""CEO-facing markdown report for the L3 whole-portfolio allocator."""


def _regime_shifts(walk_forward_weights, tol=1e-3):
    if walk_forward_weights is None or walk_forward_weights.empty:
        return 0
    diffs = walk_forward_weights.diff().abs().sum(axis=1)
    return int((diffs > tol).sum())


def allocator_report(walk_forward_weights, backtest, configs_used):
    """Build a markdown report for the CEO.

    Returns a string summarizing the winning config, walk-forward OOS alpha CI,
    deflated Sharpe, max drawdown vs benchmark, regime shifts, and an L3 gate
    recommendation. No file I/O.
    """
    winning = configs_used[0]["name"] if configs_used else "none"
    objective = configs_used[0]["objective"] if configs_used else "n/a"

    ann_ret = backtest.get("annualized_return")
    ann_vol = backtest.get("annualized_vol")
    sharpe = backtest.get("sharpe")
    max_dd = backtest.get("max_drawdown")

    alpha_line = "n/a"
    alpha_res = backtest.get("alpha")
    if alpha_res is not None:
        alpha_line = (
            f"{alpha_res['alpha_annualized']:.2%} "
            f"(CI [{alpha_res['ci_lower']:.2%}, {alpha_res['ci_upper']:.2%}])"
        )

    dsr = backtest.get("deflated_sharpe")
    dsr_line = f"{dsr['dsr']:.3f}" if dsr else "n/a"

    bench_dd = "n/a"
    excess = backtest.get("excess_vs_sp500")
    if excess is not None:
        bench_dd = f"{excess['excess_annualized']:.2%} excess vs benchmark"

    shifts = _regime_shifts(walk_forward_weights)

    verdict = "APPROVE" if (sharpe and sharpe > 0.5) else "REVIEW"
    lines = [
        "# L3 Whole-Portfolio Allocator Report",
        "",
        f"- **Winning config:** {winning} (objective: {objective})",
        f"- **Walk-forward OOS alpha (FF5)**: {alpha_line}",
        f"- **Deflated Sharpe**: {dsr_line}",
        f"- **Max drawdown**: {max_dd:.2%}" if max_dd == max_dd else "- **Max drawdown**: n/a",
        f"- **Benchmark excess**: {bench_dd}",
        f"- **Regime shifts (material weight changes)**: {shifts}",
        "",
        f"**L3 gate verdict recommendation**: {verdict}",
    ]
    return "\n".join(lines)