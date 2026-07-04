# Quantitative Sorter: Asset Classification & Valuation Dispatcher

This document details the classification logic and rules used by the portfolio pipeline to categorize assets using Aswath Damodaran's Life Cycle Stages and Peter Lynch's Stock Categories. It defines the mathematical parameters, thresholds, and dispatch behavior for downstream quantitative evaluation.

---

## 1. Matrix Mapping Overview

We construct a multi-dimensional mapping to direct assets into the appropriate valuation models:

| Damodaran Life Cycle | Peter Lynch Category | Recommended Valuation Model | Key Math / Constraints |
| :--- | :--- | :--- | :--- |
| **Start-Up / Young Growth** | Fast Growers (Early) | `TOP_DOWN_DCF` | Narrative-driven: TAM forecasting, target operating margin, Sales-to-Capital reinvestment ratio. |
| **High Growth** | Fast Growers (Scaling) | `MULTI_STAGE_DCF` | Endogenous Growth: $g = \text{Reinvestment Rate} \times \text{ROIC}$. CAP-based ROIC decay to Cost of Capital. |
| **Mature Growth** | Stalwarts | `MULTI_STAGE_DCF` (H-Model / Transition) | Linear decay of growth rate and ROIC to stable level over 5-to-10 years. Beta drifts to 1.0. |
| **Mature Stable** | Slow Growers | `GORDON_GROWTH` (Stable DCF) | Growth locked at or below risk-free rate ($g \le R_f$). ROIC $\approx$ WACC. High payout. |
| **Decline** | Turnarounds | `DISTRESS_ADJUSTED_DCF` | Probability of Survival ($p$) vs. Bankruptcy ($1 - p$). Weighting concern DCF and Liquidation Value. |
| **Decline** | Asset Plays | `ASSET_BASED_LIQUIDATION` | Zero going-concern value. Sum-of-the-parts estimation: Fair value of assets minus outstanding debt. |
| **Mature (Wildcard)** | Cyclicals | `NORMALIZED_EARNINGS_DCF` | Average metrics over a 5-to-10 year economic cycle. |

---

## 2. Classification Triggers & Quantitative Rules

### A. Fast Growers (Start-Up, Young, and Scaling Growth)
- **Primary triggers:**
  - Revenue Growth Rate: $\ge 20\%$ annual growth.
- **Sub-categorization:**
  - `TOP_DOWN_DCF`: Operating Margins $< 0$ or FCF $< 0$ (Start-Up / Young Growth). Driven by narrative, TAM, and target scaling margin.
  - `MULTI_STAGE_DCF`: Operating Margins $\ge 0$ (Scaling Stage). Expected growth calculated endogenously: $g = \text{Reinvestment Rate} \times \text{ROIC}$.

### B. Stalwarts (Mature Growth)
- **Primary triggers:**
  - Revenue Growth Rate: between $10\%$ and $20\%$ annually.
  - Market Capitalization: $> \$10\text{ Billion}$.
- **Valuation model:**
  - Transition model (e.g., H-Model) assuming linear growth rate decay over $5\text{ to }10$ years down to macroeconomic stable growth.

### C. Slow Growers (Mature Stable)
- **Primary triggers:**
  - Revenue Growth Rate: $\le 3\%$ (locked at risk-free rate / GNP growth).
  - High Dividend Payout Ratio: $> 50\%$.
- **Valuation model:**
  - `GORDON_GROWTH` where ROIC decays immediately to WACC and Beta drifts to 1.0.

### D. Cyclicals (Commodity/Macro Sensitive)
- **Primary triggers:**
  - Sector Tagging: Auto-tag `autos`, `airlines`, `tires`, `steel`, `defense`, `chemicals`, `natural_resources`.
  - Margin Variance: Standard deviation of operating margins over 5–10 years exceeds $8\%$ with high correlation to macroeconomic cycles.
- **Valuation model:**
  - `NORMALIZED_EARNINGS_DCF`: Earnings/Margins averaged over the trailing 10 years to smooth out economic peaks/troughs.

### E. Turnarounds (Distressed Decline)
- **Primary triggers:**
  - Negative or severely depressed earnings.
  - High Market Debt-to-Capital Ratio: $\ge 70\%$.
  - Plunging Interest Coverage Ratio: $< 1.0$.
  - Low Cash Burn Runway: $\frac{\text{Cash Balance}}{\lvert\text{EBITDA}\rvert} \times 12 < 6\text{ months}$.
- **Valuation model:**
  - `DISTRESS_ADJUSTED_DCF`: Value $= p \times \text{DCF}_{\text{Going Concern}} + (1 - p) \times \text{Liquidation Value}$, where $p$ is the survival probability.

### F. Asset Plays (Asset Accumulation/Liquidations)
- **Primary triggers:**
  - Low Valuation: Price-to-Book Ratio ($P/B$) $< 1.0$.
  - Stagnant operating growth but high cash, real estate, or patent holdings on balance sheet.
- **Valuation model:**
  - `ASSET_BASED_LIQUIDATION`: Total assets revalued at liquidation values minus total liabilities.

---

## 3. Fuzzy Membership Mapping

To prevent sharp, binary shifts at the threshold boundaries, the classifier calculates a membership distribution vector:

$$\vec{w} = [w_{\text{startup}}, w_{\text{high\_growth}}, w_{\text{stalwart}}, w_{\text{slow\_grower}}, w_{\text{cyclical}}, w_{\text{turnaround}}, w_{\text{asset\_play}}]$$

where $\sum w_i = 1.0$. Downstream parameters (e.g., target ROIC, CAP length) are computed as the dot product of the membership weights and the corresponding category defaults.
