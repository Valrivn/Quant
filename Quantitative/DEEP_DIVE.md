# DEEP DIVE — `Quantitative/` (Regime Stack, Volatility, Quality & Valuation Math)

> Workstream B documentation. This is the quantitative core: regime detection
> (BOCPD + IO-HMM + micro-states + two-layer shield), multifractal volatility,
> quality composites, swap barriers, survival adjustment, and the v5/v7 topologies
> that route into `ingestion/harness` gates.

## 1. The regime stack (v4 → v5 → two-layer shield)

Four layers compose today's regime system. Pre-reg v5 (`config/pre_registration_v5.yaml`)
was the calibration contract; `regime_shield_v3.py` implements the two-layer
production shield.

### 1a. `bocpd.py` — Bayesian Online Changepoint Detection

- Gaussian-Gamma conjugate prior on run-length; `hazard = 0.01` (≈ 1 changepoint
  per 100 days a priori); decision `threshold = 0.95` on run-length posterior.
- Guard rails: `min_run_length = 5`, `max_run_length = 500`.
- Emits a "force CRISIS" circuit-breaker flag when a changepoint is detected —
  consumed by `unified_regime.py` (override weight 0.8).
- Pure numpy; no sklearn dependency. Tests: `Quantitative/tests/test_bocpd.py`.

### 1b. `io_hmm.py` — Input-Output Hidden Markov Model

- 3 states: `EXPANSION / CHOPPY / CRISIS`. Multinomial-logit transition
  probabilities (state → next state) with a deterministic duration input.
- Numerics: softmax + logsumexp throughout; no raw exp overflow path.
- Weights in the unified fusion: `io_hmm_weight = 1.0`.
- Tests: `Quantitative/tests/test_io_hmm.py`; cracking tests in
  `tests/test_hmm_dwell.py` (see known failure modes).

### 1c. `micro_states.py` — 4 micro-state classifier (v5)

`MicroState` enum: `GOLDILOCKS / LATE_CYCLE_DRAG / LIQUIDITY_CRUNCH / STAGFLATION`,
driven by FRED features with `MicroStateConfig` thresholds:

| Feature | Goldilocks bound |
|---|---|
| inflation (CPI YoY) | ≤ 2.5 |
| real rate (10Y − T10YIE) | ≤ 1.5 |
| credit spread (BAA−AAA) | ≤ 150 bp |
| VIX | ≤ 20 |
| curve (10Y−3M) | ≥ 50 bp |

Micro-state overrides feed the unified regime with weight 1.0 and are the only
layer allowed to *upgrade* a regime (e.g. GOLDILOCKS cannot be declared while
LIQUIDITY_CRUNCH spread conditions hold).

### 1d. `unified_regime.py` — fusion + circuit breakers (v5)

`UnifiedRegimeConfig`: `bocpd_hazard 0.01`, `bocpd_threshold 0.95`,
`io_hmm_states {EXPANSION, CHOPPY, CRISIS}`, `micro_enabled true`;
override weights `bocpd_override_weight 0.8, io_hmm_weight 1.0, micro_weight 1.0`;
`crisis_threshold 0.7`, `choppy_threshold 0.4`. BOCPD is a *circuit breaker*:
its CRISIS vote jumps the fused state regardless of HMM posterior (until the
changepoint signal decays).

### 1e. `regime_shield_v3.py` — TWO-LAYER PRODUCTION SHIELD

**Layer 1 — daily, class-weighted 3-state GaussianHMM** on CANONICAL features:

| # | Feature | Role |
|---|---|---|
| 0 | CREDIT_SPREAD_VELOCITY_21D | primary sort key |
| 1 | YIELD_CURVE_10Y2Y | level |
| 2 | REALIZED_VOLATILITY_21D | vol |
| 3 | FINANCIAL_STRESS_INDEX (STLFSI2) | stress |
| 4 | REAL_RATE_10Y | rate |
| 5 | UNANTICIPATED_INFLATION = CPI YoY − T10YIE | inflation surprise |

**CESI is GONE.** It was previously constructed as
`np.random.normal(0, 20, len(dates))` — a zero-synthetic-data violation. The
random series is banned (`Quantitative/contracts/feature_contract.py:
FORBIDDEN_SERIES`; `ingestion/harness/feature_gate.py` denylist;
`tests/test_zero_synthetic_cesi_removal.py` enforces it). Feature 5 replaced it.

- States: `EXPANSION / SHOCK / CRISIS`; **CRISIS class weight 2.0** (asymmetric
  penalty for false calm); `sticky_kappa = 500` (stickiness prior on state
  persistence). Retraining on quarterly + macro-release cadence.
- **Layer 2 — monthly macro gates, Bayesian log-odds fusion**:
  `logit(P_final) = logit(P_HMM) + λ · logit(P_macro)` with λ = 0.3.
  Macro features: CPI & Unemployment at **45-day lag** (release lag), Real Rate at
  1-day lag.
- **Hard overrides** (non-Bayesian, cannot be fused away): Sahm rule +
  RealRate conditions → forced `0.70` CRISIS probability; `hmm_gates_v3`
  occupancy guards → `0.40` cap on any state that blows past its historical
  occupancy bound.
- Regime acceptance criterion (older v4 contract, still enforced for the ARI
  component in Sprint 6): **ARI ≥ 0.45 on ≥ 3 of 4 fold windows**.

**The `FactorHMMv3` import break.** `regime_shield_v3.py` no longer exports
`FactorHMMv3`. Two legacy tests still import it:
`tests/test_hmm_dwell.py`, `tests/test_phase9_core.py` (Pre-EXISTING
`ImportError` — verified pre-sweep; not caused by Workstream B moves). Fixing
those tests is a Workstream A code task, not a docs task.

## 2. Volatility: HAR + multifractal (`har_abs_vol.py`, `multifractal_vol.py`)

- `HARAbsVol`: Heterogeneous Auto-Regressive model on **|r|** with lags
  `(1, 5, 22)` (daily/weekly/monthly), Ridge with `alpha_ridge = 1.0`;
  conditional sigma `σ̂ = |r̂| · √(π/2)` (E|N(0,σ)| = σ√(2/π) inversion).
  Horizons `τ ∈ {5, 10, 20}` → `σ_fractal(τ) = σ̂_daily · √τ`.
- `multifractal_vol.py` adds **sector regime multipliers**
  (`REGIME_K_MULTIPLIER`: EXPANSION 1.0 / SHOCK 0.8 / CRISIS 0.5) and
  per-sector overrides (e.g. Tech `{tau: 10, k: 2.2}`).
- Consumed by: trailing-exit scaling (`validation/trailing_cv.py`),
  swap-barrier widths, and `validation/vol_gates.py` (see its DEEP_DIVE).

## 3. Quality composite & factor PCA

- `quality_composite.py`: MAD z-score (scaled by 0.6745 = 1/Φ⁻¹(0.75)), winsorize
  at (0.01, 0.99), composite over 8 factor families; QCS premium entry gate.
- `factor_model_pca.py`: 12 *structural* factors — e.g.
  `curvature = 2·DGS5 − DGS3M − DGS10`, `VIX_term_structure = VIX/VIX3M`,
  `quality_spread` — PCA-1/equal-weight aggregation; used by Arm B
  (`three_arm_runner.py`).
- Missing values in the z-scored universe → **0.0 neutral fill** (not dropped),
  per frozen Arm B spec.

## 4. Swap barriers & survival

- `swap_barrier_v3.py` (RS-25, pre-reg v4 250 bps variant v4):
  `barrier = C · exp(−γ · t_held / t_target) + moat_penalty`,
  with `C = 0.0250`, `γ = 0.25`, `t_target = 30` months. Sanity gates on barrier
  monotonicity and swap churn live in `validation/swap_gates_v3.py`.
- `survival_adjustment.py`: P(survival | fundamentals) cascade applied to
  intrinsic value: `LV = 0.70·TBV_ex_cash + Net_Cash`, capped at
  `IV_gc · 1.0001` (never a haircut beyond the going-concern value).

## 5. Known failure modes & migration notes

1. **CESI synthetic series** — removed; replaced by UNANTICIPATED_INFLATION. Any
   re-introduction trips `feature_gate.py` denylist + `test_zero_synthetic_cesi_removal.py`.
2. **`.bfill()` pre-history injection** — v4-era regime series were backfilled
   before the PIT fix; v5 + shield require authentic PIT (45-day CPI/Unemployment
   lag) or `PITValidationError`.
3. **Zero-std columns in HMM training** — scale rotation: the `canonical_features`
   packer drops constant columns with a logged warning before `StandardScaler`.
4. **`FactorHMMv3` import error** in `tests/test_hmm_dwell.py` &
   `tests/test_phase9_core.py` — pre-existing, unrelated to reorganization.
5. **Sub-package note**: `Quantitative/regimes/` and `Quantitative/valuation/`
   are currently empty-of-modules (placeholders); `Quantitative/allocation/`,
   `audit/`, `bonds/`, `gold_etf/`, `stochastic/`, `sensitivity/`,
   `calibration/`, `data/`, `funds/`, `shared/`, `contracts/`, `validation/`,
   `tests/` hold real code. Keep imports inside `Quantitative` relative
   (`from Quantitative.x import y`), not `from x import y`.

## 6. Entrypoints

```bash
python -m Quantitative.calibration.train_unified_regime          # fit + calibrate v5 regime
python -m Quantitative.regimes                                   # (placeholder)
python -m validation.vol_gates --test                            # multifractal consistency gates
python -m tests.test_regime_shield_v2 (pytest)                   # shield regression tests
```

## 7. Key files quick-map

| File | Highlights |
|---|---|
| `bocpd.py` | Gaussian-Gamma conjugate, hazard 0.01, min_run 5 / max_run 500, force-CRISIS breaker |
| `io_hmm.py` | 3-state multinomial-logit HMM, softmax/logsumexp numerics |
| `micro_states.py` | 4 micro-states, FRED thresholds (goldilocks inflation ≤2.5, real ≤1.5, spread ≤150, VIX ≤20, curve ≥50) |
| `unified_regime.py` | Fusion + BOCPD breaker (0.8), crisis/choppy thresholds 0.7/0.4 |
| `regime_shield_v3.py` | Two-layer shield: HMM (6 canonical features, CRISIS weight 2.0, sticky_kappa 500) + monthly macro logit fusion (λ 0.3) + 0.70/0.40 hard overrides |
| `har_abs_vol.py` | Ridge lags (1,5,22), α_ridge 1.0, σ̂=|r̂|√(π/2), taus (5,10,20) |
| `multifractal_vol.py` | Sector regime multipliers (1.0/0.8/0.5), Tech τ=10 k=2.2 override |
| `quality_composite.py` | MAD z 0.6745, winsorize (0.01,0.99), QCS premium gate |
| `factor_model_pca.py` | 12 structural factors (curvature, VIX term, quality spread…) |
| `swap_barrier_v3.py` | `C·exp(−γ·t_held/t_target)+moat`, C=0.0250, γ=0.25, t_target=30 |
| `survival_adjustment.py` | P(survival) cascade, LV=0.70·TBV_ex_cash+Net_Cash, cap ≤ IV_gc·1.0001 |
| `contracts/feature_contract.py` | CANONICAL_FEATURES / FORBIDDEN_SERIES / assert_* helpers |

## 8. Cross-references

- Pre-regs: `config/pre_registration_v5.yaml`, `config/pre_registration_v7.yaml`,
  `config/calibrated_unified_regime_params.json` (sha256 pinned)
- Consumers: `ingestion/harness/gate_engine.py` (HMM ARI component),
  `ingestion/harness/three_arm_runner.py` (Arm A/B features),
  `validation/vol_gates.py`, `validation/trailing_cv.py`
- Tests: `Quantitative/tests/`, `tests/test_regime_shield_v2.py`,
  `tests/test_har_abs_vol.py`, `tests/test_factor_model_pca.py`,
  `tests/test_swap_barrier_v3.py`, `tests/test_survival_adjustment.py`