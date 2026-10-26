# Phase 9D Gate Calibration on 10,000 Null Portfolios (v7)

- envelope: `provenance_manifest.json` (sha256 `d1337d568ae86cbe7324dca8708d24a4ea821485b2eb0c7a377dd2d0d618849b`)
- chained head: `0339efbd4293128b9c5d72198a3c79064df4f715336bf88377cdadde21ffe0d1`
- genesis (pre-registration v7 sha256): `c80a47bf6ae8d1c595f3b5e607c2588478bec0c47c4f0f97eb6c374b277a1685`
- artifacts: 2

| artifact | bytes | sha256 |
|---|---:|---|
| `gate_calibration_phase9_v7.json` | 123227 | `4adfecbe365e00d24fec93c57ec474c9ddc3f9e8ef79a31b684bcb31e8400701` |
| `gate_calibration_phase9_v7.json.sha256` | 66 | `76526f27ca85d87381257167347f4e60c8508ac71393960754b2da1a5098f76d` |

## Verify

```bash
python -m validation.phase9d_emit --verify archives/phase9_gate_calibration_20260930
```
