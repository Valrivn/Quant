# Quant-Py — MASTER ROADMAP

A three-pillar quantitative investment research platform: **regime-aware
quantitative selection** (Sprint 6 composite-DSR gate), **qualitative
alternate-data NLP** (sentiment, hiring, moat), and **Monte Carlo valuation**
(EVA-modal gates, survival-adjusted intrinsic value) — fused into
conviction-scored, risk-adjusted portfolios across three arms (A: pure quant
RS-05, B: pure qual z-composite, C: hybrid overlay).

> **Operating note:** `agent_orchestrator.py` is the plan-first orchestrator —
> every Tier-2/3 build must pass a ≥0.90 confidence planning gate
> (`ops/planning_gate.py`) before execution. Pre-registration v7 is FROZEN and
> SHA-256 pinned (`config/pre_registration_v7.yaml` → `c80a47bf...`,
> enforced by `ingestion/harness/prereg.py`). **Do not edit any frozen artifact
> without a CEO-ruled pin change.**

---

## 1. Master Pipeline (ordered)

| # | Stage | Owner folders | Status | Deep dive |
|---|---|---|---|---|
| 0 | Init & decisions | `startup/`, `docs/`, `archives/` | ✅ | — |
| 1 | Ingestion & PIT data | `ingestion/`, `db/`, `config/` | ✅ (live) | `ingestion/harness/DEEP_DIVE.md` |
| 1.5 | Universe discovery | `discovery/` | ✅ (live) | — |
| 2 | Analysis core (regime/vol/quality) | `Quantitative/` | ✅ (v5 shield + two-layer) | `Quantitative/DEEP_DIVE.md` |
| 2.5 | Backtest kernel & research | `backtesting/`, `research/`, `optimization/` | ✅ | `backtesting/DEEP_DIVE.md`, `research/DEEP_DIVE.md` |
| 3 | **Gatekeeper (falsification-first)** | `validation/`, `ingestion/harness/` | ✅ **FROZEN (v7 composite DSR)** | `validation/DEEP_DIVE.md`, `ingestion/harness/DEEP_DIVE.md` |
| 3.5 | Risk state & guards | `regime/` | ✅ | — |
| 4 | Portfolio & diversification | `portfolio/`, `diversification/` | ✅ | — |
| 5 | Execution & TCA | `execution/` | ✅ (fee_sim3 default) | — |
| 6 | Reporting | `center/`, `valuation_alpha/` | ✅ | — |
| 7 | Monitoring | `dashboard/` | ✅ | — |
| 8 | Operations | `scripts/` (incl. `diagnostics/`, `runs_logs/`) | ✅ (reorg complete) | — |

**Status legend:** ✅ = working/live · 🧊 = frozen (do not edit without pin change) ·
⚠ = pre-existing issue (documented) · 🚧 = needs ruling / placeholder

## 2. Folder Index

| Folder | Purpose | Docs |
|---|---|---|
| `startup/` | Founding briefs + decision logs | `startup/README.md` |
| `ingestion/` | Loaders, PIT provenance, **harness (3-arm + gate engine)** | `ingestion/README.md`, `ingestion/harness/DEEP_DIVE.md` |
| `Quantitative/` | Regime, vol, quality, valuation math | `Quantitative/README.md`, `Quantitative/DEEP_DIVE.md` |
| `validation/` | **Gate stack (composite DSR, null calibration)** | `validation/README.md`, `validation/DEEP_DIVE.md` |
| `backtesting/` | Walk-forward harness, chi-square gate | `backtesting/README.md`, `backtesting/DEEP_DIVE.md` |
| `research/` | Survival surfaces, lifecycle, grids | `research/README.md`, `research/DEEP_DIVE.md` |
| `portfolio/` | Position state, allocation | `portfolio/README.md` |
| `regime/` | De-risk / embargo / high-bar guards | `regime/README.md` |
| `valuation/` | V1 intrinsic + **EVA modal gate** | `valuation/README.md` |
| `valuation_alpha/` | Alpha pipeline (moat, reinvestment, PIT store) | `valuation_alpha/README.md` |
| `execution/` | TCA v1/v2 | `execution/README.md` |
| `optimization/` | Optuna + A/B | `optimization/README.md` |
| `discovery/` | Frontier, consensus, sentinel | `discovery/README.md` |
| `diversification/` | Sleeves, risk models, **fee_sim3** | `diversification/README.md` |
| `Qualitative/` | Psychological + scraper lines | `Qualitative/README.md` |
| `center/` | Decision artifacts | `center/README.md` |
| `dashboard/` | Streamlit tabs | `dashboard/README.md` |
| `docs/` | Architecture/decision context | `docs/README.md` |
| `scripts/` | Ops, diagnostics, runs_logs | `scripts/README.md` |
| `tests/` | ~150 test modules | `tests/README.md` |
| `config/` | **Frozen pre-regs + calibrated params (sha256)** | `config/README.md` |
| `data/` | Data lake (git-ignored except validation/provenance) | `data/README.md` |
| `logs/` | Runtime logs (scratch — not evidence) | `logs/README.md` |
| `archives/` | Phase snapshots & debates | `archives/README.md` |
| `db/` | SQLite layer (`DB_PATH = "reddit_quant.db"`) | `db/README.md` |
| `commands/` | CLI placeholder | `commands/README.md` |
| `opencode_scripts/` | Orchestrator tooling, lanes | `opencode_scripts/README.md` |
| `ops/` | Workstream A planning gates (🚧 not yet created) | `ops/README.md` |

Root-level `harness/` and `Instructions/` are **not on the reorganization list**
(left in place by design).

## 3. Root Keepers & Ruling Queue

These files intentionally remain at root; do **not** move them:

- `agent_orchestrator.py` — plan-first orchestrator entrypoint.
- `gate_engine.py` — root copy imported by `tests/test_workstream_b_config_composite.py:29`.
- `reddit_quant.db` (203 MB) — required by `db/connection.py:6`; ~40 modules
  depend on it. Moving it requires a `db/connection.py` change first → **needs
  CEO ruling**.
- `startup.py`, `test_workstream_a.py`, `opencode.json`, `.env`,
  `skills-lock.json`, `README.md`.

**NEEDS_RULING (candidate moves deferred):** `real_backtest.py`,
`walkforward_backtest.py`, `verify_universe.py`, `alpha_analysis.py`,
`diagnose_cesi_ari.py`, `build_prereg_v7.py`, `build_valuation_table.py`,
`extract_raw_xbrl.py`, `log_regime_v6_stack_checkpoint.py` /
`log_regime_v6_verification_checkpoint.py` (interdependent pair),
`sprint1_parallel_ingest.py` (imported by 3 discovery modules),
`etf_selection_cache.json`, `college_append.jsonl`, `pip_err.txt`,
`psychological` / `scraper` pointer files, `8/`, `lane_results/`,
`cookiesinstagram.txt`.

## 4. Core Entrypoints

```bash
# Build the three arms (monthly, PIT) — then composite + gate
python -m ingestion.harness.three_arm_runner
python -m validation.composite_score

# Null calibration of the frozen v7 gate (10k Dirichlet(1) zero-alpha portfolios)
python -m validation.gate_calibration --run --n 10000 --phase9

# Nested CV: trailing fractal exit vs fixed exit
python -m validation.trailing_cv --run

# Volatility consistency & swap churn gates
python -m validation.vol_gates --test
python -m validation.swap_gates_v3 --run

# Regime calibration
python -m Quantitative.calibration.train_unified_regime

# Tests (scoped collect — repo-wide collect hangs, pre-existing)
python -m pytest tests --collect-only -q -p no:cacheprovider

# Dashboard
streamlit run dashboard/stream_quant.py
```

## 5. Efficient Pathways

Self-contained, pre-registered, falsification-first routes (labels are resolved
by `ops/efficient_paths.py` once Workstream A lands it):

| Label | Path |
|---|---|
| `universe_build` | `discovery/` frontier+consensus → `ingestion/universe_builder.py` → `ingestion/persistence/merged_universe.py` |
| `regime_validation` | `Quantitative/regime_shield_v3.py` → `validation/vol_gates.py` → `regime/` guards |
| `three_arm_backtest` | `ingestion/harness/three_arm_runner.py` → `validation/composite_score.py` → `ingestion/harness/gate_engine.py` (v7 composite DSR) |
| `gate_audit` | `ingestion/harness/prereg.py` (pin check) + `validation/gate_calibration.py` (null rate) + `config/gate_calibration_phase9_v7.json` |
| `provenance_audit` | `data/provenance/manifest.jsonl` + `data/**/*.provenance.json` + `opencode_scripts/scan_db.py` |
| `full_pipeline` | Stages 1 → 8 above; attribution at `data/backtests/attribution_20261008/` |

## 6. Known State (as of 2026-10-11)

- **Workstream B (organization + docs):** sweep complete — all `.py` moved
  modules verified import-clean; 5 DEEP_DIVEs + 28 folder READMEs added; this
  roadmap rebuilt. See `docs/WORKSTREAM_B_REORGANIZATION_REPORT.md`.
- **Pre-existing (not caused by reorganization):** repo-wide `pytest
  --collect-only` hangs (scope to `tests/`); `tests/test_hmm_dwell.py` and
  `tests/test_phase9_core.py` fail to collect (`FactorHMMv3` import — see
  `Quantitative/DEEP_DIVE.md` §5); `scripts/runs_logs/temp_pop.py` and
  `write_v1_final.py` are junk stubs failing `py_compile`.
- **CESI ban:** the synthetic CESI series is permanently denied
  (`Quantitative/contracts/feature_contract.py`); replacement is
  UNANTICIPATED_INFLATION (CPI YoY − T10YIE).
- **Mock value:** `validation/boundary_hitrate.py:oos_hit_rate = 0.62` is a
  hardcoded mock until the Phase 10 OOS walk completes.

## 7. Active Benchmark Strategy Results (Quantitative Pure Value + 15% Bond Cushion)

> **Scope Note:** This benchmark represents the **Quantitative Fundamental Pillar (Arm A)** only. The Qualitative Alternate Data NLP Pillar (Arm B) and Markov Hybrid Regime Overlay (Arm C) will layer on top of this foundation.

### How We Conduct Our Backtests
Our backtesting framework enforces strict institutional simulation standards:
1. **100% Real Point-In-Time Fundamentals:** Sourced from 83,265 genuine SEC XBRL filings across 2,179 tickers (`data/pit_fundamentals/valuation_fundamentals.parquet`). Lookahead bias is eliminated by keying all accounting variables to `filed_date` rather than quarter-end dates.
2. **Point-In-Time Universe:** 84 monthly slices of 1,000 liquid large/mid-cap equities (`merged_pit_universe.parquet`) spanning January 2018 to December 2024.
3. **Price Integrity:** Split- and dividend-adjusted daily OHLCV prices from `data/source/yfinance/` matched on `filed_date + 1 day`.
4. **Logistical Overvaluation & Thesis-Breach Exit Engine:** Stocks are bought when deeply undervalued ($\text{ROIC} > \text{WACC}$, positive FCF) and held until objective exit triggers fire:
   * **Take-Profit / Overvaluation Ceiling:** $P \ge 1.15 \times IV_{\text{Fair}}$ or companion regression residual $\epsilon_i \ge +2.0\sigma$.
   * **Thesis Breach:** $\text{ROIC} \le \text{WACC}$ ($\Delta_{\text{EVA}} \le 0$).
   * **Moat Decay:** Gross Margin decays $> 300\text{ bps}$ YoY or Operating Cash Flow turns negative.
   * *Impact:* Slashed average monthly portfolio turnover from **42.53% down to 7.16%**.
5. **Asset Allocation & Black-Swan Buffer:** 85% active undervalued compounders, 15% short-term Treasuries (`BIL`) for downside preservation and coupon carry.
6. **Dual-Regime Execution Friction:** Evaluated under both **Sunny Day (15 bps TCA drag)** and **Market Stress / Liquidity Crisis (50 bps TCA drag)**.
7. **The 4-Gate Statistical Falsification Suite (`validation/falsification_battery.py`):**
   * **Gate 1 (FF5 Regression):** Strips $Mkt-RF$, $SMB$, $HML$, $RMW$, $CMA$ beta. Requires $\alpha > 0$ and $t \ge 2.0$.
   * **Gate 2 (Moving-Block Bootstrap):** Resamples $B=1,000$ contiguous 12-month blocks. Requires empirical $p < 0.05$.
   * **Gate 3 (Deflated Sharpe Ratio):** Penalizes for multi-trial selection bias. Requires $\text{DSR} \ge 0.85$.
   * **Gate 4 (Regime Consistency):** Requires win-rate consistency across both Bull and Bear market regimes.

---

### Quantitative Strategy Performance vs. S&P 500 (2018–2024)
*(Initial Capital: **\$10,000.00 USD** | Evaluation Window: 83 Months)*

| Metric | Optimal Value (Sunny Day 15 bps) | Optimal Value (Crisis Day 50 bps) | S&P 500 Benchmark (SPY) | Advantage vs. SPY (Sunny Day) |
| :--- | :---: | :---: | :---: | :---: |
| **Initial Capital** | **\$10,000.00** | **\$10,000.00** | **\$10,000.00** | — |
| **Final Portfolio Wealth** | **\$30,438.83** | **\$29,817.51** | **\$23,283.20** | **+\$7,155.63 (+30.73% more)** |
| **Total Cumulative Net Return** | **+204.39%** | **+198.18%** | **+132.83%** | **+71.56%** |
| **Annualized Return (CAGR)** | **+17.46%** | **+17.11%** | **+13.00%** | **+4.46% per year** |
| **Annualized Volatility** | **18.63%** | **18.63%** | **17.25%** | +1.38% |
| **Net Sharpe Ratio ($r_f = 2.0\%$)** | **0.83** | **0.81** | **0.64** | **+0.19** |
| **Maximum Drawdown** | **-20.77%** | **-20.83%** | **-23.93%** | **+3.16% shallower** |
| **Calmar Ratio** | **0.84** | **0.82** | **0.54** | **+0.30** |
| **Average Monthly Turnover** | **7.16%** | **7.16%** | — | **-35.37% vs baseline (42.5%)** |
| **Bull Month Win Rate ($R_p > R_{\text{SPY}}$)** | **56.36%** (31 / 55) | **56.36%** | — | **Steady Bull Outperformance** |
| **Bear Month Win Rate ($R_p > R_{\text{SPY}}$)** | **67.86%** (19 / 28) | **67.86%** | — | **Exceptional Bear Defense** |

#### Statistical Falsification Audit:
* **Gate 1 (FF5 Alpha):** $\alpha = +6.80\%$/yr ($t = 2.27, p = 0.0257$) under 15 bps; $\alpha = +6.49\%$/yr ($t = 2.17, p = 0.0328$) under 50 bps $\rightarrow$ **PASS**
* **Gate 2 (Bootstrap $B=1,000$):** Empirical $p(\alpha \le 0) = 0.0130 < 0.05$ $\rightarrow$ **PASS**
* **Gate 3 (Deflated Sharpe Ratio):** $\text{DSR} = 0.8763$ across candidate trials $\rightarrow$ **PASS**
* **Gate 4 (Regime Consistency):** Bull Win 56.4% / Bear Win 67.9% $\rightarrow$ **PASS**

---

## License

Internal use only.