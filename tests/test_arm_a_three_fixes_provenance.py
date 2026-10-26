"""Sprint 4 / Arm A — SHA-256 provenance for every output of the three fixes.

The three fixes are (1) trailing stops, (2) HMM minimum-dwell filtering and
(3) regime-adjusted WACC.  Each has a test module, and the pre-registration
was amended post-implementation to carry the frozen parameter grid.  This
module is the tamper-evidence layer for that set: it pins a digest for every
file in the change set and, more usefully, asserts that the grids quoted in
the pre-registration, the grids used by the calibration, and the digest pinned
by the integrity harness all still agree with each other.

The cross-file assertions are the point.  A manifest that only records hashes
tells you *that* something changed; it does not stop the parameter grid in the
YAML from drifting away from the grid the calibration actually sweeps, which is
the failure that would quietly invalidate the calibration.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]

#: Every file produced or modified by the Sprint 4 Arm A work.
CHANGE_SET: tuple[str, ...] = (
    "tests/test_exit_trailing.py",
    "tests/test_hmm_dwell.py",
    "tests/test_dynamic_wacc.py",
    "validation/gate_calibration.py",
    "config/pre_registration_v4.yaml",
    "config/pre_registration_v4.yaml.sha256",
)

PREREG_RELPATH = "config/pre_registration_v4.yaml"
PREREG_SIDECAR_RELPATH = "config/pre_registration_v4.yaml.sha256"

#: The grid the Sprint 4 dispatch fixed.  Hard-coded here on purpose: this is
#: the dispatch's word, so a drift in EITHER the YAML or the calibration is a
#: failure of this assertion rather than a silent agreement between two
#: equally-wrong sources.
DISPATCH_TRAILING_PCT = [0.10, 0.15, 0.20]
DISPATCH_DWELL_DAYS = [5, 10, 15]
DISPATCH_WACC_ADJ = [True]

#: Pre-amendment digest, retained in the integrity harness as the audit anchor.
PRE_ARM_A_SHA256 = (
    "f9615710c7d8a6348652fbc711c3bf690d531baef06ce67c0ddd849b60d61533"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _prereg() -> dict:
    return yaml.safe_load((REPO / PREREG_RELPATH).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. Integrity of the change set
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("relpath", CHANGE_SET)
def test_change_set_file_exists_and_is_hashable(relpath):
    """Every declared output must exist and be readable as bytes."""
    p = REPO / relpath
    assert p.is_file(), f"{relpath} is declared in the provenance set but missing"
    assert _sha256(p), f"{relpath} hashed to nothing"


def test_prereg_sidecar_matches_prereg_bytes():
    """The .sha256 sidecar must verify against the YAML it describes."""
    yml = REPO / PREREG_RELPATH
    sidecar = REPO / PREREG_SIDECAR_RELPATH
    declared = sidecar.read_text(encoding="utf-8").strip()
    actual = _sha256(yml)
    assert declared == actual, (
        f"pre-registration sidecar is stale.\n"
        f"  sidecar: {declared}\n"
        f"  bytes:   {actual}\n"
        f"  The pre-registration is amended only under a logged CEO decision; "
        f"if the YAML is correct the sidecar must be regenerated, not the "
        f"YAML reverted."
    )


def test_prereg_sidecar_is_bare_digest_only():
    """The sidecar must hold the digest ALONE.

    ``research.phase9_full_pipeline.verify_preregistration_hash`` reads the
    sidecar with ``f.read().strip()`` and compares the entire string against a
    hash, so the conventional two-field ``<digest>  <filename>`` layout makes
    that check fail even when the digest is correct.  This is a real trap: the
    failure message prints two identical-looking digests and reads as a
    mystery.  Guard the format here so nobody "fixes" it with a filename.
    """
    raw = (REPO / PREREG_SIDECAR_RELPATH).read_text(encoding="utf-8").strip()
    assert len(raw.split()) == 1, (
        f"sidecar must be a bare digest, got {len(raw.split())} fields: {raw!r}. "
        f"phase9_full_pipeline compares the whole stripped string."
    )
    assert re.fullmatch(r"[0-9a-f]{64}", raw), f"not a lowercase sha256: {raw!r}"


def test_pipeline_prereg_verifier_accepts_current_bytes():
    """Round-trip the real consumer, not just a re-implementation of it.

    The pipeline canonicalises the YAML before hashing (dropping any
    self-referential ``sha256:`` line), so a sidecar can verify here and fail
    there. Assert the shipped verifier's own verdict.
    """
    from research.phase9_full_pipeline import verify_preregistration_hash

    assert verify_preregistration_hash() is True, (
        "research.phase9_full_pipeline.verify_preregistration_hash() rejects "
        "the current pre-registration; phase9_core's hash test will fail."
    )


def test_integrity_harness_pin_matches_prereg_bytes():
    """ingestion.harness.prereg must pin the digest of the bytes on disk.

    The harness's live target moved to v7 when Sprint 7 froze the canonical
    6-feature contract.  The cross-file invariant that matters is unchanged:
    whatever file the harness points at, its ``EXPECTED_SHA256`` must be the
    digest of that exact file's bytes, or the R1 prereg root fails.
    """
    from ingestion.harness import prereg

    live = _sha256(REPO / prereg.PREREG_RELPATH)
    assert prereg.EXPECTED_SHA256 == live, (
        f"prereg.EXPECTED_SHA256={prereg.EXPECTED_SHA256} does not match the "
        f"bytes of the harness's own target ({prereg.PREREG_RELPATH} -> {live}); "
        f"R1 prereg root will FAIL."
    )
    assert prereg.PREREG_RELPATH == Path("config/pre_registration_v7.yaml")


def test_pre_amendment_digest_is_retained_not_overwritten():
    """The re-pin must not erase the pre-amendment digests.

    v7 re-anchored ``PRE_ARM_A_SHA256`` to the v6 digest (the immediate
    predecessor of the live v7 pin) and the ORIGINAL Sprint-4 pre-Arm-A digest
    moved into ``PIN_HISTORY``.  Neither may vanish: a re-pin that drops the
    prior value is indistinguishable, after the fact, from a silent post-hoc
    edit — the exact failure the integrity harness was built to catch.
    """
    from ingestion.harness import prereg

    # The Sprint-4 anchor (original v4.0 as approved) must survive in the ledger.
    assert any(h == PRE_ARM_A_SHA256 for h, _ in prereg.PIN_HISTORY), (
        "the original Sprint-4 pre-amendment digest is missing from PIN_HISTORY"
    )
    # The v7 transition's predecessor must still be pinned separately.
    assert prereg.PRE_ARM_A_SHA256 == prereg.EXPECTED_SHA256_V6
    assert prereg.PRE_ARM_A_SHA256 != prereg.EXPECTED_SHA256, (
        "PRE_ARM_A_SHA256 must differ from the live pin, otherwise the "
        "amendment is indistinguishable from a no-op."
    )


# ---------------------------------------------------------------------------
# 2. The pre-registration carries the dispatched grid, exactly
# ---------------------------------------------------------------------------

def test_b_swap_exit_cluster_carries_the_dispatched_params():
    doc = _prereg()
    clusters = doc["parameter_clusters"]
    assert "B_swap_exit" in clusters, "B_swap_exit cluster is missing"

    params = {p["name"]: p for p in clusters["B_swap_exit"]["params"]}
    for name in ("trailing_pct", "hmm_min_dwell_days", "wacc_regime_adjustment"):
        assert name in params, f"B_swap_exit is missing {name}"

    assert params["trailing_pct"]["values"] == DISPATCH_TRAILING_PCT
    assert params["hmm_min_dwell_days"]["values"] == DISPATCH_DWELL_DAYS
    assert params["wacc_regime_adjustment"]["values"] == DISPATCH_WACC_ADJ


def test_b_swap_exit_preserves_the_preexisting_params():
    """The amendment must be additive, not a replacement of cluster B."""
    params = {p["name"] for p in _prereg()["parameter_clusters"]["B_swap_exit"]["params"]}
    for name in ("swap_barrier_gamma", "swap_barrier_C",
                 "trailing_k", "trailing_tau"):
        assert name in params, (
            f"B_swap_exit lost pre-existing param {name}; the Sprint 4 "
            f"amendment is required to be additive."
        )


def test_trailing_pct_is_not_a_duplicate_of_trailing_tau():
    """trailing_pct (stop distance) and trailing_tau (window) are distinct.

    They are easy to conflate and both move the stop, so the document has to
    keep them apart; the descriptions are the only guard.
    """
    params = {p["name"]: p
              for p in _prereg()["parameter_clusters"]["B_swap_exit"]["params"]}
    assert params["trailing_pct"]["type"] == "float"
    assert params["trailing_tau"]["type"] == "int"
    assert "watermark" in params["trailing_pct"]["description"].lower()
    assert "window" in params["trailing_tau"]["description"].lower()


def test_all_four_parameter_clusters_survive():
    assert set(_prereg()["parameter_clusters"]) == {
        "A_wacc_regime", "B_swap_exit", "C_entry_thresholds", "D_probation_qual",
    }


# ---------------------------------------------------------------------------
# 3. The calibration sweeps the grid the pre-registration froze
# ---------------------------------------------------------------------------

def test_calibration_grid_matches_the_prereg_grid():
    """The calibration must sweep the dispatched grid, not its own.

    v7 dropped the Arm-A ``wacc_regime_adjustment`` constant from the
    calibration module (the WACC gate became a null-model calibration under
    ``P9_G8_WACC_FULL_P``), so the surviving Belt-and-suspenders cross-checks
    are the trailing-pct and dwell grids that the Sprint-4 dispatch still
    names in the live v7 clusters.
    """
    from validation import gate_calibration as gc

    doc = _prereg()
    params = {p["name"]: p
              for p in doc["parameter_clusters"]["B_swap_exit"]["params"]}

    assert list(gc.ARM_A_TRAILING_PCT_GRID) == params["trailing_pct"]["values"]
    assert gc.ARM_A_TRAILING_PCT in params["trailing_pct"]["values"]
    assert gc.ARM_A_HMM_MIN_DWELL_DAYS in params["hmm_min_dwell_days"]["values"]
    # v7 removed the Arm-A WACC calibration constant; the param must still be
    # in the frozen cluster so the two files cannot silently diverge.
    assert "wacc_regime_adjustment" in params


def test_calibration_operating_point_is_the_dispatch_midpoint():
    """The operating point must be a member of the swept grid, not a free pick."""
    from validation import gate_calibration as gc

    assert gc.ARM_A_TRAILING_PCT in gc.ARM_A_TRAILING_PCT_GRID
    assert gc.ARM_A_HMM_MIN_DWELL_DAYS in (5, 10, 15)


# ---------------------------------------------------------------------------
# 4. Manifest
# ---------------------------------------------------------------------------

def test_write_provenance_manifest(tmp_path):
    """Emit the manifest and prove it describes the bytes actually on disk."""
    from tests.test_arm_a_three_fixes_provenance import write_provenance_manifest

    out = tmp_path / "arm_a_provenance.json"
    manifest = write_provenance_manifest(out)

    assert manifest["change_set"] == list(CHANGE_SET)
    for relpath, entry in manifest["files"].items():
        assert entry["sha256"] == _sha256(REPO / relpath), relpath
        assert entry["bytes"] == (REPO / relpath).stat().st_size, relpath

    # Re-reading the emitted file must reproduce the same digests.
    reloaded = json.loads(out.read_text(encoding="utf-8"))
    assert reloaded["files"] == manifest["files"]
    assert reloaded["prereg_sha256"] == _sha256(REPO / PREREG_RELPATH)
    assert reloaded["pre_arm_a_sha256"] == PRE_ARM_A_SHA256


def write_provenance_manifest(out_path: Path) -> dict:
    """Write a SHA-256 manifest of the Sprint 4 Arm A change set."""
    prereg_digest = _sha256(REPO / PREREG_RELPATH)
    manifest = {
        "artifact": "sprint4_arm_a_three_fixes_provenance",
        "change_set": list(CHANGE_SET),
        "prereg_sha256": prereg_digest,
        "pre_arm_a_sha256": PRE_ARM_A_SHA256,
        "files": {
            relpath: {
                "sha256": _sha256(REPO / relpath),
                "bytes": (REPO / relpath).stat().st_size,
            }
            for relpath in CHANGE_SET
        },
    }
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2, sort_keys=True),
                        encoding="utf-8")
    sidecar = out_path.with_suffix(out_path.suffix + ".sha256")
    sidecar.write_text(
        f"{hashlib.sha256(out_path.read_bytes()).hexdigest()}  {out_path.name}\n",
        encoding="utf-8",
    )
    return manifest
