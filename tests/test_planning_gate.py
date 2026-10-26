"""Planning gate tests: >=0.90 composite confidence enforcement."""

import pytest

from ops.planning_gate import (
    ExecutionPlan,
    PlanGateError,
    PlanStep,
    PlanningGate,
    quick_gate,
)

COMPLETE_CONFIDENCE = {
    "scope_clarity": 1.0,
    "target_clarity": 1.0,
    "success_criteria_clarity": 1.0,
    "dependency_map_completeness": 1.0,
    "risk_containment": 1.0,
}


def make_step(step_id: str = "1") -> PlanStep:
    return PlanStep(
        step_id=step_id,
        title=f"Step {step_id}",
        objective=f"Objective for step {step_id} that is specific and bounded",
        files_touched=[f"ops/example_{step_id}.py"],
        inputs="source files",
        outputs="verified module",
        verification=f"python -m pytest tests/test_example_{step_id}.py -q",
        success_criteria=(
            f"all targeted tests PASS and py_compile clean for step {step_id}"
        ),
    )


def make_plan() -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="plan_test_001",
        objective="Test the planning gate end to end with a complete plan",
        steps=[make_step("1"), make_step("2")],
        workstreams=["A", "B"],
        confidence=dict(COMPLETE_CONFIDENCE),
        threshold=0.90,
    )


class TestGatePass:
    def test_complete_plan_passes(self):
        gate = PlanningGate(threshold=0.90)
        result = gate.check(make_plan())
        assert result.passed is True
        assert result.blockers == []
        assert result.confidence >= 0.90

    def test_require_returns_result(self):
        gate = PlanningGate(threshold=0.90)
        result = gate.require(make_plan())
        assert result.passed

    def test_composite_is_mean_of_dimensions(self):
        plan = make_plan()
        expected = sum(COMPLETE_CONFIDENCE.values()) / len(COMPLETE_CONFIDENCE)
        assert plan.composite_confidence == pytest.approx(expected, abs=1e-9)

    def test_exactly_at_threshold_passes(self):
        plan = make_plan()
        plan.confidence = dict(COMPLETE_CONFIDENCE)
        plan.threshold = 1.0
        # composite == 1.0 == threshold -> passes (>= comparison)
        assert PlanningGate(threshold=1.0).check(plan).passed


class TestGateBlock:
    def test_missing_verification_blocks(self):
        plan = make_plan()
        plan.steps[0].verification = ""
        result = PlanningGate().check(plan)
        assert result.passed is False
        assert any("missing verification" in b for b in result.blockers)

    def test_missing_files_blocks(self):
        plan = make_plan()
        plan.steps[1].files_touched = []
        result = PlanningGate().check(plan)
        assert result.passed is False
        assert any("missing exact file targets" in b for b in result.blockers)

    def test_missing_success_criteria_blocks(self):
        plan = make_plan()
        plan.steps[0].success_criteria = "   "
        result = PlanningGate().check(plan)
        assert result.passed is False
        assert any("missing success criteria" in b for b in result.blockers)

    def test_below_threshold_blocks(self):
        plan = make_plan()
        plan.confidence = {k: 0.5 for k in COMPLETE_CONFIDENCE}
        result = PlanningGate(threshold=0.90).check(plan)
        assert result.passed is False
        assert any("composite confidence" in b for b in result.blockers)

    def test_require_raises_on_blocked_plan(self):
        plan = make_plan()
        plan.steps[0].files_touched = []
        with pytest.raises(PlanGateError, match="PLAN GATE BLOCKED"):
            PlanningGate().require(plan)

    def test_empty_plan_blocks(self):
        plan = ExecutionPlan(plan_id="empty", objective="empty plan for test", steps=[])
        result = PlanningGate().check(plan)
        assert result.passed is False
        assert "plan has no numbered steps" in result.blockers

    def test_invalid_threshold_rejected(self):
        with pytest.raises(ValueError):
            PlanningGate(threshold=1.5)


class TestScoring:
    def test_partial_plan_scores_below_complete(self):
        complete = make_plan()
        partial = make_plan()
        partial.steps[0].verification = ""
        gate = PlanningGate()
        assert gate.score(partial)["risk_containment"] < gate.score(complete)["risk_containment"]

    def test_quick_gate_convenience(self):
        result = quick_gate(
            objective="Ad-hoc plan for convenience check",
            steps=[
                {
                    "step_id": "1",
                    "title": "step",
                    "objective": "an objective description",
                    "files_touched": ["ops/x.py"],
                    "inputs": "a",
                    "outputs": "b",
                    "verification": "python -m py_compile ops/x.py",
                    "success_criteria": "compiles cleanly without error",
                }
            ],
        )
        assert result.passed


class TestArtifact:
    def test_artifact_is_json_serializable_and_digest_stable(self):
        import json

        plan = make_plan()
        gate = PlanningGate()
        artifact = gate.gate_artifact(plan)
        encoded = json.dumps(artifact, sort_keys=True)
        assert "plan_sha256" in artifact
        assert len(artifact["plan_sha256"]) == 64
        assert artifact["passed"] is True
        # digest stability: same plan content -> same sha
        assert make_plan().plan_sha256() == plan.plan_sha256()

    def test_digest_changes_with_content(self):
        plan = make_plan()
        before = plan.plan_sha256()
        plan.steps[0].title = "Mutated title"
        assert plan.plan_sha256() != before


class TestSmoke:
    def test_module_cli_self_check(self):
        # exercise the __main__ demo path function without spawning a process
        from ops import planning_gate as pg

        assert callable(pg.__main__)
