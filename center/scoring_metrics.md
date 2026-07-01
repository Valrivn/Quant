# Quantitative Scoring Metrics & Optimization Framework

This document outlines the mathematical formulations, optimization strategies, and statistical scoring frameworks governing the asset conviction translation engine across the target equity universe, incorporating David Spiegelhalter’s statistical principles and Aswath Damodaran's valuation matrix.

---

## 1. Segregated Parallel Analytical Lanes (Two-Tier Matrix System)

To avoid the **"Bermuda Triangle of Valuation"** (blending intrinsic valuation drivers with pricing/momentum vectors inside a single linear equation), the portfolio architecture processes assets through 4 strictly segregated parallel analytical lanes. Qualitative inputs act as modulating coefficients for fundamental quantitative parameters rather than additive terms.

```
                  ┌──────────────────────────────────────────┐
                  │ QUANTITATIVE PORTFOLIO ARCHITECTURE GAP  │
                  └────────────────────┬─────────────────────┘
                                       │
         ┌─────────────────────────────┴─────────────────────────────┐
         ▼                                                           ▼
┌─────────────────────────────────┐                         ┌─────────────────────────────────┐
│ Tier 1: Intrinsic Matrix (DCF)  │                         │ Tier 2: Pricing & Momentum      │
│   - expected growth (g)         │                         │   - Companion regressions       │
│   - moat CAP scaling (M)        │                         │   - Pricing deviation           │
│   - Cost of Capital adjustments │                         │   - Execution timing catalyst   │
└─────────────────────────────────┘                         └─────────────────────────────────┘
```

---

## 2. Lane Specifications & Variable Formulations

### 2.1 Lane 1: Intrinsic Valuation Matrix Engine (What to Buy)
Determine true economic value and corporate capital health by eliminating historical growth extrapolation and using endogenous expected growth:

*   **Expected Fundamental Growth ($g$):**
    $$g = \text{Reinvestment Rate (RR)} \times \text{ROIC}$$
*   **Reinvestment Rate (RR):** Calculated by treating R&D as capital expenditure and subtracting R&D amortization:
    $$\text{RR} = \frac{\text{Capital Expenditures} - \text{Depreciation} + \text{R\&D Expense} - \text{R\&D Amortization} + \Delta\text{Non-Cash Working Capital}}{\text{EBIT}(1 - t)}$$
*   **Return on Invested Capital (ROIC):** Capitalizes unamortized R&D assets into the Invested Capital base:
    $$\text{ROIC} = \frac{\text{EBIT}(1 - t)}{\text{Book Value of Debt} + \text{Book Value of Equity} - \text{Cash} + \text{Unamortized R\&D Asset}}$$
*   **Competitive Advantage Period (CAP):** The qualitative `MoatComposite` score ($M \in [0,1]$) scales the horizon years ($N$) where $\text{ROIC} > \text{WACC}$ before fading to terminal baseline levels:
    $$N_{\text{CAP}} = N_{\text{baseline}} \times (1 + \alpha \cdot M)$$
*   **Discount Rate Adjustments:** Synthetic bond rating is mapped using the Interest Coverage Ratio ($\text{EBIT} / \text{Interest Expense}$) to set the automated cost of debt ($R_d$). Leadership stability and organizational culture metrics serve as ERP risk-reduction scaling modifiers.

### 2.2 Lane 2: Market Mood & Relative Multiple Matrix Engine (When to Buy)
Isolates high-velocity, lower-validity sentiment/hype metrics (WSB counts, RSI, moving averages) to track short-term market inefficiencies and timing catalysts:

*   **Companion Variable Cross-Sectional Regressions:** Deploys sector regressions to find justified multiples:
    *   *EV/Sales* controlled for After-Tax Operating Margin.
    *   *Price/Book* controlled for ROE.
    *   *EV/Invested Capital* controlled for ROIC.
*   **Pricing Deviation Delta:** Calculates momentum deltas:
    $$\Delta_{\text{Pricing Deviation}} = \text{Actual Multiple} - \text{Justified Multiple}$$

### 2.3 Lane 3: Uncertainty & Simulation Engine (Probabilistic Risk)
Rejects static points in favor of probability distributions derived from qualitative indicators:
*   Runs a **10,000-pass Monte Carlo simulation** driven by three core variables:
    1. **Target Operating Margin ($M$):** Normal Distribution $\mathcal{N}(\mu_M, \sigma_M)$ where margin volatility $\sigma_M = \sigma_{\text{base}} \times \lambda$ ($\sigma_{\text{base}} = 0.04$). Volatility multiplier $\lambda = 1.0 + \frac{1.5}{1 + e^{-10(R - 0.5)}}$ where Risk Index $R = K_c \times (1.0 - C)$.
    2. **Reinvestment Efficiency / Sales-to-Capital Ratio ($S/C$):** Log-Normal Distribution $\ln\mathcal{N}(\mu_{SC}, \sigma_{SC})$ ($\sigma_{SC} = 0.15$) where expected scale is modulated by $\mu_{SC} = \text{Base Ratio} \times (0.5 + H) \times \text{sc\_penalty}$ to penalize geopolitical supply concentration.
    3. **Competitive Advantage Period ($N_{\text{CAP}}$):** Discrete / Uniform Distribution $\mathcal{U}(A, B)$ where Ecosystem Health $E = M \times (1.0 - A_{\text{tech}})$, Fuzzy Horizon Modifier $H = \frac{1}{1 + e^{-8(E - 0.5)}}$, boundaries $A = \max(3, \text{Round}(3 + H \times 5))$ and $B = \max(A + 2, \text{Round}(5 + H \times 10))$.
*   Integrates macro stress condition variables to model supply chain shock probabilities, localized inflation deviations, and geopolitical risk factors.

### 2.4 Lane 4: Framework Audit & Verification Gate (Compliance Gate)
*   **Leakage Detection:** Ensures no pricing/hype indicators contaminate the intrinsic DCF assumptions (Lane 1), and no cash flow metrics are distorted by short-term sentiment (Lane 2).
*   **Reliability vs. Validity Verification:** Identifies and flags data sources displaying low variability (reliable) but representing incorrect targets (invalid).
*   **Terminal Convergence Check:** Confirms that Year 10 ROIC has converged to a realistic industry baseline (equal to WACC or terminal baseline) so assets aren't endowed with impossible infinite growth mechanics.

---

## 3. Master Scoring & Conviction Matrix

The following matrix represents the final processed scores stored in the database for the 10 target technology equities:

| Ticker | Sub-Sector | Quality Score | Financial Score | Trajectory Score | Momentum | Blended Quality | Fwd FCF Margin Δ | Final Conviction |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **NVDA** | semiconductors | 0.684 | 0.854 | 0.852 | 0.632 | 0.7867 | 0.0254 | **8 / 10** |
| **AAPL** | hardware_oem | 0.662 | 0.893 | 0.864 | 0.508 | 0.6571 | 0.0043 | **7 / 10** |
| **AMZN** | hardware_oem | 0.655 | 0.776 | 0.868 | 0.497 | 0.6182 | 0.0086 | **7 / 10** |
| **AVGO** | semiconductors | 0.620 | 0.907 | 0.814 | 0.531 | 0.4593 | -0.0059 | **7 / 10** |
| **GOOGL**| platform_software | 0.654 | 0.767 | 0.868 | 0.516 | 0.6116 | -0.0023 | **7 / 10** |
| **META** | platform_software | 0.650 | 0.558 | 0.870 | 0.537 | 0.5849 | 0.0039 | **7 / 10** |
| **MSFT** | platform_software | 0.669 | 0.853 | 0.860 | 0.521 | 0.6841 | -0.0092 | **7 / 10** |
| **TSLA** | hardware_oem | 0.609 | 0.805 | 0.810 | 0.530 | 0.3704 | 0.0083 | **7 / 10** |
| **AMD** | semiconductors | 0.624 | 0.604 | 0.817 | 0.520 | 0.4856 | 0.0175 | **6 / 10** |
| **INTC** | semiconductors | 0.461 | 0.384 | 0.100 | 0.382 | -0.1216 | 0.0914 | **3 / 10** |
