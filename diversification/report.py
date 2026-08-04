"""Markdown report for the L2 diversification sleeve backtest."""

import numpy as np
import pandas as pd


def sleeve_backtest_report(results: pd.DataFrame, decisions: pd.DataFrame) -> str:
    """Build a markdown summary of the sleeve backtest for the CEO.

    Covers best/worst sleeve by out-of-sample alpha CI, deflated Sharpe, count
    of regime switches, and a verdict on whether the diversification sleeve
    improves the whole portfolio. Returns a string; no file I/O.
    """
    if results is None or results.empty:
        return "No sleeve backtest results available."

    lines = []
    lines.append("# L2 Diversification Sleeve — Backtest Report")
    lines.append("")

    portfolio_row = results[results["sleeve"] == "portfolio"]
    sleeve_rows = results[results["sleeve"] != "portfolio"]

    if not sleeve_rows.empty:
        alpha_col = "alpha_annualized"
        if sleeve_rows[alpha_col].notna().any():
            best = sleeve_rows.loc[sleeve_rows[alpha_col].idxmax()]
            worst = sleeve_rows.loc[sleeve_rows[alpha_col].idxmin()]
            lines.append(f"Best sleeve by OOS alpha: **{best['sleeve']}** "
                         f"({best[alpha_col]:.2%} ann, CI [{best['alpha_ci_lower']:.2%}, "
                         f"{best['alpha_ci_upper']:.2%}])")
            lines.append(f"Worst sleeve by OOS alpha: **{worst['sleeve']}** "
                         f"({worst[alpha_col]:.2%} ann, CI [{worst['alpha_ci_lower']:.2%}, "
                         f"{worst['alpha_ci_upper']:.2%}])")
        else:
            best = sleeve_rows.loc[sleeve_rows["sharpe"].idxmax()]
            worst = sleeve_rows.loc[sleeve_rows["sharpe"].idxmin()]
            lines.append(f"Best sleeve by Sharpe: **{best['sleeve']}** ({best['sharpe']:.2f})")
            lines.append(f"Worst sleeve by Sharpe: **{worst['sleeve']}** ({worst['sharpe']:.2f})")

        best_dsr = sleeve_rows.loc[sleeve_rows["deflated_sharpe"].idxmax()]
        lines.append(f"Highest deflated Sharpe: **{best_dsr['sleeve']}** "
                     f"({best_dsr['deflated_sharpe']:.2f})")
    else:
        lines.append("No sleeve rows to evaluate.")

    lines.append("")
    if decisions is not None and not decisions.empty and "spread_regime" in decisions.columns:
        switches = 0
        prev = None
        for regime in decisions["spread_regime"]:
            if prev is not None and regime != prev:
                switches += 1
            prev = regime
        lines.append(f"Credit regime switches over the window: **{switches}**")
        lines.append(f"Rebalance decisions recorded: **{len(decisions)}**")
    else:
        lines.append("No decision history available.")

    lines.append("")
    if not portfolio_row.empty:
        p = portfolio_row.iloc[0]
        lines.append("## Verdict")
        if not np.isnan(p.get("alpha_annualized", np.nan)):
            if p["alpha_annualized"] > 0 and p["alpha_ci_lower"] > 0:
                verdict = "The diversification sleeve improves the whole portfolio: positive OOS alpha with a CI above zero."
            elif p["alpha_annualized"] > 0:
                verdict = "The diversification sleeve shows positive OOS alpha, but the CI spans zero — evidence is not conclusive."
            else:
                verdict = "The diversification sleeve does not yet show positive OOS alpha; revisit sleeve construction."
        else:
            if p.get("sharpe", np.nan) > 0:
                verdict = "The diversification sleeve is Sharpe-positive; factor alpha unavailable for a stronger verdict."
            else:
                verdict = "The diversification sleeve is not Sharpe-positive in this window."
        lines.append(verdict)
    else:
        lines.append("## Verdict\nNo portfolio row available for a verdict.")

    return "\n".join(lines)