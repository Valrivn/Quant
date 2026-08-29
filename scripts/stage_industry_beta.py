"""Staged monthly refresh helper for config/industry_beta.yaml (D-20260828-001).

Imports the Damodaran industry -> unlevered beta table from a candidate source
(by default the live table embedded in dashboard/stream_quant.py's DAMODARAN_DATA)
and writes a CANDIDATE file to config/industry_beta_candidate.yaml for CEO
review. It NEVER auto-replaces the live config: `auto_replace` stays false and
the merge is a separate, explicit, CEO-approved step.

Usage:
  python scripts/stage_industry_beta.py [--source path/to/candidate.json]
"""

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from discovery.industry_beta import load_industry_beta  # noqa: E402

# Default candidate source: import DAMODARAN_DATA from the dashboard module so
# a refresh re-reads the same source of truth the live config was promoted from.
_DEFAULT_SOURCE = None


def _load_live_source() -> dict:
    """Import DAMODARAN_DATA (industry -> unlevered beta) from stream_quant."""
    try:
        from dashboard.stream_quant import DAMODARAN_DATA  # type: ignore
        return dict(DAMODARAN_DATA)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"could not import DAMODARAN_DATA: {exc}")


def _load_candidate_json(path: str) -> dict:
    with open(path, "r") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise SystemExit("candidate source must be a JSON mapping industry -> beta")
    return data


def stage(source: dict, out_path: Path) -> Path:
    """Write a staged candidate file from raw {industry: beta}; never live-replace."""
    live = load_industry_beta()

    industries = {}
    for name, beta in source.items():
        if not isinstance(beta, (int, float)) or beta <= 0:
            print(f"WARN: skipping non-positive beta for {name!r}: {beta!r}")
            continue
        old = live["industries"].get(name)
        sub_area = old.get("sub_area") if isinstance(old, dict) else None
        industries[name] = {
            "unlevered_beta": float(beta),
            "sub_area": sub_area if sub_area else ("unknown"),
        }

    if not industries:
        raise SystemExit("no valid industries produced for staging")

    candidate = {
        "industries": dict(sorted(industries.items())),
        "created": str(date.today()),
        "note": "STAGED CANDIDATE for CEO review. Not live. Merge only after approval.",
    }
    out_path.write_text(
        yaml.safe_dump(candidate, sort_keys=False, default_flow_style=False)
    )
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Stage a Damodaran beta refresh")
    ap.add_argument("--source", default=None, help="JSON path, else live DAMODARAN_DATA")
    args = ap.parse_args()

    source = (
        _load_candidate_json(args.source) if args.source else _load_live_source()
    )
    out = ROOT / "config" / "industry_beta_candidate.yaml"
    stage(source, out)
    print(f"Staged candidate -> {out}")
    print("Review and approve before merging into config/industry_beta.yaml.")


if __name__ == "__main__":
    main()
