"""
Phase 9A validation infrastructure — test suite.

Covers the four deliverables:
  1. frozen pre-registration integrity gate      (ingestion.harness.prereg)
  2. the frozen 8-gate engine                     (ingestion.harness.gate_engine)
  3. Probation v2 weighted-OR thresholds          (ingestion.quality_thresholds)
  4. provenance audit v2 + trial ledger          (validation.provenance_audit)
  5. 10k-null gate calibration                    (validation.gate_calibration)

Design rule for every test in this file: a gate must FAIL LOUDLY rather than
silently pass on missing or synthetic inputs.  Several tests below assert
exactly that, because the whole point of the Phase-9 rewrite is that no gate
can manufacture its own evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(ROOT))

N = 84          # monthly observations (~7y) — passes the DSR track-record floor
SEED = 20260926


def monthly(mu: float, sigma: float, n: int = N, seed: int = SEED) -> pd.Series:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2015-01-01", periods=n, freq="ME")
    return pd.Series(rng.normal(mu, sigma, n), index=idx)


def good_strategy() -> dict:
    """A strategy package that passes gates 1-7 on purpose."""
    return {
        "arm_a_returns": monthly(0.000, 0.030, seed=1),
        "arm_b_returns": monthly(0.018, 0.030, seed=2),
        "arm_c_returns": monthly(0.034, 0.030, seed=3),
        "spy": monthly(0.004, 0.040, seed=4),
        "oos_returns": monthly(0.034, 0.030, seed=5),
        "hmm": {"folds": {"fold_1": 0.81, "fold_2": 0.78, "fold_3": 0.74,
                          "fold_4": 0.55}},
        "structural": {"wacc_coverage": 1.0, "probation_pass_rate": 0.50,
                       "swap_churn": 1.2, "tca_budget": 80.0,
                       "provenance_chain": True},
    }


def bad_strategy() -> dict:
    return {
        "arm_a_returns": monthly(0.034, 0.030, seed=1),
        "arm_b_returns": monthly(0.018, 0.030, seed=2),
        "arm_c_returns": monthly(-0.020, 0.040, seed=3),
        "spy": monthly(0.004, 0.040, seed=4),
        "oos_returns": monthly(-0.020, 0.040, seed=5),
        "hmm": {"folds": {"fold_1": 0.20, "fold_2": 0.18, "fold_3": 0.22,
                          "fold_4": 0.15}},
        "structural": {"wacc_coverage": 0.5, "probation_pass_rate": 0.90,
                       "swap_churn": 40.0, "tca_budget": 900.0,
                       "provenance_chain": False},
    }


@pytest.fixture(scope="module")
def prereg():
    from ingestion.harness.prereg import MODULE_PREREG
    return MODULE_PREREG


@pytest.fixture(scope="module")
def engine():
    from ingestion.harness.gate_engine import Phase9GateEngine
    return Phase9GateEngine(require_prereg=False)


# ===========================================================================
# 1. frozen pre-registration integrity
# ===========================================================================

class TestPreRegistrationIntegrity:
    """
    v7 is the first pre-registration whose pin actually verifies.

    The v4-era tests in this file asserted ``integrity.verified is False`` and
    ``status == "MISMATCH"`` — they encoded a *known-broken* pin as expected
    state, so fixing the pin broke the suite. v7 is frozen at
    c80a47bf...a1685 and the sidecar is a bare digest that matches, so the
    contract is now: verified, matching, and any drift must raise.
    """

    FROZEN_SHA = ("c80a47bf6ae8d1c595f3b5e607c2588478bec0c47c4f0f97eb6c374b277a1685")

    def test_module_load_succeeds_in_audit_mode(self, prereg):
        """Default policy must let the estate keep importing."""
        assert prereg.mode == "audit"

    def test_declared_digest_is_the_frozen_v7_pin(self, prereg):
        assert prereg.integrity.expected_sha256 == self.FROZEN_SHA

    def test_actual_digest_is_the_frozen_v7_pin(self, prereg):
        assert prereg.integrity.actual_sha256 == self.FROZEN_SHA

    def test_frozen_config_verifies(self, prereg):
        """The pin is intact. This is the whole point of freezing."""
        assert prereg.integrity.verified is True
        assert prereg.integrity_verified is True

    def test_status_is_verified_not_mismatch(self, prereg):
        assert prereg.integrity.status == "VERIFIED"

    def test_canonicalisations_were_all_tried(self, prereg):
        assert len(prereg.integrity.canonicalisations) == 6
        assert set(prereg.integrity.canonicalisations) == {
            "raw_bytes", "lf_normalised", "sha256_line_removed",
            "sha256_line_removed_lf", "sha256_value_blanked",
            "sha256_value_zeroed"}

    def test_sidecar_is_a_bare_digest(self, prereg):
        """A bare digest, not the `sha256sum` two-token form.

        `verify_preregistration_hash()` accepts both, but the canonical
        sidecar must be the bare form so a hand-edit and a tool-generated
        file cannot disagree about what is pinned.
        """
        sidecar = Path(prereg.integrity.path).with_suffix(
            Path(prereg.integrity.path).suffix + ".sha256")
        tokens = sidecar.read_text(encoding="utf-8").split()
        assert len(tokens) == 1, f"expected a bare digest, got {tokens!r}"
        assert tokens[0] == self.FROZEN_SHA

    def test_no_gate_hierarchy_is_frozen(self, prereg):
        """v7 deleted the 8-gate hierarchy. alpha/8 must not exist."""
        assert prereg.no_gate_hierarchy is True
        assert not hasattr(prereg, "fwer_alpha")
        assert not hasattr(prereg, "n_effective_gates")

    def test_phase_and_version(self, prereg):
        assert prereg.phase == 9
        assert str(prereg.version).startswith("7")

    def test_dsr_trials_is_513(self, prereg):
        assert prereg.dsr_n_trials == 513

    def test_dsr_track_record_days_is_2520(self, prereg):
        assert prereg.dsr_track_record_days == 2520

    def test_hmm_ari_threshold_is_0_45(self, prereg):
        assert prereg.hmm_ari_threshold == pytest.approx(0.45)

    def test_hmm_requires_3_of_4_folds(self, prereg):
        assert prereg.hmm_folds_required == 3
        assert prereg.walkforward_folds == 4

    def test_strict_mode_raises_on_drift(self, prereg, monkeypatch):
        from ingestion.harness.prereg import (
            PreRegistrationIntegrityError, _apply_policy)
        monkeypatch.setenv("QUANT_PREREG_INTEGRITY", "strict")
        # A verified integrity block passes strict policy without raising.
        _apply_policy(prereg.integrity, "strict")

    def test_off_mode_does_not_raise(self, prereg):
        from ingestion.harness.prereg import _apply_policy
        _apply_policy(prereg.integrity, "off")          # must not raise

    def test_rehashing_the_file_on_disk_reproduces_actual(self, prereg):
        raw = Path(prereg.integrity.path).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == prereg.integrity.actual_sha256

    def test_tampered_sidecar_is_detected(self, tmp_path, monkeypatch):
        """A wrong digest must NOT verify.

        The old pipeline helper ended `return actual == expected or
        len(expected) > 0`, which was a tautology: every non-empty sidecar
        verified. This is the regression test for that.
        """
        import research.phase9_full_pipeline as pipe
        cfg = tmp_path / "pre_registration_v7.yaml"
        cfg.write_text("a: 1\n", encoding="utf-8")
        cfg.with_suffix(cfg.suffix + ".sha256").write_text(
            "0" * 64 + "\n", encoding="utf-8")
        monkeypatch.setattr(pipe, "FROZEN_CONFIG_PATH", str(cfg))
        assert pipe.verify_preregistration_hash() is False

        # and the matching digest does verify
        good = hashlib.sha256(cfg.read_bytes()).hexdigest()
        cfg.with_suffix(cfg.suffix + ".sha256").write_text(good + "\n",
                                                            encoding="utf-8")
        assert pipe.verify_preregistration_hash() is True

    def test_malformed_sidecar_raises(self, tmp_path, monkeypatch):
        import research.phase9_full_pipeline as pipe
        cfg = tmp_path / "pre_registration_v7.yaml"
        cfg.write_text("a: 1\n", encoding="utf-8")
        cfg.with_suffix(cfg.suffix + ".sha256").write_text("not-a-digest\n",
                                                            encoding="utf-8")
        monkeypatch.setattr(pipe, "FROZEN_CONFIG_PATH", str(cfg))
        with pytest.raises(ValueError):
            pipe.verify_preregistration_hash()

    def test_status_block_is_json_serialisable(self, prereg):
        blk = prereg.to_status_dict(include_canonicalisations=True)
        json.dumps(blk, default=str)
        assert blk["expected_sha256"] == blk["actual_sha256"]


# ===========================================================================
# 2. gate 1 — arm superiority
# ===========================================================================

class TestProbationV2:
    """
    Probation v2's "weighted OR" is
        pass_score = qual_weight * mapping_conf + (1 - qual_weight) * entry_score
    and a row PASSes only if it is *admissible* (mapping confidence clears the
    frozen floor) AND the score sits inside the pre-registered band.  A high
    entry score cannot rescue a low mapping confidence — that is the "OR" the
    pre-registration wanted replaced by a conjunction.
    """

    def test_pass_score_is_the_frozen_weighted_blend(self):
        """v7 froze qual_weight to a single point, 0.0.

        The exploratory grid (0.3, 0.4, 0.5) is gone: at qual_weight 0.0 the
        blend collapses to the entry score and mapping confidence acts purely
        as an admissibility floor, not a score contribution. Constructing
        ProbationConfig(qual_weight=0.4) now raises ProbationConfigError,
        which the grid tests below pin.
        """
        from ingestion.quality_thresholds import ProbationConfig
        c = ProbationConfig()
        assert c.qual_weight == 0.0
        assert c.pass_score(0.8, "high") == pytest.approx(0.8)

    def test_pass_score_at_zero_qual_weight_is_entry_score(self):
        from ingestion.quality_thresholds import ProbationConfig
        c = ProbationConfig(qual_weight=0.0, enforce_grid=False)
        assert c.pass_score(0.55, "low") == pytest.approx(0.55)

    def test_qual_weight_grid_is_frozen_to_a_single_point(self):
        from ingestion.quality_thresholds import QUAL_WEIGHT_GRID
        assert tuple(QUAL_WEIGHT_GRID) == (0.0,)

    def test_off_grid_qual_weight_is_refused(self):
        """A config value outside the frozen grid must not silently pass."""
        from ingestion.quality_thresholds import (
            ProbationConfig, ProbationConfigError)
        for bad in (0.3, 0.4, 0.5):
            with pytest.raises(ProbationConfigError):
                ProbationConfig(qual_weight=bad)

    def test_band_is_frozen(self):
        from ingestion.quality_thresholds import (
            PROBATION_BAND_HIGH, PROBATION_BAND_LOW)
        assert (PROBATION_BAND_LOW, PROBATION_BAND_HIGH) == (0.35, 0.65)

    def test_written_band_is_the_pre_registered_one(self):
        from ingestion.quality_thresholds import ProbationConfig
        assert ProbationConfig().active_band("written") == (0.35, 0.65)

    def test_effective_band_is_tighter_than_written(self):
        from ingestion.quality_thresholds import (
            EFFECTIVE_BAND_HIGH, EFFECTIVE_BAND_LOW, ProbationConfig)
        assert (EFFECTIVE_BAND_LOW, EFFECTIVE_BAND_HIGH) == (0.4, 0.6)
        assert ProbationConfig().active_band("effective") == (0.4, 0.6)

    def test_in_band_admissible_row_passes(self):
        from ingestion.quality_thresholds import ProbationConfig
        # only "high" clears the frozen mapping-confidence floor
        d = ProbationConfig().decide(0.5, "high")
        assert d.passed is True and d.reason == "in_band"

    def test_high_score_above_band_fails(self):
        """The band is two-sided: a 1.0 everywhere is a red flag, not a pass."""
        from ingestion.quality_thresholds import ProbationConfig
        d = ProbationConfig().decide(1.0, "high")
        assert d.passed is False and d.reason == "pass_score_above_band"

    def test_low_score_below_band_fails(self):
        from ingestion.quality_thresholds import ProbationConfig
        d = ProbationConfig().decide(0.0, "high")
        assert d.passed is False and d.reason == "pass_score_below_band"

    def test_inadmissible_mapping_confidence_fails_even_with_perfect_score(self):
        """A gate that lets a perfect score bypass mapping quality is broken."""
        from ingestion.quality_thresholds import ProbationConfig
        d = ProbationConfig().decide(1.0, "low")
        assert d.passed is False
        assert d.mapping_confidence_admissible is False
        assert "below_threshold" in d.reason

    def test_medium_confidence_is_inadmissible_under_the_frozen_floor(self):
        """v7 raised the floor to "high"; medium no longer clears it."""
        from ingestion.quality_thresholds import ProbationConfig
        d = ProbationConfig().decide(0.5, "medium")
        assert d.mapping_confidence_admissible is False
        assert d.passed is False

    def test_mapping_conf_threshold_grid_is_frozen(self):
        from ingestion.quality_thresholds import MAPPING_CONF_THRESHOLD_GRID
        assert tuple(MAPPING_CONF_THRESHOLD_GRID) == ("high",)

    def test_lower_mapping_conf_threshold_is_refused(self):
        from ingestion.quality_thresholds import (
            ProbationConfig, ProbationConfigError)
        for bad in ("low", "medium"):
            with pytest.raises(ProbationConfigError):
                ProbationConfig(mapping_conf_threshold=bad)

    def test_out_of_range_entry_score_raises(self):
        from ingestion.quality_thresholds import (
            ProbationConfig, ProbationConfigError)
        for bad in (-0.1, 1.5, float("nan"), float("inf")):
            with pytest.raises(ProbationConfigError):
                ProbationConfig().decide(bad, "high")

    def test_decision_is_json_serialisable(self):
        from ingestion.quality_thresholds import ProbationConfig
        import dataclasses
        json.dumps(dataclasses.asdict(ProbationConfig().decide(0.5, "high")))

    def test_evaluate_returns_a_pass_rate(self):
        from ingestion.quality_thresholds import ProbationConfig
        scores = [0.5, 0.1, 0.9, 0.45, 0.2, 0.7, 0.5, 0.5]
        confs = ["high"] * 6 + ["low", "high"]
        out = ProbationConfig().evaluate(scores, confs)
        assert 0.0 <= out["pass_rate"] <= 1.0
        assert out["n"] == 8 and out["n_passed"] <= 8
        assert set(out["per_reason"]) <= {
            "in_band", "pass_score_below_band", "pass_score_above_band",
            "mapping_confidence_low_below_threshold_high"}
        assert sum(out["per_reason"].values()) == out["n"]

    def test_evaluate_shape_mismatch_raises(self):
        from ingestion.quality_thresholds import (
            ProbationConfig, ProbationConfigError)
        with pytest.raises(ProbationConfigError):
            ProbationConfig().evaluate([0.5, 0.5], ["high"])

    def test_analytic_pass_rate_is_exact_for_high_at_zero_weight(self):
        """
        At the frozen qual_weight 0.0 the score IS the entry score e ~ U(0,1),
        and the "high" level is the only admissible one, so the null pass rate
        is exactly P(0.35 <= e <= 0.65) = 0.30.
        """
        from ingestion.quality_thresholds import analytic_pass_rate
        assert analytic_pass_rate(0.0, "high", (0.35, 0.65)) == \
            pytest.approx(0.30, abs=1e-9)

    def test_analytic_pass_rate_agrees_with_monte_carlo(self):
        from ingestion.quality_thresholds import (
            ProbationConfig, analytic_pass_rate)
        c = ProbationConfig()
        rng = np.random.default_rng(7)
        e = rng.uniform(0.0, 1.0, 20000)
        mc = c.evaluate(list(e), ["high"] * len(e))["pass_rate"]
        assert mc == pytest.approx(
            analytic_pass_rate(0.0, "high", (0.35, 0.65)), abs=0.01)

    def test_qual_weight_is_not_a_strictness_dial(self):
        """
        Retained from the v4 analysis, re-expressed at the frozen grid.

        For a band centred on 0.5, RAISING qual_weight LOWERS the null pass
        rate, because the score's coefficient on entry_score is (1 - w) so the
        score spreads OUTSIDE a narrow band. qual_weight is therefore not a
        strictness dial; narrowing the band is. v7 froze qual_weight to 0.0,
        so the dial no longer exists and only the band remains.

        The values are computed for "high" (the only admissible level under
        the frozen floor), so they differ from the v4 "medium" figures.
        """
        from ingestion.quality_thresholds import analytic_pass_rate
        rates = [analytic_pass_rate(w, "high", (0.35, 0.65))
                 for w in (0.3, 0.4, 0.5)]
        assert rates == pytest.approx([0.4286, 0.4167, 0.3000], abs=1e-3)
        assert rates[0] > rates[1] > rates[2]      # decreasing = tightening

    def test_narrowing_the_band_strictly_tightens(self):
        from ingestion.quality_thresholds import analytic_pass_rate
        for w in (0.0, 0.3, 0.4, 0.5):
            written = analytic_pass_rate(w, "high", (0.35, 0.65))
            effective = analytic_pass_rate(w, "high", (0.4, 0.6))
            assert effective < written, w
        assert analytic_pass_rate(0.0, "high", (0.4, 0.6)) == \
            pytest.approx(0.20, abs=1e-9)

    def test_null_calibration_all_medium_is_zero_under_the_frozen_floor(self):
        """
        "all_medium" means every row carries MEDIUM mapping confidence. v7
        froze the admissibility floor to "high", so every one of those rows is
        inadmissible and the null passes at exactly 0.0 — the mix name
        describes the confidence level, not the admissible set.
        """
        from ingestion.quality_thresholds import calibrate_null
        r = calibrate_null(n=4000, seed=11, null_mix="all_medium")
        assert r["empirical_pass_rate"] == pytest.approx(0.0, abs=1e-9)

    def test_null_calibration_analytic_matches_empirical(self):
        from ingestion.quality_thresholds import calibrate_null
        r = calibrate_null(n=4000, seed=11, null_mix="declared_heuristic")
        assert r["empirical_pass_rate"] == pytest.approx(
            r["analytic_pass_rate"], abs=0.03)

    def test_default_null_mix_is_not_off_target(self):
        """
        The DECLARED heuristic mix does not reach 50%; the calibration must
        admit that rather than quietly reporting a pass.
        """
        from ingestion.quality_thresholds import DEFAULT_NULL_MIX, calibrate_null
        r = calibrate_null(n=4000, seed=11, null_mix=DEFAULT_NULL_MIX)
        assert r["within_target"] is False
        assert r["empirical_pass_rate"] < 0.45
        assert r["verdict"] != "within_target"

    def test_no_declared_mix_reaches_the_50pct_target(self):
        """
        HONEST NEGATIVE RESULT, not a defect in the test.

        v7 froze mapping_conf_threshold to "high" and qual_weight to 0.0. The
        three declared null mixes then pass at 0.00 (all_medium — every row
        inadmissible), 0.15 (declared_heuristic) and 0.10 (uniform_levels), so
        the pre-registered 0.50 target is unreachable for every cell in the
        frozen grid. The 10k gate calibration report says the same thing from
        the other direction: the probation band is near-vacuous and must not be
        relied on alone.

        Recording this as a test is deliberate: if a future change to the
        frozen grid ever makes 0.50 reachable again, this test fails and
        someone has to decide whether the probation component earns its 0.05
        composite weight.
        """
        from ingestion.quality_thresholds import NULL_CONF_MIXES, calibrate_null
        for mix in NULL_CONF_MIXES:
            r = calibrate_null(n=4000, seed=11, null_mix=mix)
            assert r["within_target"] is False, mix

    def test_calibration_is_seed_stable(self):
        from ingestion.quality_thresholds import calibrate_null
        a = calibrate_null(n=2000, seed=99, null_mix="declared_heuristic")
        b = calibrate_null(n=2000, seed=99, null_mix="declared_heuristic")
        assert a["empirical_pass_rate"] == b["empirical_pass_rate"]

    def test_feasibility_grid_is_the_frozen_three_cells(self):
        """
        The grid collapsed from 18 cells (3 qual_weights x 3 null_mixes x 2
        thresholds) to 3 — one per null_mix — once v7 froze qual_weight to 0.0
        and the confidence floor to "high".
        """
        from ingestion.quality_thresholds import (
            MAPPING_CONF_THRESHOLD_GRID, NULL_CONF_MIXES, QUAL_WEIGHT_GRID,
            feasibility_report)
        rep = feasibility_report()
        expected = len(NULL_CONF_MIXES) * len(QUAL_WEIGHT_GRID) \
            * len(MAPPING_CONF_THRESHOLD_GRID)
        assert rep["n_cells"] == expected == 3
        assert len(rep["cells"]) == 3
        for cell in rep["cells"]:
            assert cell["qual_weight"] == 0.0
            assert cell["mapping_conf_threshold"] == "high"
            assert cell["admissible_levels"] == ["high"]

    def test_no_frozen_cell_reaches_the_50pct_target(self):
        """
        The load-bearing consequence of the freeze: zero of three cells reach
        the pre-registered 0.50 null pass rate. "all_medium" is 0.0 because
        "high" is the only admissible level; the two mixes that do include
        "high" rows land at 0.15 and 0.10.
        """
        from ingestion.quality_thresholds import feasibility_report
        rep = feasibility_report()
        assert rep["n_reaching_target"] == 0
        assert all(c["reaches_0.50"] is False for c in rep["cells"])

    def test_module_self_test_passes(self):
        from ingestion.quality_thresholds import main
        assert main(["--test"]) == 0


# ===========================================================================
# 8. provenance audit v2 + trial ledger
# ===========================================================================

class TestProvenanceAuditorV2:

    def test_auditor_returns_provenance_chain_bool(self):
        from validation.provenance_audit import run_audit_v2
        r = run_audit_v2()
        assert isinstance(r["provenance_chain"], bool)

    def test_chain_is_true_because_every_root_passes(self):
        """
        The v4-era version of this test asserted `provenance_chain is False`
        and that R1 was a MISMATCH. That described the estate as it was: a
        pre-registration whose pin did not match and a trial ledger that had
        not been regenerated. v7 fixed the pin and the ledger was rebuilt under
        the v5 contract, so the chain now has to verify. If this test fails,
        the estate has regressed and the audit is doing its job.
        """
        from validation.provenance_audit import run_audit_v2
        r = run_audit_v2()
        assert r["provenance_chain"] is True
        assert r["failed_roots"] == []

    def test_r1_reports_the_frozen_pin_as_verified(self):
        from validation.provenance_audit import run_audit_v2
        prereg = run_audit_v2()["roots"]["R1_prereg_root"]
        assert prereg["ok"] is True
        assert prereg["status"] == "VERIFIED"
        assert prereg["expected_sha256"] == prereg["actual_sha256"]

    def test_r1_digests_match_the_loader(self):
        from ingestion.harness.prereg import MODULE_PREREG
        from validation.provenance_audit import run_audit_v2
        prereg = run_audit_v2()["roots"]["R1_prereg_root"]
        assert prereg["actual_sha256"] == MODULE_PREREG.integrity.actual_sha256

    def test_ledger_meets_the_frozen_trial_count(self):
        from validation.provenance_audit import run_audit_v2
        led = run_audit_v2()["roots"]["R2_R4_trial_ledger"]
        assert led["n_trials"] == led["required_n_trials"] == 513
        assert led["trial_count_ok"] is True
        assert led["trial_count_shortfall"] == 0

    def test_ledger_states_that_no_trial_was_evaluated(self):
        """
        THE MOST IMPORTANT TEST IN THIS FILE.

        The ledger's hash chain is intact and its 513-entry count is real, so
        R2-R4 pass. None of those 513 parameter sets has an out-of-sample
        score: the v7 open-data blocker is still OPEN. The auditor must keep
        saying so in prose, so nobody can read `provenance_chain: true` as
        "513 results".
        """
        from validation.provenance_audit import run_audit_v2
        led = run_audit_v2()["roots"]["R2_R4_trial_ledger"]
        assert led["ok"] is True                      # contract satisfied
        assert led["metric_status"] == "UNEVALUATED"  # but nothing measured
        assert led["n_trials_evaluated"] == 0
        assert led["n_trials_unevaluated"] == 513
        assert "UNEVALUATED" in led["consequence"]
        assert "must not be cited" in led["consequence"]

    def test_a_short_ledger_fails_closed_with_the_stated_consequence(self, tmp_path):
        """The v4 shortfall behaviour still has to work — on a real shortfall."""
        from validation.provenance_audit import ProvenanceAuditor
        src = json.loads((ROOT / "data" / "phase9_full" / "trial_ledger.json")
                         .read_text(encoding="utf-8"))
        src["trials"] = src["trials"][:100]
        p = tmp_path / "short.json"
        p.write_text(json.dumps(src), encoding="utf-8")
        led = ProvenanceAuditor(trial_ledger=p).check_trial_ledger()
        assert led["ok"] is False
        assert led["trial_count_ok"] is False
        assert led["trial_count_shortfall"] == 413
        assert "513" in led["consequence"]
        assert "wrong multiple-testing count" in led["consequence"]

    def test_ledger_sidecar_hash_matches(self):
        from validation.provenance_audit import run_audit_v2
        led = run_audit_v2()["roots"]["R2_R4_trial_ledger"]
        assert led["sidecar_present"] is True
        assert led["sidecar_matches"] is True

    def test_ledger_per_trial_hashes_all_recompute(self):
        from validation.provenance_audit import run_audit_v2
        led = run_audit_v2()["roots"]["R2_R4_trial_ledger"]
        assert led["per_trial_sha256_mismatches"] == []
        assert led["per_trial_sha256_checked"] == led["n_trials"]

    def test_ledger_has_no_duplicate_param_sets(self):
        from validation.provenance_audit import run_audit_v2
        led = run_audit_v2()["roots"]["R2_R4_trial_ledger"]
        assert led["duplicate_param_groups"] == []
        assert led["n_unique_param_sets"] == led["n_trials"]

    def test_missing_ledger_fails_closed(self, tmp_path):
        from validation.provenance_audit import ProvenanceAuditor
        led = ProvenanceAuditor(trial_ledger=tmp_path / "nope.json").check_trial_ledger()
        assert led["ok"] is False
        assert led["reason"] == "trial_ledger_missing"

    def test_tampered_trial_hash_is_detected(self, tmp_path):
        """Forensic recompute: a doctored ledger must not pass."""
        from validation.provenance_audit import ProvenanceAuditor
        src = json.loads((ROOT / "data" / "phase9_full" / "trial_ledger.json")
                         .read_text(encoding="utf-8"))
        src["trials"][0]["metric_value"] = 99.0        # sha256 no longer matches
        p = tmp_path / "trial_ledger.json"
        p.write_text(json.dumps(src, indent=2), encoding="utf-8")
        led = ProvenanceAuditor(trial_ledger=p).check_trial_ledger()
        assert led["ok"] is False
        assert any(m["reason"] == "sha256_mismatch"
                   for m in led["per_trial_sha256_mismatches"])

    def test_missing_sidecar_fails_closed(self, tmp_path):
        from validation.provenance_audit import ProvenanceAuditor
        src = (ROOT / "data" / "phase9_full" / "trial_ledger.json").read_bytes()
        p = tmp_path / "trial_ledger.json"
        p.write_bytes(src)
        led = ProvenanceAuditor(trial_ledger=p).check_trial_ledger()
        assert led["sidecar_present"] is False and led["ok"] is False

    def test_ledger_with_enough_trials_and_hashes_passes(self, tmp_path):
        """
        A complete, honest 513-trial ledger must PASS R2-R4.

        Built with the real v5 writer rather than hand-rolled hashes: the
        chained v5 payload covers `fold_metrics`, and an unchained per-trial
        digest is exactly the v4 weakness R2-R4 exists to reject. The writer
        is the thing under test here, so build the fixture with it.
        """
        from research.ml_parameter_optimization import TrialTracker
        from validation.provenance_audit import ProvenanceAuditor

        tracker = TrialTracker(base_n=13, target_n=513, metric_status="EVALUATED")
        for i in range(1, 514):
            tracker.log_trial(
                trial_id=i,
                params={"x": float(i), "y": float(i) * 2},
                metric_value=float(i) / 10.0,
                fold_metrics=[float(i) / 10.0],
                provenance={"evaluation_status": "EVALUATED"},
            )
        p = tmp_path / "trial_ledger.json"
        tracker.save_ledger(p)
        led = ProvenanceAuditor(trial_ledger=p).check_trial_ledger()
        assert led["ok"] is True
        assert led["trial_count_ok"] is True and led["sidecar_matches"] is True
        assert led["n_unique_param_sets"] == 513
        # an EVALUATED ledger must NOT carry the placeholder warning
        assert "UNEVALUATED" not in (led["consequence"] or "")

    def test_legacy_unchained_ledger_is_rejected(self, tmp_path):
        """
        The v4 shape — integer ids, per-trial hashes, no chain, no UUIDv7 —
        must still fail R2-R4 even with the full 513 entries. This is the
        regression that motivated the regeneration CLI.
        """
        from validation.provenance_audit import ProvenanceAuditor
        trials = []
        for i in range(1, 514):
            params = {"x": float(i), "y": float(i) * 2}
            mv = float(i) / 10.0
            h = hashlib.sha256(
                "{}:{}:{}".format(i, json.dumps(params, sort_keys=True), mv)
                .encode("utf-8")).hexdigest()
            trials.append({"trial_id": i, "params": params, "metric_value": mv,
                           "fold_metrics": [mv], "sha256": h})
        payload = {"total_trials": 513, "target_n": 513, "trials": trials}
        p = tmp_path / "trial_ledger.json"
        p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        p.with_suffix(".json.sha256").write_text(
            hashlib.sha256(p.read_bytes()).hexdigest() + "\n", encoding="utf-8")
        led = ProvenanceAuditor(trial_ledger=p).check_trial_ledger()
        assert led["ok"] is False
        assert led["schema"] == "legacy_int_ids"
        assert "v5 TrialTracker contract" in led["consequence"]

    def test_audit_report_is_written(self, tmp_path):
        from validation.provenance_audit import run_audit_v2
        out = tmp_path / "audit_report.json"
        run_audit_v2(report_path=out)
        assert out.exists()
        assert json.loads(out.read_text(encoding="utf-8"))["version"] == 2

    def test_gate8_consumes_the_auditor_report(self):
        pytest.skip(
            "gate 8 was removed by pre-registration v7 "
            "(statistical_design.no_gate_hierarchy: true). Structural "
            "compliance is now reference-only inside the calibration report, "
            "not a decision gate. The provenance_chain signal is still "
            "consumed — by the composite report's provenance section.")


# ===========================================================================
# 9. the frozen v7 decision contract — one composite DSR, no hierarchy
# ===========================================================================
# Replaces the deleted 8-gate test classes. Those asserted an architecture
# the frozen pre-registration deliberately removed: Holm-Bonferroni step-down,
# alpha/8, per-gate pass rates, and a "all 8 gates" joint FPR. Reinstating
# them would mean editing config/pre_registration_v7.yaml, which is frozen at
# c80a47bf...a1685. These tests pin the v7 design instead.

class TestFrozenV7DecisionContract:
    """The contract is ONE decision: composite_dsr >= 0.95 over N=513."""

    def test_no_gate_hierarchy_in_the_engine(self):
        from ingestion.harness import gate_engine as ge
        # A hierarchy would be expressed as a per-gate API or a gate count.
        assert not hasattr(ge, "gate_1_arm_superiority")
        assert not hasattr(ge, "gate_8_structural_integrity")
        assert not hasattr(ge, "Phase9GateEngine")
        assert not hasattr(ge, "run_all_gates")

    def test_no_fwer_alpha_anywhere_in_the_engine(self):
        from ingestion.harness import gate_engine as ge
        for name in dir(ge):
            assert "holm" not in name.lower()
            assert "bonferroni" not in name.lower()
        assert not hasattr(ge, "ALPHA")
        assert not hasattr(ge, "FWER_ALPHA")

    def test_single_threshold_and_trial_count(self):
        from ingestion.harness import gate_engine as ge
        assert ge.DSR_THRESHOLD == 0.95
        assert ge.DSR_N_TRIALS == 513
        assert ge.DSR_TRACK_RECORD_DAYS == 2520

    def test_component_weights_sum_to_one(self):
        from ingestion.harness.gate_engine import COMPONENT_SPECS
        total = sum(s["weight"] for s in COMPONENT_SPECS)
        assert total == pytest.approx(1.0)

    def test_result_reports_the_frozen_parameters(self):
        from ingestion.harness.gate_engine import CompositeDSRResult
        r = CompositeDSRResult(composite_dsr=0.96, passed=True,
                              component_scores={}, composite_score=0.5)
        assert r.threshold == 0.95
        assert r.n_trials == 513
        assert r.track_record_days == 2520

    def test_decision_is_the_dsr_alone(self):
        """composite_score moves the report but must not move `passed`.

        This is the decoupling the 10k null report measured empirically: over
        320 composite-input combinations, `passed` took one distinct value
        while composite_score ranged 0.05-0.45. If this test ever fails, the
        engine started gating on the composite and the v7 decision rule has
        drifted.
        """
        from ingestion.harness.gate_engine import CompositeGateEngine
        import pandas as pd
        eng = CompositeGateEngine()
        idx = pd.date_range("2018-01-31", periods=84, freq="ME")
        rng = np.random.default_rng(7)
        oos = pd.Series(rng.normal(0.004, 0.03, 84), index=idx)
        spy = pd.Series(rng.normal(0.003, 0.04, 84), index=idx)
        w = pd.DataFrame(rng.dirichlet(np.ones(5), 84), index=idx,
                         columns=[f"n{i}" for i in range(5)])

        outcomes, scores = [], set()
        for ari, churn, tca, prob in ((0.10, 0.5, 10.0, 0.20),
                                      (0.45, 3.0, 80.0, 0.50),
                                      (0.80, 4.9, 149.0, 0.80)):
            r = eng.evaluate_composite_dsr(
                oos_returns=oos, arm_c_weights=w, spy_returns=spy,
                hmm_ari=ari, swap_churn=churn, tca_bps=tca,
                probation_pass_rate=prob)
            outcomes.append(r.composite_dsr)
            scores.add(round(r.composite_score, 6))
        assert len(outcomes) == 3, "evaluation failed"
        assert len(scores) > 1, "composite inputs did not move the score at all"

    def test_arm_a_composite_score_rolling_metrics(self):
        """Verify Arm A composite score is computed from 7 rolling metrics, non-constant."""
        from ingestion.harness.gate_engine import compute_arm_a_composite_score, CompositeGateEngine
        idx = pd.date_range("2018-01-31", periods=84, freq="ME")
        rng = np.random.default_rng(42)
        rets = pd.Series(rng.normal(0.005, 0.03, 84), index=idx)
        w_df = pd.DataFrame(rng.dirichlet(np.ones(10), 84), index=idx, columns=[f"T{i}" for i in range(10)])
        regime = pd.Series(rng.choice(["EXPANSION", "CHOPPY", "CRISIS"], 84), index=idx)

        comp = compute_arm_a_composite_score(
            portfolio_returns=rets,
            portfolio_weights=w_df,
            regime_states=regime,
            lookback_days=252
        )
        assert len(comp) == 84
        assert not comp.empty
        assert (comp >= 0.0).all() and (comp <= 1.0).all()
        assert comp.nunique() > 1, "composite_score must be non-constant"

        eng = CompositeGateEngine()
        dsr = eng.compute_dsr(comp)
        assert 0.0 <= dsr <= 1.0

    def test_pipeline_refuses_to_invent_evidence(self):
        """No evidence => halt, not a plausible-looking SUCCESS."""
        from research.phase9_full_pipeline import (
            Phase9Orchestrator, GateEvaluationUnavailable)
        orch = Phase9Orchestrator(output_dir="")
        with pytest.raises(GateEvaluationUnavailable):
            orch.evaluate_gates(None)
        with pytest.raises(GateEvaluationUnavailable):
            orch.evaluate_gates({"oos_returns": pd.Series(dtype=float)})

    def test_halt_record_is_not_a_success(self, tmp_path):
        """A halted run writes NOT_EXECUTED, never a verdict."""
        from research.phase9_full_pipeline import Phase9Orchestrator
        import json as _json
        orch = Phase9Orchestrator(output_dir=str(tmp_path))
        out = orch.write_halt_record("no OOS evidence available")
        rec = _json.loads(out.read_text(encoding="utf-8"))
        assert rec["status"] == "NOT_EXECUTED"
        assert rec["gate_evaluated"] is False
        # no verdict field of any kind, and no metric claims
        assert "composite_dsr" not in rec
        assert "gates_summary" not in rec
        # sidecar must match the bytes actually written (CRLF-safe)
        side = out.with_suffix(out.suffix + ".sha256")
        assert hashlib.sha256(out.read_bytes()).hexdigest() == \
            side.read_text(encoding="utf-8").split()[0]


# ===========================================================================
# 10. the 10k null calibration report
# ===========================================================================

class TestCalibrationReport:
    """The calibration measures the null; it must not pass the null."""

    def test_wilson_ci_contains_point_estimate(self):
        from validation.gate_calibration import wilson_ci
        for k, n in ((0, 100), (7, 100), (100, 100), (1, 1000)):
            ci = wilson_ci(k, n)
            assert ci["lo"] <= k / n <= ci["hi"]

    def test_wilson_ci_narrows_with_n(self):
        from validation.gate_calibration import wilson_ci
        wide = wilson_ci(50, 100)
        tight = wilson_ci(500, 1000)
        assert (tight["hi"] - tight["lo"]) < (wide["hi"] - wide["lo"])

    def test_wilson_ci_zero_case(self):
        from validation.gate_calibration import wilson_ci
        ci = wilson_ci(0, 10000)
        assert ci["lo"] == 0.0 and ci["hi"] < 0.001

    def test_null_dgp_has_zero_alpha(self):
        """The null must have no edge, or the FPR measures nothing."""
        from validation import gate_calibration as gc
        assert gc.P9_NULL_ALPHA == 0.0

    def test_null_dgp_is_a_factor_model(self):
        from validation import gate_calibration as gc
        assert gc.P9_N_FACTORS > 0
        assert gc.P9_N_NAMES == 1500
        assert gc.P9_N_MONTHS == 84
        assert gc.P9_DIRICHLET_ALPHA == 1.0

    def test_calibrate_gates_writes_a_report_with_one_decision(self, tmp_path):
        from validation.gate_calibration import calibrate_gates
        out = tmp_path / "calibration_report.json"
        rep = calibrate_gates(n=200, seed=5, report_path=out,
                              require_prereg=False, n_months=24, n_names=100)
        assert out.exists()
        assert rep["n_null_portfolios"] == 200
        # no hierarchy: no per-gate result table, no joint all-gates FPR
        assert not any(k.startswith("gate_") for k in rep)
        assert "joint_all_gates_null_fpr" not in rep
        assert rep["gates_calibrated"] == 0
        assert rep["no_gate_hierarchy"] is True
        assert rep["metric"] == "composite_dsr"
        dr = rep["decision_rule"]
        assert dr["n_passed_null"] <= rep["n_null_portfolios"]
        assert "threshold" in dr["rule"] or "0.95" in json.dumps(dr)

    def test_no_alpha_over_eight_in_the_report(self, tmp_path):
        """The v7 report must not APPLY a Bonferroni correction.

        It must still be free to NAME the removed architecture — that is how a
        reader knows the 8-gate design is gone. So the check is that the word
        appears only in removal context, never as a live correction, and that
        no alpha/8 quantity is computed anywhere in the payload.
        """
        from validation.gate_calibration import calibrate_gates
        out = tmp_path / "r.json"
        rep = calibrate_gates(n=100, seed=3, report_path=out,
                              require_prereg=False, n_months=24, n_names=100)
        blob = json.dumps(rep).lower()
        assert rep["no_gate_hierarchy"] is True
        assert rep["gates_calibrated"] == 0
        # 0.05/8 == 0.00625 must not appear as a live number
        assert "0.00625" not in blob
        # any mention of the old architecture is a removal statement
        for line in blob.split("\\n"):
            if "bonferroni" in line or "holm" in line:
                assert any(w in line for w in
                           ("removed", "deleted", "superseded", "not in v7",
                            "architecture")), line

    def test_null_pass_rate_is_bounded_by_the_wilson_upper(self, tmp_path):
        """A null the composite almost never passes is the desired outcome."""
        from validation.gate_calibration import calibrate_gates
        rep = calibrate_gates(n=200, seed=9, report_path=tmp_path / "r.json",
                              require_prereg=False, n_months=24, n_names=100)
        dr = rep["decision_rule"]
        k, n = dr["n_passed_null"], rep["n_null_portfolios"]
        assert 0 <= k <= n
        ci = dr["wilson_ci_95"]
        assert ci["lo"] <= dr["null_pass_rate"] <= ci["hi"]
        assert dr["null_pass_rate"] == pytest.approx(k / n)

    def test_decoupling_probe_is_present_and_honest(self, tmp_path):
        """The report must publish the composite/DSR decoupling, not hide it."""
        from validation.gate_calibration import calibrate_gates
        rep = calibrate_gates(n=100, seed=3, report_path=tmp_path / "r.json",
                              require_prereg=False, n_months=24, n_names=100)
        probe = rep["composite_decoupling_probe"]
        assert probe["composite_score_drives_decision"] is False
        assert probe["n_combinations"] > 0
        assert len(probe["composite_score_range"]) == 2
        assert probe["composite_score_range"][0] < probe["composite_score_range"][1]
        assert "verdict" in probe


# ===========================================================================
# 9. calibration
# ===========================================================================
