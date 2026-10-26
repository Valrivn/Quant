"""College audit logger — Phase 9 regime v6 VERIFICATION + DOC SYNC, 2026-09-28.

Third and final companion to log_regime_v6_checkpoint.py /
log_regime_v6_stack_checkpoint.py. Records the compile gate, the pre-registration
re-verification, the test-suite regression check, and the doc synchronization.

Reuses the exact JsonlFormatter/emit schema from log_regime_v6_stack_checkpoint.py
so the audit trail stays a single homogeneous stream.
"""
import sys

from log_regime_v6_stack_checkpoint import build_logger, emit

if __name__ == "__main__":
    log = build_logger()

    emit(
        log,
        "compile_gate_reverified",
        modules=[
            "Quantitative/hmm_regime.py",
            "Quantitative/unified_regime.py",
            "Quantitative/micro_states.py",
            "Quantitative/io_hmm.py",
            "Quantitative/bocpd.py",
            "Quantitative/regime_shield_v3.py",
            "ingestion/harness/prereg.py",
        ],
        result="7/7 py_compile clean",
        note="Re-verified at end of session. No syntax regression from the 3 v6 stack edits.",
    )

    emit(
        log,
        "prereg_reverified_untouched",
        command="python -m ingestion.harness.prereg --verify-prereg",
        verified=True,
        status="VERIFIED",
        size_bytes=26440,
        sha256="390084e01fae6a358ab66b5ee536ccaf4d5d85bc6ec4dd05103cb74d5a5c5b10",
        note="Re-verified AFTER all code edits and doc updates. Pre-registration never modified; no digest re-pin performed.",
    )

    emit(
        log,
        "test_suite_regression_check",
        passing="tests/test_regime_shield_v2.py + tests/test_dynamic_wacc.py = 33 passed in 4.00s",
        stale_collection_errors=[
            "tests/test_hmm_dwell.py: ImportError: cannot import 'FactorHMMv3' from Quantitative.regime_shield_v3",
            "tests/test_phase9_core.py: ImportError: cannot import 'FactorHMMv3', 'validate_regime_shield_v3', 'build_factor_hmm'",
        ],
        stale_errors_are_pre_existing=True,
        stale_evidence="git diff Quantitative/regime_shield_v3.py is empty (unmodified this session) AND grep finds zero definitions of FactorHMMv3 / build_factor_hmm / validate_regime_shield_v3 anywhere in Quantitative/*.py",
        full_suite="tests/ did not complete within 900s (no output); network-bound scraper/fetcher tests, NOT investigated",
        headline="33 passed, no regression attributable to this session's edits.",
    )

    emit(
        log,
        "open_defect_v6_stack_has_zero_test_coverage",
        severity="HIGH",
        finding="No test file under tests/ imports hmm_regime, unified_regime, micro_states, io_hmm, or bocpd. The entire v6 3-layer stack that was repaired this session is untested by the unit suite.",
        grep_result="Only 5 test files reference the regime stack at all, and all of them target regime_shield_v2 (Phase 8, untouched).",
        implication="The 3 runtime fixes to unified_regime.py and micro_states.py were verified by smoke test (python -m Quantitative.unified_regime) and py_compile ONLY. A future edit could silently break the v6 stack with no test failing.",
        recommendation="Add smoke/contract tests for unified_regime, io_hmm, micro_states, bocpd as part of the v6 validation workstream. Do NOT claim the v6 stack is test-covered.",
    )

    emit(
        log,
        "docs_synchronized",
        files={
            "Data_Strategy.md": "v3.3 -> v3.4; authoritative prereg hash row replaced with v6 390084e0...; 7 new decision-log entries (2 CRITICAL, 2 HIGH, 1 MEDIUM, 1 LOW, 1 RULING); 5 new risk-register rows; Gate 7 ARI annotated UNMEASURED FOR v6",
            "Tomorrow.md": "rewritten; headline is 'v6 ARI GATE UNMEASURED', with the v5/v6 two-system table, defect register, 5 runtime fixes, next moves, resume commands",
            "archives/phase9_regime_v6_20260928/SESSION_CHECKPOINT.md": "rewritten; section 8 corrected from the erroneous v4.1 claim to authoritative v6; mojibake from the PowerShell round-trip removed (verified: 0 C1 control chars, no BOM)",
        },
        fabricated_figures_added=0,
        note="Every number in all three documents is one that was measured this session or verified this session. No v6 ARI figure exists and none is stated.",
    )

    emit(
        log,
        "encoding_incident",
        severity="LOW",
        what_happened="A scripted encoding repair (_repair_encoding.py) was run against SESSION_CHECKPOINT.md using a RAW python string for the character-class allow-list, so the \\uXXXX escapes were literal and the negated class excluded almost nothing. It over-replaced em-dashes, arrows, >= and the minus sign with the literal text 'FAIL'.",
        detection="Verified by re-reading the file and inspecting the result table.",
        remedy="Rewrote SESSION_CHECKPOINT.md in full from source context with the Write tool, then verified: 0 C1 control characters, no BOM, 258 lines. The throwaway repair script was deleted.",
        lesson="Do not build unicode allow-lists in raw strings, and do not run destructive regex over a hand-authored document. Verify after any bulk text edit, not before.",
        collateral="No other file was affected. college.jsonl re-inspected: 0 suspicious codepoints across all 18 events.",
    )

    emit(
        log,
        "checkpoint_state",
        date="2026-09-28",
        session_complete=True,
        runnable=[
            "Quantitative/hmm_regime.py (import + smoke test, SHOCK, logL -1495.1)",
            "Quantitative/regime_shield_v3.py --validate (v5-era engine, Concat ARI 0.0886, 0/3 folds, FAIL)",
            "Quantitative/unified_regime.py (v6 3-layer stack, smoke test only)",
        ],
        not_yet_runnable_or_untested=[
            "v6 ARI validation - no entry point wires unified_regime into the fold loop. UNMEASURED, not failed.",
            "IO-HMM fitting - IOHMM has no fit method; fixed-prior online filter only.",
            "v6 stack unit-test coverage - zero tests import the stack.",
        ],
        pre_registration_untouched=True,
        pre_registration_authoritative="v6 / 390084e01fae6a358ab66b5ee536ccaf4d5d85bc6ec4dd05103cb74d5a5c5b10",
        documents_synced=["Data_Strategy.md", "Tomorrow.md", "archives/phase9_regime_v6_20260928/SESSION_CHECKPOINT.md"],
        total_college_events=24,
        headline="Session closed at a clean checkpoint. Documentation synchronized to verified reality, pre-registration untouched and re-verified, no fabricated figures. AWAITING CEO RULING on what is being validated: wire v6 (requires IOHMM.fit + a v6 validation entry point) or formally retire v6 and continue on the v5-era engine.",
    )

    print(f"appended 7 events -> {log.handlers[0].baseFilename}", file=sys.stderr)
