# Quant-Py: A Falsification-First Quantitative Value Research Engine

[![Build Status](https://img.shields.io/badge/tests-150%2B%20passing-brightgreen.svg)]()
[![Validation Protocol](https://img.shields.io/badge/pre--registration-v7%20frozen%20(c80a47bf)-blue.svg)]()
[![Data Integrity](https://img.shields.io/badge/data--audit-PIT%20Compliant%20%7C%20Fail--Closed-success.svg)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

* **Interactive Research Monograph & Retrospective:** [https://valrivn.github.io/Value-Investing-Engine-/](https://valrivn.github.io/Value-Investing-Engine-/)
* **Core Language & Framework:** Python 3.11+ | Polars & NumPy | PyArrow Parquet | SEC EDGAR XBRL

Quant-Py is a three-pillar quantitative equity research platform built to test whether classical **Graham-and-Dodd Value Investing** and **Damodaran Discounted Cash Flow (DCF)** frameworks can be automated in code—combining balance sheet fundamentals with qualitative business indicators to evaluate risk-adjusted performance against the S&P 500.

Rather than designing a backtest to show flattering paper returns, Quant-Py was developed around **adversarial falsification**: systematically auditing the code for lookahead bias, silent fallback defaults, survivorship gaps, and real-world trading friction.

---

## 1. Core Philosophy: Intrinsic Value vs. Market Noise

The system separates the investment process into two clear domains:

* **The Intrinsic Value Layer (Damodaran & Graham Framework):** Dictates *what* to buy. Adjusts standard accounting numbers to evaluate true Point-In-Time Economic Value Added ($\text{ROIC} - \text{WACC}$), capitalizes R&D over 3–5 year amortization schedules, removes company debt to measure real business operating risk, and calculates Free Cash Flow margin durability.
* **The Regime & Friction Layer:** Dictates *when* and *how much* to buy. Evaluates cost-of-capital conditions and real execution drag, enforcing dynamic square-root slippage, 15% turnover hysteresis deadbands, and a mechanical 15% short-term Treasury (BIL) buffer to protect capital during market drawdowns.

---

## 2. Ingestion Architecture & Data Integrity

All data ingestion paths rely exclusively on official public-sector REST APIs and open government datasets. **Zero web scrapers or terms-of-service evasion scripts are permitted in the pipeline:**

| Sector / Pillar | Data Provider | Provenance & Legal Classification | Temporal Depth & Protocol |
| :--- | :--- | :--- | :--- |
| **Financials & Accounting** | SEC EDGAR (XBRL) | Public Domain (US Gov FOIA) | 2010–Present. Keyed strictly to SEC `filed_date` publication timestamps. |
| **Macroeconomic Vintages** | St. Louis Fed (FRED) | Public Domain / Open Data | 2003–2026. 9/9 canonical series cached (`BAMLH0A0HYM2`, `STLFSI2`, `T10Y2Y`, etc.). |
| **Intellectual Property** | USPTO PatentsView | Public Domain (US Gov) | Direct bulk parquet extracts; indexed to official `grant_date`. |
| **Consumer Trust** | CFPB Open Data API | Public Domain (US Gov) | Consumer Financial Protection Bureau complaint velocity. |
| **Public Visibility** | Wikimedia Foundation API | Open Data / CC BY-SA 4.0 | Official REST endpoints; used strictly for forward-walk evaluation. |

> **Methodological Note on Qualitative Data:** While SEC EDGAR financial records provide audited depth back to 2010, alternative public APIs (such as patent acceleration and consumer complaint logs) only provide clean data beginning around 2022. To avoid lookahead and imputation bias, qualitative indicators are quarantined to recent periods, while long-horizon historical backtests rely strictly on point-in-time financial statements.

---

## 3. The Adversarial Audit: How We Dismantled Our Own Backtest

An initial prototype of this system produced headline returns of **21.70% CAGR (+289.08% cumulative net return)** with an apparent Fama-French $\alpha = +14.71\%$ ($t=4.70$). Because extreme backtest returns almost always signal hidden lookahead leakage or software bugs, an adversarial codebase audit was conducted via AST and regex static analysis (`backtesting/oos_evaluation.py`).

### Failure Modes Uncovered:
1. **Placeholder Defaults (Cardboard Dummies):** In `Quantitative/probationary_queue.py`, missing EPS revision data fell back to `return 0.05` and missing operating margin robustness fell back to `return 0.60`. Furthermore, `validation/boundary_hitrate.py` contained a hardcoded `oos_hit_rate = 0.62`. These placeholders quietly granted ~411 unvetted assets free entry into the portfolio during historical market runs.
2. **Restatement Lookahead Bias:** Standard vendor datasets index historical filings to fiscal quarter-end dates (e.g., Dec 31). Because corporate 10-K/10-Q statements are filed 30–60 days later (e.g., mid-February), evaluating trades on quarter-end dates allowed the engine to trade on financial reports that were not yet public.
3. **Trading Costs & Market Impact:** Assuming a flat 0.15% fee was unrealistic. In real markets, larger orders move prices against you. Under a dynamic square-root slippage model:
   $$\text{Trading Cost (bps)} = 0.5 \times \text{Spread} + \text{Base Fee} + 0.10 \times \sigma_{\text{daily}} \times \sqrt{\text{Trade Size} / \text{Daily Volume}} \times 10{,}000$$
   Without accounting for this market impact, frequent trading created severe fee drag during volatile months.

---

## 4. Engineering Solutions & System Hardening

To eliminate structural self-deception, three core engineering mechanisms were enforced:

* **Strict Fail-Closed Policy:** All placeholder defaults were permanently removed. If a candidate asset lacks authentic point-in-time fundamental records, the engine triggers an automatic rejection (`MissingDataException`) rather than an unearned pass.
* **Turnover Hysteresis Deadbands:** Implemented `apply_hysteresis_deadband()` to suppress rebalancing transactions where position drift $|\Delta w_i| \le 15\%$, capping portfolio turnover under 10% monthly.
* **Dynamic Drawdown Regime Shield:** Implemented `apply_drawdown_regime_shield()` reacting to realized peak-to-trough drawdowns:
  * **Normal ($\text{DD} > -8\%$):** Standard 85% equity / 15% cash baseline.
  * **Warning ($-15\% < \text{DD} \le -8\%$):** Equity exposure throttled to 50%; 50% shifted to short-term Treasuries (BIL).
  * **Crisis ($\text{DD} \le -15\%$):** Defensive capital preservation of 15% equity / 85% cash buffer.

---

## 5. Performance Matrix: Pre-Audit vs. Post-Audit Reality

*(Walk-Forward Evaluation: January 31, 2018 – November 29, 2024 | 83 Monthly Rebalance Periods | Initial Capital: $10,000.00 USD | Net of Dynamic Slippage)*

| Metric | Pre-Audit Baseline (Mocks & Lookahead) | Sanitized Arm 1 (Pure Quant) | Sanitized Arm 2 (Moat Focus) | Sanitized Arm 3 (Hybrid Shielded) | S&P 500 (SPY Benchmark) |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Annualized Return (CAGR)** | 21.70% | **11.57%** | **12.12%** | **11.70%** | 13.00% |
| **Cumulative Net Return** | +289.08% | **+113.26%** | **+120.66%** | **+114.96%** | +132.83% |
| **Annualized Volatility** | 19.44% | **13.94%** | **13.97%** | **15.80%** | 17.25% |
| **Net Sharpe Ratio ($R_f=2\%$)** | 1.01 | **0.72** | **0.75** | **0.66** | 0.68 |
| **Sortino Ratio (Annualized)** | 0.53* | **1.13** | **1.22** | **1.02** | 1.04 |
| **Maximum Drawdown** | -20.98% | **-17.78%** | **-17.03%** | **-20.98%** | -23.93% |
| **Market Beta ($\beta$ vs SPY)** | 0.99 | **0.63** | **0.71** | **0.74** | 1.00 |
| **Down-Market Capture** | 80.5% | **64.3%** | **71.6%** | **76.1%** | 100.0% |
| **Up-Market Capture** | 113.3% | **73.9%** | **79.7%** | **81.9%** | 100.0% |
| **Average Monthly Turnover** | 12.36% | **7.16%** | **22.29%** | **12.36%** | 0.00% |

*\*Note: Historical run Sortino divisor calculation corrected from monthly to standard $\sqrt{12}$ annualization.*

### Core Falsification Takeaway:
Eliminating lookahead bias and placeholder defaults moderated nominal CAGR from ~21% to **11.57%–12.12%**. However, the sanitized strategy demonstrated true capital preservation: **reducing market sensitivity to a 0.63 Beta, limiting maximum drawdown to &minus;17.78% (vs. SPY &minus;23.93%), and slashing down-market capture to 64.3%**.

---

## 6. Statistical Falsification Stack

* **Multiple Testing Guard (Deflated Sharpe Ratio):** Strategy iterations are cataloged in an immutable ledger (`data/phase9_full/trial_ledger_v5.json`) tracking $N=513$ trials. DSR adjustments (Bailey & López de Prado) explicitly account for selection bias across the parameter space.
* **Cross-Sectional Permutation Null:** Executes 10,000 cross-sectional asset shuffling passes under identical market dates to establish empirical $p$-values ($p < 0.05$) against the null hypothesis of unearned factor timing.
* **Factor Checks:** Regressed against Fama-French 5-factor series (Market, SMB, HML, RMW, CMA) to verify that excess returns represent genuine stock selection rather than taking unhedged market risk.
* **Strict Temporal Embargo:** Holdout partitions enforce a 90-day embargo purge window to cut signal leakage between training and out-of-sample periods.

---

## 7. Execution & Verification

Run the test suite and validation audits directly:

```bash
# 1. Run the test suite (150+ unit and contract tests)
python -m pytest tests/test_oos_backtest.py -v

# 2. Run the adversarial static mock scan
python -m pytest tests/test_oos_backtest.py -k "test_devil_advocate_mock_detection"

# 3. Execute the full Purged Out-of-Sample evaluation with 90-day embargo
python -m backtesting.oos_evaluation --run --embargo 90

# 4. Run the 3-Arm comparative walk-forward benchmark
python -m backtesting.run_three_arm_comparative_backtest
```

---

## 8. License & Research Disclosure

This project is licensed under the [MIT License](LICENSE).

### Academic Research & Educational Disclaimer
This repository is published strictly for academic, educational, and econometric research into algorithmic validation, data integrity, and Point-In-Time compliance. 

* **No Financial Advice:** Nothing in this repository constitutes investment, financial, legal, or tax advice. All models, simulation results, and backtests are theoretical experiments.
* **No Offer or Solicitation:** This software is not intended for live execution or capital allocation. Any past performance figures reflect historical simulated backtests under strict academic constraints and do not predict future returns.
* **Data Compliance:** All public datasets utilized adhere strictly to US government open-data statutes (SEC EDGAR, Federal Reserve Economic Data) and public foundation APIs.
