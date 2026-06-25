# Investment Plan & Valuation Framework

This document outlines the investment methodology, organized into three key analytical pillars: **Psychological**, **Qualitative**, and **Quantitative**. It defines how we generate actionable buy and short signals by identifying contrasts between retail sentiment (psychology) and fundamental value (qualitative + quantitative metrics), and establishes our backtesting and timing framework.

---

## 🏛️ Part 1: The Psychological Pillar
The psychological pillar measures the emotional state, sentiment, and behavior of market participants. It acts as a contrarian indicator, identifying retail panic or irrational euphoria.

### 1. Reddit & WallStreetBets Sentiment
* **Retail Hype Tracking**: Run NLP/sentiment scoring (using VADER or fine-tuned financial LLMs) on subreddits like `r/wallstreetbets` and relevant stock forums.
* **Volume & Discussion Spikes**: Monitor the raw number of mentions and rapid changes in comment velocity to spot emerging retail trends.
* **Panic/Greed Identification**: Measure the ratio of bullish terms ("call", "moon", "long") to bearish terms ("put", "crash", "short") to locate psychological extremes.

### 2. Employee Sentiment & Welfare Scraper
* **Glassdoor & Blind Analytics**: Scrape and analyze rating trends, CEO approval ratings, and workplace culture sentiment.
* **Internal Health**: Internal distress (e.g., mass layoffs, unionization struggles, toxic management discussions) often precedes official financial drops, capturing negative psychological shifts early.

### 3. Developer & Community Affinity
* **Open Source Engagement**: Track GitHub star growth, active contributor count, and issue resolution velocity for tech moats (e.g., CUDA, PyTorch, React). High developer affinity reflects organic psychological backing and technical trust.

---

## 🏛️ Part 2: The Qualitative Pillar
The qualitative pillar assesses the structural durability, business moat, and ethical standing of a company. It ensures we only invest in viable, high-quality businesses.

### 1. Moral Screening & Exclusions
* **Ethical Filters**: Hard exclusion parameters for industries outside our moral scope (e.g., defense contractors, tobacco companies, weapons manufacturers).
* **Corporate Governance**: Review board structures, voting power concentration, and regulatory compliance records.

### 2. Business Moat & Product Quality
* **Switching Costs & Network Effects**: Classify the strength of the company’s lock-in effects.
* **Leadership and Strategy**: Qualitatively analyze execution track records and whether corporate cash flows are reinvested into high-return R&D or wasted on value-destroying operations.

---

## 🏛️ Part 3: The Quantitative Pillar
The quantitative pillar computes the intrinsic mathematical value of the asset and classifies macro-market states.

### 1. DCF Valuation & Intrinsic Value Modeling (Damodaran Methodology)
* **Free Cash Flow Projection**: Model Free Cash Flows to the Firm (FCFF) and Free Cash Flows to Equity (FCFE).
* **Cost of Capital**: Calculate WACC using bottom-up beta calculations (unlevering and levering betas) and live `yfinance` risk premiums.
* **10% Margin of Error (MoE)**: Define a target valuation range. If the stock price is outside this range, it indicates potential mispricing.

### 2. Bond Valuation & Yield Curve Comparisons
* **Yield-to-Maturity (YTM)**: Model fixed-income yield structures relative to par values and inflation rates.
* **Risk Premium Comparisons**: Compare stock dividend yields and earnings yields directly against the bond yield curve (risk-free rates) to justify equity exposure.

### 3. Market State Classification
* **Markov Chains**: Use transition matrices to determine current regimes (e.g., Bullish, Bearish, Sideways) and state transition probabilities.
* **Poisson Distributions**: Model the frequency and impact of extreme macroeconomic shocks.

### 4. Dynamic Asset Allocation Engine
* **Asset Class Rules**: Automate weightings across stocks, bonds, gold, and real estate depending on inflation regimes and market drawdowns.

---

## 🚦 The Contrast Signal Framework (Buy & Short Signals)
To timing the market successfully, we look for **divergences** where the **Psychological (Sentiment)** state directly contradicts the combined **Qualitative + Quantitative (Fundamentals)** state. 

```
                                  DIVERGENCE
  [ Psychological State ] <------------------------> [ Qual + Quant State ]
   (Sentiment/Euphoria/Panic)                        (Moats/Valuation/Yields)
```

### 1. 🟢 The Ultimate Buy Signal
We buy when there is **extreme psychological pessimism** contrasted against **strong qualitative and quantitative fundamentals**:
* **Psychological**: Panic, capitulation, negative Reddit sentiment, low Glassdoor ratings based on short-term noise, high retail put-buying.
* **Qualitative + Quantitative**: High intrinsic value (trading > 10% below DCF), strong developer/community moats, robust cash flows, and positive long-term corporate governance.
* **Rationale**: The crowd is irrationally fearful, but the business is structurally and financially underpriced.

### 2. 🔴 The Ultimate Short / Sell Signal
We short (or sell) when there is **extreme psychological euphoria** contrasted against **weak qualitative and quantitative fundamentals**:
* **Psychological**: High hype, retail FOMO, explosive comment volume, extreme greed.
* **Qualitative + Quantitative**: Severely overvalued relative to DCF, weak or decaying developer affinity, deteriorating cash flow metrics, or severe corporate governance concerns.
* **Rationale**: The crowd is in a bubble, ignoring weak fundamentals and massive valuation premiums.

---

## 🧪 Backtesting Methodology: How to Ensure it Works

Backtesting validates whether our contrast signals reliably capture alphas and successfully time market overvaluations/undervaluations.

### 1. Data Ingestion & Alignment
* **Historical Fundamental Database**: Ingest daily/weekly stock price history alongside quarterly SEC filing data (balance sheets, cash flow statements) to construct historical DCF valuations.
* **Historical Sentiment Archive**: Use historical Reddit/social databases to construct daily sentiment time-series matching the historical periods.

### 2. Signal Simulation Engine
* **Event-Driven Backtest**: Step through historical daily/weekly intervals. 
* At each step, compute:
  1. **Historical DCF Target vs. Stock Price** (Quantitative Value).
  2. **Historical Moat Scores** (Qualitative).
  3. **Historical Sentiment Score** (Psychological).
* Trigger simulated trades (Buy/Short/Hold) when the **Contrast Threshold** is met.

### 3. Performance & Alpha Metrics
We evaluate the backtest using standard quantitative risk-adjusted metrics:
* **Alpha ($\alpha$)**: Measure excess returns above the market benchmark (e.g., S&P 500).
* **Beta ($\beta$)**: Measure systemic risk/volatility relative to the market.
* **Sharpe & Sortino Ratios**: Evaluate return per unit of volatility and downside risk.
* **Maximum Drawdown**: Assess the largest peak-to-trough drop to verify portfolio survivability.

### 4. Profitability & Timing Validation
The goal of the backtest is to confirm that our strategy behaves as an **arbitrage on market sentiment vs. reality**:
* **Timing Validation**: Plot buy/short signals on historical charts to visually and mathematically confirm that signals fire near local troughs (undervalued + fearful) and local peaks (overvalued + euphoric).
* **Profit Realization**: Verify that the profits generated from the differences in these regimes consistently outperform a passive buy-and-hold strategy.
