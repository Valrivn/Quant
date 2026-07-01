import os
import pytest
from pathlib import Path
from opencode_scripts.lanes.lane_epsilon.generate_summary import build_opus_summary_content, main

def test_lane_epsilon_summary_generation(tmp_path):
    content = build_opus_summary_content()
    assert "Comprehensive Parallel Sweeps & Architectural Summary Report (Opus 4.6)" in content
    assert "Lane Alpha" in content
    assert "Lane Beta" in content
    assert "Lane Gamma" in content
    assert "Lane Delta" in content
    assert "Lane Epsilon" in content
    assert "Active Asset Conviction Ratings" in content

def test_lane_epsilon_execution():
    workspace_root = Path(__file__).resolve().parents[1]
    summary_file = workspace_root / "center" / "lane_summary.md"
    assert summary_file.exists()
    assert summary_file.stat().st_size > 500
