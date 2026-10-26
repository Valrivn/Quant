# `Quantitative/` — Regime Detection, Volatility, Quality & Valuation Math

## Roadmap Position
Pipeline stage 2 (analysis core): the quantitative layer consumed by the gate
harness and validation — regime state (BOCPD + IO-HMM + micro-states + the
two-layer shield), multifractal volatility, quality composites, factor PCA,
swap barriers, and survival adjustment. **See `Quantitative/DEEP_DIVE.md` for
formulas, constants, and known failure modes.**

## Folder Summary
`Quantitative/` is the math engine of the repo. Top-level modules are the
regime/vol/quality stack; sub-packages hold their focused concerns: `bonds/`,
`gold_etf/`, `stochastic/`, `allocation/`, `audit/`, `sensitivity/`,
`calibration/`, `data/` (FRED feed/backfill), `contracts/` (feature contracts),
`funds/`, `shared/`, `validation/` (walk-forward), `tests/`.
`regimes/` and `valuation/` are currently placeholder sub-packages.

## Skeleton
```
Quantitative/
  bocpd.py · io_hmm.py · micro_states.py · unified_regime.py · regime_shield_v3.py · hmm_regime.py
  har_abs_vol.py · multifractal_vol.py · quality_composite.py · factor_model_pca.py
  swap_barrier_v3.py · survival_adjustment.py · stat_tests.py
  contracts/ (feature_contract.py)   data/ (fred_feed.py, fred_backfill.py)
  bonds/ gold_etf/ stochastic/ audit/ allocation/ calibration/ sensitivity/ funds/ shared/ validation/ tests/
```

## Files (top-level)

| File | Purpose |
|---|---|
| `bocpd.py` | Bayesian online changepoint detection (hazard 0.01, max 500) |
| `io_hmm.py` | 3-state input-output HMM (EXPANSION/CHOPPY/CRISIS) |
| `micro_states.py` | 4 micro-states (Goldilocks / LCC / Liquidity / Stagflation) |
| `unified_regime.py` | regime fusion + BOCPD circuit breaker |
| `regime_shield_v3.py` | **two-layer shield**: HMM (6 canonical features) + monthly macro logit fusion |
| `har_abs_vol.py` | HAR |r| volatility (lags 1/5/22) |
| `multifractal_vol.py` | fractal vol + sector regime multipliers |
| `quality_composite.py` | quality composite score (MAD z, winsorized) |
| `factor_model_pca.py` | 12 structural factors → PCA |
| `swap_barrier_v3.py` | RS-25 swap barrier (`C·exp(−γt)+moat`) |
| `survival_adjustment.py` | survival-probability haircut on intrinsic value |
| `stat_tests.py` | statistical test helpers |

## Entrypoints
```bash
python -m Quantitative.calibration.train_unified_regime   # calibrate v5 regime
python -m validation.vol_gates --test                     # fractal-vol consistency (cross-folder)
```

## Dependencies
`config/pre_registration_v5.yaml`, `config/calibrated_unified_regime_params.json`, FRED data in `data/fred_cache/`, `ingestion/data/fred_feed.py` (auth PIT).

## Efficient Pathways
- `regime_validation`: `regime_shield_v3.py` + `calibration/train_unified_regime.py` + `validation/vol_gates.py`
- `full_pipeline`: this folder is the analysis core every arm consumes
- `gate_audit`: `contracts/feature_contract.py` (canonical vs forbidden series)