"""Tests for the Audit Report Generator."""
import pytest
from Quantitative.audit.audit_report_generator import (
    AuditReportGenerator, AuditReport, AuditStage
)


class TestAuditReportGenerator:
    def test_empty_report(self):
        gen = AuditReportGenerator()
        report = gen.generate({"SHY": 0.30, "IAU": 0.20})
        assert len(report.stages) == 0
        assert report.final_allocations == {"SHY": 0.30, "IAU": 0.20}

    def test_report_to_dict(self):
        gen = AuditReportGenerator()
        report = gen.generate({})
        d = report.to_dict()
        assert "stages" in d
        assert "final_allocations" in d
        assert "summary" in d

    def test_report_to_markdown(self):
        gen = AuditReportGenerator()
        report = gen.generate({"SHY": 0.30})
        md = report.to_markdown()
        assert "# Quantitative ETF Allocation Audit Report" in md
        assert "SHY" in md

    def test_add_gatekeeper_results(self):
        gen = AuditReportGenerator()
        from Quantitative.bonds.liquidity_gatekeeper import GatekeeperResult, GateDetail, GateResult
        mock_result = GatekeeperResult(
            ticker="TEST",
            overall_pass=True,
            gates=[],
        )
        gen.add_gatekeeper_results({"TEST": mock_result})
        report = gen.generate({})
        assert len(report.stages) == 1
        assert report.stages[0].stage_name == "Liquidity Gatekeeper"
        assert report.stages[0].status == "OK"

    def test_full_pipeline_stages(self):
        from types import SimpleNamespace
        gen = AuditReportGenerator()

        gen.add_credit_spread(SimpleNamespace(
            current_spread_bps=185.0,
            regime=SimpleNamespace(value="NORMAL"),
            direction=SimpleNamespace(value="STABLE"),
            regime_change_alert=False,
        ))
        gen.add_gold_macro_valuation(SimpleNamespace(
            signal=SimpleNamespace(value="FAIR_VALUE"),
            composite_score=0.45,
            metrics=SimpleNamespace(real_rate_10y=1.8, m2_yoy_growth=4.2),
            explanation="Fair value",
        ))
        gen.add_sensitivity_vectors({
            "VCSH": SimpleNamespace(composite=0.3, label_composite=SimpleNamespace(value="MODERATE_EXPOSURE")),
        })
        gen.add_tactical_adjustments([{"reason": "Neutral", "adjustments": {}}])
        gen.add_order_drafts([{
            "ETF": "VCSH", "Action": "BUY", "Shares": 10.0,
            "Target_Allocation_Pct": "15.00%", "Delta_Value": "$500.00"
        }])

        report = gen.generate({"VCSH": 0.15, "IAU": 0.20})
        assert len(report.stages) == 5
        assert report.final_orders[0]["ETF"] == "VCSH"

    def test_summary_error_state(self):
        gen = AuditReportGenerator()
        gen._stages.append(AuditStage(stage_name="Test", status="ERROR", details={}))
        report = gen.generate({})
        assert "error" in report.summary.lower()
