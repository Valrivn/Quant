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
