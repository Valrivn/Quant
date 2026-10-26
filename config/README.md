# `config/` — Frozen Pre-Registrations & Calibrated Parameters

## Roadmap Position
Cross-cutting (configuration of record): every frozen pre-registration, its
sha256 sidecar, and every calibrated parameter set. **The single source of
truth is `pre_registration_v7.yaml` (pinned by
`ingestion/harness/prereg.py:EXPECTED_SHA256` = `c80a47bf...`).**

## Folder Summary
- **Pre-regs**: `pre_registration_v2.yaml` (trailing CV exits), `_v3` (RS-25 swap
  gates), `_v4` (250 bps barrier variant), `_v5` (BOCPD/IO-HMM/micro-states),
  `_v6` (8-gate era — superseded), `_v7` (Sprint 6 composite DSR — FROZEN),
  `_phase8` (HAR/quality).
- **Calibrated**: `calibrated_unified_regime_params.json(_tuned).json`,
  `gate_calibration_phase9_v7.json` — each with `.sha256`.
- **Weight/factor configs**: `weights_*.yaml`, `quality_thresholds.yaml`,
  `hybrid_weights.yaml`, `reddit_weights.yaml`, `sector_beta.yaml`,
  `sector_stats.yaml`, `industry_beta.yaml`, `master_universe*.yaml`,
  `position_sizing.yaml`, `regime_thresholds.yaml`.
- **Credential/scheduler**: `reddit_credentials.yaml`, `fintech_credentials.yaml`,
  `fred_schedule.json`, `sentinel.yaml`, `etf_config.yaml`,
  `google_trends_config.json`, `damodaran_erp.yaml`, `dividend_yields.yaml`.
- **Data contracts**: `xbrl_concept_map.json(.provenance.json)`,
  `constants.py`, `weights.py`, `logging_config.py`.
- `browser_profiles/` (chromium profiles), `staging_damodaran/` (sha256'd
  yaml), `taxonomy/us-gaap-2024/`.
- Quarantined: `pre_registration_frozen.yaml.QUARANTINED_20260926_stale_hash`
  (history), `pre_registration_bogus.yaml` (negative test).

## Skeleton
```
config/
  pre_registration_v2..v7.yaml(.sha256)  pre_registration_phase8.yaml
  calibrated_unified_regime_params.json(.sha256)  gate_calibration_phase9_v7.json(.sha256)
  weights_*.yaml  quality_thresholds.yaml  regime_thresholds.yaml  sector_beta.yaml ...
  reddit_credentials.yaml  fintech_credentials.yaml  fred_schedule.json  sentinel.yaml ...
  constants.py  weights.py  logging_config.py  xbrl_concept_map.json(.provenance.json)
```

## Files (key)

| File | Purpose |
|---|---|
| `pre_registration_v7.yaml` | SPRINT 6 FROZEN design (composite DSR; sha256 c80a47bf) |
| `pre_registration_v2.yaml` | nested-CV trailing-exit design |
| `pre_registration_v5.yaml` | regime stack design |
| `calibrated_unified_regime_params.json` | calibrated regime params (sha256) |
| `gate_calibration_phase9_v7.json` | Phase 9C null calibration result (sha256) |
| `xbrl_concept_map.json` | XBRL concept mapping with provenance |

## Entrypoints
None directly. Access **only** via `ingestion.harness.prereg.load_prereg()`;
every calibrated artifact is sha256-verified before use.

## Dependencies
`ingestion/harness/prereg.py`, `validation/gate_calibration.py`,
`config/constants.py`.

## Efficient Pathways
- `gate_audit`: `pre_registration_v7.yaml` + `prereg.py` is the single source of truth — edit → broken pin, not silent behavior change
- `regime_validation`: `calibrated_unified_regime_params.json` is consumed post-pin
- `provenance_audit`: never trust `.yaml` without `.sha256`