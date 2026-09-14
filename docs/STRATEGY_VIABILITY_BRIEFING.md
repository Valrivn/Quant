# Strategy Viability Briefing: 4-Stage Value Strategy & Edge-Case Architecture

**Author / Architect:** Nemotron Brain (Pilot) & Gemini 3 Flash (Builder)  
**Date:** September 6, 2026  
**Status:** APPROVED & ARCHITECTURALLY INTEGRATED  
**Experiment Context:** `sentiment-cascade-value-v3`

---

## 1. Executive Summary & Viability Rating

The proposed **4-Stage Dynamic Value & Regime Shield Strategy** is **HIGHLY VIABLE**, offering a disciplined bridge between intrinsic DCF fundamentals, grassroots alternative signal detection, and macro risk-off preservation.

### Core Strengths
- **Decoupled Execution Corridors:** Separates fundamental screening ($IV_{Fair}$, DCF) from entry signals ($P \le 0.90 \times IV_{Fair}$, Analyst Coverage $\le 3$, Grassroots lead $C_t > 0$).
- **Regime-Aware Asset Allocation:** Automatically transitions idle capital into short-term Treasuries ($BIL/SHV$) and physical gold ($GLD$) when no single-stock entry criteria are satisfied.
- **Asymmetric Black-Swan Opportunism:** Capitalizes on market panics ($CRISIS$ state or system-wide selloffs) by relaxing price thresholds to extreme discounts while enforcing strict economic moat stability filters.

---

## 2. Comprehensive 4-Stage Evaluation & Edge Case Architecture

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                        STAGE 1: INTRINSIC VALUE & FUNDAMENTAL GATE                     │
│  • Capitalized R&D + Reconstructed ROIC >= WACC + 2.0%                                 │
│  • Solvency Gate: ICR > 3.0x & Operating Cash Flow > 0                                 │
│  • Profit Margin Health Signal: Trend(Gross Margin) >= 0 & Operating Margin Stability  │
│  • Moat Expansion & Organic Adoption: OMR >= 0.50                                      │
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │
                                            ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                    STAGE 2: ASYMMETRIC ENTRY & LIQUIDITY TRACKING                      │
│  • Buy Trigger: Market Price <= 0.90 * IV_Fair (>= 10% Margin of Safety)               │
│  • Grassroots Lead: C_t > 0, T_t & W_t quiet; Street Coverage <= 3 Analysts             │
│  • Liquidity Filter: Average Daily Volume (ADV_30d) >= $5M & Bid-Ask Spread <= 0.35%   │
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │
                                            ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                        STAGE 3: DISCIPLINED PROFIT HARVEST (EXIT)                      │
│  • Take Profit Trigger: Market Price > 1.15 * IV_Fair (+15% Premium Over Fair Value)   │
│  • Market Hysteria: Bullish Hype P95 or Swarm Activity (T_t / W_t)                     │
│  • Structural Break Exit: Gross Margin Decay > 300 bps YoY OR EVA <= 0                  │
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │
                                            ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                 STAGE 4: MARKOV MACRO SHIELD & BLACK SWAN OPPORTUNISM                  │
│  • State 0 (EXPANSION): Target Equity Allocation 80-100%                               │
│  • State 1 (SHOCK): Target Equity Allocation 40-60% (Rebalance to Treasuries/Gold)     │
│  • State 2 (CRISIS / BLACK SWAN):                                                      │
│    - Idle Capital Fallback: 60% Short-Term Treasuries (BIL/SHV) + 40% Gold (GLD/IAU)  │
│    - Black Swan Opportunistic Buying: Aggressive re-entry at Price <= 0.75 * IV_Fair    │
│      IF Moat Score & Profit Margin Health remain unchanged.                            │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Detailed Edge Case Handling & Specifications

### Edge Case 1: Idle Capital Fallback (Static Gold & Bond Buffer)
- **Problem:** Holding raw cash in low-yield or non-discount regimes causes cash drag and inflationary erosion.
- **Solution:** When zero equity targets satisfy Stage 2 rules:
  - **60% Short-Term Treasuries (e.g., BIL, SHV):** Low duration, capital preservation, yield capture.
  - **40% Gold ETFs (e.g., GLD, IAU):** Monetary hedge and non-correlated store of value during systemic fiat/macro stress.
- **Dynamic Transition:** As soon as an equity target drops into the $\le 0.90 \times IV_{Fair}$ buy corridor, capital is automatically liquidated from the Treasuries/Gold fallback in a tax-efficient, lowest-slippage order.

### Edge Case 2: Black Swan Opportunistic Buying
- **Problem:** Broad market panic triggers mass liquidation, dropping quality companies to deep discounts ($\ge 25\%$ below fair value). Traditional risk models incorrectly sell or freeze buying during high volatility.
- **Solution:**
  1. Detect macro $CRISIS$ state via HMM or rolling 5-day market drawdowns $> 3.5\sigma$.
  2. Activate **Moat Verification Check**: Confirm that gross margins and organic product engagement ($C_t$) have NOT degraded.
  3. If Moat is intact: Increase individual position sizing limits from $5\%$ up to $10\%$, buying aggressively at $P \le 0.75 \times IV_{Fair}$.

### Edge Case 3: Quantitative Health Metric (Profit Margin Trajectory)
- **Problem:** DCF models can be misled by revenue growth that sacrifices profitability (unprofitable unit economics).
- **Solution:** Incorporate **Gross & Operating Margin Trajectory**:
  $$\text{Margin Health Score} = \Delta \text{Gross Margin}_{YoY} + \text{Min}\left(\text{Operating Margin}, 0\right)$$
  - Any company exhibiting Gross Margin degradation $> 300\text{ bps}$ over a 4-quarter rolling window is flagged for structural moat decay and blocked from buying, regardless of valuation discount.

### Edge Case 4: Liquidity Tracking & Execution Drift
- **Problem:** Micro-cap or under-followed equities ($\le 3$ analysts) may suffer high bid-ask spreads and market impact cost.
- **Solution:** Enforce strict liquidity controls:
  $$\text{ADV}_{30d} \ge \$5,000,000 \quad \text{and} \quad \frac{\text{Ask} - \text{Bid}}{\text{Mid}} \le 0.35\%$$
  - Positions are limited to $\le 1\%$ of ADV over a 3-day execution window to eliminate execution drift.

---

## 4. Multi-Agent Execution Architecture

- **Nemotron Brain (Orchestrator & Governance):** Performs top-level routing, monitors HMM state transitions, enforces risk caps, and manages the pre-registered trial protocol.
- **Gemini 3 Flash (Builder & Sensory Layer):** Executes code generation, parses SEC 13F EDGAR filings, computes financial ratios (ROIC, DCF, Margins), runs high-frequency alternative data ingestion pipelines, and builds quantitative strategy modules.
