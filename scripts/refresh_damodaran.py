"""Staged monthly refresh helper for Damodaran live industry betas.

Fetches live Damodaran data via discovery.damodaran, refreshes local cache, and
stages a candidate YAML file for CEO review (matching industry_beta.yaml contract).

Usage:
  python scripts/refresh_damodaran.py [--cache data/damodaran] [--out config/industry_beta_candidate.yaml]
"""

import argparse
import os
import sys
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from discovery.damodaran import refresh, load_betas, industry_beta_map  # noqa: E402
from discovery.industry_beta import load_industry_beta  # noqa: E402


def stage_from_damodaran(cache_dir: str, out_path: Path) -> Path:
    """Refresh cache, build candidate dictionary preserving sub_areas, write YAML."""
    refresh(cache_dir=cache_dir)
    betas_df = load_betas(cache_dir=cache_dir)
    beta_map = industry_beta_map(betas_df)

    try:
        live_cfg = load_industry_beta()
        live_industries = live_cfg.get("industries", {})
    except Exception:  # noqa: BLE001
        live_industries = {}

    industries = {}
    for name, beta_val in beta_map.items():
        old_entry = live_industries.get(name)
        sub_area = old_entry.get("sub_area") if isinstance(old_entry, dict) else None
        industries[name] = {
            "unlevered_beta": float(beta_val),
            "sub_area": sub_area if sub_area else "unknown",
        }

    candidate = {
        "industries": dict(sorted(industries.items())),
        "created": str(date.today()),
        "note": "STAGED CANDIDATE for CEO review. Not live. Merge only after approval.",
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        yaml.safe_dump(candidate, sort_keys=False, default_flow_style=False)
    )
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Refresh Damodaran data and stage candidate YAML")
    ap.add_argument("--cache", default="data/damodaran", help="Cache directory")
    ap.add_argument(
        "--out",
        default="config/industry_beta_candidate.yaml",
        help="Output candidate YAML path",
    )
    args = ap.parse_args()

    out_path = Path(args.out)
    stage_from_damodaran(args.cache, out_path)

    betas_df = load_betas(cache_dir=args.cache)
    print(f"Staged candidate with {len(betas_df)} industries -> {out_path}")
    print("Review and approve before merging.")


if __name__ == "__main__":
    main()
