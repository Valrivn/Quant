# Bond ETF Blueprint: Strategy & Mathematical Reference

This document defines the strategy, quantitative gates, and mathematical formulations for the Fixed-Income ETF sub-engine within the `quant-py` portfolio pipeline.

---

## 1. Execution Context & Account Constraints

- **Account Type:** Fidelity Individual Youth Account
- **Restrictions:** Individual corporate/municipal bonds are legally restricted. Execution must occur strictly via highly liquid exchange-traded funds (ETFs).
- **Baseline Target Allocation:** 30% of the sub-portfolio allocated to Fixed Income.
- **Fractional Shares:** Fidelity Youth Account supports fractional trading to 3 decimal places. All order drafts output exact fractional share quantities.

---

## 2. Candidate Universe

Only investment-grade and Treasury ETFs are permitted. High-yield ("junk bond") baskets are **permanently excluded** to prevent 2008-style systemic correlation failure.

| Ticker | Category | Expense Ratio | Description |
|:---|:---|:---|:---|
| **BIL** | Core Treasury | 0.14% | 1-3 Month U.S. Treasury Bills |
| **SHY** | Core Treasury | 0.15% | 1-3 Year U.S. Treasury Bonds |
| **IEF** | Core Treasury | 0.15% | 7-10 Year U.S. Treasury Bonds |
| **VCSH** | IG Corporate | 0.04% | Vanguard Short-Term Corporate Bond |
| **VCIT** | IG Corporate | 0.04% | Vanguard Intermediate-Term Corporate Bond |

**Permanently Excluded:** `HYG`, `JNK` — High-yield corporate bonds exhibit equity-like correlation during systemic crashes, defeating their purpose as a defensive anchor.

---

## 3. The Quantitative Pipeline

### Step 1: Liquidity Gatekeeper (Pass/Fail)

Three hard statistical gates. Failure on any single gate disqualifies the ETF:

| Gate | Metric | Threshold | Rationale |
|:---|:---|:---|:---|
| **ADV** | Average Daily Volume | > 1,000,000 shares/day | Prevents high execution premiums |
| **Spread** | Median Bid-Ask Spread | ≤ 0.02% | Prevents hidden transaction costs |
| **NAV** | Premium/Discount to NAV | ±0.10% | Prevents ETF/underlying asset decoupling |

**Critical Trigger:** If Discount to NAV drops below **-0.50%**, flag immediately as an underlying asset liquidity failure (OTC bond freezing or toxic asset decoupling).

### Step 2: Peer-Group Filtering via Z-Score

For Corporate Bond ETFs (VCSH, VCIT), execute a bottom-up look-through of the top 10-20 corporate holdings:

1. **Scrape Holdings:** Extract top corporate issuers from ETF fact sheets.
2. **Pull Financials:** Retrieve EBIT and Interest Expense from SEC EDGAR XBRL for each issuer.
3. **Compute Weighted Interest Coverage Ratio:**
   $$\text{ICR}_{\text{fund}} = \sum_{i=1}^{N} w_i \times \frac{\text{EBIT}_i}{\text{Interest Expense}_i}$$
4. **Z-Score Filter:**
   $$Z_{\text{ICR}} = \frac{\text{ICR}_{\text{fund}} - \mu_{\text{peer}}}{\sigma_{\text{peer}}} \ge +1.0 \quad \text{(significantly safer than average)}$$
   $$Z_{\text{ER}} = \frac{\text{ER}_{\text{fund}} - \mu_{\text{peer}}}{\sigma_{\text{peer}}} \le -1.0 \quad \text{(significantly cheaper than average)}$$
5. **Defensive Overlay:** If no corporate ETF passes both Z-score gates, auto-default to SHY/BIL.

**⚠️ SEC Rate Limiting:** The SEC EDGAR API enforces a strict 10 requests/second limit. The `corporate_look_through.py` module implements `time.sleep(0.1)` throttle per request to prevent IP bans.

### Step 3: Macro-Valuation Triggers (Credit Spread Matrix)

Monitor the credit yield spread between Corporate Bond ETFs and equivalent-maturity U.S. Treasuries:

- FRED Series: `BAA10Y`, `BAMLC0A4CBBB`, `DGS10`
- **Override Logic:** If credit spreads widen while agency ratings remain unchanged → override the rating and flag hidden market-priced default risk.

---

## 4. Data Sources & Fallback Chain

| Data Point | Primary Source | Fallback Source |
|:---|:---|:---|
| ADV, Price | `yfinance` | ETFdb.com (BeautifulSoup) |
| Bid-Ask Spread | `yfinance` | ETFdb.com rolling 30-day median |
| NAV Premium/Discount | ETFdb.com | Computed from (Price - NAV) / NAV |
| FRED Macro Data | FRED public HTML scraping | `pandas_datareader` fallback |
| SEC Corporate Financials | SEC EDGAR XBRL API | Manual CIK lookup with throttling |

---

## 5. Rebalancing Rules

Data collection and auditing run **nightly**. Trade order drafts are generated only on:
- **Friday nights** (weekly scheduled rebalancing), OR
- **Macro-regime trigger** (inflation crossing 4% threshold, or credit spreads widening exponentially)

---

## 6. Folder Structure

```
Quantitative/bonds/
├── __init__.py
├── bond_blueprint.md              ← This document
├── liquidity_gatekeeper.py        # Step 1: ADV, Spread, NAV Pass/Fail
├── bond_etf_screener.py           # Step 2: Z-score peer-group filter
├── corporate_look_through.py      # Bottom-up holdings scraper
├── treasury_anchor.py             # SHY/BIL defensive overlay
├── credit_spread_monitor.py       # Step 3: FRED credit spread matrix
└── tests/
    ├── __init__.py
    ├── test_gatekeeper.py
    ├── test_screener.py
    └── test_credit_spreads.py
```
