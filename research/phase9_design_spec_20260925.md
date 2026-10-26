# Phase 9 Strategy Redesign Specification
**Document Control ID**: `research/phase9_design_spec_20260925.md`  
**Date**: September 25, 2026  
**Author**: Antigravity Quantitative Research Agent (`alpha-worker`)  
**Status**: APPROVED DESIGN SPECIFICATION  
**Target Architecture**: Phase 9 Hybrid Fractal v2 Framework  
**Pre-Registration Override Authority**: CEO Decision 1 & CEO Decision 2 (2026-09-25)

---

## EXECUTIVE SUMMARY & ARCHITECTURAL BLUEPRINT

The Phase 9 Strategy Redesign addresses the structural flaws identified in Phase 8 empirical backtesting across macro regimes (S6 Bear Shield: 0% vs -18.65%, S7 AI Boom: +63.96%, 2.15 Sharpe, S8 Growth Recovery: -3.89%, -0.09 Sharpe). While Phase 8 demonstrated robust drawdown mitigation in crash regimes, it exhibited three critical systemic failures:
1. **Regime Model Degeneracy**: 3-state raw HMM collapsed into a single dominant state (94.1% SHOCK, ARI = 0.123 vs >0.70 target, economically inverted $k_e$).
2. **WACC Nowcaster Floor Breach**: $k_e$ dispersion threshold (50bps) was mathematically impossible due to irreducible single-stock beta sampling noise (77bps empirical floor).
3. **Probation Over-Constrained**: 3-gate AND logic rejected 89% of candidates (11.1% pass vs 40-60% target), severely starving portfolio breadth.
4. **Regime Adaptation & Swap Churn**: Fixed entry discount (0.85×IV) missed momentum compounders in growth regimes (S8), while rapid barrier decay ($\gamma=0.5, C=150\text{bps}$) caused excessive swap churn (14.32/yr vs <5 target).

Phase 9 unifies all fixes into the **Hybrid Fractal v2 System Architecture**:

```
+---------------------------------------------------------------------------------------+
|                            FACTOR-MODEL PCA HMM (Nowcasting)                          |
|  Macro/Credit/Vol Observables -> PCA (>80% Var) -> 3-State Sticky HMM (kappa=50, nu=6)|
+---------------------------------------------------------------------------------------+
                                           |
                                           v
+---------------------------------------------------------------------------------------+
|                      REGIME-CONDITIONAL ASYMMETRIC ENTRY & EXIT                       |
|  EXP: 0.85x IV_P25 + Grassroots Boost | SHOCK: 0.95x IV_P25 | CRISIS: 0.75x IV_P25    |
+---------------------------------------------------------------------------------------+
                                           |
                                           v
+---------------------------------------------------------------------------------------+
|                      WACC NOWCASTER V3 & PROBATION SCORING ENGINE                     |
|  JS-Beta Shrinkage + ATM IV ERP | Dynamic Threshold (80bps) | Weighted OR (w1,w2,w3)   |
+---------------------------------------------------------------------------------------+
                                           |
                                           v
+---------------------------------------------------------------------------------------+
|                    MULTIFRACTAL VOL & EXPONENTIAL SWAP BARRIER V3                     |
|  HAR-|r| Vol Forecast | Dynamic Barrier (C=250bps, gamma=0.3, Moat Penalty=200bps)     |
+---------------------------------------------------------------------------------------+
                                           |
                                           v
+---------------------------------------------------------------------------------------+
|                       STAGGERED TWAP EXECUTION & TCA V3 SIMULATOR                     |
|  TWAP 5% ADV over 3-5 days | Dynamic Correlation Clustering | 150bps Cost Budget       |
+---------------------------------------------------------------------------------------+
```

---

## 1. MATHEMATICAL SPECIFICATION OF REDESIGNED COMPONENTS

### 1.1 FLAW 1: Factor-Model HMM with PCA & Economic Ordering

#### A. Feature Matrix & PCA Dimensionality Reduction
Let $\mathbf{X}_t \in \mathbb{R}^{K}$ ($K=8$) be the standardized vector of daily macro-financial observables:
$$\mathbf{X}_t = \left[ \text{Term Spread}_t, \text{Credit Spread (BAA-AAA)}_t, \text{VIX}_t, \sigma_{\text{SPY,1m},t}, \text{CESI}_t, \text{DXY}_t, \sigma_{\text{Oil,1m},t}, \text{Steepness}_t \right]^T$$

Standardize $\mathbf{X}_t$ using rolling 252-day window mean $\boldsymbol{\mu}_t$ and standard deviation $\boldsymbol{\sigma}_t$:
$$\mathbf{Z}_t = \mathbf{\Sigma}_t^{-1} (\mathbf{X}_t - \boldsymbol{\mu}_t)$$

Perform Principal Component Analysis (PCA) on the empirical covariance matrix $\mathbf{\Omega}_t = \frac{1}{N}\mathbf{Z}^T\mathbf{Z}$. Retain $m$ eigenvectors $\mathbf{V}_m = [\mathbf{v}_1, \dots, \mathbf{v}_m]$ associated with eigenvalues $\lambda_1 \ge \dots \ge \lambda_m$ satisfying the minimum variance threshold:
$$\frac{\sum_{j=1}^m \lambda_j}{\sum_{j=1}^K \lambda_j} \ge 0.80$$
The low-dimensional factor score vector fed to the HMM is $\mathbf{f}_t = \mathbf{V}_m^T \mathbf{Z}_t \in \mathbb{R}^m$.

#### B. Sticky Student-t Factor HMM Formulation
The latent regime $S_t \in \{1, 2, 3\}$ follows a first-order Markov chain with sticky transition probability matrix $\mathbf{A} \in \mathbb{R}^{3 \times 3}$:
$$P(S_t = j \mid S_{t-1} = i) \propto A_{ij} + \kappa \cdot \delta_{ij}$$
where $\kappa = 50$ is the sticky prior parameter enforcing state persistence, and $\delta_{ij}$ is the Kronecker delta.

The emission probability distribution given state $S_t = k$ is modeled as a Multivariate Student-t distribution with $\nu = 6$ degrees of freedom:
$$p(\mathbf{f}_t \mid S_t = k) = \frac{\Gamma\left(\frac{\nu + m}{2}\right)}{\Gamma\left(\frac{\nu}{2}\right) (\nu \pi)^{m/2} |\mathbf{\Sigma}_k|^{1/2}} \left[ 1 + \frac{1}{\nu} (\mathbf{f}_t - \boldsymbol{\mu}_k)^T \mathbf{\Sigma}_k^{-1} (\mathbf{f}_t - \boldsymbol{\mu}_k) \right]^{-\frac{\nu + m}{2}}$$

#### C. Economic Monotonicity & Ordering Constraint
State indices $k \in \{1, 2, 3\}$ are explicitly mapped to economic regimes (1 = EXPANSION, 2 = SHOCK, 3 = CRISIS) by enforcing strict monotonicity on state mean volatility and Cost of Equity ($k_e$):
$$\mu_{\text{VIX}, 1} < \mu_{\text{VIX}, 2} < \mu_{\text{VIX}, 3} \quad \text{and} \quad \mu_{k_e, 1} < \mu_{k_e, 2} < \mu_{k_e, 3}$$
If an unconstrained HMM estimation yields non-monotonic means, states are re-indexed post-fit by ascending order of mean VIX factor score prior to state filtering.

#### D. Nowcasting & Re-estimation Protocol
- **Daily Filtered Probability**: Update $\boldsymbol{\xi}_{t\mid t} = P(S_t = k \mid \mathbf{f}_{1:t})$ daily using Hamilton filtering.
- **Monthly Parameter Re-estimation**: Re-fit parameters $(\boldsymbol{\mu}_k, \mathbf{\Sigma}_k, \mathbf{A})$ on the 1st trading day of each month using Baum-Welch (EM) over a 5-year rolling lookback window.

---

### 1.2 FLAW 2: WACC Nowcaster v3 & Shrinkage Engine

#### A. James-Stein Beta Shrinkage
To eliminate noise in single-stock historical CAPM beta estimates $\hat{\beta}_i$, apply cross-sectional James-Stein shrinkage toward the sector-average beta $\bar{\beta}_{g(i)}$:
$$\hat{\beta}_{i, \text{JS}} = w_i \hat{\beta}_i + (1 - w_i) \bar{\beta}_{g(i)}$$
where the shrinkage factor $w_i \in [0, 1]$ is derived from empirical variance:
$$w_i = 1 - \frac{(N_g - 3) s_{\text{error}, i}^2}{\sum_{j \in g} (\hat{\beta}_j - \bar{\beta}_{g(i)})^2}$$
where $N_g$ is the number of assets in sector $g$, and $s_{\text{error}, i}^2$ is the standard error of beta estimation for asset $i$.

#### B. Option-Implied Term Structure ERP Refinement
Refine the option-implied Equity Risk Premium ($\text{ERP}_{\text{opt}}$) using the 30-day at-the-money (ATM) implied volatility term structure slope:
$$\text{ERP}_{\text{opt}, t} = \frac{\text{IV}_{30\text{d}, \text{ATM}, t}}{\sqrt{12}} \cdot \left( 1 + \theta \cdot \left[ \frac{\text{IV}_{60\text{d}}}{\text{IV}_{30\text{d}}} - 1 \right] \right) - R_{f, t}$$
where $\theta = 0.5$ scales the term structure slope premium. The combined ERP remains a weighted blend:
$$\text{ERP}_t = 0.30 \cdot \text{ERP}_{\text{opt}, t} + 0.70 \cdot \text{ERP}_{\text{Damodaran}}$$
bounded by $\text{ERP}_t \in [0.0050, 0.0800]$.

#### C. Dynamic $k_e$ Dispersion Threshold & Regime Conditioning
The single-stock Cost of Equity dispersion metric across portfolio assets is:
$$\text{Dispersion}(k_e) = \sqrt{\frac{1}{N} \sum_{i=1}^N (k_{e, i} - \bar{k}_e)^2}$$
- **Threshold Relaxation**: Upper bound relaxed to **80bps** (`ke_var_threshold = 0.0080`, per CEO Decision 1).
- **Regime-Conditional Scaling**: In CRISIS regime, flight-to-quality dynamics expand dispersion; the effective threshold scales dynamically:
$$\text{Threshold}_{\text{eff}}(S_t) = \begin{cases} 80\text{ bps} & \text{if } S_t = \text{EXPANSION} \\ 100\text{ bps} & \text{if } S_t = \text{SHOCK} \\ 120\text{ bps} & \text{if } S_t = \text{CRISIS} \end{cases}$$

---

### 1.3 FLAW 3: Probation Engine v2 (Weighted OR & Capital Ramps)

#### A. Weighted Continuous Scoring Function
Replace the rigid 3-gate boolean AND logic ($G_1 \land G_2 \land G_3$) with a continuous composite pass score $S_i \in [0, 1]$:
$$S_i = w_1 \cdot \mathbb{I}(\text{ROIC}_{\text{cap\_rd}} \ge \text{WACC}_i + 0.02) + w_2 \cdot \mathbb{I}(\text{Qual\_Composite}_i \ge 0.50) + w_3 \cdot \mathbb{I}(\text{Risk\_Score}_i \le 0.40)$$
where weights are pre-registered as $w_1 = 0.40$, $w_2 = 0.35$, $w_3 = 0.25$ ($\sum w_k = 1.0$).

A security satisfies Probation Pass if $S_i \ge S_{\text{threshold}} = 0.60$.

#### B. Tiered Capital Allocation Ramps
Securities entering probation are assigned capital allocation weights $W_{\text{cap}}$ based on consecutive monthly passes:
$$W_{\text{cap}}(m) = \begin{cases} 
0.33 & \text{if } m = 1 \text{ consecutive pass (Tier 1 - Entry)} \\ 
0.66 & \text{if } m = 2 \text{ consecutive passes (Tier 2 - Accumulation)} \\ 
1.00 & \text{if } m \ge 3 \text{ consecutive passes (Tier 3 - Full Weight)} 
\end{cases}$$

#### C. Effective Probation Band Override
Per CEO Decision 2, to maintain an empirical effective pass rate band of $[0.40, 0.60]$ under two-tailed significance testing ($\alpha = 0.05$), the written pre-registration bounds in `quality_thresholds.yaml` are expanded:
$$\text{Written Band} = [0.35, 0.65] \implies \text{Effective Operational Band} = [0.40, 0.60]$$

---

### 1.4 FLAW 4: Regime-Conditional Entry & Exit Architecture

#### A. Regime-Dependent Entry Thresholds
Entry discount factor $\alpha(S_t)$ applied to Intrinsic Value 25th percentile ($\text{IV\_P25}$) scales as:
$$P_i \le \alpha(S_t) \cdot \text{IV\_P25}_i$$
where:
$$\alpha(S_t) = \begin{cases} 
0.85 & \text{if } S_t = \text{EXPANSION (Aggressive entry to capture compounders)} \\ 
0.95 & \text{if } S_t = \text{SHOCK (Lenient re-entry on dips)} \\ 
0.75 & \text{if } S_t = \text{CRISIS (Black swan protection; requires Moat=True)} 
\end{cases}$$

#### B. S8 Expansion Growth Overlay
In `EXPANSION` regime ($S_t = 1$), an asset with strong grassroots momentum ($C_i(t) > 0.50$, derived from patent grants + Glassdoor sentiment + web traffic) receives a +5% allowance on entry discount:
$$\alpha_{\text{eff}}(S_t=1, C_i) = 0.85 + 0.05 \cdot \mathbb{I}(C_i(t) > 0.50) = 0.90$$

#### C. Dynamic Exit Target Multiples
Exit target multiples $\beta(S_t)$ applied to $\text{IV\_P25}$ scale as:
$$P_i > \beta(S_t) \cdot \text{IV\_P25}_i \implies \text{Trigger Profit Exit}$$
where $\beta(\text{EXP}) = 1.20$, $\beta(\text{SHOCK}) = 1.10$, and $\beta(\text{CRISIS}) = 1.05$.

---

### 1.5 FLAW 5: Exponential Swap Barrier v3 & Staggered Exit Execution

#### A. Dynamic Barrier Formula
The dynamic cost threshold barrier $B_i(t)$ for swapping candidate $i$ out of the portfolio is:
$$B_i(t) = C \cdot \exp\left(-\gamma \frac{t}{t_{\text{target}}}\right) + \text{Penalty}_{\text{Moat}} \cdot \mathbb{I}(\text{Moat Breached})$$
with Phase 9 recalibrated parameters:
- **Base Cost Threshold ($C$)**: $250\text{ bps}$ (up from 150bps)
- **Decay Parameter ($\gamma$)**: $0.30$ (down from 0.50, slowing decay convergence)
- **Horizon Target ($t_{\text{target}}$)**: $36\text{ months}$ (up from 24 months)
- **Moat Breach Penalty**: $200\text{ bps}$ (up from 100bps)

#### B. Staggered Exit TWAP & Correlation Clustering
- **Execution Slice**: Exit trades executed via Time-Weighted Average Price (TWAP) capped at $5\%$ of Average Daily Volume (ADV) over a 3 to 5 day execution window.
- **Correlation Clustering Execution**: Assets with pairwise return correlation $\rho_{ij} > 0.75$ undergoing simultaneous exit are queued with staggered start days ($t_j = t_i + 1\text{ day}$) to prevent market impact clustering.

---

## 2. IN-CODE CONFIG OVERRIDES (PRE-REGISTRATION LOG)

All Phase 9 parameter updates override frozen Phase 8 parameters via explicit in-code overrides tagged with pre-registration markers.

```python
# ==============================================================================
# PHASE 9 PRE-REGISTRATION OVERRIDES (CEO RULING 2026-09-25)
# ==============================================================================

# PRE-REG OVERRIDE: CEO-20260925-REGIME-01
PHASE9_HMM_CONFIG = {
    "n_states": 3,
    "distribution": "student_t",
    "nu": 6,
    "sticky_prior_alpha": 50,
    "factor_count": 8,
    "pca_variance_threshold": 0.80,
    "factors": [
        "TERM_SPREAD_10Y2Y", "CREDIT_SPREAD_BAA_AAA", "VIX_INDEX",
        "SPY_REALIZED_VOL_1M", "CESI_MACRO_SURPRISE", "DXY_DOLLAR_INDEX",
        "OIL_VOLATILITY_1M", "CURVE_STEEPNESS"
    ],
    "ordering_constraint": "monotonic_vix_and_ke"
}

# PRE-REG OVERRIDE: CEO-20260925-WACC-02
PHASE9_WACC_CONFIG = {
    "ke_var_threshold": 0.0080,          # CEO Decision 1: 80bps threshold (was 50bps)
    "ke_dispersion_regime_scaling": {
        "EXPANSION": 0.0080,
        "SHOCK": 0.0100,
        "CRISIS": 0.0120
    },
    "beta_shrinkage_method": "james_stein",
    "erp_option_tenor": "30d_atm_term_structure"
}

# PRE-REG OVERRIDE: CEO-20260925-PROBATION-03
PHASE9_PROBATION_CONFIG = {
    "logic": "weighted_or",
    "weights": {"roic_gate": 0.40, "qual_gate": 0.35, "risk_gate": 0.25},
    "score_threshold": 0.60,
    "written_probation_band": [0.35, 0.65],  # CEO Decision 2: Written for eff [0.40, 0.60]
    "effective_operational_band": [0.40, 0.60],
    "capital_ramps": {
        "tier1_pass_1": 0.33,
        "tier2_pass_2": 0.66,
        "tier3_pass_3": 1.00
    }
}

# PRE-REG OVERRIDE: CEO-20260925-REGIME-ENTRY-04
PHASE9_ENTRY_EXIT_CONFIG = {
    "entry_discount_by_regime": {
        "EXPANSION": 0.85,    # 0.85x IV_P25
        "SHOCK": 0.95,        # 0.95x IV_P25
        "CRISIS": 0.75        # 0.75x IV_P25 (Moat required)
    },
    "exit_multiple_by_regime": {
        "EXPANSION": 1.20,
        "SHOCK": 1.10,
        "CRISIS": 1.05
    },
    "grassroots_momentum_boost": {
        "enabled": True,
        "regime": "EXPANSION",
        "grassroots_threshold": 0.50,
        "allowance_boost": 0.05   # Boosts EXP entry to 0.90x
    }
}

# PRE-REG OVERRIDE: CEO-20260925-BARRIER-05
PHASE9_SWAP_BARRIER_CONFIG = {
    "cost_threshold_bps": 250.0,       # C parameter (was 150bps)
    "decay_gamma": 0.30,               # Decay rate (was 0.50)
    "target_horizon_months": 36,       # Horizon (was 24 months)
    "moat_penalty_bps": 200.0,         # Moat breach penalty (was 100bps)
    "twap_max_adv_pct": 0.05,          # 5% ADV max
    "twap_window_days": [3, 5]
}
```

---

## 3. WALK-FORWARD VALIDATION PROTOCOL

### 3.1 Fold Structure & Data Isolation
Validation follows a strict 4-Fold Walk-Forward Cross-Validation protocol across the 2,060 ticker Point-in-Time (PIT) dataset.

```
       2018         2020         2022         2024         2026
Fold 1: |=== Train ===|--Emb--|-Pur-|= Test =|
Fold 2:              |=== Train ===|--Emb--|-Pur-|= Test =|
Fold 3:                           |=== Train ===|--Emb--|-Pur-|= Test =|
Fold 4:                                        |=== Train ===|--Emb--|-Pur-|= Test =|
```

- **Train Window**: 36 months sliding.
- **Embargo Window**: 12 months (prevents overlap of multi-year valuation horizon signals).
- **Purge Window**: 6 months (eliminates quarterly financial restatement leakage).
- **Test Window**: 12 months out-of-sample per fold.

### 3.2 Full 16-Gate Validation Suite Matrix

| Gate Code | Metric / Gate Name | Phase 8 Baseline / Status | Phase 9 Target / Threshold | Validation Method |
| :--- | :--- | :--- | :--- | :--- |
| **Gate 1** | Arm Alpha vs Benchmark | Synthetically Guarded | $\alpha > +3.5\%$ p.a. vs SPY | 4-Fold Walk-Forward |
| **Gate 2** | Sharpe Dominance | Synthetically Guarded | Sharpe ratio $\ge 1.25$ | 4-Fold Walk-Forward |
| **Gate 3** | Max Drawdown Containment | Pass (S6: 0%) | MaxDD $< 15.0\%$ in S6/S8 | Stress Scenario Test |
| **Gate 4** | Factor Neutrality | Synthetically Guarded | Factor $|t| < 2.0$ on Fama-French 5 | Cross-Sectional Reg |
| **Gate 5** | Regime Model ARI | **0.123 (FAIL)** | **ARI $> 0.70$ on $\ge 3/4$ folds** | NBER/Vol Alignment |
| **Gate 6** | State Dwell & Persistence | Degenerate (94% SHOCK) | Mean state dwell $> 6$ months | State Transition Audit |
| **Gate 7** | Economic Label Monotonicity | Inverted ($k_e$ CRISIS < SHOCK) | $k_{e, \text{EXP}} < k_{e, \text{SHOCK}} < k_{e, \text{CRISIS}}$ | Mean Test across states |
| **Gate 8** | Qualitative Info Coefficient | Synthetically Guarded | Rank IC $> 0.04$ ($p < 0.05$) | 12m Lead Spearman IC |
| **Gate 9** | WACC Universe Coverage | 100% (Pass) | Maintain 100% PIT Coverage | Coverage Audit |
| **Gate 10**| $k_e$ Dispersion Compliance | **208bps (FAIL)** | **Pass at $\le 80\text{bps}$ threshold** | Cross-sectional Var |
| **Gate 11**| Beta Shrinkage Variance | Un-shrunk noise | JS Beta Var Reduction $> 25\%$ | Analytical Audit |
| **Gate 12**| Probation Pass Rate | **11.1% (FAIL)** | **$40\% - 60\%$ Pass Rate** | Real + Null Portfolios |
| **Gate 13**| Swap Churn Frequency | **14.32/yr (FAIL)** | **$< 5.0$ Swaps / Year** | Annualized Count |
| **Gate 14**| Execution Slippage Budget | Unmonitored | Transaction Cost $< 150\text{bps}$ | LOB Simulation |
| **Gate 15**| S8 Growth Recovery Sharpe | **-0.09 (FAIL)** | **Sharpe $> 1.10$, Return $> +15\%$**| S8 Out-of-Sample Test |
| **Gate 16**| S7 AI Boom Performance | +63.96% / 2.15 Sharpe | Maintain Sharpe $> 2.00$ | S7 Out-of-Sample Test |

---

## 4. COMPONENT SUCCESS CRITERIA

1. **Factor-Model HMM**:
   - Walk-forward ARI $> 0.70$ against historical benchmark cycles on $\ge 3$ out of 4 folds.
   - Mean state dwell time $> 6.0$ months (no micro-state flipping).
   - Strict monotonic mean ordering: $\bar{k}_{e, \text{EXP}} < \bar{k}_{e, \text{SHOCK}} < \bar{k}_{e, \text{CRISIS}}$.

2. **WACC Nowcaster v3**:
   - 100% PIT coverage across active US equity universe.
   - $k_e$ dispersion passes under the relaxed 80bps threshold (with CRISIS regime expansion to 120bps).
   - James-Stein beta shrinkage achieves $\ge 25\%$ variance reduction over raw OLS betas.

3. **Probation Engine v2**:
   - Probation Pass Rate strictly bounded within $[40\%, 60\%]$ across both real candidate universe and null baseline portfolios.
   - Minimum 400 total probation trigger events evaluated over test window.

4. **S8 Regime Adaptation**:
   - S8 Stratum annualized return $> +15.0\%$ with Sharpe ratio $> 1.10$ (reversing Phase 8's $-3.89\%$ loss).
   - S7 AI Boom Sharpe ratio maintained $> 2.00$.

5. **Swap Barrier v3**:
   - Annualized portfolio swap frequency reduced to $< 5.0$ swaps/year (from 14.32/year).
   - Transaction cost budget contained within $150\text{bps}$ total execution slippage limit.

---

## 5. RISK REGISTER & MITIGATION MATRIX

| Risk ID | Risk Description | Severity | Probability | Mitigation Strategy |
| :--- | :--- | :--- | :--- | :--- |
| **R-01** | **PCA Dimensionality Collapse**: Top 3 components fail to explain >80% variance during sudden macro regime shifts. | High | Low | Dynamic component expansion: If $m=3$ components explain $<80\%$, automatically incorporate 4th component in real time. |
| **R-02** | **Over-Aggressive Entry in EXP**: Entry threshold at 0.85×IV in EXP regime buys overvalued momentum traps before market pullbacks. | High | Medium | Enforce fundamental quality circuit-breaker: ROIC cap/rd must exceed WACC by $\ge 3\%$, and OMR must exceed 0.60 for 0.85× entry. |
| **R-03** | **Swap Barrier Stagger Lock**: Holding positions longer ($C=250\text{bps}, \gamma=0.3$) holds decaying value traps during structural disruption. | Critical | Low | Hard Moat Breach Override: If `Moat_Condition == False`, bypass dynamic barrier completely and execute immediate TWAP exit. |
| **R-04** | **Data Sparsity in Qual Vectors**: Qualitative signals (Glassdoor/Patents) drop coverage in out-of-sample windows. | Medium | Medium | Sector-neutral median imputation fallback guarded by Arm B/C missingness flags. |
| **R-05** | **TWAP Market Impact in Low ADV**: 5% ADV cap over 3-5 days suffers market impact in illiquid small-caps ($<\$2\text{B}$ cap). | Medium | Medium | Liquidity-scaled execution window: Expand TWAP window to 8 days for stocks with ADV $<\$5\text{M}$. |

---

## 6. IMPLEMENTATION PRIORITY & DEPENDENCY MAP

The implementation of Phase 9 components must strictly follow a 4-Stage dependency sequence:

```
+---------------------------------------------------------------------------------------+
| STAGE 1: CORE ENGINE UPGRADES                                                         |
| 1.1 FactorHMM (PCA + Sticky Student-t + Monotonic Ordering)                           |
| 1.2 WACC v3 (JS Beta Shrinkage + ATM IV ERP Term Structure)                          |
+---------------------------------------------------------------------------------------+
                                           |
                                           v
+---------------------------------------------------------------------------------------+
| STAGE 2: FILTER & PROBATION RE-ARCHITECTING                                          |
| 2.1 Probation v2 (Weighted OR Logic + Tiered Capital Ramps + [0.35,0.65] Band)        |
| 2.2 Dynamic Regime Entry/Exit (EXP 0.85x, SHOCK 0.95x, CRISIS 0.75x + Momentum Boost) |
+---------------------------------------------------------------------------------------+
                                           |
                                           v
+---------------------------------------------------------------------------------------+
| STAGE 3: EXECUTION & BARRIER CALIBRATION                                              |
| 3.1 Dynamic Swap Barrier v3 (C=250bps, gamma=0.30, t=36m, Moat Penalty=200bps)        |
| 3.2 Staggered TWAP & Correlation Clustering TCA Simulator                             |
+---------------------------------------------------------------------------------------+
                                           |
                                           v
+---------------------------------------------------------------------------------------+
| STAGE 4: WALK-FORWARD VALIDATION & GATE HARNESS                                      |
| 4.1 4-Fold Walk-Forward Backtest Execution                                           |
| 4.2 16-Gate Full Audit & CEO Sign-Off Report                                         |
+---------------------------------------------------------------------------------------+
```

### Module Priority Schedule:
1. `Quantitative/hmm_regime.py` — Implement `FactorHMM` with PCA and monotonicity constraints (Priority 1).
2. `Quantitative/wacc_engine.py` — Integrate James-Stein shrinkage and dynamic $k_e$ dispersion (Priority 2).
3. `Quantitative/probation_engine.py` — Build Weighted OR continuous scorer and capital ramps (Priority 3).
4. `Quantitative/regime_shield.py` — Apply regime-conditional entry/exit thresholds and momentum overlay (Priority 4).
5. `Quantitative/swap_barrier.py` — Recalibrate barrier equation and TWAP staggered execution (Priority 5).
6. `Quantitative/gate_engine.py` — Update validation suite for all 16 gates (Priority 6).
