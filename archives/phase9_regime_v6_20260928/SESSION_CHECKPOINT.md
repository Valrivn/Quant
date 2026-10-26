# Phase 9 Regime v6 - Session Checkpoint (2026-09-28)

**Verdict: FAIL - and the failure is not yet attributable to v6.** The walk-forward validation
returns ARI 0.0886 against a 0.45 bar, but the code path producing that number is a **v5-era
single-layer HMM**, not the frozen v6 3-layer architecture. Separately, the v6 stack itself has
been brought to a runnable state this session and now executes end-to-end.

**No v6 ARI number exists.** No figure below is estimated or carried forward unverified.

---

## 1. What Was Actually Done This Session

| # | Action | Verification |
|---|--------|--------------|
| 1 | Resolved the `IndentationError` preventing `Quantitative/hmm_regime.py` from importing | `python -m py_compile Quantitative/hmm_regime.py` -> exit 0 |
| 2 | Smoke tested the module end-to-end | `python -m Quantitative.hmm_regime` -> detected `SHOCK`, logL = -1495.1, 3 states |
| 3 | Re-ran walk-forward OOS validation (impossible before the fix) | exit 1, reproduced twice, identical numbers |
| 4 | Quantified the synthetic-`CESI` defect via read-only monkeypatch diagnostic | `cesi_ari_diagnostic.json` |
| 5 | Logged 18 architectural events to the college audit trail | `logs/college/2026-09-28/college.jsonl` (18/18 valid JSON) |
| 6 | Removed 56 scratch indentation-repair scripts from the repo root | 0 remaining; `hmm_regime.py` still compiles |
| 7 | Brought the v6 3-layer stack to a runnable state (5 runtime defects fixed) | `python -m Quantitative.unified_regime` -> runs to completion |

The indentation defect had been mis-diagnosed in the prior session as a single stray line near
L382. It was in fact **five separate broken regions** across two functions, and an over-eager
scripted "fix" had flattened whole blocks (loop bodies and multi-line call continuations) to
the wrong depth. The repair used targeted edits, not another script, and the scratch scripts
were then deleted so the pattern cannot repeat.

---

## 2. Validation Result - Honest FAIL

```
python -m Quantitative.regime_shield_v3 --validate --folds 4 --ari-threshold 0.45
```

| Metric | Value | Threshold | Result |
|--------|-------|-----------|--------|
| Fold 0 ARI | 0.0088 | >= 0.45 | FAIL |
| Fold 1 ARI | 0.0859 | >= 0.45 | FAIL |
| Fold 2 ARI | -0.0977 | >= 0.45 | FAIL |
| **Concat ARI** | **0.0886** | **>= 0.45** | **FAIL** |
| Passing folds | 0 / 3 | >= 3 | FAIL |
| Concat CRISIS P / R / F1 | 0.243 / 0.113 / 0.155 | - | FAIL |

Artifacts: `validation_ari_20260928.log`, `data/regime/two_layer_v1/oos_validation.json`.

---

## 3. THE CENTRAL FINDING - the validation never tested v6 (CRITICAL)

`Quantitative/regime_shield_v3.py` is the `--validate` entry point. It imports only
`load_fred_cache` and `RegimeDetector` from `hmm_regime`, then runs **its own**
`ClassWeightedGaussianHMM` / `TwoLayerRegimeEngine`. It imports **none** of:

- `Quantitative.bocpd`
- `Quantitative.io_hmm`
- `Quantitative.micro_states`
- `Quantitative.unified_regime`

It also loads **`config/pre_registration_v5.yaml`** (line 642), while the frozen, pinned
pre-registration is **v6** (`prereg.py:98` `PREREG_RELPATH`, `:104` `EXPECTED_SHA256`).

**Therefore the ARI 0.0886 result is evidence about a v5-era single-layer class-weighted HMM.
It is not evidence that the v6 BOCPD -> IO-HMM -> Micro-States design fails.** The v6 stack had
never been executed. The ARI gate remains **unmeasured** for v6, not failed by v6.

### 3.0 `IOHMM` has no `fit` method (CRITICAL)

```
public methods: ['compute_velocity_features', 'get_current_state', 'initialize', 'update']
has fit: False
```

`trans_base`, `emit_mean`, `emit_cov` are set once in `initialize()` and never estimated from
data. The v6 Tier-1 "Wind-Vane" is a **fixed-prior online filter, not a learned IO-HMM**. The
observed belief `[0.600, 0.300, 0.100]` is one forward step from the default prior. Any ARI
computed on it would measure the prior, not the model.

Related: `io_hmm.py:239-243` is a no-op transition-count block whose own comment concedes it
does not track the previous state, so the online transition-learning path is inert even for the
sufficient statistics that do accumulate.

### 3.1 v6 stack bring-up - 5 runtime defects fixed

| # | Component | Defect | Fix |
|---|-----------|--------|-----|
| 1 | `unified_regime.py::initialize` | `UnboundLocalError: IOHMMConfig` - first referenced at the construction site, then re-imported *later inside the function*, so Python scoped it as a function-local | Removed in-function re-imports; build from `self.config` |
| 1b | same | `bocpd_cfg` built from config then discarded for hard-coded `BOCPDConfig(0.01, 0.95)` | Now config-driven (behaviour-preserving - defaults are identical) |
| 2 | `micro_states.py` | `NameError: softmax` - used at line 201, never imported | `from scipy.special import softmax` (matches `io_hmm.py:25`) |
| 3 | `unified_regime.py::_compile_actions` | `NameError: macro_name` - callers pass `macro_map` (int), body tested against `'CRISIS'` / `'CHOPPY'` | Resolve name from index via `config.io_hmm_states` |
| 4 | `unified_regime.py::_compute_io_hmm_inputs` | `TypeError` - `update()` called it with no args; its return is also never consumed | Made `data` optional; documented as a batch helper |
| 5 | `unified_regime.py::_get_latest_io_inputs` | IO-HMM belief `[nan nan nan]` | See below |

**Defect 5 root cause (the NaN):**

```python
curve_level = (load_fred_cache("DGS10") - load_fred_cache("DGS2")).iloc[-1] / 100.0
```

DGS10's last observation is **2026-09-10**; DGS2's is **2026-09-24**. Pandas aligns the
subtraction on the index *union*, so every date past the shorter leg is NaN and `.iloc[-1]`
selects that NaN - which propagates through the emission log-probs into the forward step and
softmax. Fix: `.dropna().iloc[-1]`, taking the last date where **both** legs are known. This is
also the PIT-correct choice - it does not ffill a stale leg forward to manufacture a reading.

**v6 stack smoke result (single snapshot, NOT a validation):**

| Field | Value |
|-------|-------|
| regime_name | `EXPANSION/STAGFLATION` |
| confidence | 0.75 |
| macro_state | EXPANSION |
| micro_state | STAGFLATION |
| bocpd_alarm | False (P(r=0) = 0.010) |
| io_hmm_belief | `[0.59996368, 0.30000454, 0.10003178]` (was `[nan nan nan]`) |
| actions | `macro_action=HOLD`, `micro_action=ROUTE_CASH_TO_GOLD`, `wacc_adjustment_bps=0` |

All 6 v6 modules `py_compile` clean. Caveat: `STAGFLATION` as a Tier-2 micro state paired with
an `EXPANSION` macro state is internally odd - flagged for review, not endorsed.

### 3.2 v6 config naming inconsistency (LOW)

`pre_registration_v6.yaml` names the middle state **both ways in the same `hmm` block**:
`state_names = ['EXPANSION','CHOPPY','CRISIS']` but the `class_weights` keys and
`validation.label_definition` use **'SHOCK'**. `unified_regime` / `io_hmm` use CHOPPY;
`regime_shield_v3` and the label generator use SHOCK. Latent mismatch for any ARI or
confusion-matrix join keyed on state name.

---

## 4. Three further defects on the v5-era path

### 4.1 Synthetic `CESI` placeholder - violates the no-synthetic-data rule (HIGH)

`Quantitative/regime_shield_v3.py:829-832`

```python
# CESI - NOT AVAILABLE IN FRED CACHE; synthetic placeholder with fixed seed
np.random.seed(45)
feature_data["CESI"] = pd.Series(np.random.normal(0, 20, len(dates)), index=dates)
```

One of the six HMM input features is i.i.d. Gaussian noise. This directly contradicts the
standing CEO constraint that all features come from the real FRED cache, and contradicts
v6's own `synthetic_fallback: disabled`.

**Measured cost** (engine not mutated; `build_layer1_features` monkeypatched in-process):

| Variant | Fold ARIs | Concat ARI |
|---------|-----------|------------|
| baseline (synthetic CESI) | 0.0088 / 0.0859 / -0.0977 | 0.0886 |
| CESI neutralized (constant -> std 0) | 0.0902 / 0.0859 / -0.0951 | **0.1334** |

Removing the noise dimension buys **+0.0448 ARI (+50% relative)** but still leaves the model at
0.1334 against a 0.45 bar. **CESI is a genuine defect, not the primary cause.**

### 4.2 Feature-contract drift between two regime implementations (HIGH)

| | `hmm_regime.py` | `regime_shield_v3.py` |
|---|---|---|
| HMM class | `GaussianHMM` | `ClassWeightedGaussianHMM` |
| Wrapper | `RegimeDetector` | `TwoLayerRegimeEngine` |
| Feature contract | `FEATURE_NAMES` = **12** cols | **6** cols (hard-coded) |
| State ordering | `argsort(mu[:, _F_CREDIT=3])` -> expects `BAA10Y_spread` | keyed to `BAA10Y` as first feature |

`build_macro_features()` lives in `hmm_regime.py` but returns only **6** columns. So in
`RegimeDetector.retrain()`, `mu[:, 3]` is `RealizedVol21d`, **not** credit spread. States are
silently ordered by volatility rather than credit.

**Blast radius** (consume `RegimeDetector` + `build_macro_features`):

- `Quantitative/backtest_harness.py:50, 201, 336`
- `Quantitative/portfolio_orchestrator.py:36, 185, 187`

`regime_shield_v3.py` is internally consistent, so **the validation path is not affected** by
this drift - the drift threatens the backtest/orchestrator consumers, not the ARI number above.

### 4.3 Fold-count off-by-one silently under-delivers (MEDIUM)

`regime_shield_v3.py:663-670`

`--folds 4` executes only **3** folds. Fold geometry is
`train_end = (f+1)*fold_size + 252`, `test_end = min(n, (f+2)*fold_size + 252)`. With
`n = 1826`, `fold_size = 456`, fold 3 gets `test_start = 2076 >= n` and the loop breaks.

Note `folds_required = 3` in v6 matches the 3 folds that actually run, so this is **not** a
violation of the pre-registration. The defect is that the CLI accepts `--folds` and silently
ignores it beyond the break - a caller asking for 4 silently gets 3 with no warning.

---

## 5. Contributing Weaknesses in the Labels

| Fold | EXPANSION | SHOCK | CRISIS | Note |
|------|-----------|-------|--------|------|
| full window | 1166 | 392 | 269 | 2018-01-01..2024-12-31 |
| fold 1 | 260 | 92 | 104 | |
| fold 2 | 152 | 191 | 113 | SHOCK-heavy |
| fold 3 | 181 | 26 | **0** | **zero CRISIS - ARI degenerate** |

Fold 3 contains no CRISIS observations at all, so the 3-state problem is ill-posed there and
its ARI is not meaningful. Combined with the ~65/20/15 EXPANSION/SHOCK/CRISIS imbalance, the
label set is too sparse and too imbalanced to support a well-identified 3-state HMM over a
~456-day test window.

---

## 6. Recommended Next Moves (in priority order)

1. **Decide what is being validated.** Either wire a validation entry point to
   `unified_regime` so v6 is actually measured, or formally retire v6 and continue on the
   v5-era engine. Right now the frozen pre-registration and the tested code are different
   systems. Nothing else can be trusted until this is settled.
2. **Give `IOHMM` a fitting procedure**, or drop the IO-HMM from the architecture and amend
   v6. A fixed-prior filter cannot satisfy an ARI gate and should not pretend to.
3. **Rule on the `CESI` feature** - source a real series or drop the column. Either way it is a
   pre-registration change requiring a new version, new digest, and sidecar re-pin.
4. **Rule on the canonical feature contract** - one engine, one `FEATURE_NAMES` (fixes 4.2 for
   the backtest and orchestrator consumers).
5. **Fix the fold geometry** so `--folds N` yields N folds or fails loudly.
6. **Address label sparsity** - 2-state (EXPANSION/CRISIS) with SHOCK as a derived transition
   is the fallback already contemplated; the fold-3 zero-CRISIS result argues for it.
7. Only then re-run. Best historical anchors for a reachable target: 2-state ARI 0.5175,
   static 3-state GMM on 6 features ARI 0.48 - both above the 0.45 bar.

---

## 7. Files

| File | Role |
|------|------|
| `validation_ari_20260928.log` | Raw validation output (exit 1) |
| `cesi_ari_diagnostic.json` | CESI ARI-cost experiment |
| `SESSION_CHECKPOINT.md` | This document |
| `../log_regime_v6_checkpoint.py` | Reproducible college-logger emitter (validation + v5-path defects) |
| `../log_regime_v6_stack_checkpoint.py` | Reproducible college-logger emitter (v6 stack bring-up) |
| `../diagnose_cesi_ari.py` | Reproducible CESI diagnostic |

## 8. Pre-Registration Status

The authoritative frozen pre-registration is **v6**:
`config/pre_registration_v6.yaml`, SHA-256
`390084e01fae6a358ab66b5ee536ccaf4d5d85bc6ec4dd05103cb74d5a5c5b10`, `status: APPROVED`,
`version: 6.0`, 26,440 bytes, verified via
`python -m ingestion.harness.prereg --verify-prereg` (`verified=True`, `VERIFIED`).

Pinned at `ingestion/harness/prereg.py:98` (`PREREG_RELPATH`) and `:104` (`EXPECTED_SHA256`).

**No pre-registration file was modified this session and no SHA-256 re-pin was performed.**
`config/pre_registration_v5.yaml` (SHA-256
`9d0d82c91d56e92fc25bee3e58e574a7ffd1a63b8b8a3d3231cf2c8caaec6704`) is what the validation
harness actually reads.

Any change arising from Section 6 items 3-4 requires a new version, a new digest, and a
sidecar re-pin before re-execution.
