# Phase 9D Full Provenance Audit v2 (deep)

- envelope: `provenance_manifest.json` (sha256 `6c7e3bf9f76d17f9c68bb1d901a2f5f5a6d9f69f068ca1bdefbe9d273dda336a`)
- chained head: `551d52103e0a59e45b25d117dc866b20e883a2b34638cc5234c716d9ef083f57`
- genesis (pre-registration v7 sha256): `c80a47bf6ae8d1c595f3b5e607c2588478bec0c47c4f0f97eb6c374b277a1685`
- artifacts: 3

| artifact | bytes | sha256 |
|---|---:|---|
| `provenance_audit_v2.json` | 3362 | `4a931ea19f83865217630342a73919dbb0249d59415e2e8b0a1e10886be035fd` |
| `provenance_audit_v1.json` | 17945 | `f735d77a57bf60fa7b188c29f804167517c1271d140de0e65a63161b72c46378` |
| `provenance_chain_verdict.json` | 3065 | `2c8cd7419227ce6723a12a6e4c693618f663ab2f3cb9ca2ba6d455830a20e918` |

## Verify

```bash
python -m validation.phase9d_emit --verify archives/phase9_provenance_audit_v2_20260930
```
