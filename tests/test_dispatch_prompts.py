"""Dispatch prompt builder tests: plan-level granularity."""

import re

from ops.dispatch_prompts import (
    DEFAULT_PROTECTED_PATHS,
    build_dispatch_prompt,
    build_plan_artifact,
)
from ops.planning_gate import ExecutionPlan, PlanStep, PlanningGate

SECTION_HEADERS = [
    "## 1. SESSION CONTEXT & RULINGS",
    "## 2. TERRITORY",
    "## 3. NUMBERED TASKS",
    "## 4. INPUTS & OUTPUTS PER TASK",
    "## 5. CONSTRAINTS",
    "## 6. VERIFICATION COMMANDS WITH EXPECTED OUTPUTS",
    "## 7. REQUIRED REPORT FORMAT",
    "## 8. PLAN STEP CITATION",
]


def make_step() -> PlanStep:
    return PlanStep(
        step_id="2",
        title="Build ops package",
        objective="Implement the planning-gate layer with tests",
        files_touched=["ops/planning_gate.py", "ops/dispatch_prompts.py"],
        inputs="implementation plan step 2",
        outputs="ops package + 3 passing test files",
        verification="python -m pytest tests/test_planning_gate.py -q",
        success_criteria="26+ tests pass and py_compile clean",
    )


def build_prompt() -> str:
    return build_dispatch_prompt(
        workstream="Workstream A (gemini-worker)",
        plan_step=make_step(),
        ruling_context="R1: plan gate >= 0.90 enforced. R2: no autonomous architecture.",
    )


class TestSections:
    def test_all_eight_sections_in_order(self):
        prompt = build_prompt()
        positions = [prompt.index(h) for h in SECTION_HEADERS]
        assert positions == sorted(positions)

    def test_ruling_context_present(self):
        prompt = build_prompt()
        assert "R1: plan gate >= 0.90 enforced" in prompt
        assert "Workstream A (gemini-worker)" in prompt

    def test_exact_file_targets_listed(self):
        prompt = build_prompt()
        assert "ops/planning_gate.py" in prompt
        assert "ops/dispatch_prompts.py" in prompt

    def test_plan_step_citation(self):
        prompt = build_prompt()
        assert "'2'" in prompt
        assert "Build ops package" in prompt

    def test_verification_and_report_format_present(self):
        prompt = build_prompt()
        assert "tests/test_planning_gate.py" in prompt
        assert "REPORT FORMAT" in prompt


class TestConstraints:
    def test_all_default_protected_paths_listed(self):
        prompt = build_prompt()
        for protected in DEFAULT_PROTECTED_PATHS:
            assert protected in prompt

    def test_no_commit_and_no_architecture_directives(self):
        prompt = build_prompt()
        assert "No git commit" in prompt
        assert "No architectural decisions" in prompt

    def test_custom_protected_files_replaces_defaults(self):
        prompt = build_dispatch_prompt(
            workstream="W",
            plan_step=make_step(),
            ruling_context="ctx",
            protected_files=["config/pre_registration*"],
        )
        assert "config/pre_registration*" in prompt
        assert ".env*" not in prompt  # defaults replaced, not merged

    def test_extra_instructions_injected(self):
        prompt = build_dispatch_prompt(
            workstream="W",
            plan_step=make_step(),
            ruling_context="ctx",
            extra_instructions="Never edit tests/ outside your territory.",
        )
        assert "Never edit tests/ outside your territory." in prompt


class TestPlanArtifact:
    def test_artifact_chains_to_plan_digest(self):
        plan = ExecutionPlan(
            plan_id="p1",
            objective="Build the ops package with verified tests",
            steps=[make_step()],
            workstreams=["A"],
        )
        artifact = build_plan_artifact(plan)
        assert artifact["plan_sha256"] == plan.plan_sha256()
        assert "artifact_sha256" in artifact
        assert len(artifact["artifact_sha256"]) == 64

    def test_artifact_includes_gate_verdict(self):
        plan = ExecutionPlan(
            plan_id="p2",
            objective="Build the ops package with verified tests",
            steps=[make_step()],
            workstreams=["A"],
        )
        gate_result = PlanningGate().check(plan)
        artifact = build_plan_artifact(plan, gate_result)
        expected = PlanningGate().check(plan)
        assert artifact["gate"]["passed"] == expected.passed
        assert artifact["gate"]["blockers"] == expected.blockers
        assert artifact["gate"]["confidence"] == expected.confidence

    def test_artifact_digest_stable_for_same_plan(self):
        plan = ExecutionPlan(
            plan_id="p3",
            objective="Deterministic artifact digest check",
            steps=[make_step()],
        )
        assert build_plan_artifact(plan) == build_plan_artifact(plan)


class TestSmoke:
    def test_module_cli_self_check(self):
        from ops import dispatch_prompts as dp

        assert callable(dp.__main__)
        assert len(SECTION_HEADERS) == 8
        assert re.match(r"^#+ ", SECTION_HEADERS[0])
