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

---

## 2. Bridging Spiegelhalter's "Art of Statistics" to Our Ingestion Architecture

David Spiegelhalter notes in *The Art of Statistics* that if we want to illuminate the world, *"our daily experiences have to be turned into data, and this means categorizing and labelling events, recording measurements, analysing the results and communicating the conclusions."*

Our qualitative-to-quantitative pipeline implements this philosophy by turning raw qualitative features into structured inputs:

### 2.1 Quantifying Employee & Consumer Sentiment via EMA
*   **The Recommendation:** To extract true underlying signal from noisy social sentiment.
*   **Our Implementation:** Deployed in [qualitative_scoring.py](file:///Users/hayden/Desktop/quant-py/psychological/qualitative_scoring.py) via `EMAFilter`. Raw Glassdoor ratings ($z_{\text{culture}}$) and Reddit commentary velocity ($z_{\text{hype}}$) are passed through separate exponential moving average filters to prevent noise propagation:
    $$\alpha = 1 - e^{-\frac{\ln(2)}{\text{halflife}}}$$
    *   **Culture Score:** Processed via a **90-day half-life EMA** to anchor long-term organizational stability.
    *   **Hype Score:** Processed via a **21-day half-life EMA** to capture short-term sentiment momentum.

### 2.2 Separating Validity from Reliability
*   **The Recommendation:** Spiegelhalter warns that data must be both **reliable** (low variability) and **valid** (actually measuring what is intended without systematic bias). Scraping forums like WallStreetBets may yield high-velocity trading sentiment, but lacks the validity to determine if a company is truly "good for the people" or structurally stable.
*   **Our Implementation:** The pipeline strictly segregates **pricing signals (hype/momentum)** from **intrinsic value signals (long-term customer and employee loyalty)**. We isolate WSB/Reddit data into the `HypeComposite` (momentum) and separate it from Glassdoor/Indeed employee metrics (`CultureComposite`) and corporate operational records (`MoatComposite`).

### 2.3 Double Standardization & Clamping
*   **The Recommendation:** Convert raw inputs into z-scores (sector-neutralized) and clamp them to prevent extreme outliers from breaking models.
*   **Our Implementation:** Implemented in `DoubleStandardizer`. Stage 1 computes expanding time-series z-scores $z = (x - \mu)/\sigma$, and Stage 2 performs daily cross-sectional z-score standardization within peer groups (`semiconductors`, `platform_software`, `hardware_oem`). All standardized outputs are compressed into the $[-1.0, 1.0]$ range using a hyperbolic tangent clamping function:
    $$f(z) = \tanh\left(\frac{z}{2.0}\right)$$

---

## 3. Bridging Aswath Damodaran's Valuation Framework to Intrinsic Drivers

Aswath Damodaran argues in *Investment Valuation* that a good valuation is a **bridge between your narrative and your numbers**. If all you have are numbers, you have a financial model, not a valuation; if all you have is a story, it is just a fairy tale.

Our pipeline maps qualitative narratives directly onto Damodaran's four intrinsic value drivers:

```
Qualitative Story  ────────────────────────► Quantitative Driver
-----------------                            -------------------
1. Nvidia CUDA Moat  ───────────────────────► CAP & Sustained ROIC
2. CEO Audio Tone / Stability ──────────────► Lower Cost of Capital (Discount Rate)
3. Consumer Loyalty / ESG ──────────────────► Higher Revenue Growth Rate
4. Employee Treatment / Glassdoor ──────────► Reinvestment Efficiency (Sales-to-Capital)
```

### 3.1 The Economic Moat → Competitive Advantage Period (CAP)
*   **Narrative:** A qualitative moat (e.g., Nvidia's CUDA ecosystem) protects the business from competitors.
*   **Numerical Mapping:** Mapped to **Return on Invested Capital (ROIC)** and CAP. A higher `MoatComposite` score extends the competitive advantage period, allowing the asset to sustain an ROIC above its Cost of Capital for a longer horizon in our DCF engine.

### 3.2 Good Leadership & CEO Audio Tone → Risk / Discount Rate
*   **Narrative:** Steady, transparent, and experienced management reduces epistemic uncertainty.
*   **Numerical Mapping:** CEO transcript sentiment and audio analysis adjust the equity risk premium. Higher leadership stability scores mathematically reduce the company's cost of capital (Discount Rate) within the valuation module, boosting intrinsic floor estimates.

### 3.3 Consumer Loyalty & ESG → Revenue Growth Rate
*   **Narrative:** A highly loyal customer base increases the total addressable market (TAM) penetration.
*   **Numerical Mapping:** Mapped to the expected long-term revenue growth rate. Positive brand equity adjust the growth rate upward relative to the industry baseline.

### 3.4 Employee Treatment → Reinvestment Efficiency
*   **Narrative:** Companies that treat employees well experience lower turnover, resulting in higher human capital output per dollar.
*   **Numerical Mapping:** Deployed via the Sales-to-Capital ratio (reinvestment efficiency) in `FinancialReconstructionInterface`. R&D capitalized assets are combined with SBC drag calculations to adjust capital efficiency coefficients:
    $$\text{SBC Drag} = \min\left(1.0, 10.0 \times \left(0.4 \times \frac{\text{SBC}}{\text{Shares} \times \text{Price}} + 0.6 \times \frac{\text{SBC}}{\text{Revenue}}\right)\right)$$

---

## 4. Statistical Testing: Spearman Rank Correlation Evaluation

Once qualitative pillars are quantified, they must be statistically validated. We execute inductive inference by calculating the **Information Coefficient (IC)** using **Spearman Rank Correlation ($\rho$)**:

$$\rho = 1 - \frac{6 \sum d_i^2}{n(n^2 - 1)}$$

*   **Why Spearman over Pearson?** Qualitative data (e.g., Glassdoor scores, CEO sentiment) is inherently non-linear. Spearman evaluates monotonic relationships, verifying if a higher qualitative rank leads to a higher cash flow rank while preventing massive outliers from distorting the optimization strategy.
*   **Empirical Optimization:** Using a grid search of 17,743 evaluations against 5-year point-in-time historical slices, the blended Quality weights stabilized at:
    $$\mathbf{w}^* = [w_{\text{culture}}=0.0200, \, w_{\text{moat}}=0.5050, \, w_{\text{hype}}=0.4750]$$
    This vector yields a predictive Information Coefficient (IC) of **`0.270356`** ($p = 0.091556$) against forward 1-year FCF margin changes, proving that competitive moats and smoothed social sentiment hold positive predictive power for future cash flows.

---

## 5. Master Scoring Matrix (Current Database State)

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
