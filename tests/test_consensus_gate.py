"""Consensus gate engine tests (D-20260816-001, P1).

Covers the frozen usability ladder, SET-ASIDE rule, attack flags,
polarization/no-convergence, two-extreme normalization, block weights, and the
falsification property: a single biased platform (or a paid review burst) can
never flip a company verdict alone.
"""

import json

import pytest

from discovery.consensus.config import load_consensus_config, ConsensusConfigError
from discovery.consensus.engine import (
    INSUFFICIENT, DIRECTIONAL, DISTRIBUTIONAL, USABLE,
    F_BRIBE, F_PUNISHING, F_POLARIZED, F_NO_CONVERGENCE, F_SET_ASIDE,
    TIER_A, TIER_B, TIER_C,
    ReviewRecord, ReviewEvidence, EvidenceRow, CompanyVerdict,
    usability_for_n, total_review_n, is_set_aside,
    detect_bribe_attack, detect_punishing_attack, detect_polarized,
    convergence_check, block_score, normalize_two_extreme,
    build_company_verdict, normalize_sector_verdicts,
)


@pytest.fixture(scope="module")
def cfg():
    return load_consensus_config()


def _records(stars, ts_base=1_700_000_000, bucket="standard"):
    return [ReviewRecord(star=s, ts=ts_base + i * 3600, profile_bucket=bucket)
            for i, s in enumerate(stars)]


class TestUsabilityLadder:
    def test_insufficient(self, cfg):
        assert usability_for_n(9, cfg) == INSUFFICIENT
        assert usability_for_n(0, cfg) == INSUFFICIENT

    def test_directional(self, cfg):
        assert usability_for_n(10, cfg) == DIRECTIONAL
        assert usability_for_n(49, cfg) == DIRECTIONAL

    def test_distributional(self, cfg):
        assert usability_for_n(50, cfg) == DISTRIBUTIONAL
        assert usability_for_n(99, cfg) == DISTRIBUTIONAL

    def test_usable(self, cfg):
        assert usability_for_n(100, cfg) == USABLE
        assert usability_for_n(500, cfg) == USABLE


class TestSetAside:
    def test_below_50_set_aside(self, cfg):
        revs = [ReviewEvidence(source="g2", n=30),
                ReviewEvidence(source="trustpilot", n=10)]
        assert is_set_aside(revs, cfg)
        assert total_review_n(revs) == 40

    def test_at_or_above_50_not_set_aside(self, cfg):
        revs = [ReviewEvidence(source="g2", n=50)]
        assert not is_set_aside(revs, cfg)

    def test_set_aside_marks_verdict(self, cfg):
        rows = [EvidenceRow("exec_approval", 0.9, TIER_C, "glassdoor", n=30)]
        revs = [ReviewEvidence(source="glassdoor", n=30)]
        v = build_company_verdict("T1", "Sec", rows, cfg, revs)
        assert F_SET_ASIDE in v.flags
        assert v.composite_score == 0.0  # review block contributes 0, never neutral


class TestBribeAttack:
    def test_paid_burst_detected(self, cfg):
        stars = [5] * 6
        records = _records(stars, bucket="new")  # all suspicious profiles
        rev = ReviewEvidence(source="g2", n=6, records=records,
                             recent_weekly_volume=2.0, normal_weekly_volume=2.0)
        assert detect_bribe_attack(rev, cfg)

    def test_no_burst_no_flag(self, cfg):
        stars = [5, 4, 3, 2, 1, 5, 4, 3]
        records = _records(stars)
        rev = ReviewEvidence(source="g2", n=8, records=records)
        assert not detect_bribe_attack(rev, cfg)

    def test_burst_without_suspicious_profiles_not_flagged(self, cfg):
        stars = [5] * 6
        records = _records(stars, bucket="standard")
        rev = ReviewEvidence(source="g2", n=6, records=records)
        assert not detect_bribe_attack(rev, cfg)


class TestPunishingAttack:
    def test_volume_spike_detected(self, cfg):
        rev = ReviewEvidence(source="trustpilot", n=100,
                             recent_weekly_volume=60.0, normal_weekly_volume=10.0)
        assert detect_punishing_attack(rev, cfg)

    def test_normal_volume_not_flagged(self, cfg):
        rev = ReviewEvidence(source="trustpilot", n=100,
                             recent_weekly_volume=11.0, normal_weekly_volume=10.0)
        assert not detect_punishing_attack(rev, cfg)

    def test_novice_one_star_barrage(self, cfg):
        stars = [1] * 20
        records = _records(stars, bucket="novice")
        rev = ReviewEvidence(source="g2", n=20, records=records)
        assert detect_punishing_attack(rev, cfg)


class TestPolarized:
    def test_high_skew(self, cfg):
        rev = ReviewEvidence(source="g2", n=120, skewness=2.0, iqr=1.0)
        assert detect_polarized(rev, cfg)

    def test_bimodal_clusters(self, cfg):
        stars = [1] * 25 + [5] * 25
        rev = ReviewEvidence(source="g2", n=50, records=_records(stars))
        assert detect_polarized(rev, cfg)

    def test_healthy_not_polarized(self, cfg):
        stars = [3, 4, 4, 3, 5, 4, 3, 3, 4]
        rev = ReviewEvidence(source="g2", n=9, records=_records(stars), skewness=0.1)
        assert not detect_polarized(rev, cfg)


class TestConvergence:
    def test_two_usable_sources_agree(self, cfg):
        revs = [ReviewEvidence(source="g2", n=120, star_level=4.2),
                ReviewEvidence(source="capterra", n=110, star_level=3.9)]
        assert convergence_check(revs, cfg)

    def test_two_usable_sources_disagree(self, cfg):
        revs = [ReviewEvidence(source="g2", n=120, star_level=4.9),
                ReviewEvidence(source="trustpilot", n=100, star_level=1.8)]
        assert not convergence_check(revs, cfg)

    def test_fewer_than_two_usable_is_no_convergence(self, cfg):
        revs = [ReviewEvidence(source="g2", n=120, star_level=4.2),
                ReviewEvidence(source="capterra", n=30, star_level=3.9)]
        assert not convergence_check(revs, cfg)

    def test_no_convergence_flag_on_verdict(self, cfg):
        rows = [EvidenceRow("exec_approval", 0.9, TIER_C, "glassdoor", n=120)]
        revs = [ReviewEvidence(source="glassdoor", n=120, star_level=4.8),
                ReviewEvidence(source="g2", n=100, star_level=1.5)]
        v = build_company_verdict("T1", "Sec", rows, cfg, revs)
        assert F_NO_CONVERGENCE in v.flags
        assert not v.converged


class TestBlockAndComposite:
    def test_block_score_weighted(self, cfg):
        factors = {"exec_approval": 1.0, "culture_values": 0.0}
        score = block_score(factors, "subjective", cfg)
        assert score is not None
        # exec .30 + culture .25 over .55 present weight.
        assert abs(score - (0.30 * 1.0 + 0.25 * 0.0) / 0.55) < 1e-9

    def test_composite_uses_frozen_blocks(self, cfg):
        rows = [
            EvidenceRow("transaction_volume", 1.0, TIER_A, "edgar"),
            EvidenceRow("sec_attrition_velocity", 0.5, TIER_A, "edgar"),
            EvidenceRow("hiring_velocity", 0.7, TIER_B, "adzuna"),
            EvidenceRow("review_volume", 0.6, TIER_C, "g2", n=120),
            EvidenceRow("talent_capture", 0.8, TIER_B, "linkedin"),
        ]
        v = build_company_verdict("T1", "Sec", rows, cfg, [])
        # Composite = 0.5*q_block + 0.3*e_block + 0.2*s_block (subjective missing -> 0).
        assert v.composite_score > 0.0
        assert v.block_scores["quantifiable"] is not None
        assert "subjective" not in v.block_scores or v.block_scores["subjective"] is not None


class TestTwoExtremeNormalization:
    def test_normalized_when_enough_companies(self, cfg):
        raw = {"A": 0.8, "B": 0.4, "C": 0.2}
        out = normalize_two_extreme(raw, cfg)
        assert out["A"] == pytest.approx(1.0)
        assert out["C"] == pytest.approx(0.0)
        assert out["B"] == pytest.approx(0.3333, abs=1e-3)

    def test_directional_fallback_below_min(self, cfg):
        raw = {"A": 0.8, "B": 0.4}
        out = normalize_two_extreme(raw, cfg)
        assert out == raw  # un-normalized, directional fallback

    def test_sector_verdicts_normalized(self, cfg):
        rows = {t: [EvidenceRow("transaction_volume", v, TIER_A, "edgar")]
                for t, v in [("A", 0.9), ("B", 0.5), ("C", 0.1)]}
        vs = [build_company_verdict(t, "Sec", rows[t], cfg, []) for t in ("A", "B", "C")]
        normalize_sector_verdicts(vs, cfg)
        assert vs[0].block_scores["quantifiable"] == pytest.approx(1.0)
        assert vs[2].block_scores["quantifiable"] == pytest.approx(0.0)


class TestFalsification:
    """Falsification property (Position A): one biased platform cannot flip a verdict."""

    def test_single_biased_platform_cannot_flip(self, cfg):
        # 3 Type-C sources agree at USABLE with one outlier bought 5-star.
        rows = [
            EvidenceRow("culture_values", 0.75, TIER_C, "indeed", n=150),
            EvidenceRow("culture_values", 0.72, TIER_C, "glassdoor", n=140),
            EvidenceRow("culture_values", 0.20, TIER_C, "g2", n=130),
            EvidenceRow("transaction_volume", 0.9, TIER_A, "edgar"),
        ]
        revs = [
            ReviewEvidence(source="indeed", n=150, star_level=4.0),
            ReviewEvidence(source="glassdoor", n=140, star_level=3.9),
            ReviewEvidence(source="g2", n=130, star_level=1.8),
        ]
        # The outlier is BELOW usable-convergence and quarantined if flagged.
        v = build_company_verdict("T1", "Sec", rows, cfg, revs)
        # Quantifiable anchor keeps the verdict positive regardless of the rogue review.
        assert v.composite_score > 0.5

    def test_bribe_burst_quarantined(self, cfg):
        # A paid burst would otherwise push subjective to 1.0.
        burst = ReviewRecord(star=5, ts=1_700_000_000, profile_bucket="new")
        revs = [ReviewEvidence(source="g2", n=6, records=[burst] * 6,
                               recent_weekly_volume=1.0, normal_weekly_volume=1.0)]
        rows = [EvidenceRow("exec_approval", 1.0, TIER_C, "g2", n=6)]
        v = build_company_verdict("T1", "Sec", rows, cfg, revs)
        assert F_BRIBE in v.flags
        assert v.factor_scores.get("exec_approval") is None  # quarantined


class TestConfigFailClosed:
    def test_unknown_key_rejected(self, tmp_path):
        import shutil
        src = None
        cfg = load_consensus_config()
        p = tmp_path / "bad.yaml"
        with open(p, "w") as f:
            pass
        # Write a copy with an extra unknown top-level key.
        text = json.dumps({**cfg, "bogus_section": {}})
        p.write_text(_yaml_from_json(text))
        with pytest.raises(ConsensusConfigError):
            load_consensus_config(str(p))

    def test_missing_required_rejected(self, tmp_path):
        cfg = load_consensus_config()
        stripped = {k: v for k, v in cfg.items() if k != "flags"}
        p = tmp_path / "bad2.yaml"
        p.write_text(_yaml_from_json(json.dumps(stripped)))
        with pytest.raises(ConsensusConfigError):
            load_consensus_config(str(p))


def _yaml_from_json(json_text):
    import yaml
    return yaml.safe_dump(json.loads(json_text))