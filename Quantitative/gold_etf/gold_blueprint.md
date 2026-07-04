# Gold ETF Blueprint: Strategy & Mathematical Reference

This document defines the strategy, quantitative gates, and mathematical formulations for the Alternative Assets (Gold) ETF sub-engine within the `quant-py` portfolio pipeline.

---

## 1. Execution Context & Account Constraints

- **Account Type:** Fidelity Individual Youth Account
- **Restrictions:** Physical precious metals/bullion desks are legally restricted. Execution must occur strictly via highly liquid exchange-traded funds (ETFs).
- **Baseline Target Allocation:** 20% of the sub-portfolio allocated to Alternative Assets (Gold).
- **Fractional Shares:** Fidelity Youth Account supports fractional trading to 3 decimal places.

---

## 2. Candidate Universe

| Ticker | Expense Ratio | Status | Rationale |
|:---|:---|:---|:---|
| **GLDM** | 0.10% | ✅ Preferred | Lowest expense ratio, optimal for long-term holding |
| **IAU** | 0.25% | ✅ Eligible | Established, highly liquid physical gold ETF |
| **GLD** | 0.40% | ❌ **Permanently Disqualified** | Inefficient expense ratio drag (0.40%) |

---

## 3. The Quantitative Pipeline

### Step 1: Liquidity Gatekeeper (Pass/Fail)

Identical hard gates as Bond ETFs:

| Gate | Metric | Threshold |
|:---|:---|:---|
| **ADV** | Average Daily Volume | > 1,000,000 shares/day |
| **Spread** | Median Bid-Ask Spread | ≤ 0.02% |
| **NAV** | Premium/Discount to NAV | ±0.10% |

### Step 2: Optimal Gold ETF Selection

#### A. Tracking Error Protocol

Calculate the tracking error against physical spot gold (FRED: `GOLDPMGBD228NLBM`):

$$\text{Tracking Error} = \sqrt{\frac{\sum_{i=1}^{n} (R_{\text{ETF},i} - R_{\text{Spot},i})^2}{n-1}}$$

Where:
- $R_{\text{ETF},i}$ = Daily log return of the ETF
- $R_{\text{Spot},i}$ = Daily log return of physical spot gold

#### B. Correlation Gate

The Pearson correlation coefficient between ETF daily returns and spot gold daily returns must satisfy:

$$r \in [0.99, 1.00]$$

Any ETF with $r < 0.99$ is disqualified as it indicates structural drift from the physical asset.

#### C. Annualized Tracking Difference Check (Directional)

Unlike Tracking Error (which uses squared terms and is always positive), the Tracking Difference preserves the mathematical sign:

$$\text{TD}_{\text{annual}} = R_{\text{ETF,annual}} - R_{\text{Spot,annual}}$$

**Interpretation:**
- If $|\text{TD}_{\text{annual}}| \approx \text{Expense Ratio}$: ETF is tracking correctly (expense drag only).
- If $|\text{TD}_{\text{annual}}| > \text{Expense Ratio}$: **FLAG — Derivative Counterparty Risk.** The ETF is structurally underperforming beyond what fees explain, indicating potential synthetic derivative exposure rather than physical gold backing.
- If $\text{TD}_{\text{annual}} > 0$ (ETF outperforms spot): **FLAG — Suspicious.** Physical gold ETFs should never outperform spot after expenses.

### Step 3: Macro-Valuation Triggers (Gold Matrix)

Monitor two FRED macro indicators:

| Indicator | FRED Series | Signal |
|:---|:---|:---|
| **Real Interest Rates** | `DFII10` (10-Year TIPS) | High real rates → Gold is "fundamentally cheap" (inverse correlation) |
| **M2 Money Supply** | `M2SL` | Accelerating M2 expansion → Gold is an "essential systemic shield" |

**Gold Valuation Logic:**
- When real rates rise above +2.0%, gold becomes undervalued relative to historical purchasing power.
- When M2 growth rate exceeds +10% YoY, gold serves as a monetary debasement hedge.

---

## 4. Data Sources & Fallback Chain

| Data Point | Primary Source | Fallback Source |
|:---|:---|:---|
| ETF ADV, Price, Returns | `yfinance` | ETFdb.com (BeautifulSoup) |
| Spot Gold (London Fix) | FRED `GOLDPMGBD228NLBM` (HTML scraping) | `yfinance` ticker `GC=F` (gold futures) |
| Real Interest Rates | FRED `DFII10` (HTML scraping) | `pandas_datareader` |
| M2 Money Supply | FRED `M2SL` (HTML scraping) | `pandas_datareader` |

All FRED data is retrieved via public page scraping with stealth HTTP patterns (randomized delays 2.0s–5.0s, rotating user-agents) to prevent IP bans. No API key required.

---

## 5. Folder Structure

```
Quantitative/gold_etf/
├── __init__.py
├── gold_blueprint.md               ← This document
├── gold_etf_screener.py            # Step 2: Tracking error, correlation, expense filter
├── spot_gold_tracker.py            # FRED spot gold price feed
├── gold_macro_valuation.py         # Step 3: Real rates + M2 expansion monitor
└── tests/
    ├── __init__.py
    ├── test_gold_gatekeeper.py
    ├── test_tracking_error.py
    └── test_macro_valuation.py
```
