# Quality Gate — Quant (the 90%)

**Owned by:** conductor. This is the concrete metric for THIS repo. The
concept (≥90%) is general; the commands below are project-local.

## Primary metric

Run from repo root:

```
python -m pytest tests/ -q
```

- **PASS** requires: collected tests pass rate ≥ **90%** AND **zero new
  failures** versus the last recorded run (baseline stored in
  `center/baseline-test-results.json` — update this file after every gate pass).
- **FAIL** otherwise. No task labeled "done" may proceed on FAIL.
- **Self-bootstrapping baseline:** if `center/baseline-test-results.json` does
  not exist, the FIRST gate run that PASSES writes it (with the failing-test set
  recorded). If the first run FAILS with no baseline, report the failure list as
  blocking and flag an environment check (`python -m pytest tests/ -q` needs
  pytest + project deps installed in the active env).

## Secondary checks (if available, non-blocking unless noted)

- No `.py` syntax errors anywhere (import smoke: `python -c "import compileall,sys; sys.exit(0 if compileall.compile_dir('scraper') else 1)"` on the touched package).
- No secrets committed: grep for patterns in `config/*_credentials.yaml`
  appearing in tracked non-config files.

## Conductor procedure

1. `python -m pytest tests/ -q` (capture pass rate + failures).
2. Compare failure set to baseline `center/baseline-test-results.json`.
3. Emit the Conductor report (contracts.md format) to the requester.
4. On PASS: update baseline file, clear any blocking flag, report done.
5. On FAIL: return the blocking-issue list; nothing merges, nothing is "done".

## Gate application

- T2 and T3: always.
- T1: spot-check 1 in 5 runs; always when the change touches `Qualitative/psychological/`, `db/`, or the stochastic core.
- Discovery: not required (no code changes).

## Tuning

Change the 90% threshold or the command in this file with a CEO ruling
(T3) — the gate is itself an invariant (blueprint rule 5).

## Gate log

### 2026-08-03 — D-20260803-002 (fee-sim + ledger-recording)
- Command: `python -m pytest tests/ -q`
- Result: **841 passed, 18 skipped, 0 failed** (859 collected), 64 warnings, 155.70s
- Pass rate: 0.979 (≥ 0.90) — PASS
- New failures vs baseline: 0 (baseline failing set: [])
- Baseline updated: `center/baseline-test-results.json` (2026-08-03)

### 2026-08-03 — D-20260803-003 (Phase-1 delivery: diversification/sleeves.py, diversification/macro_state.py, diversification/risk_minimizer.py, diversification/fee_sim3.py, tests/test_diversification_phase1.py, datastore.fetch_nasdaq)
- Command: `python -m pytest tests/ -q`
- Result: **857 passed, 18 skipped, 0 failed** (875 collected), 64 warnings, 146.97s
- Pass rate: 0.979 (≥ 0.90) — PASS
- New failures vs baseline: 0 (baseline failing set: [])
- Baseline updated: `center/baseline-test-results.json` (2026-08-03)

### 2026-08-03 — D-20260803-004 (Phase-2 delivery: diversification/dividend_audit.py, diversification/opportunistic.py, datastore.fetch_dividend_history additions, sleeves.py, fee_sim3.py DIVIDEND strategy, tests/test_diversification_phase2.py)
- Command: `python -m pytest tests/ -q`
- Result: **881 passed, 18 skipped, 0 failed** (899 collected), 64 warnings, 166.63s
- Pass rate: 0.980 (≥ 0.90) — PASS
- New failures vs baseline: 0 (baseline failing set: [])
- Baseline updated: `center/baseline-test-results.json` (2026-08-03)

### 2026-08-03 — D-20260803-005 (Phase-3 delivery: diversification/allocator.py, fee_sim3.py run_sim_phase3 + strategies, config/weights_diversification.yaml, sleeves.py P3_TICKERS + MDY/IWM yields, tests/test_diversification_phase3.py, one edited test in test_diversification_phase1.py)
- Command: `python -m pytest tests/ -q`
- Result: **900 passed, 18 skipped, 0 failed** (918 collected), 64 warnings, 163.54s
- Pass rate: 0.980 (≥ 0.90) — PASS
- New failures vs baseline: 0 (baseline failing set: [])
- Baseline updated: `center/baseline-test-results.json` (2026-08-03)
