# Quantitative Scoring Metrics & Optimization Framework

This document outlines the mathematical formulations, optimization strategies, and statistical scoring frameworks governing the asset conviction translation engine across the target equity universe.

---

## 1. Architectural Strategy & Scoring Pillars

The final **Conviction Score (0–10)** represents a weighted blend of four independent qualitative and quantitative pillars:

$$\text{Conviction Score} = \text{round}\left(10 \times \left(0.30 \times \text{Quality} + 0.25 \times \text{Trajectory} + 0.25 \times \text{Financials} + 0.20 \times \text{Momentum}\right)\right)$$

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    FINAL CONVICTION SCORE (0 - 10)                      │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │
         ┌───────────────────────────┼───────────────────────────┐
         ▼                           ▼                           ▼
 ┌───────────────┐           ┌───────────────┐           ┌───────────────┐
 │ Quality (30%) │           │  Traj. (25%)  │           │ Fin. (25%) &  │
 │  Culture,     │           │   Piecewise   │           │ Momentum(20%) │
 │  Moat, Hype   │           │   Corridors   │           │ R&D / SBC / Z │
 └───────────────┘           └───────────────┘           └───────────────┘
```

### Pillar 1: Quality Score (30% Weight)
The Quality Score evaluates competitive advantages and brand strength. It is a mathematical composite of three sub-branches:
*   **Culture ($z_{\text{culture}}$):** Employee reviews, Glassdoor metrics, and organizational stability. Smooths noise using a 90-day half-life EMA.
*   **Moat ($z_{\text{moat}}$):** Product breadth, network effects, and developer momentum. Smooths noise using a 60-day half-life EMA.
*   **Hype ($z_{\text{hype}}$):** Short-term sentiment velocity and Reddit/social bull-bear ratios. Smooths noise using a 21-day half-life EMA.

The sub-branches are combined using optimized weights and clamped to a unit scale via a hyperbolic tangent function:

$$\text{Quality Score} = \tanh\left(\frac{w_{\text{culture}} \cdot z_{\text{culture}} + w_{\text{moat}} \cdot z_{\text{moat}} + w_{\text{hype}} \cdot z_{\text{hype}}}{2.0}\right)$$

### Pillar 2: Trajectory Score (25% Weight)
Computed by the `TrajectoryCorridorEngine`. It assesses where the company sits relative to its long-term growth corridor:
1.  Calculates the current growth coordinate $z_{\text{growth}}$.
2.  If the coordinate is within normal bounds, it uses a linear mapping.
3.  If the coordinate indicates extreme anomalies ($z > 3.0$), a piecewise decay function is applied to prevent scaling blowouts, ensuring a floor of $0.15$ and a ceiling of $0.92$.

### Pillar 3: Financial Health (25% Weight)
Formulated by the `FinancialReconstructionInterface` to penalize dilutive structures and capitalize innovation:
*   **R&D Capitalization:** Current-year R&D expenditures are capitalized and amortized straight-line (5.0 years for hardware/semiconductors, 4.0 years for platform software) rather than expensed immediately.
*   **Stock-Based Compensation (SBC) Drag:** Measures dilution risk and revenue intensity via:
    $$\text{SBC Drag} = \min\left(1.0, 10.0 \times \left(0.4 \times \frac{\text{SBC}}{\text{Shares} \times \text{Price}} + 0.6 \times \frac{\text{SBC}}{\text{Revenue}}\right)\right)$$
*   **Combined Score:** $\text{Financials} = \text{R\&D Efficiency} \times (1.0 - \text{SBC Drag})$.

### Pillar 4: Momentum (20% Weight)
Tracks YoY changes in peer-group neutralized z-scores, 3-year trailing Free Cash Flow growth rates, and 3-year revenue growth rates.

---

## 2. Branch Weight Optimization & Spearman Metrics

To eliminate human bias, the sub-branch weights within the **Quality Score** were derived by running an exhaustive grid search optimization (17,743 iterations) on the 5-year point-in-time historical baseline dataset (`data/historical_5y_baseline.csv`).

### Optimization Objective
Maximize the **Information Coefficient (IC)**, defined as the **Spearman Rank Correlation ($\rho$)** between the Blended Quality Score and the forward 1-year FCF margin change ($\Delta \text{FCF Margin}_{t+1}$):

$$\rho = 1 - \frac{6 \sum d_i^2}{n(n^2 - 1)}$$

*Where $d_i$ is the difference between the ranks of the blended score and the forward FCF margin change, and $n$ is the number of observations.*

### Why Spearman Rank Correlation?
1.  **Non-Parametric Robustness:** Spearman evaluates monotonic relationships rather than strict linear ones, preventing outliers from distorting the weights.
2.  **Ordinal Alignment:** It aligns the ordinal structure of our final conviction rankings (1 to 10) directly with the ordinal performance of corporate cash flows.

### Optimization Results
*   **Optimal Weights Vector:**
    $$\mathbf{w}^* = [w_{\text{culture}}=0.0200, \, w_{\text{moat}}=0.5050, \, w_{\text{hype}}=0.4750]$$
*   **Information Coefficient (Spearman $\rho$):** `0.270356` ($p = 0.091556$).
*   **Interpretation:** Moat strength ($0.5050$) and Hype momentum ($0.4750$) carry the highest predictive power for forward FCF changes, while Culture ($0.0200$) acts as a stabilizing long-term anchor.

---

## 3. Double Standardization & Mathematical Bounds

Every input metric undergoes a two-stage peer neutralization process:

```
[Raw Ingested Metric]
         │
         ▼
[Stage 1: Time-Series Normalization]  ──►  z_t = (x_t - μ_ts) / σ_ts
         │
         ▼
[Stage 2: Cross-Sectional Neutralization] ──► Neutralize within Sub-Sector
         │
         ▼
[Tanh Clamping & Scaling]             ──► f(z) = tanh(z / 2.0)
```

1.  **Stage 1: Time-Series Normalization**
    $$z_{i,t} = \frac{x_{i,t} - \mu_i}{\sigma_i}$$
    *Where $\mu_i$ and $\sigma_i$ are expanding historical parameters calculated on asset $i$.*

2.  **Stage 2: Cross-Sectional Neutralization**
    The time-series z-scores are standardized daily against their assigned sub-sector peers (`semiconductors`, `platform_software`, or `hardware_oem`) to eliminate macro sector beta:
    $$z_{i,t}^{\text{neutralized}} = \frac{z_{i,t} - \mu_{\text{sector}}}{\sigma_{\text{sector}}}$$

3.  **Hyperbolic Clamping**
    To prevent extreme outliers from dominating the portfolios, all scores are compressed into a $[-1.0, 1.0]$ range:
    $$f(z) = \tanh\left(\frac{z}{2.0}\right)$$

---

## 4. Master Scoring Matrix (Current Database State)

The following matrix represents the final processed scores stored in the database for the 10 target technology equities:

| Ticker | Sub-Sector | Quality Score | Financial Score | Trajectory Score | Momentum | Blended Quality | Fwd FCF Margin Δ | Final Conviction |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **NVDA** | semiconductors | 0.887 | 0.813 | 0.241 | 0.769 | 0.7867 | 0.0254 | **7 / 10** |
| **AVGO** | semiconductors | 0.743 | 0.902 | 0.354 | 0.591 | 0.4593 | -0.0059 | **7 / 10** |
| **AMD** | semiconductors | 0.753 | 0.660 | 0.357 | 0.501 | 0.4856 | 0.0175 | **6 / 10** |
| **MSFT** | platform_software | 0.851 | 0.821 | 0.233 | 0.557 | 0.6841 | -0.0092 | **6 / 10** |
| **GOOGL**| platform_software | 0.817 | 0.519 | 0.418 | 0.568 | 0.6182 | 0.0086 | **6 / 10** |
| **META** | platform_software | 0.808 | 0.494 | 0.383 | 0.678 | 0.5849 | 0.0039 | **6 / 10** |
| **TSLA** | hardware_oem | 0.721 | 0.909 | 0.388 | 0.518 | 0.3704 | 0.0083 | **6 / 10** |
| **AAPL** | platform_software | 0.835 | 0.797 | 0.231 | 0.511 | 0.6571 | 0.0043 | **6 / 10** |
| **AMZN** | platform_software | 0.820 | 0.589 | 0.412 | 0.681 | 0.6182 | 0.0086 | **6 / 10** |
| **INTC** | semiconductors | 0.420 | 0.336 | 0.172 | 0.413 | -0.1216 | 0.0914 | **3 / 10** |
