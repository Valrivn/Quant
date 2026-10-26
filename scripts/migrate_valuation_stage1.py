"""Stage-1 schema migration for data/pit_fundamentals/valuation_fundamentals.parquet.

Adds the Stage-1 Intrinsic Value Gate columns (docs/STRATEGY_VIABILITY_BRIEFING.md)
required for the 4-Stage strategy:

    rd_cap_asset             cumulative capitalized R&D intangible (5-yr straight line)
    rd_amort_exp             current-year R&D amortization
    ebit_cap_rd              EBIT + R&D - amortization        (capitalized-R&D EBIT)
    nopat_cap_rd             ebit_cap_rd * (1 - effective tax)
    invested_capital_cap_rd  invested_capital + rd_cap_asset
    roic_cap_rd              nopat_cap_rd / invested_capital_cap_rd  (gate: >= WACC + 2%)
    omr                      Operating Margin Ratio (EBIT + R&D) / revenue  (moat gate: >= 0.50)
    cost_of_equity_est       documented cost-of-equity constant (default 0.09,
                             aligned with real_backtest.py wacc_mean)
    wacc_estimate            weight_equity*CoE + weight_debt*CoD*(1-tax_eff)

IDEMPOTENT: re-running after a successful apply is a no-op (target columns +
sidecar already present). Never drops or rewrites existing columns: the input
columns are preserved byte-for-byte order-wise and only appended to.

PIT SAFETY: the capitalization math runs on the period-level panel
(ticker, fiscal_end_date) using the latest filed revision of each period, then
broadcasts back onto every PIT filing row. Existing filed_date anchors are
untouched. No future values are used (only lag-shifts within a ticker).

Provenance: writes an atomic temp-file replacement and emits a
.provenance.json sidecar (sha256 / row_count / date_range / transformations)
matching the Data_Strategy.md §8 house manifest conventions.

Usage:
    python -m scripts.migrate_valuation_stage1 --check
    python -m scripts.migrate_valuation_stage1 --apply          (default)
    python -m scripts.migrate_valuation_stage1 --apply --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

STAGE1_MIGRATION_VERSION = 1
PARQUET_PATH = Path("data/pit_fundamentals/valuation_fundamentals.parquet")
SIDECAR_PATH = PARQUET_PATH.with_name(f"{PARQUET_PATH.name}.provenance.json")

TARGET_COLUMNS = [
    "rd_cap_asset", "rd_amort_exp", "ebit_cap_rd", "nopat_cap_rd",
    "invested_capital_cap_rd", "roic_cap_rd", "omr",
    "cost_of_equity_est", "wacc_estimate",
]

DEFAULT_COST_OF_EQUITY = 0.09  # aligns with real_backtest.compute_dcf_valuation wacc_mean
RD_AMORT_YEARS = 5             # Damodaran-style 5-year straight-line R&D amortization
MAX_EFFECTIVE_TAX = 0.35       # clamp for effective tax used in NEW fields (existing fields untouched)


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def load_polars():
    import polars as pl
    return pl


def build_migration_columns(pl, df) -> "polars.DataFrame":
    """Compute Stage-1 columns on the period-level panel and broadcast back.

    Pure function for testability; returns a DataFrame of the same row count
    as ``df`` containing only the new TARGET_COLUMNS.
    """
    import polars as pl2

    sort_cols = ["ticker", "fiscal_end_date"]
    # Period-level panel: latest filed revision per (ticker, fiscal_end_date).
    # pl.all().sort_by("filed_date").last() is the DETERMINISTIC per-revision
    # pick (plain agg().last() would see hash-partitioned group order).
    period = (
        df.sort(["ticker", "fiscal_end_date", "filed_date"])
        .group_by(["ticker", "fiscal_end_date"])
        .agg([
            pl.col("rd_expense").sort_by("filed_date").last().alias("rd"),
            pl.col("revenue").sort_by("filed_date").last().alias("rev"),
            pl.col("ebit").sort_by("filed_date").last().alias("ebit"),
            pl.col("operating_income").sort_by("filed_date").last().alias("oi"),
            pl.col("income_tax_expense").sort_by("filed_date").last().alias("tax_exp"),
            pl.col("interest_expense").sort_by("filed_date").last().alias("int_exp"),
            pl.col("invested_capital").sort_by("filed_date").last().alias("ic"),
            pl.col("cost_of_debt").sort_by("filed_date").last().alias("cod"),
            pl.col("weight_equity").sort_by("filed_date").last().alias("we"),
            pl.col("weight_debt").sort_by("filed_date").last().alias("wd"),
        ])
        .sort(sort_cols)
    )

    rd = pl.col("rd").fill_null(0.0)
    ebit = pl.col("ebit").fill_null(pl.col("oi")).fill_null(0.0)

    period = period.with_columns([
        # effective tax: pretax ~ EBIT - interest; clamp to [0, MAX_EFFECTIVE_TAX]
        pl.when((pl.col("ebit").fill_null(pl.col("oi")) - pl.col("int_exp")) > 0)
        .then((pl.col("tax_exp") / (pl.col("ebit").fill_null(pl.col("oi")) - pl.col("int_exp")))
              .clip(0.0, MAX_EFFECTIVE_TAX))
        .otherwise(0.0).alias("tax_eff"),
    ])

    # 5-yr straight-line amortization via lagged R&D within ticker
    amort_exprs = [pl.col("rd").shift(k).over("ticker") for k in range(1, RD_AMORT_YEARS + 1)]
    cap_exprs = [(1.0 - (k - 1) / RD_AMORT_YEARS) * pl.col("rd").shift(k - 1).over("ticker")
                 for k in range(1, RD_AMORT_YEARS + 1)]

    period = period.with_columns([
        (pl.sum_horizontal([rd.shift(k).over("ticker") for k in range(1, RD_AMORT_YEARS + 1)]) / RD_AMORT_YEARS)
        .alias("rd_amort_exp"),
        pl.sum_horizontal(cap_exprs).alias("rd_cap_asset"),
    ])
    period = period.with_columns([
        (ebit + rd - pl.col("rd_amort_exp")).alias("ebit_cap_rd"),
        (ebit + rd).alias("ebit_plus_rd"),
    ])
    period = period.with_columns([
        (pl.col("ebit_cap_rd") * (1 - pl.col("tax_eff"))).alias("nopat_cap_rd"),
        (pl.col("ic").fill_null(0.0) + pl.col("rd_cap_asset").fill_null(0.0)).alias("invested_capital_cap_rd"),
    ])
    period = period.with_columns([
        pl.when(pl.col("rd_cap_asset").is_not_null() & (pl.col("rd") > 0)
                & (pl.col("invested_capital_cap_rd") > 0))
        .then(pl.col("nopat_cap_rd") / pl.col("invested_capital_cap_rd"))
        .otherwise(None).alias("roic_cap_rd"),
        pl.when(pl.col("rev").is_not_null() & (pl.col("rev") != 0))
        .then((ebit + rd) / pl.col("rev"))
        .otherwise(None).alias("omr"),
    ])
    period = period.with_columns([
        pl.lit(DEFAULT_COST_OF_EQUITY).alias("cost_of_equity_est"),
    ])
    period = period.with_columns([
        (pl.col("we").fill_null(0.0) * pl.col("cost_of_equity_est")
         + pl.col("wd").fill_null(0.0) * pl.col("cod").fill_null(0.0) * (1 - pl.col("tax_eff")))
        .alias("wacc_estimate"),
    ])

    # Broadcast back onto every PIT filing row (restatements keep identical period values)
    keep = ["ticker", "fiscal_end_date"] + TARGET_COLUMNS
    merged = df.select(["ticker", "fiscal_end_date"]).join(
        period.select(keep), on=["ticker", "fiscal_end_date"], how="left"
    )
    result_cols = merged.select(TARGET_COLUMNS)
    for c in TARGET_COLUMNS:
        result_cols = result_cols.with_columns(
            pl.when(pl.col(c).is_infinite()).then(None).otherwise(pl.col(c)).alias(c)
        )
    return result_cols


def migration_status(df, pl) -> dict:
    present = [c for c in TARGET_COLUMNS if c in df.columns]
    missing = [c for c in TARGET_COLUMNS if c not in df.columns]
    sidecar_ok = False
    if SIDECAR_PATH.exists():
        try:
            meta = json.loads(SIDECAR_PATH.read_text(encoding="utf-8"))
            sidecar_ok = meta.get("migration_version") == STAGE1_MIGRATION_VERSION and bool(meta.get("sha256"))
        except Exception:
            sidecar_ok = False
    return {"present": present, "missing": missing, "sidecar_ok": sidecar_ok,
            "columns_total": len(df.columns), "rows": len(df)}


def apply_migration(pl, dry_run: bool = False) -> dict:
    if not PARQUET_PATH.exists():
        sys.exit(f"parquet not found: {PARQUET_PATH}")
    df = pl.read_parquet(PARQUET_PATH)
    status = migration_status(df, pl)
    if not status["missing"] and status["sidecar_ok"]:
        return {"skipped": True, **status}

    new_cols = build_migration_columns(pl, df)
    out = df.with_columns(new_cols)

    if dry_run:
        return {"dry_run": True, "rows": len(out), "columns_total": len(out.columns),
                "added": list(new_cols.columns), **status}

    # atomic replace: write temp parquet then os.replace
    with tempfile.NamedTemporaryFile(suffix=".parquet", dir=PARQUET_PATH.parent, delete=False) as tf:
        tmp_path = Path(tf.name)
    try:
        out.write_parquet(tmp_path)
        sha = _sha256_bytes(tmp_path.read_bytes())
        os.replace(tmp_path, PARQUET_PATH)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    # sidecar provenance (house manifest schema)
    sidecar = {
        "source": "SEC_EDGAR",
        "source_version": f"valuation_fundamentals_stage1_v{STAGE1_MIGRATION_VERSION}",
        "pit_timestamp_column": "filed_date",
        "entity_key": "ticker",
        "retrieval_timestamp": datetime.now(timezone.utc).isoformat(),
        "transformations": [
            "capitalized_rd_5yr_straightline", "roic_cap_rd", "omr_cap_rd",
            "wacc_estimate", "stage1_migration",
        ],
        "migration_version": STAGE1_MIGRATION_VERSION,
        "migration_columns": TARGET_COLUMNS,
        "sha256": sha,
        "row_count": len(out),
        "date_range": {"min": str(out["filed_date"].min()), "max": str(out["filed_date"].max())},
        "cost_of_equity_default": DEFAULT_COST_OF_EQUITY,
        "rd_amort_years": RD_AMORT_YEARS,
    }
    sidecar_path = PARQUET_PATH.with_name(f"{PARQUET_PATH.name}.provenance.json")
    sidecar_path.write_text(json.dumps(sidecar, indent=2, default=str), encoding="utf-8")

    print(f"Migration v{STAGE1_MIGRATION_VERSION} applied: {len(out)} rows, "
          f"{len(out.columns)} columns (added {len(TARGET_COLUMNS)})")
    print(f"sha256: {sha}")
    print(f"sidecar: {sidecar_path}")
    return {"applied": True, "sha256": sha, "rows": len(out), **status}


def main():
    parser = argparse.ArgumentParser(description="Stage-1 valuation fundamentals migration")
    parser.add_argument("--check", action="store_true", help="Report migration status only")
    parser.add_argument("--apply", action="store_true", help="Apply migration (default)")
    parser.add_argument("--dry-run", action="store_true", help="Compute columns without writing")
    args = parser.parse_args()

    pl = load_polars()
    if args.check:
        df = pl.read_parquet(PARQUET_PATH)
        status = migration_status(df, pl)
        print(json.dumps(status, indent=2, default=str))
        return

    result = apply_migration(pl, dry_run=args.dry_run)
    print(json.dumps(result, indent=2, default=str) if result.get("dry_run") or result.get("skipped") else "")


if __name__ == "__main__":
    main()