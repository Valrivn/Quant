# Quant-Py: A Falsification-First Quantitative Research Engine

[![Build Status](https://img.shields.io/badge/tests-150%2B%20passing-brightgreen.svg)]()
[![Validation Protocol](https://img.shields.io/badge/pre--registration-v7%20frozen%20(c80a47bf)-blue.svg)]()
[![Data Integrity](https://img.shields.io/badge/data--audit-PIT%20Compliant%20%7C%20Fail--Closed-success.svg)]()

Quant-Py is a three-pillar quantitative equity research platform built to investigate the intersection of **economic intrinsic value (EVA/ROIC spreads)**, **alternative data discovery**, and **macroeconomic regime switching**. 

Rather than engineering a paper backtest to maximize cosmetic returns, Quant-Py was developed around **adversarial falsification**: systematically auditing quantitative pipelines for lookahead bias, silent fallback contamination, survivorship artifacts, and microstructure friction.

---

## 1. The Core Philosophy: Intrinsic Value vs. Market Pricing

The system separates the investment process into two distinct domains:
* **The Intrinsic Value Layer (Damodaran Framework):** Dictates *what* to buy. Strips GAAP accounting conventions to evaluate Point-In-Time Economic Value Added ($\text{ROIC} - \text{WACC}$) and Free Cash Flow margin resilience.
* **The Regime & Microstructure Layer:** Dictates *when* and *how much* to buy. Uses macro indicators (Yield Curve Slope, Credit Spread Velocities, Realized Volatility) to modulate gross market exposure and protect capital during structural downshifts.

---

## 2. Ingestion Architecture & Data Integrity

All data ingestion paths adhere strictly to open-access government and institutional APIs:

| Sector / Pillar | Data Provider | Provenance & Legal Classification | Temporal Depth & Protocol |
| :--- | :--- | :--- | :--- |
| **Financials & Accounting** | SEC EDGAR (XBRL) | Public Domain (US Gov FOIA) | 2010–Present. Keyed strictly to SEC `filed_date` timestamps. |
| **Macroeconomic Vintages** | St. Louis Fed (FRED) | Public Domain / Open Data | 2003–2026. 9/9 canonical series cached (`BAMLH0A0HYM2`, `STLFSI2`, `T10Y2Y`, etc.). |
| **Intellectual Property** | USPTO PatentsView | Public Domain (US Gov) | Direct bulk parquet extracts; indexed to official `grant_date`. |
| **Healthcare / Trials** | ClinicalTrials.gov / openFDA | Public Domain (US Gov) | API v2; indexed to `results_first_submitted_date`. |
| **Public Sentiment / Code** | GitHub Archive / Wikimedia | Open Data / CC BY-SA | Public APIs used strictly for forward-walk evaluation. |

*Note on Scrapers and Data Ethics:* In compliance with platform Terms of Service and data ethics standards, all non-API web-scraping components have been decoupled from production pipelines. Historical evaluations rely exclusively on verified, point-in-time public datasets.

---

## 3. The Adversarial Audit: How We Dismantled Our Own Backtest

An initial prototype of this system produced headline returns of **21.70% CAGR (+289.08% cumulative net return)**. Because extreme returns in quantitative simulations are almost always the result of hidden lookahead bias or operational bugs, an adversarial codebase audit was conducted via AST/regex static analysis (`backtesting/oos_evaluation.py`).

### Failure Modes Uncovered:
1. **Silent Fallback Contamination:** In `Quantitative/probationary_queue.py`, missing EPS revision data fell back to `return 0.05` and missing operating margin robustness fell back to `return 0.60`. Furthermore, `validation/boundary_hitrate.py` contained a hardcoded `oos_hit_rate = 0.62`. These dummy values allowed ~411 unvetted companies to slip into the portfolio during bull market runs.
2. **Alternative Data Granularity Gap:** While fundamental SEC data possesses true Point-In-Time depth back to 2018, public alternative data feeds are sparse prior to 2022–2023. Running long-window backtests on retroactively imputed qualitative factor panels created an artificial informational advantage.
3. **Turnover Friction & Microstructure:** Standard flat 15 bps fee assumptions severely understated execution drag. Under dynamic square-root slippage:
   $$\text{Cost}_{\text{bps}} = \frac{\text{Spread}}{2} + \text{Fee} + \eta \cdot \sigma_{\text{daily}}\sqrt{\frac{\text{TradeSize}}{\text{ADV}}} \times 10{,}000$$
   Unconstrained daily portfolio rebalancing incurred unsustainable transaction costs.

---

## 4. Engineering Solutions & System Hardening

To eliminate structural self-deception, three core mechanisms were integrated into the execution kernel:

* **Fail-Closed Policy:** All hardcoded defaults were excised. If a candidate asset lacks authentic point-in-time fundamental records, the engine triggers an automatic rejection (`MissingDataException`) rather than an unearned pass.
* **Turnover Hysteresis Deadbands:** Implemented `apply_hysteresis_deadband()` to suppress rebalancing tickets where position drift $|\Delta w_i| \le 15\%$, capping portfolio turnover under 10% monthly.
* **Dynamic Drawdown Regime Shield:** Implemented `apply_drawdown_regime_shield()`:
  * **Normal ($\text{DD} > -8\%$):** Standard 85% equity / 15% cash baseline.
  * **Warning ($-15\% < \text{DD} \le -8\%$):** Equity exposure throttled to 50%; 50% shifted to short-term Treasuries (BIL).
  * **Crisis ($\text{DD} \le -15\%$):** Defensive posture of 15% equity / 85% cash buffer.

---

## 5. Performance Matrix: Pre-Audit vs. Post-Audit Reality

*(Point-In-Time Evaluation: January 31, 2018 – November 29, 2024 | 83 Months | Initial Capital: $10,000.00 USD)*

| Metric | Pre-Audit Arm 3 (Unvetted Fallbacks) | Sanitized Arm 1 (Pure Quant) | Sanitized Arm 2 (Moat Focus) | Sanitized Arm 3 (Hybrid Shielded) | S&P 500 (SPY Benchmark) |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Annualized Return (CAGR)** | 21.70% | **11.57%** | **12.12%** | **11.70%** | 13.00% |
| **Cumulative Net Return** | +289.08% | **+113.26%** | **+120.66%** | **+114.96%** | +132.83% |
| **Annualized Volatility** | 19.44% | **13.94%** | **13.97%** | **15.80%** | 17.25% |
| **Net Sharpe Ratio ($R_f=2\%$)** | 1.01 | **0.72** | **0.75** | **0.66** | 0.68 |
| **Sortino Ratio (Annualized)** | 0.53* | **1.13** | **1.22** | **1.02** | 1.04 |
| **Maximum Drawdown** | -20.98% | **-17.78%** | **-17.03%** | **-20.98%** | -23.93% |
| **Market Beta ($\beta$ vs SPY)** | 0.99 | **0.63** | **0.71** | **0.74** | 1.00 |
| **Down-Market Capture** | 80.5% | **64.3%** | **71.6%** | **76.1%** | 100.0% |
| **Average Monthly Turnover** | 12.36% | **7.16%** | **22.29%** | **12.36%** | 0.00% |

*\*Note: Historical run Sortino divisor bug corrected from monthly to standard $\sqrt{12}$ annualization.*

### Key Takeaway:
Eliminating silent fallbacks reduced nominal CAGR from ~21% to a realistic **11.6%–12.1%**. However, the sanitized strategy successfully demonstrated institutional risk reduction: **cutting portfolio beta to 0.63–0.74 and down-market capture to 64.3%–76.1%**, outperforming the benchmark on capital preservation during market shocks.

---

## 6. Statistical Falsification Stack

* **Multiple Testing Guard (Deflated Sharpe Ratio):** Strategy iterations are cataloged in an immutable ledger (`data/phase9_full/trial_ledger_v5.json`) tracking $N=513$ trials. DSR adjustments explicitly account for selection bias across parameter spaces.
* **Factor Neutralization:** Regressed against Fama-French 5-factor series (Market, SMB, HML, RMW, CMA) to verify that excess returns represent true idiosyncratic alpha rather than unhedged small-cap or momentum beta.
* **Strict Temporal Embargo:** Holdout periods enforce a 3-month embargo purge window to sever autoregressive signal leakage between training and out-of-sample partitions.

---

## 7. Execution & Verification

Run the test suite and validation gates:

```bash
# 1. Run the test suite (150+ unit and contract tests)
python -m pytest tests/test_oos_backtest.py -v

# 2. Run the adversarial mock scan
python -m pytest tests/test_oos_backtest.py -k "test_devil_advocate_mock_detection"

# 3. Execute the sanitized out-of-sample backtest
python -m backtesting.oos_evaluation --run --embargo 90
```

---

## 8. License & Research Disclosure

This repository is published strictly for academic and educational research into econometric validation and data integrity. All data sources comply with US government open-data statutes and public foundation APIs.
