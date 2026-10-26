# Math_Problem — Complete Mathematical & Statistical Catalog

**Version:** 1.0 | **Date:** 2026-09-24 | **Status:** OBJECTIVE TRANSLATION — No code changes made

---

## Executive Summary

This document catalogs **every mathematical formula, statistical process, and financial model** in the codebase, organized by functional section. Each entry specifies:
- **Concept name** (standard financial/statistical terminology)
- **Mathematical formula** (in LaTeX notation)
- **File location** + **function/class** + **parameter names**
- **Financial purpose** (what it measures/controls)

Followed by an **Audit Section** with questions from a Data Scientist and Quant perspective, and root-cause analysis where structural defects were identified in Phase 7B/8 walk-forward validation.

---

## TABLE OF CONTENTS

| Section | Description |
|---------|-------------|
| **1** | Hidden Markov Models (Regime Detection) |
| **2** | Volatility Models (HAR-|r|, GARCH, Multifractal) |
| **3** | Valuation / DCF / WACC Engines |
| **4** | Portfolio Construction (Sizing, Rebalancing, Swap Barriers) |
| **5** | Exit / Entry / Probationary Logic |
| **6** | Factor Models (PCA, Beta, Total Beta) |
| **7** | Survival / Liquidation Adjustment |
| **8** | Quality Composite & Peak Tracking |
| **9** | Backtest Metrics & Gates (Chi-Square, Sharpe, Calmar, ARI) |
| **10** | Alpha Estimation (FF5, Slippage, Excess vs SPY) |
| **11** | Spike Guards & Data Gates (MAD, S/C Constraint, Quarantine) |
| **12** | Lead-Lag / Cascade Validation (C_t → T_t → W_t) |
| **13** | Monte Carlo & Bootstrap Methods |
| **14** | Audit Questions & Root-Cause Analysis |

---

## 1. HIDDEN MARKOV MODELS (REGIME DETECTION)

### 1.1 Gaussian HMM (3-State) — `Quantitative/hmm_regime.py`

| Element | Specification |
|---------|---------------|
| **Concept** | Gaussian Hidden Markov Model with diagonal covariance via Baum-Welch EM |
| **States** | EXPANSION (0), SHOCK (1), CRISIS (2) — ordered by credit-spread mean |
| **Features (z-scored)** | f0=BAA10Y credit spread, f1=10Y-2Y slope, f2=21d market return, f3=21d realized vol |
| **Training** | Rolling 3-year window, quarterly retrain, 5 restarts, max 200 iterations |
| **Emission** | $\log B_i(x) = -\frac{1}{2} \left[ d \log(2\pi) + \sum \log \sigma_i^2 + \sum \frac{(x-\mu_i)^2}{\sigma_i^2} \right]$ |
| **Forward/Backward** | Scaled log-space for numeric stability |
| **Labeling** | Deterministic: `np.argsort(mu[:, credit_spread])` maps [lowest, middle, highest] → [EXP, SHOCK, CRISIS] |
| **Inference** | Filter smoothing (forward pass only, causal) — `posterior()` uses $\alpha \cdot \beta$ |

**Key Functions:**
- `GaussianHMM.fit(obs, max_iter=200, tol=1e-4, n_restarts=5)` — Baum-Welch EM
- `GaussianHMM._forward(obs)` — scaled forward, returns `(alpha_hat, c, log_lik)`
- `GaussianHMM._backward(obs, c)` — backward pass
- `GaussianHMM.posterior(obs)` — filtered state probabilities $\gamma_t$
- `GaussianHMM.viterbi(obs)` — most-likely path (log-space DP)
- `RegimeDetector.retrain(features, until)` — quarterly re-estimation on trailing window
- `RegimeDetector.detect(features, today)` — auto-retrain + decode

**Config Parameters (pre-registration frozen):**
- `n_states=3`, `train_years=3`, `retrain_days=63`, `seed=0`, `n_restarts=4`

---

### 1.2 Sticky t-HMM (3-State) — `Quantitative/regime_shield_v3.py`

| Element | Specification |
|---------|---------------|
| **Concept** | Multivariate Student-t HMM with Sticky Dirichlet prior + Mahalanobis Dwell Filter |
| **Distribution** | Student-t with $\nu$ degrees of freedom: $\log p(x) = \log \Gamma(\frac{\nu+d}{2}) - \log \Gamma(\frac{\nu}{2}) - \frac{d}{2}\log(\nu\pi) - \frac{1}{2}\log|\Sigma| - \frac{\nu+d}{2}\log(1+\frac{D_M^2}{\nu})$ |
| **Mahalanobis Distance** | $D_M = \sqrt{(x-\mu)^T \Sigma^{-1} (x-\mu)}$ |
| **Sticky Prior** | Dirichlet with $\kappa$ on diagonal: $A_{ij} \propto \xi_{ij} + \kappa \mathbb{I}(i=j)$ |
| **Dwell Filter** | $\tau_{dwell} = \text{clamp}(\tau_{base} \cdot (1 + \frac{\gamma}{D_M + \epsilon}), \tau_{min}, \tau_{max})$ |
| **Transition Check** | Only allow state change if `days_in_current_state >= tau_dwell` |

**Key Functions:**
- `StickyHMMWithDwell.monthly_reestimate(history_df)` — MLE with sticky prior, VIX-mean ordering
- `StickyHMMWithDwell.daily_nowcast(daily_obs)` — forward filter with frozen params
- `StickyHMMWithDwell.compute_mahalanobis(x_t, state_idx)` — Mahalanobis distance
- `StickyHMMWithDwell.compute_dwell_days(mahalanobis_dist)` — dwell calculation
- `StickyHMMWithDwell.should_transition(new_state, days_held, mahal)` — dwell gate

**Config Parameters (frozen in `pre_registration_v3.yaml`):**
- `df=6`, `sticky_kappa=100`, `observables=[BAA10Y_spread, VIX, DGS10_10Y2Y_slope]`
- `dwell_filter: {enabled=True, tau_base=10, gamma=2.0, tau_min=3, tau_max=15, epsilon=1e-6}`

---

### 1.3 2-State t-HMM (Phase 8 Redesign) — `Quantitative/regime_shield_v2.py` (referenced)

| Element | Specification |
|---------|---------------|
| **States** | EXPANSION (0), CRISIS (1) |
| **Labeling Rule** | `VIX_mean_ascending` — CRISIS must have higher VIX mean |
| **Validation Gates** | `validation/hmm_gates.py` |
| **Crisis Precision/Recall** | Label: BAA10Y > 300bps; Predict: state==1; Thresholds: precision>0.80, recall>0.70 |
| **State Stability (ARI)** | Adjusted Rand Index across walk-forward folds; threshold: min_ARI > 0.80 (Phase 4: >0.70) |
| **Phase 4 Walk-Forward ARI** | Refit compact Gaussian HMM (k=3) per fold vs persisted states; fail if ≥2 folds below bar |

---

## 2. VOLATILITY MODELS

### 2.1 HAR-|r| (Heterogeneous Autoregressive Absolute Returns) — `Quantitative/multifractal_vol.py`

| Element | Specification |
|---------|---------------|
| **Model** | $E[|r_t|] = \beta_0 + \beta_1 |r_{t-1}| + \beta_5 \frac{1}{5}\sum_{i=1}^5 |r_{t-i}| + \beta_{22} \frac{1}{22}\sum_{i=1}^{22} |r_{t-i}|$ |
| **Daily Vol** | $\hat{\sigma}_{daily} = \hat{E}[|r_t|] \cdot \sqrt{\pi/2}$ (normality assumption: $E|r| = \sigma\sqrt{2/\pi}$) |
| **Horizon Scaling** | $\sigma_\tau = \sigma_{daily} \cdot \sqrt{\tau}$ |
| **Sector Multiplier** | $k_{sector}$ from overrides (e.g., Tech: 2.2, Energy: 1.5) |
| **Regime Multiplier** | $k_{regime} \in \{EXP:1.0, SHOCK:0.8, CRISIS:0.5\}$ |
| **Adjusted Vol** | $\sigma_{adj} = \sigma_\tau \cdot k_{sector} \cdot k_{regime}$ |

**Key Functions:**
- `HARAbsVol._prepare_har_features(abs_ret)` — constructs lag-1, lag-5, lag-22 features
- `HARAbsVol.estimate(returns_series, date, tau, sector, window=252)` — raw fractal vol
- `HARAbsVol.estimate_with_regime(..., regime, sector)` — applies regime multiplier
- `HARAbsVol.nested_cv_calibrate(returns_panel, sectors)` — walk-forward outer CV + time-series inner CV grid search over `tau ∈ {5,10,20}`, `k ∈ {1.2,1.5,1.8,2.0,2.2}`

**Sector Overrides (frozen):**
```python
{"Tech": {"tau": 10, "k": 2.2}, "Energy": {"tau": 20, "k": 1.5},
 "Financial": {"tau": 20, "k": 1.6}, "default": {"tau": 20, "k": 1.8}}
```

---

### 2.2 GARCH(1,1) IV Nowcast — `Quantitative/iv_nowcast.py`

| Element | Specification |
|---------|---------------|
| **GARCH(1,1)** | $\sigma_t^2 = \omega + \alpha r_{t-1}^2 + \beta \sigma_{t-1}^2$ |
| **MLE** | Negative log-likelihood: $-\sum \log(2\pi\sigma_t^2) - \sum r_t^2/\sigma_t^2$ |
| **Bounds** | $\omega \in [1e-8, 1e-2]$, $\alpha \in [1e-4, 0.3]$, $\beta \in [0.5, 0.98]$ |
| **Duration Adjustment** | $IV_{nowcast} = IV_{baseline} \cdot (1 - D_{WACC} \cdot \Delta WACC)$ |
| **Daily Interpolation** | $IV_{daily} = IV_{duration} \cdot \sqrt{\frac{var_{daily}}{var_{weekly}}}$ (clipped to [0.70, 1.40]) |

**Key Functions:**
- `GARCHIVNowcast.fit(returns_series)` — L-BFGS-B optimization
- `GARCHIVNowcast.interpolate_iv(weekly_iv, date, daily_returns, d_wacc, delta_wacc)` — full nowcast
- `analytical_duration_adjustment(iv_baseline, d_wacc, delta_wacc)` — closed-form Taylor

---

### 2.3 Multifractal Volatility — `Quantitative/har_abs_vol.py` (referenced)

| Element | Specification |
|---------|---------------|
| **Estimator** | HAR on absolute returns across horizons $\tau \in \{5,10,20\}$ |
| **Sector Calibration** | Nested CV per sector for $(\tau, k)$ parameters |
| **Regime Integration** | `estimate_with_regime()` applies $k_{regime}$ multiplier |

---

## 3. VALUATION / DCF / WACC ENGINES

### 3.1 DCF Nowcaster (Decoupled Architecture) — `Quantitative/dcf_nowcaster.py`

| Element | Specification |
|---------|---------------|
| **FCF Matrix (Low-Freq)** | Updated ONLY on SEC filing dates (PIT) |
| **R&D Capitalization** | $RD_{amort} = RD_{expense} / Y_{cap}$; $EBIT_{adj} = EBIT + RD_{expense} - RD_{amort}$ |
| **SBC Drag** | $FCF = NOPAT + D\&A - CapEx - \Delta NWC - SBC_{expense}$ |
| **FCF Projection** | $FCF_t = FCF_0 \cdot (1+g)^t$ for $t=1..N$; Terminal = $FCF_N \cdot (1+g_T) / (WACC - g_T)$ |
| **Analytical Duration** | $D_{WACC} = \frac{1}{PV} \sum_{t=1}^N \frac{t \cdot FCF_t}{(1+WACC)^{t+1}} + \frac{N \cdot Terminal}{(1+WACC)^{N+1}}$ |
| **WACC Engine (High-Freq)** | Daily: $WACC = w_e \cdot k_e + w_d \cdot k_d \cdot (1-\tau)$ |
| **Cost of Equity** | $k_e = r_f + \beta \cdot ERP$ (60M beta, option-implied ERP in Phase 8) |
| **Nowcast Formula** | $IV_t \approx IV_0 \cdot (1 - D_{WACC} \cdot \Delta WACC)$ |

**Key Classes/Functions:**
- `FCFMatrix.update_on_filing(ticker, filing_date, companyfacts_row, ...)` — PIT update
- `WACCEngine.compute_daily_wacc(ticker, date)` — daily WACC
- `DurationNowcaster.nowcast_iv_daily(ticker, date)` — analytical nowcast

---

### 3.2 DCF Screen (Stage 1) — `Quantitative/dcf_screen.py`

| Element | Specification |
|---------|---------------|
| **R&D Capitalization** | $R\&D_{cap} = R\&D_{expense} / 0.15$ (Damodaran) |
| **FCF** | $FCF = EBIT(1-\tau) + D\&A - CapEx - \Delta NWC + R\&D_{expense} - R\&D_{amort}$ |
| **Invested Capital** | $IC = Debt + BookEquity - Cash + R\&D_{cap}$; $BookEquity = Revenue \cdot sector\_multiplier$ |
| **ROIC** | $ROIC = FCF / IC$ |
| **Solvency** | $(EBITDA - CapEx) / Total Debt$ |
| **Margin Health** | $EBIT Margin \cdot (1-\tau)$ |
| **Monte Carlo DCF** | 10,000 passes: $g \sim N(0.05, 0.02)$, $WACC \sim N(0.08, 0.015)$, $TerminalMult \sim N(15, 3)$ |
| **Entry Threshold** | $Price \le IV_{P25} \cdot (1 - MOS)$; $MOS=10\%$ default |
| **Sector Discount** | $discount_{sector} = \max(0.08, 1.2 \cdot \sigma_{sector\_historical})$ |

**Pass Criteria (evaluate_dcf_screen):**
1. $Price \le FairValue \cdot (1 - MOS)$
2. $ROIC > WACC_{MC\_mean}$ (8%)
3. $Solvency > 1.0$
4. $MarginHealth$ available (degraded if missing)

---

### 3.3 Narrative Valuation (Phase 8) — `Quantitative/narrative_valuation.py`

| Element | Specification |
|---------|---------------|
| **Horizon** | 10 years = 7 transition + 3 mature |
| **Young (<5y public)** | $g_t$ linear: $g_{start} \to g_{industry}$ over 7y; $RR_t = \max(0, \Delta Rev / S_C / NOPAT_t)$ |
| **Mature** | $g_t = r_f - \pi$ (steady); $RR_t = \text{clamp}(g_t / ROIC, 0, 0.95)$ |
| **Terminal Value** | $TV = FCF_{10} \cdot (1+g_T) / (WACC - g_T)$; **Guard**: $WACC - g_T \ge 50bps$ else $g_T = WACC - 50bps$ |
| **Unfunded Growth** | $Required = \Delta Rev / S_C$; $Available = OCF + Cash + CreditCapacity$; $CreditCapacity = \text{target\_D/E} \cdot Assets - Debt$; if $Required > 1.2 \cdot Available$: $g_T \times 0.5$, $WACC + 200bps$ |
| **S/C Resolution** | Young → industry S/C; Mature → company 3y median S/C; fallback → cross-sectional P95; winsorize $[1e-6, 10]$ |
| **Reinvestment Identity (C6.1)** | $RR_t = (\Delta Rev_t / S_C) / EBIT(1-\tau)_t$; $Growth_t = RR_t \cdot ROIC_t$; $FCF_t = EBIT(1-\tau)_t \cdot (1 - RR_t)$ |

**Key Functions:**
- `reinvestment_rate_v2(delta_rev, sc_ratio, ebit, tax_rate)` — corrected RR formula
- `revenue_growth_path(g_start, g_industry, transition_years, horizon)` — linear convergence
- `terminal_value(fcf_10, g_terminal, wacc)` — with 50bps guard
- `unfunded_growth_check(delta_rev, sc_ratio, ocf, cash, credit_capacity)` — 1.2x threshold
- `compute_narrative_valuation(...)` — full pipeline returning `NarrativeValuationResult`

---

### 3.4 WACC Engine v2 (Phase 8) — `ingestion/wacc_engine_v2.py` (referenced)

| Element | Specification |
|---------|---------------|
| **Option-Implied ERP** | $ERP = VIX / \sqrt{12} - r_f$ (Defect 1 fix) |
| **Term Structure** | 3-state t-HMM regime conditioning on WACC components |
| **Omega Stress** | $\Omega = 1 + w_{cb} \cdot stress_{cashburn} + w_{icr} \cdot stress_{icr} + sector\_factor$; clamped $[1.0, 3.0]$ |
| **Beta** | Total Beta with sigmoid ceiling: $\beta_{total} = 5.0 / (1 + \exp(-\beta_{raw}/3.0))$ |
| **Correlation Floor** | $\rho_{used} = \max(\rho_{market\_60m}, 0.25)$; negative $\rho$ clamped to floor |

---

## 4. PORTFOLIO CONSTRUCTION

### 4.1 Position Sizing & Rebalancing — `Quantitative/sizing_rebalancing.py`

| Element | Specification |
|---------|---------------|
| **Base Position** | 5% of equity |
| **Conviction Multipliers** | High: 1.5x, Medium: 1.0x, Low: 0.5x |
| **Hard Caps** | Max single: 15%, Max sector: 30% |
| **Dry Powder** | Baseline 10%, Crisis Tier1 25%, Crisis Tier2 75-100% |
| **Regime Allocation** | EXP: 80-100% equity; SHOCK: 40-60%; CRISIS: 60% BIL / 40% GLD |
| **Conviction Logic** | High if C_t accelerating >1.2x baseline; Low if exit triggered |
| **Rebalancing** | 2% drift bands, min $1000 trade, 15 bps cost budget |

**Key Functions:**
- `get_regime_equity_target(regime, hmm_crisis_prob)` — returns (equity_pct, fallback_pct, cash_pct)
- `determine_conviction(entry_signal, exit_signal, black_swan_tier)` — conviction level
- `compute_base_target_weight(conviction, config, regime, hmm_crisis_prob)` — applies regime adjustment
- `enforce_sector_caps(targets, max_sector_pct=0.30)` — proportional scaling
- `check_rebalance_needed(targets, band_width=0.02, min_trade_size=1000)` — drift + cost estimation

---

### 4.2 Exponential Swap Barrier — `Quantitative/swap_barrier.py` (Phase 8)

| Element | Specification |
|---------|---------------|
| **Barrier Formula** | $B(t) = (C \cdot e^{-\gamma \cdot t / t_{target}} + Penalty_{moat}) \cdot M_{regime}$ |
| **Parameters (frozen)** | $C=150bps$, $\gamma=0.5$, $t_{target}=24$ months, $\epsilon=1e-6$ |
| **Moat Penalty** | $+10bps$ per 0.1 OMR above 0.5 threshold; fallback 100bps if moat protected but OMR unknown |
| **Regime Multipliers** | EXP: 1.00, RECOVERY: 1.15, SHOCK: 1.25, CRISIS: 1.50 |
| **Swap Decision** | Swap if $(upside_j - upside_i) \cdot 10000 > B(t) + \epsilon$ |
| **Moat Eligibility** | $ROIC > WACC + 2\%$ AND $OMR \ge 0.5$ |

**Key Functions:**
- `ExponentialSwapBarrier.calculate_barrier_bps(holding_months, omr, is_moat_protected, regime)`
- `ExponentialSwapBarrier.should_swap(upside_i, upside_j, holding_months, omr_i, is_moat_protected, regime)`
- `evaluate_swap_candidates(holdings, candidates, asof_date, regime_df, ledger, config_path)` — full evaluation with trade audit

---

### 4.3 Bidirectional Swap Barrier v2 — `Quantitative/swap_barrier_v2.py`

| Element | Specification |
|---------|---------------|
| **Barrier** | $B(t) = C \cdot e^{-\gamma \cdot t / t_{target}} + (moat\_penalty \text{ if moat else } 0)$ |
| **Entry Thresholds** | EXP: 0.85, SHOCK: 0.75, CRISIS: 0.65 (P/IV) |
| **Stop-Loss Reentry** | Bypass barrier if recently stop-lossed (20-day lookback) |
| **Cash-Aware** | Evaluates cash entry gates independently |

---

### 4.4 Exit Scheduler — `Quantitative/exit_scheduler.py`

| Element | Specification |
|---------|---------------|
| **TWAP Exit** | Max 5% ADV per day, staggered over 3 days |
| **Correlation Clustering** | Group correlated exits to manage concurrent liquidation |
| **Regime Acceleration** | CRISIS regime accelerates exit schedule |

---

## 5. EXIT / ENTRY / PROBATIONARY LOGIC

### 5.1 Exit Engine (Stage 3) — `Quantitative/exit_engine.py`

| Element | Specification |
|---------|---------------|
| **Take Profit** | $Price > 1.15 \times IV_{Fair}$ (IV_P50) |
| **Hysteria Exit** | Hype P95 > 0.95 OR T_t/W_t swarm OR $Price > 1.25 \times IV_{Fair}$ |
| **Structural Break** | Gross Margin decay > 300bps YoY OR EVA ≤ 0 |
| **EVA** | $NOPAT - (Invested Capital \times WACC)$; $IC \approx Debt + MarketCap - Cash$ |
| **Margin Decay** | $(Margin_{prior} - Margin_{current}) \times 10000$ bps |
| **Priority** | Structural Break > Hysteria > Take Profit |

---

### 5.2 Entry Timing (Stage 2) — `Quantitative/entry_timing.py`

| Element | Specification |
|---------|---------------|
| **Asymmetric Corridor** | $[IV_{P25} \cdot (1 - discount_{sector}), IV_{P25}]$ |
| **C_t Grassroots Velocity** | 6 sources: GDELT, GitHub, EDGAR 13F, Ken French, Google Trends, Kaggle Analyst |
| **C_t Composite** | Weighted avg: $w = [0.20, 0.20, 0.15, 0.15, 0.15, 0.15]$; normalized over available sources |
| **C_t Acceleration** | $composite > EMA_5(composite)$ |
| **T_t / W_t Quiet** | Top-down macro (HMM) and Wall Street (analyst) signals must be quiet |
| **Liquidity Gates** | ADV ≥ 1M shares; Spread ≤ 2bps (20-day median) |
| **Entry Logic** | In corridor AND C_t accelerating AND T_t/W_t quiet AND liquidity OK |

**C_t Lead-Lag Validation (C1 Gate):**
- $\rho(C \to T, k<0) > 0.1$ AND $\rho(T \to W, k<0) > 0.1$ at negative lags up to 10 days
- `validate_cascade_lead_lag(c_t_series, t_t_series, w_t_series, max_lag=10)`

---

### 5.3 Probationary Queue — `Quantitative/probationary_queue.py`

| Element | Specification |
|---------|---------------|
| **Staging** | 10-20 days holding before active entry |
| **Gate 1** | Forward EPS revisions slope ≥ 0 over 30 days |
| **Gate 2** | Operating Margin Robustness (OMR) ≥ 0.50 |
| **Gate 3** | Volatility Contraction: $|r| < \text{trailing 5-day avg}$ for 3 consecutive days |
| **Decision Rule (Phase 3)** | Weighted OR: $score = \sum w_i \cdot check_i$; pass if $score \ge 0.5$; weights: {asymmetric_entry: 0.4, intrinsic_value: 0.4, disciplined_exit: 0.2} |

**Phase 3 Validation (validation/probation_gates.py):**
- **Band Test**: Two-sided binomial exact on pass rate $\in [0.40, 0.60]$
- **Min Triggers**: 400
- **Capital Ramp**: 25%/50%/75%/100% over quarters 1-4
- **Anti-Drift**: On-edge rate (0.40 or 0.60 exactly) fails statistical interiority test

---

## 6. FACTOR MODELS

### 6.1 15-Factor PCA Pipeline — `Quantitative/factor_model_pca.py`

| Element | Specification |
|---------|---------------|
| **Factors (15)** | Term Spread, Credit Spread, VIX, Realized Vol 21d, ERP (VIX/√12 - Rf), Yield Curve Slope, Inflation 5Y5Y, Sector Breadth, Put/Call Ratio, DXY, Commodity Momentum, Quality Spread, HY Spread, Liquidity Proxy, Financial Stress |
| **Pre-Whitening** | AR(1): $e_t = x_t - \rho x_{t-1}$; $\rho \in [-0.95, 0.95]$ |
| **Rolling Z-Score** | 252-day lookback, causal (PIT) |
| **PCA** | 15 components, minimum 85% variance explained |
| **State Label Stability** | Adjusted Rand Index across walk-forward folds; threshold ARI > 0.70 |

**Key Functions:**
- `pre_whiten_ar1(df)` — per-column AR(1) residualization
- `rolling_zscore(df, lookback=252)` — causal z-scoring
- `FactorModelPCA.fit_transform(factor_df)` — full pipeline
- `FactorModelPCA.check_state_label_stability(fold_states)` — ARI audit

---

### 6.2 Total Beta (Phase 8) — `Quantitative/total_beta.py`

| Element | Specification |
|---------|---------------|
| **Unlevered Beta** | 7 custom-sector medians from Damodaran 94-industry (2026.09) |
| **Hamada Levering** | $\beta_L = \beta_U \cdot (1 + (1-\tau) \cdot D/E_{net})$; net-debt basis |
| **Financials Cap** | $D/E$ capped at 0.60 (sector P95 proxy) before levering |
| **Correlation Floor** | $\rho_{used} = \max(\rho_{60m}, 0.25)$; negative $\rho$ clamped to floor |
| **Raw Total Beta** | $\beta_{total\_raw} = \beta_L / \rho_{used}$ |
| **Sigmoid Ceiling** | $\beta_{total\_adj} = 5.0 / (1 + \exp(-\beta_{total\_raw} / 3.0))$ |
| **Application Criteria** | Small cap (<$2B) OR private OR concentrated (>3% NAV) OR low ADV (<$5M) |

**Key Functions:**
- `total_beta_raw(beta_levered, rho_market_60m)` — raw ratio with floor
- `total_beta_adjusted(beta_levered, rho_market_60m)` — sigmoid ceiling
- `total_beta_applies(market_cap, adv_usd, position_pct_nav, is_private)` — criteria check
- `compute_beta_wacc_columns(df, ...)` — per-filing batch computation

---

### 6.3 Beta / Factor Calculations — `valuation_alpha/ratios.py`, `valuation_alpha/alpha.py`

| Element | Specification |
|---------|---------------|
| **Peer Percentiles** | Cross-sectional rank within sector (0-100) |
| **Mahalanobis Distance** | Z-score metrics cross-sectionally; $D_M = \sqrt{Z^T \Sigma^{-1} Z}$; Ledoit-Wolf shrinkage if $\text{cond}(\Sigma) > 10^{12}$ |
| **FF5 Residual Alpha** | Monthly regression: $r - RF = \alpha + \beta_{MKT}(MKT-RF) + \beta_{SMB}SMB + \beta_{HML}HML + \beta_{RMW}RMW + \beta_{CMA}CMA + \epsilon$; annualized $\times 12$ |
| **Covariance Guard** | Ledoit-Wolf shrinkage if $\text{cond}(X^TX) > 10^{12}$ |
| **Slippage** | $fee = |\Delta w| \cdot slippage$; charged only when $|\Delta w| > 0.05$ |

---

## 7. SURVIVAL / LIQUIDATION ADJUSTMENT

### 7.1 Survival Adjustment — `Quantitative/survival_adjustment.py`

| Element | Specification |
|---------|---------------|
| **p_survival Priority Cascade** | 1. ICR < 1.5 → 0.40 (distressed); 2. years_public < 5 → $0.60 + 0.05 \cdot years$ (young ramp); 3. ROIC - WACC > 5% → 0.95 (moat); 4. else → 0.90 (mature) |
| **Clamp** | $[0.05, 0.95]$ |
| **Liquidation Value (Equity Basis)** | $LV = 0.70 \cdot (Equity - Intangibles - Cash) + (Cash - Debt)$; NO cash double-count |
| **Survival-Adjusted IV** | $SAIV = p \cdot IV_{GC} + (1-p) \cdot LV_{ps}$; capped: $SAIV \le IV_{GC} \cdot 1.0001$ |
| **Position Cap** | $p_{survival} < 0.70 \to \max\_weight = 0.5\%$ |

---

## 8. QUALITY COMPOSITE & PEAK TRACKING

### 8.1 Quality Composite Score (QCS) — `Quantitative/quality_composite.py`

| Element | Specification |
|---------|---------------|
| **Quant Pillar (60%)** | ROIC-WACC Spread (25%), FCF Margin (25%), ICR capped [0.5,10] (20%), Rev Quality: -Accruals + CashConv (15%), Balance Sheet: -D/E + CashRunway (15%) |
| **Qual Pillar (40%)** | GH Velocity (25%), ATS Hiring (25%), Patent Citation (20%), RPO Growth (15%), MDA Tone (15%) |
| **MAD Z-Score** | $Z = 0.6745 \cdot (x - median) / MAD$; winsorize at 1%/99% |
| **Qual Coverage Gate** | If sector qual coverage < 20% → Qual weight = 0, Quant weight = 100% |
| **Premium Entry** | $QCS > 2.0$ AND $Price/IV_{P25} > 1.10$ AND $OMR \ge 0.5$ → 0.90x IV allowance |

---

### 8.2 Peak Threshold Tracker — `Quantitative/peak_threshold_tracker.py`

| Element | Specification |
|---------|---------------|
| **Lookback** | 5 years (1260 trading days) |
| **Percentiles** | P90 (alert), P95 (trim alert) |
| **Sector Blending** | 50% ticker + 50% sector for histories 500-1260 days |
| **NVDA Discount** | Track $Price$ where $Price/IV_{P25} < 0.85$ |

---

## 9. BACKTEST METRICS & STATISTICAL GATES

### 9.1 Core Metrics — `backtesting/metrics_extra.py`

| Metric | Formula |
|--------|---------|
| **Sharpe** | $\frac{\mu}{\sigma} \sqrt{252}$ |
| **Sortino** | $\frac{\mu - target}{\sqrt{E[\min(r-target,0)^2]}} \sqrt{252}$ |
| **Calmar** | $\frac{AnnReturn}{|MaxDD|}$ |
| **Win Rate** | $\frac{1}{N} \sum \mathbb{I}(r > 0)$ or $\mathbb{I}(r > bench)$ |
| **Period Return** | $S_{end} / S_{start} - 1$ |
| **Monthly Returns** | Month-end resample + pct_change |

---

### 9.2 Chi-Square Gate — `backtesting/chi_square.py`

| Element | Specification |
|---------|---------------|
| **Contingency** | {Bull, Bear} × {Win, Loss} (monthly SPY beat/miss) |
| **Test** | $\chi^2$ independence; Fisher's exact if any expected < 5 (2×2 only) |
| **Regimes** | Bull: 2023-2024; Bear: 2020 COVID + 2022 |
| **Windows** | FULL: 2000→today; RECENT: 19 months |
| **Alpha** | Bonferroni: $\alpha = 0.0071$ (7 gates) |

---

### 9.3 Gate Engine — `ingestion/harness/gate_engine.py` (referenced in validation)

| Gate | Threshold | Type |
|------|-----------|------|
| 1. Chi-Square Contingency | p < 0.0071 | Statistical |
| 2. ANOVA 3-Arm | p < 0.0071 | Statistical |
| 3. Welch C vs A | p < 0.0071 | Statistical (sector-relative re-reg) |
| 4. Welch C vs B | p < 0.0071 | Statistical (sector-relative re-reg) |
| 5. OOS Sharpe (Arm C) | > 1.0 | Performance |
| 6. Max DD (Arm C) | < -20% | Risk |
| 7. Info Ratio vs SPY | > 0.3 | Performance |
| 8. Turnover Drag | < 30% | Cost |
| 9. Probabilistic Sharpe | > 0.95 | Statistical |
| 10. Calmar Ratio | > 2.0 | Risk |

**Phase 7B Holm-Bonferroni:** 11 gates, $\alpha_{FWER} = 0.003125$ per test

---

### 9.4 Deflated Sharpe — `valuation_alpha/stats.py`

| Element | Specification |
|---------|---------------|
| **Formula** | $DSR = \Phi\left( \frac{(SR - E[max SR]) \sqrt{n-1}}{\sqrt{1 - \gamma_3 SR + (\gamma_4-1)/4 \cdot SR^2}} \right)$ |
| **Expected Max SR** | $\sqrt{2 \ln N}$ (Gaussian: $\gamma_3=0, \gamma_4=3$) |
| **Multiple Testing** | $N = n_{trials}$ (candidate portfolios) |

---

### 9.5 Reality Check (White) — `valuation_alpha/stats.py`

| Element | Specification |
|--------|---------------|
| **Method** | Monte Carlo block bootstrap (n_sims ≤ 10,000) |
| **Null** | Max alpha across all candidates under resampled returns |
| **P-value** | $P(\max_{null} \alpha \ge \max_{obs} \alpha)$ |

---

## 10. ALPHA ESTIMATION

### 10.1 FF5 Residual Alpha — `valuation_alpha/alpha.py`

| Element | Specification |
|---------|---------------|
| **Regression** | $r_t - RF_t = \alpha + \sum_{k=1}^5 \beta_k F_{k,t} + \epsilon_t$ |
| **Frequency Handling** | Monthly factors: geometric-link daily returns to monthly; daily factors: align directly |
| **Annualization** | Monthly: $\times 12$; Daily: $\times 252$ |
| **Min Observations** | Monthly: 10; Daily: 60 |
| **SE(α)** | $\hat{\sigma}_\epsilon \cdot \sqrt{(X^TX)^{-1}_{00}}$ (conditioned inverse) |
| **t-stat** | $\hat{\alpha} / SE(\hat{\alpha})$ |
| **95% CI** | $\hat{\alpha} \pm t_{0.975, df} \cdot SE(\hat{\alpha})$ |

---

### 10.2 Excess vs SPY — `valuation_alpha/alpha.py`

| Element | Specification |
|---------|---------------|
| **Excess Return** | $r_{strat} - r_{SPY}$ (aligned) |
| **Annualized** | $\bar{r}_{excess} \times 252$ |
| **Tracking Error** | $\sigma(r_{excess}) \times \sqrt{252}$ |
| **Information Ratio** | $ExcessAnnualized / TrackingError$ |

---

### 10.3 Bias Ablation — `valuation_alpha/stats.py`

| Element | Specification |
|---------|---------------|
| **Run A** | All tickers (with bias tickers) |
| **Run B** | No bias tickers |
| **Verdicts** | EDGE_REAL: both positive, Run B ≥ 0.6×Run A; RIDING_BIAS: A>0, B≤0; NEW_SECTORS: B>0, A≤0; INSIGNIFICANT: else |

---

## 11. SPIKE GUARDS & DATA GATES

### 11.1 MAD Filter (Guard 1) — `validation/spike_guards.py`

| Element | Specification |
|---------|---------------|
| **Rolling Window** | 90 days |
| **Threshold** | Median ± 3 × MAD × 1.4826 (robust σ) |
| **Min Observations** | 30 |
| **MAD=0 Guard** | Constant window → NO filtering |
| **Replacement** | Outliers replaced with 90-day rolling median (not dropped) |
| **Consecutive Cycles** | `mad_outlier_cycles` = trailing flagged cycles (audit: 2 consecutive) |
| **Trigger Rate Band** | [5%, 15%] — 0% = inert, >15% = destabilizing |

---

### 11.2 S/C Constraint (Guard 2) — `validation/spike_guards.py`

| Element | Specification |
|---------|---------------|
| **Credit Capacity** | $CC = \text{target\_D/E} \cdot TotalAssets - TotalDebt$ (frozen Damodaran sector ratios) |
| **Required Reinvestment** | $Req = \Delta Rev / S_C$ |
| **Available Funding** | $Avail = OCF + Cash + CC$ |
| **Unfunded Gate** | $Req > 1.2 \times Avail \to$ growth haircut 0.5x, WACC +200bps |
| **DeltaRev ≤ 0** | NEVER flags (nothing to fund) |
| **Negative Avail** | Any positive Req → unfunded (gap = NaN) |

---

### 11.3 Data Gate (Guard 3) — `validation/spike_guards.py`

| Element | Specification |
|---------|---------------|
| **Quarantine** | < 8 PIT quarters → max weight 0.5% |
| **Probation** | 8-11 quarters → max weight 1.0%; qual weight × 0.3 |
| **Full** | ≥ 12 quarters → only after 3 consecutive ROIC > WACC (graduation) |
| **No Renormalization** | Capped weights final; unallocated remainder logged to audit |
| **Quarantined** | Weight = 0, never redistributed |

---

## 12. LEAD-LAG / CASCADE VALIDATION

### 12.1 C_t → T_t → W_t (C1 Gate) — `Quantitative/consumer_signal.py`

| Element | Specification |
|---------|---------------|
| **C_t** | Grassroots composite (6 sources, weighted) |
| **T_t** | Top-down macro (HMM regime probability) |
| **W_t** | Wall Street (analyst revisions, sentiment) |
| **Test** | Cross-correlation at negative lags: $\rho(C_t, T_{t+k}) > 0.1$ for $k < 0$ AND $\rho(T_t, W_{t+k}) > 0.1$ for $k < 0$ |
| **Max Lag** | 10 days |

---

## 13. MONTE CARLO & BOOTSTRAP METHODS

| Method | Location | Purpose |
|--------|----------|---------|
| **Monte Carlo DCF** | `Quantitative/dcf_screen.py::monte_carlo_dcf` | 10,000 passes for IV distribution |
| **Block Bootstrap Alpha** | `valuation_alpha/stats.py::block_bootstrap_alpha` | Moving-block bootstrap (1y blocks) for alpha CI |
| **Paired Clustered Bootstrap** | `validation/trailing_gates_v3.py::trailing_beats_fixed_gate` | Sector-clustered, 5000 resamples, loss containment test |
| **Reality Check MC** | `valuation_alpha/stats.py::reality_check_mc` | White's Reality Check: max alpha under null |
| **Gate Calibration** | `validation/gate_calibration.py::run_calibration` | 10,000 null portfolios for FPR estimation |
| **Nested CV (Sector)** | `Quantitative/multifractal_vol.py::nested_cv_calibrate` | Walk-forward outer + expanding inner for HAR params |

---

## 14. AUDIT QUESTIONS & ROOT-CAUSE ANALYSIS

### 14.1 Data Scientist Questions (Statistical Rigor)

#### HMM & Regime Detection
1. **Label Identifiability**: The 3-state HMM uses credit-spread mean ordering for labeling. What happens when credit spread is unavailable (pre-2021) and yield-curve momentum is used? Does the ordering remain economically meaningful?
2. **State Persistence**: Phase 7B found min ARI = -0.080 (negative!). The sticky t-HMM v3 adds dwell filter, but the fundamental issue was k-ordering inversion (CRISIS held 169d vs EXP 87d). Does the dwell filter address the root cause or mask it?
3. **Emission Distribution**: Gaussian vs Student-t (ν=6) — Phase 8 moved to t-distribution for fat tails. Was this validated on out-of-sample regime classification?
4. **Feature Selection**: Only 4 features (credit, curve, ret, vol). Phase 8 uses 15-factor PCA. What is the marginal contribution of each feature to regime separation?

#### Volatility Models
5. **HAR-|r| Normality Assumption**: Converting E[|r|] to σ via $\sqrt{\pi/2}$ assumes normality. Equity returns are fat-tailed. What is the bias in vol estimates during crisis?
6. **Regime Multiplier Interaction**: $k_{adj} = k_{sector} \times k_{regime}$. In CRISIS, $k_{regime}=0.5$ but vol spikes. Does tightening the stop (lower k) during high vol increase whip-saw risk?
7. **GARCH(1,1) Stationarity**: Bounds enforce $\alpha+\beta < 0.999$. What happens when daily returns have structural breaks (e.g., 2020 COVID)? Does re-fitting capture regime changes fast enough?

#### Valuation / WACC
8. **WACC Nowcaster Failure (Phase 7B Gate 9)**: $k_e = r_f + \beta \cdot ERP$ with constant $\beta_{60M}$ produced flat $k_e$ (0.08-0.09) across 83k rows, 37.7% coverage. Phase 8 WACC v2 uses option-implied ERP. Is the new ERP estimator stationary?
9. **Duration Approximation**: $IV_t \approx IV_0 (1 - D_{WACC} \cdot \Delta WACC)$ is first-order Taylor. For large WACC shifts (>200bps), what is the convexity error?
10. **R&D Capitalization**: 3-year straight-line amortization. What about companies with changing R&D intensity? Does the fixed 3-year window create artificial earnings smoothing?

#### Portfolio Construction
11. **Swap Barrier Churn**: Phase 7B Fold 3: 14.3 swaps/yr, 80% turnover. The exponential barrier $B(t) = C e^{-\gamma t/t_{target}}$ decays from ~1500bps (month 1) to ~247bps (month 24). Is the decay rate $\gamma=0.5$ calibrated or arbitrary?
12. **Moat Penalty**: +10bps per 0.1 OMR above 0.5. OMR is operating margin robustness — is it forward-looking or backward-looking? Circular if used to protect moat names from swap?
13. **Regime Multipliers on Barrier**: CRISIS multiplier 1.50 makes barrier tighter when vol is highest. Does this prevent necessary rebalancing during regime transitions?

#### Statistical Gates
14. **Holm-Bonferroni vs Gate Dependence**: 11 gates tested at $\alpha=0.003125$. Gates are correlated (e.g., Sharpe and Calmar both depend on returns). Is FWER actually controlled?
15. **Null Portfolio Calibration**: Gate calibration uses i.i.d. Gaussian nulls. Real returns have vol clustering, fat tails, cross-correlation. Does empirical FPR under i.i.d. null translate to real FPR?
16. **Deflated Sharpe Gaussian Assumption**: $E[max SR] = \sqrt{2 \ln N}$ assumes Gaussian returns. With fat tails, expected max is higher → DSR inflated. Is this corrected?

---

### 14.2 Quant Questions (Financial Logic)

#### Strategy Architecture
1. **Arm C Degeneracy (Phase 4)**: Pre-registered screen (ROIC>10%, ICR>3, FCF Yield>5%) produced singleton qualified set in 71/72 months. Mean qualified size = 1.014. The sector-relative dual-track screen was the fix. Does the new screen (top 25% sector ROIC-WACC spread ∪ FCF Margin achievers) produce sufficient diversification?
2. **4-Stage Strategy Asymmetry**: S7 +10.72% alpha vs baseline; S8 -1.48% alpha. The regime shield was too defensive in chop (early BIL/GLD allocation missed recovery). The fix: regime-conditional entry thresholds (EXP 0.85×IV, SHOCK 0.95×IV, CRISIS 0.75×IV). Is this curve-fitting on two strata?
3. **Black Swan Re-entry**: Tier 1 (5-day 3.5σ drawdown) / Tier 2 (HMM Crisis P>0.40) + moat invariance. No events fired in S7/S8. Is the framework untested by design?

#### Valuation Logic
4. **GOOGL Case Study**: Quant 5/6 STRONG BUY, Qual 0.730 STRONG_BUY, but DCF P50 $317.82 vs Price $337.83 (6.3% premium, no MOS). Asset growth +83.6% YoY dilutes ROIC (6.37% vs 8% WACC). The model says BUY but valuation says WAIT. Which governs position sizing?
5. **Narrative Valuation Unfunded Growth**: Haircut 0.5x on $g_T$ and +200bps WACC. Applied at terminal only? What about explicit forecast years 1-7 where growth is already converging?
6. **Survival Cascade Priority**: ICR < 1.5 → 0.40 dominates young ramp (0.60+0.05×years) and moat (0.95). A young company with high ROIC-WACC but low ICR gets p=0.40. Is this intentional (distress trumps quality)?

#### Data & Coverage
7. **Qualitative Coverage**: Phase 4 initial: 27 SEC names → 13 tradable. 9 megacaps missing prices (AMZN, COST, CSCO, JPM, MA, NFLX, PYPL, TSLA, WMT). 5 missing fundamentals. Phase 8 universe: 2,060 tickers with 3+ years PIT data. Is the current universe survivorship-bias-free?
8. **Mapping Confidence**: Heuristic entity mapping (GH org→ticker, USPTO assignee→ticker) with confidence flags. `low` confidence excluded from core arms. What fraction of qual signals are `low` confidence and thus zeroed?
9. **Price Stale Flag**: Max 2-day forward fill. For delisted names, no free price data available. How does survivorship bias affect backtest returns?

---

### 14.3 Root-Cause Analysis: Phase 7B → Phase 8 Structural Defects

| Defect | Phase 7B Evidence | Phase 8 Fix | Residual Risk |
|--------|-------------------|-------------|---------------|
| **WACC Nowcaster Flat** | $k_e$ constant 8-9% across 83k rows; coverage 37.7%; Gate 9 FAIL | WACC v2: Option-implied ERP, 3-state t-HMM regime conditioning, Ω_stress multiplier, Damodaran β_U panel (94 industries) | Option-implied ERP requires liquid options; small-cap coverage? |
| **Probation 3-Gate AND** | 60/539 triggers converted (11.1% vs 40% target); 89% rejection | Probation v2: Weighted OR (0.4/0.4/0.2) + capital ramps (25/50/75/100%) | Weighted OR may over-admit; ramp enforcement needs position-level data |
| **Regime Model ARI = -0.08** | States not reproducible; CRISIS longest (169d); k-ordering inverted | Regime v2: 15-factor PCA + HAR-|r| + GARCH(1,1) IV nowcast; target ARI > 0.7 | 15 factors may overfit; PCA stability across regimes unproven |
| **Swap Barrier Churn** | Fold 3: 14.3 swaps/yr, 80% turnover | Barrier v3: Exponential decay + moat penalty + regime multipliers (CRISIS 1.5x) | CRISIS multiplier tightens barrier when liquidity worst — may trap capital |
| **TCA Fantasy vs Reality** | Reported 75.8 bps; honest recomputation 151.6 bps | TCA v3: Full LOB simulator, queue dynamics, 74.1 bps calibrated | LOB simulator assumptions (order book shape, fill rates) unvalidated on real data |

---

### 14.4 Phase 8 Phase 3-4 Result: **AUDITED HONEST NOT CLEAN**

| Metric | Result |
|--------|--------|
| **Gate Engine** | 16-gate Holm-Bonferroni: **2/16 PASS** |
| **OOS Alpha** | Fold 2: -14.05%, Fold 3: -2.45%, Fold 4: -4.73% (all negative) |
| **Provenance** | 2,223/2,223 files SHA-256 PASS |
| **Unit Tests** | 56 (Phase 1) + 19 (Phase 2) = 75 PASS |

**Conclusion**: The 5 redesigned engines pass unit tests in isolation but **fail to integrate into a profitable strategy under honest walk-forward conditions**. The mathematical components are internally consistent but the *system* lacks economic viability on real PIT data.

---

## 15. FILE-FUNCTION-PARAMETER REFERENCE INDEX

### Core Mathematical Files

| File | Key Classes/Functions | Primary Math Concepts |
|------|----------------------|----------------------|
| `Quantitative/hmm_regime.py` | `GaussianHMM`, `RegimeDetector` | Baum-Welch EM, Forward-Backward, Viterbi |
| `Quantitative/regime_shield_v3.py` | `StickyHMMWithDwell` | Student-t HMM, Sticky Dirichlet, Mahalanobis Dwell |
| `Quantitative/multifractal_vol.py` | `HARAbsVol` | HAR-|r|, Nested CV, Regime multipliers |
| `Quantitative/iv_nowcast.py` | `GARCHIVNowcast` | GARCH(1,1) MLE, Duration adjustment |
| `Quantitative/dcf_nowcaster.py` | `FCFMatrix`, `WACCEngine`, `DurationNowcaster` | Analytical duration, Decoupled FCF/WACC |
| `Quantitative/dcf_screen.py` | `compute_fcf`, `compute_roic`, `monte_carlo_dcf` | DCF, Monte Carlo, Sector discount |
| `Quantitative/narrative_valuation.py` | `compute_narrative_valuation` | 10-yr narrative DCF, Unfunded growth, S/C identity |
| `Quantitative/total_beta.py` | `total_beta_adjusted`, `hamada_lever` | Total Beta sigmoid ceiling, Hamada net-debt |
| `Quantitative/factor_model_pca.py` | `FactorModelPCA` | 15-factor PCA, AR(1) pre-whitening, Rolling z-score |
| `Quantitative/swap_barrier.py` | `ExponentialSwapBarrier` | Exponential barrier, Moat penalty, Regime multipliers |
| `Quantitative/probationary_queue.py` | `ProbationaryQueue` | 3-gate staging, Weighted OR decision |
| `Quantitative/survival_adjustment.py` | `p_survival_priority_cascade`, `survival_adjusted_value` | Priority cascade, Liquidation equity basis, Blend cap |
| `Quantitative/quality_composite.py` | `QualityCompositeEngine` | MAD z-score, Winsorize, Quant/Qual pillars |
| `Quantitative/peak_threshold_tracker.py` | `PeakThresholdTracker` | Rolling 5y percentiles, Sector blending |
| `backtesting/chi_square.py` | `chi_square_independence`, `regime_winloss` | Chi-square/Fisher, Regime contingency |
| `valuation_alpha/alpha.py` | `ff5_residual_alpha`, `apply_slippage` | FF5 regression, Turnover-based slippage |
| `valuation_alpha/stats.py` | `deflated_sharpe`, `reality_check_mc`, `block_bootstrap_alpha` | Bailey-Lopez de Prado, White RC, Block bootstrap |
| `validation/gate_calibration.py` | `run_calibration` | 10k null portfolios, Wilson CI, FPR calibration |
| `validation/hmm_gates.py` | `test_crisis_precision_recall`, `test_state_stability` | Precision/Recall, Adjusted Rand Index |
| `validation/probation_gates.py` | `band_test`, `probationary_pass_rate_gate` | Binomial exact two-sided band |
| `validation/spike_guards.py` | `mad_filter_series`, `sc_constraint_guard`, `data_gate_state` | MAD filter, S/C constraint, Quarantine/Probation/Full |
| `validation/wacc_gates.py` | `coverage_gate`, `ke_dispersion_gate`, `regime_ke_shift_gate` | WACC panel validation, Ke regime sensitivity |
| `Quantitative/consumer_signal.py` | `compute_c_t`, `validate_cascade_lead_lag` | 6-source C_t, Cross-correlation lead-lag |

---

## 16. PRE-REGISTRATION FROZEN PARAMETERS (Reference)

| Config File | Key Frozen Parameters |
|-------------|----------------------|
| `config/pre_registration_phase8.yaml` | Phase 8 engine configs: t-HMM (ν=6, κ=50), WACC v2 (ERP=VIX/√12-Rf), Barrier (C=150bps, γ=0.5), Probation (band [0.40,0.60], ramp 4q) |
| `config/pre_registration_v3.yaml` | 3-state HMM, regime k multipliers (EXP 1.0, SHOCK 0.8, CRISIS 0.5), trailing gates |
| `config/gates_v2.yaml` | 10 gates, Bonferroni α=0.0071 |
| `config/regime_thresholds.yaml` | Regime definitions for swap barrier, exit scheduler |

---

## 17. CONCLUSION

This catalog represents a **complete objective translation** of all mathematical and statistical processes in the codebase as of 2026-09-24. The Phase 8 redesign addressed the three fundamental structural flaws identified in Phase 7B (WACC flatness, probation over-constraint, regime instability), but **Phase 3-4 walk-forward validation shows the integrated system still fails economic viability gates (2/16 pass) with negative OOS alpha**.

The audit questions above highlight where statistical assumptions (Gaussianity, stationarity, independence) may not hold in financial data, and where financial logic (regime-conditional barriers, moat protection, survival cascades) may create unintended feedback loops. The next validation cycle (Phase 8C) must demonstrate that the redesigned engines survive honest walk-forward integration before any capital allocation.

---

**End of Document** — No code changes were made during this analysis.