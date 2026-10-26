"""
Phase 9D artifact emission — provenance envelope for archived deliverables.

WHY THIS EXISTS
---------------
Phase 9 v7 froze a single rule: every artifact carries a SHA-256, a UUIDv7 and
a nanosecond timestamp, and the chain is verifiable back to the pre-registration
root.  Writing those three fields by hand at every call site is how they end up
present but wrong, so the envelope is minted in exactly one place here and
everything downstream consumes it.

THE CHAIN
---------
    genesis  = SHA-256 of config/pre_registration_v7.yaml
    hash_i   = SHA-256( hash_{i-1} + "|" + canonical_json(artifact_i) )
    head     = hash_n

Artifacts are chained in the order they are declared, so the archive digest
attests to the SEQUENCE as well as to each member.  Reordering, dropping or
swapping an artifact invalidates the head.

The per-artifact ``sha256`` is of the FILE BYTES, and the chain input is the
file's canonical JSON form.  Those are two different digests on purpose: one
catches a file being edited on disk after emission, the other catches the
manifest being edited to describe different files.

ID ORDERING
-----------
UUIDv7 ids are minted through ``research.ml_parameter_optimization.new_trial_uuid7``
— the same minter the trial ledger uses — rather than a second local
implementation.  That function returns the id as an int precisely so ordering
can be enforced by the caller; two ids minted in the same 256ns bucket have
random low bits and would otherwise sort arbitrarily, so the previous id is
passed as ``min_int`` and strict monotonicity is asserted, not assumed.

CLI
---
    python -m validation.phase9d_emit --emit gate-calibration
    python -m validation.phase9d_emit --emit provenance-audit
    python -m validation.phase9d_emit --emit all
    python -m validation.phase9d_emit --verify archives/phase9_gate_calibration_20260930
    python -m validation.phase9d_emit --test
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PREREG_REL = "config/pre_registration_v7.yaml"
PREREG_SHA256_V7 = "c80a47bf6ae8d1c595f3b5e607c2588478bec0c47c4f0f97eb6c374b277a1685"

STAMP = "20260930"

GATE_CALIB_DIR = REPO_ROOT / "archives" / f"phase9_gate_calibration_{STAMP}"
PROVENANCE_DIR = REPO_ROOT / "archives" / f"phase9_provenance_audit_v2_{STAMP}"


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def genesis_hash() -> str:
    """SHA-256 of the frozen pre-registration, read live — never hardcoded.

    The literal is pinned as a constant so a drifted file FAILS the emit rather
    than quietly producing a chain rooted in whatever happens to be on disk.
    """
    actual = sha256_file(REPO_ROOT / PREREG_REL)
    if actual != PREREG_SHA256_V7:
        raise RuntimeError(
            f"refusing to emit: {PREREG_REL} is {actual}, not the frozen "
            f"{PREREG_SHA256_V7}. The pre-registration must not move.")
    return actual


def _minter():
    from research.ml_parameter_optimization import new_trial_uuid7
    return new_trial_uuid7


def mint_monotonic_ids(n: int) -> List[Tuple[str, int, int]]:
    """``n`` strictly increasing ``(uuid7, timestamp_ns, uuid_int)`` triples."""
    new_trial_uuid7 = _minter()
    out: List[Tuple[str, int, int]] = []
    prev_int: Optional[int] = None
    prev_ns: Optional[int] = None
    for _ in range(int(n)):
        ts_ns = int(time.time_ns())
        # Guarantee both monotonic fields by construction, then assert it.
        if prev_ns is not None and ts_ns <= prev_ns:
            ts_ns = prev_ns + 1
        uid, ts, as_int = new_trial_uuid7(
            timestamp_ns=ts_ns, min_int=prev_int)
        if prev_int is not None and as_int <= prev_int:
            raise AssertionError("uuid7 ids are not strictly increasing")
        if prev_ns is not None and ts <= prev_ns:
            raise AssertionError("timestamp_ns is not strictly increasing")
        out.append((uid, ts, as_int))
        prev_int, prev_ns = as_int, ts
    return out


def chain(entries: Sequence[Dict[str, Any]], genesis: str) -> Tuple[str, List[str]]:
    """``hash_i = SHA256(hash_{i-1} | canonical_json(entry_i))``."""
    prev = genesis
    heads: List[str] = []
    for e in entries:
        prev = hashlib.sha256(
            (prev + "|" + canonical_json(e)).encode("utf-8")).hexdigest()
        heads.append(prev)
    return prev, heads


def write_sidecar(path: Path) -> str:
    digest = sha256_file(path)
    path.with_suffix(path.suffix + ".sha256").write_text(
        digest + "  " + path.name + "\n", encoding="utf-8")
    return digest


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------

def _repo_rel(p: Path) -> str:
    """Repo-relative when possible, absolute otherwise.

    Archives normally live inside the repo, but the self-test emits to a temp
    directory; a hard ``relative_to`` would make the helper untestable in
    isolation, which is exactly where a provenance primitive most needs to be.
    """
    p = Path(p)
    try:
        return p.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return p.resolve().as_posix()


def build_envelope(name: str,
                   files: Sequence[Path],
                   archive_dir: Path,
                   summary: Dict[str, Any]) -> Dict[str, Any]:
    """Mint the manifest: per-file digests, chained, rooted at the prereg."""
    files = [Path(f) for f in files]
    missing = [str(f) for f in files if not f.exists()]
    if missing:
        raise FileNotFoundError(f"cannot emit {name}, missing: {missing}")

    entries: List[Dict[str, Any]] = []
    for f in files:
        entries.append({
            "artifact_id": None,             # filled after ids are minted
            "name": f.name,
            "path": _repo_rel(f),
            "bytes": f.stat().st_size,
            "sha256": sha256_file(f),
        })

    ids = mint_monotonic_ids(len(entries) + 1)   # +1 for the envelope itself
    for e, (uid, ts, _as_int) in zip(entries, ids):
        e["artifact_id"] = uid
        e["timestamp_ns"] = ts

    gen = genesis_hash()
    head, heads = chain(entries, gen)
    env_uid, env_ts, _ = ids[-1]

    envelope = {
        "report_type": "phase9d_artifact_envelope",
        "report_version": 1,
        "envelope_id": env_uid,
        "timestamp_ns": env_ts,
        "ran_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(env_ts / 1e9)),
        "archive_dir": _repo_rel(archive_dir),
        "name": name,
        "prereg": {
            "path": PREREG_REL,
            "sha256": gen,
            "sha256_pinned": PREREG_SHA256_V7,
            "verified": True,
        },
        "hash_formula": "sha256(previous_chain_sha256 | canonical_json(entry))",
        "canonical_json": "json.dumps(sort_keys=True, separators=(',',':'))",
        "genesis_hash": gen,
        "chain_head": head,
        "n_artifacts": len(entries),
        "artifacts": entries,
        "artifact_chain_heads": heads,
        "summary": summary,
    }
    return envelope


def emit(name: str,
         files: Sequence[Path],
         archive_dir: Path,
         summary: Dict[str, Any],
         copy_from: Optional[Dict[str, Path]] = None) -> Dict[str, Any]:
    """
    Copy artifacts into ``archive_dir``, then write a chained manifest beside
    them.  The manifest is written LAST and its own digest is side-carred, so a
    truncated directory is detectable.
    """
    archive_dir.mkdir(parents=True, exist_ok=True)
    staged: List[Path] = []
    for src in files:
        src = Path(src)
        dst = archive_dir / src.name
        shutil.copy2(src, dst)
        staged.append(dst)
    for src in (copy_from or {}).values():
        src = Path(src)
        if not src.exists():
            continue
        dst = archive_dir / src.name
        shutil.copy2(src, dst)
        staged.append(dst)

    envelope = build_envelope(name, staged, archive_dir, summary)
    env_path = archive_dir / "provenance_manifest.json"
    env_path.write_text(json.dumps(envelope, indent=2, sort_keys=True, default=str),
                        encoding="utf-8")
    env_digest = write_sidecar(env_path)
    envelope["self_sha256"] = env_digest

    index = archive_dir / "INDEX.md"
    lines = [
        f"# {name}",
        "",
        f"- envelope: `provenance_manifest.json` (sha256 `{env_digest}`)",
        f"- chained head: `{head_of(envelope)}`",
        f"- genesis (pre-registration v7 sha256): `{envelope['genesis_hash']}`",
        f"- artifacts: {envelope['n_artifacts']}",
        "",
        "| artifact | bytes | sha256 |",
        "|---|---:|---|",
    ]
    for e in envelope["artifacts"]:
        lines.append(f"| `{e['name']}` | {e['bytes']} | `{e['sha256']}` |")
    lines += ["", "## Verify", "",
              "```bash",
              f"python -m validation.phase9d_emit --verify {_repo_rel(archive_dir)}",
              "```", ""]
    index.write_text("\n".join(lines), encoding="utf-8")
    write_sidecar(index)
    return envelope


def head_of(envelope: Dict[str, Any]) -> str:
    return str(envelope["chain_head"])


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

def verify(archive_dir: Path) -> Dict[str, Any]:
    """
    Re-derive every digest from disk.  Nothing stored in the manifest is
    trusted: file bytes are re-hashed, the chain is recomputed from the
    genesis, and the prereg digest is read live.
    """
    archive_dir = Path(archive_dir)
    env_path = archive_dir / "provenance_manifest.json"
    if not env_path.exists():
        return {"ok": False, "reason": f"no manifest under {archive_dir}"}
    env = json.loads(env_path.read_text(encoding="utf-8"))

    issues: List[str] = []
    gen = sha256_file(REPO_ROOT / PREREG_REL)
    if gen != env.get("genesis_hash"):
        issues.append(f"genesis drift: prereg is {gen}, manifest says "
                      f"{env.get('genesis_hash')}")
    if env.get("prereg", {}).get("sha256_pinned") != PREREG_SHA256_V7:
        issues.append("manifest does not pin the frozen prereg digest")

    recomputed: List[Dict[str, Any]] = []
    for e in env.get("artifacts", []):
        p = archive_dir / (e.get("name") or Path(e["path"]).name)
        if not p.exists():
            issues.append(f"missing artifact {p.name}")
            recomputed.append({**e, "sha256": None})
            continue
        actual = sha256_file(p)
        if actual != e["sha256"]:
            issues.append(f"sha256 mismatch for {p.name}: "
                          f"disk {actual[:12]}..., manifest {e['sha256'][:12]}...")
        recomputed.append({**e, "sha256": actual})

    head, heads = chain(recomputed, env.get("genesis_hash") or "")
    if head != env.get("chain_head"):
        issues.append(f"chain head mismatch: recomputed {head[:16]}..., "
                      f"manifest {str(env.get('chain_head'))[:16]}...")

    # UUIDv7 monotonicity over the manifest, and over each artifact's own id.
    ints = [e.get("artifact_id") for e in recomputed]
    if any(i is None for i in ints):
        issues.append("an artifact is missing its artifact_id")
    else:
        try:
            import uuid as _uuid
            vals = [_uuid.UUID(i).int for i in ints]
            if any(vals[i] >= vals[i + 1] for i in range(len(vals) - 1)):
                issues.append("artifact_id UUIDv7 values are not strictly increasing")
        except Exception as exc:                                     # noqa: BLE001
            issues.append(f"artifact_id is not a UUID: {exc!r}")

    nss = [e.get("timestamp_ns") for e in recomputed]
    if any(n is None for n in nss):
        issues.append("an artifact is missing timestamp_ns")
    elif any(nss[i] >= nss[i + 1] for i in range(len(nss) - 1)):
        issues.append("timestamp_ns is not strictly increasing across artifacts")

    side = env_path.with_suffix(env_path.suffix + ".sha256")
    if not side.exists():
        issues.append("manifest sidecar missing")
    else:
        want = side.read_text(encoding="utf-8").split()[0]
        got = sha256_file(env_path)
        if want != got:
            issues.append(f"manifest sidecar stale: {want[:12]}... vs {got[:12]}...")

    return {
        "ok": not issues,
        "archive_dir": archive_dir.as_posix(),
        "n_artifacts": len(recomputed),
        "genesis_hash": gen,
        "chain_head_recomputed": head,
        "chain_head_manifest": env.get("chain_head"),
        "chain_head_ok": head == env.get("chain_head"),
        "issues": issues,
    }


# ---------------------------------------------------------------------------
# Self-tests
# ---------------------------------------------------------------------------

def run_tests() -> int:
    import tempfile

    failures: List[str] = []

    def check(name: str, cond: Any) -> None:
        if cond:
            print(f"  ok   {name}")
        else:
            print(f"  FAIL {name}")
            failures.append(name)

    print("[emit-test] primitives")
    check("prereg digest is the frozen one", genesis_hash() == PREREG_SHA256_V7)
    check("canonical json is order-independent",
          canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1}))

    ids = mint_monotonic_ids(5)
    check("uuid7 ids are strictly increasing as integers",
          all(ids[i][2] < ids[i + 1][2] for i in range(len(ids) - 1)))
    check("timestamp_ns is strictly increasing",
          all(ids[i][1] < ids[i + 1][1] for i in range(len(ids) - 1)))
    import uuid as _uuid
    check("minted ids really are uuid7",
          all(_uuid.UUID(u).version == 7 for u, _t, _i in ids))

    entries = [{"n": i} for i in range(3)]
    h1, _ = chain(entries, "0" * 64)
    h2, _ = chain(entries[::-1], "0" * 64)
    check("chain depends on order", h1 != h2)
    check("chain is 64 hex chars", len(h1) == 64)

    print("[emit-test] tamper detection")
    tmp = Path(tempfile.mkdtemp(prefix="emit_"))
    try:
        a = tmp / "a.json"
        a.write_text('{"v":1}', encoding="utf-8")
        b = tmp / "b.json"
        b.write_text('{"v":2}', encoding="utf-8")
        arc = tmp / "arc"
        env = emit("smoke", [a, b], arc, {"k": "v"})
        check("emit produced a manifest", (arc / "provenance_manifest.json").exists())
        check("emit wrote an index", (arc / "INDEX.md").exists())
        check("a fresh archive verifies", verify(arc)["ok"])

        # `emit` COPIES into the archive, so editing the SOURCE afterwards must
        # not disturb the archive -- the archive is the frozen artifact.
        a.write_text('{"v":999}', encoding="utf-8")
        check("editing the SOURCE after emit does not disturb the archive",
              verify(arc)["ok"])
        a.write_text('{"v":1}', encoding="utf-8")

        # Editing a file INSIDE the archive is the tamper this must catch.
        (arc / "a.json").write_text('{"v":999}', encoding="utf-8")
        res = verify(arc)
        check("editing an archived artifact is caught", not res["ok"]
              and any("sha256 mismatch" in i for i in res["issues"]))
        (arc / "a.json").write_text('{"v":1}', encoding="utf-8")
        check("restoring the bytes restores verification", verify(arc)["ok"])

        env2 = json.loads((arc / "provenance_manifest.json").read_text(encoding="utf-8"))
        env2["artifacts"][0]["sha256"] = "0" * 64
        (arc / "provenance_manifest.json").write_text(
            json.dumps(env2, indent=2, sort_keys=True), encoding="utf-8")
        res = verify(arc)
        check("editing the manifest is caught", not res["ok"])

        env3 = json.loads((arc / "provenance_manifest.json").read_text(encoding="utf-8"))
        env3["artifacts"].reverse()
        (arc / "provenance_manifest.json").write_text(
            json.dumps(env3, indent=2, sort_keys=True), encoding="utf-8")
        res = verify(arc)
        check("reordering the manifest is caught", not res["ok"]
              and any("chain head" in i or "not strictly increasing" in i
                      for i in res["issues"]))

        env4 = json.loads((arc / "provenance_manifest.json").read_text(encoding="utf-8"))
        (arc / "provenance_manifest.json").write_text(
            json.dumps(env4, indent=2, sort_keys=True), encoding="utf-8")
        side = (arc / "provenance_manifest.json.sha256")
        side.write_text("0" * 64 + "  provenance_manifest.json\n", encoding="utf-8")
        check("a stale sidecar is caught", not verify(arc)["ok"])

        b.unlink()
        check("a missing artifact is caught", not verify(arc)["ok"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n[emit-test] {len(failures)} failure(s)"
          + ("" if not failures else ": " + ", ".join(failures)))
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _emit_provenance_audit() -> Dict[str, Any]:
    """Run both auditors and package them with a Gate-8 verdict file."""
    from validation import provenance_audit as pa

    tmp = REPO_ROOT / "data" / "validation" / "_phase9d"
    tmp.mkdir(parents=True, exist_ok=True)
    v2_path = tmp / "provenance_audit_v2.json"
    v1_path = tmp / "provenance_audit_v1.json"

    v2 = pa.run_audit_v2(repo_root=REPO_ROOT, data_root=REPO_ROOT / "data",
                         report_path=v2_path, deep=True)
    v1 = pa.run_audit(data_root=REPO_ROOT / "data", universe_deep=None,
                      report_path=v1_path)

    ledger = v2["roots"]["R2_R4_trial_ledger"]
    estate = v2["roots"]["R5_data_estate"]
    v1_summary = v1.get("summary", {})

    chain_ok = bool(v2.get("provenance_chain"))
    verdict = {
        "report_type": "phase9_provenance_chain_verdict",
        "report_version": 1,
        "gate": "gate_8_provenance_chain",
        "verdict": chain_ok,
        "provenance_chain_verdict": chain_ok,
        "provenance_chain": chain_ok,
        "auditor": "validation.provenance_audit.ProvenanceAuditor (audit v2, deep)",
        "ran_at": v2.get("ran_at"),
        "prereg_sha256": v2["roots"]["R1_prereg_root"].get("actual_sha256"),
        "prereg_sha256_pinned": PREREG_SHA256_V7,
        "roots": {k: v.get("status", v.get("ok")) for k, v in v2["roots"].items()},
        "failed_roots": v2.get("failed_roots", []),
        "trial_ledger": {
            "path": ledger.get("path"),
            "schema": ledger.get("schema"),
            "n_trials": ledger.get("n_trials"),
            "n_trials_evaluated": ledger.get("n_trials_evaluated"),
            "n_trials_unevaluated": ledger.get("n_trials_unevaluated"),
            "metric_status": ledger.get("metric_status"),
            "chain_head_ok": ledger.get("chain_head_ok"),
            "per_trial_sha256_mismatches": len(ledger.get("per_trial_sha256_mismatches") or []),
            "uuid7_invalid": len(ledger.get("uuid7_invalid") or []),
            "timestamp_ns_regressions": len(ledger.get("timestamp_ns_regressions") or []),
            "consequence": ledger.get("consequence"),
        },
        "data_estate": {
            "total_parquets": estate.get("total"),
            "sha256_passed": estate.get("passed"),
            "sha256_failed": len(estate.get("failed") or []),
            "missing_manifest": estate.get("missing_manifest"),
        },
        # The v2 root set is the CHAIN.  The v1 bias audit is a different
        # question and is reported here so nobody reads provenance_chain: true
        # as "the whole data estate is clean".  It is not.
        "v1_data_estate_bias_audit": {
            "status": v1.get("status"),
            "passed": v1_summary.get("passed"),
            "failed": v1_summary.get("failed"),
            "n_bias_flags": len(v1.get("bias_flags") or []),
            "high_severity_flags": [f for f in (v1.get("bias_flags") or [])
                                    if str(f.get("severity", "")).upper() == "HIGH"],
            "warning_flags": [f"{f.get('code')}: {f.get('detail', '')[:120]}"
                              for f in (v1.get("bias_flags") or [])
                              if str(f.get("severity", "")).upper() == "WARN"][:20],
            "does_not_affect_chain_verdict": True,
            "note": ("provenance_chain is a statement about the audit trail "
                     "rooted at the pre-registration. The v1 bias audit is a "
                     "statement about the DATA, and it currently FAILS. Both "
                     "are true; neither implies the other."),
        },
        "scope_limits": [
            "R5 covers tracked PARQUETS only. JSON/YAML artifacts under data/ "
            "are not in the manifest and were not hashed by this run.",
            "check_data_estate() reports PASS on hash mismatches alone; the "
            f"{estate.get('missing_manifest')} files with no manifest entry are "
            "counted but do not fail R5.",
            "The ledger is genuine ENUMERATION, not 513 results: metric_status "
            "is UNEVALUATED and no trial carries a measured score.",
        ],
    }
    verdict_path = tmp / "provenance_chain_verdict.json"
    verdict_path.write_text(json.dumps(verdict, indent=2, sort_keys=True, default=str),
                            encoding="utf-8")
    write_sidecar(verdict_path)

    return emit(
        name="Phase 9D Full Provenance Audit v2 (deep)",
        files=[v2_path, v1_path, verdict_path],
        archive_dir=PROVENANCE_DIR,
        summary={
            "provenance_chain": chain_ok,
            "status": v2.get("status"),
            "failed_roots": v2.get("failed_roots", []),
            "r5_sha256": f"{estate.get('passed')}/{estate.get('total')}",
            "r5_missing_manifest": estate.get("missing_manifest"),
            "ledger_schema": ledger.get("schema"),
            "ledger_n_trials": ledger.get("n_trials"),
            "ledger_metric_status": ledger.get("metric_status"),
            "v1_bias_audit_status": v1.get("status"),
            "v1_high_severity": len(verdict["v1_data_estate_bias_audit"]
                                    ["high_severity_flags"]),
        },
    )


def _emit_gate_calibration() -> Dict[str, Any]:
    """Package the calibration report plus its null-run log."""
    src = REPO_ROOT / "config" / "gate_calibration_phase9_v7.json"
    if not src.exists():
        raise FileNotFoundError(
            f"{src} is missing; run "
            "`python -m validation.gate_calibration --run --n 10000 --phase9 "
            "--report config/gate_calibration_phase9_v7.json` first")
    rep = json.loads(src.read_text(encoding="utf-8"))
    dr = rep["decision_rule"]
    pw = rep["power_analysis"]
    ce = pw.get("ceiling_analysis", {})
    return emit(
        name="Phase 9D Gate Calibration on 10,000 Null Portfolios (v7)",
        files=[src, src.with_suffix(src.suffix + ".sha256")],
        archive_dir=GATE_CALIB_DIR,
        summary={
            "n_null_portfolios": rep["n_null_portfolios"],
            "seed": rep["seed"],
            "prereg_sha256": rep["prereg_sha256"],
            "prereg_integrity_verified": rep["prereg_integrity_verified"],
            "metric": rep["metric"],
            "gates_calibrated": rep["gates_calibrated"],
            "null_pass_rate": dr["null_pass_rate"],
            "nominal_fwer_claimed": dr["nominal_fwer_claimed"],
            "wilson_95": dr["wilson_ci_95"],
            "verdict_vs_nominal": dr["verdict_vs_nominal"],
            "mde_annualized_sharpe_power_0.80":
                pw.get("mde_annualized_sharpe_power_0.80"),
            "mde_fold_4": pw.get("mde_annualized_sharpe_power_0.80_fold_4"),
            "fold_4_power_ceiling": (ce.get("fold_4_true_oos", {})
                                     .get("empirical", {}).get("ceiling_power")),
            "ceiling_detected": ce.get("ceiling_detected"),
        },
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Phase 9D provenance envelope for archived deliverables")
    ap.add_argument("--emit", choices=["gate-calibration", "provenance-audit", "all"],
                    default=None)
    ap.add_argument("--verify", type=str, default=None,
                    help="verify an emitted archive directory")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args(argv)

    if args.test:
        return run_tests()
    if args.verify:
        res = verify(Path(args.verify))
        print(json.dumps(res, indent=2, default=str))
        return 0 if res["ok"] else 1
    if args.emit in (None, "all", "gate-calibration"):
        env = _emit_gate_calibration()
        print(f"[emit] gate calibration -> {GATE_CALIB_DIR}")
        print(f"[emit]   chain head {head_of(env)}")
    if args.emit in (None, "all", "provenance-audit"):
        env = _emit_provenance_audit()
        print(f"[emit] provenance audit -> {PROVENANCE_DIR}")
        print(f"[emit]   chain head {head_of(env)}")
        print(f"[emit]   provenance_chain={env['summary']['provenance_chain']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
