"""Build the dense quarterly fundamental panel.

Merges ``mapped_concepts.parquet`` into a wide, point-in-time quarterly factor
table and forward-fills it to every monthly rebalance date in
``data/universe/monthly_reconstitution.parquet``.

Pipeline
--------
1.  Canonical facts are de-duplicated per (ticker, canonical, fiscal_end) using
    tag priority then earliest ``filed`` (as-reported, no restatement leakage).
2.  Duration facts are collapsed to a **quarterly** basis: a directly reported
    3-month figure wins; otherwise the quarter is derived from year-to-date
    facts (``YTD_t - YTD_{t-1}``) with the derived availability date set to the
    later of the two filings (strict PIT).
3.  Instant (balance-sheet) facts are taken at ``fiscal_end``.
4.  Wide factors are derived (ROIC, FCF, margins, WACC components, ...) and
    forward-filled to each rebalance date via an as-of join anchored on
    ``filed_date <= rebalance_date``.
5.  ``fallback_tier`` / ``concept_coverage_pct`` summarise data provenance per
    ticker-month.

Output: ``data/pit_fundamentals/dense_fundamentals.parquet``.

CLI
---
    python -m ingestion.build_dense_fundamentals \
        --mapped data/pit_fundamentals/xbrl_full/mapped_concepts.parquet \
        --universe data/universe/monthly_reconstitution.parquet \
        --output data/pit_fundamentals/dense_fundamentals.parquet
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional

import polars as pl

from ingestion.pit_provenance import write_provenance

log = logging.getLogger("build_dense_fundamentals")

ROOT = Path(__file__).resolve().parents[1]
SOURCE_VERSION = "companyfacts_20261010"

# fresh / stale thresholds mirror ingestion.persistence.merged_universe
FRESH_DAYS = 120
MAX_STALE_DAYS = 730
QUARTER_MIN_DAYS = 45
QUARTER_MAX_DAYS = 105
DEFAULT_TAX_RATE = 0.21
MAX_DEBT_WEIGHT = 0.60

TIER_FILED = 0
TIER_FORWARD = 1
TIER_MISSING = 2
TIER_NO_DATA = 3

CORE_COVERAGE_FACTORS = [
    "revenue", "operating_income", "ebit", "interest_expense", "net_income",
    "operating_cash_flow", "capex", "total_assets", "total_debt", "equity",
    "cash", "roic", "fcf", "fcf_margin", "interest_coverage",
    "cost_of_debt", "weight_debt", "weight_equity",
]

# A genuine financial period must expose at least one of these statement-level
# facts.  Cover-page artifacts (e.g. dei share counts on an odd "cover" date)
# otherwise masquerade as the newest period and hijack the backward as-of join.
ANCHOR_FACTORS = [
    "revenue", "net_income", "operating_income", "operating_cash_flow",
    "total_assets", "total_liabilities", "equity", "cash", "pretax_income",
]

KEY_COLS = ["ticker", "cik", "fiscal_end", "filed_date", "rebalance_date"]


# ---------------------------------------------------------------------------
# Loading / normalisation
# ---------------------------------------------------------------------------

def _load_canonical_long(mapped_path: Path) -> pl.DataFrame:
    """Load mapped facts, normalise dates/units, de-dup synonyms."""
    lf = pl.scan_parquet(mapped_path).filter(pl.col("canonical").is_not_null())

    keep_unit = (
        ((pl.col("unit_family") == "USD") & (pl.col("unit") == "USD"))
        | ((pl.col("unit_family") == "shares") & (pl.col("unit") == "shares"))
        | (
            (pl.col("unit_family") == "per_share")
            & (pl.col("unit").str.contains("USD/shares"))
        )
    )
    lf = lf.filter(keep_unit).select([
        "ticker", "cik", "canonical", "priority", "period_type",
        "value", "unit", "fiscal_end", "filed", "accession", "start",
    ])

    df = lf.with_columns([
        pl.col("fiscal_end").str.to_date("%Y-%m-%d", strict=False).alias("fiscal_end"),
        pl.col("filed").str.to_date("%Y-%m-%d", strict=False).alias("filed"),
        pl.col("start").str.to_date("%Y-%m-%d", strict=False).alias("start"),
    ]).filter(
        pl.col("fiscal_end").is_not_null() & pl.col("filed").is_not_null()
    ).collect()

    # de-dup synonyms / amended filings: best priority, then earliest filed.
    df = (
        df.sort(["ticker", "canonical", "fiscal_end", "start", "priority", "filed"])
        .unique(subset=["ticker", "canonical", "fiscal_end", "start"], keep="first")
    )
    return df


def _quarterly_from_durations(dur: pl.DataFrame) -> pl.DataFrame:
    """Collapse duration facts to one quarterly value per (ticker, canonical, end)."""
    dur = dur.with_columns(
        (pl.col("fiscal_end") - pl.col("start")).dt.total_days().alias("pd")
    ).filter(pl.col("pd") > 0)

    # 1. directly reported quarterly figures (3-month durations)
    direct = (
        dur.filter(pl.col("pd") <= QUARTER_MAX_DAYS)
        .sort(["ticker", "canonical", "fiscal_end", "pd", "priority", "filed"])
        .unique(subset=["ticker", "canonical", "fiscal_end"], keep="first")
        .select([
            "ticker", "canonical", "fiscal_end",
            pl.col("value").alias("q_direct"),
            pl.col("filed").alias("q_direct_filed"),
        ])
    )

    # 2. YTD -> quarterly differences (same period start within a fiscal year)
    ytd = (
        dur.sort(["ticker", "canonical", "start", "fiscal_end", "priority", "filed"])
        .unique(subset=["ticker", "canonical", "start", "fiscal_end"], keep="first")
        .sort(["ticker", "canonical", "start", "fiscal_end"])
        .with_columns([
            pl.col("value").shift(1).over(["ticker", "canonical", "start"]).alias("prev_val"),
            pl.col("filed").shift(1).over(["ticker", "canonical", "start"]).alias("prev_filed"),
            pl.col("pd").shift(1).over(["ticker", "canonical", "start"]).alias("prev_pd"),
        ])
    )
    ytd = ytd.with_columns([
        (pl.col("value") - pl.col("prev_val")).alias("delta_val"),
        (pl.col("pd") - pl.col("prev_pd")).alias("delta_pd"),
    ])
    ytd = ytd.with_columns([
        pl.when(pl.col("prev_val").is_null() & (pl.col("pd") <= QUARTER_MAX_DAYS))
        .then(pl.col("value"))
        .when(
            pl.col("prev_val").is_not_null()
            & pl.col("delta_pd").is_between(QUARTER_MIN_DAYS, QUARTER_MAX_DAYS)
        )
        .then(pl.col("delta_val"))
        .otherwise(None)
        .alias("q_derived"),
        pl.when(pl.col("prev_val").is_null())
        .then(pl.col("filed"))
        .otherwise(pl.max_horizontal("filed", "prev_filed"))
        .alias("q_derived_filed"),
    ])
    derived = (
        ytd.filter(pl.col("q_derived").is_not_null())
        .sort(["ticker", "canonical", "fiscal_end", "pd", "priority", "filed"])
        .unique(subset=["ticker", "canonical", "fiscal_end"], keep="first")
        .select(["ticker", "canonical", "fiscal_end", "q_derived", "q_derived_filed"])
    )

    joined = direct.join(
        derived, on=["ticker", "canonical", "fiscal_end"], how="full", coalesce=True
    )
    return joined.with_columns([
        pl.coalesce(["q_direct", "q_derived"]).alias("value"),
        pl.coalesce(["q_direct_filed", "q_derived_filed"]).alias("filed"),
    ]).select(["ticker", "canonical", "fiscal_end", "value", "filed"])


def _canonical_long(df: pl.DataFrame) -> pl.DataFrame:
    """Return a tidy (ticker, canonical, fiscal_end, value, filed) table."""
    # A fact is instant iff it has no period start (duration facts always do).
    # NB: `period_type` can be null in the mapped table, so OR-ing it in with
    # `==` would yield null and silently drop every duration row.
    is_instant = pl.col("start").is_null() | pl.col("period_type").eq("instant").fill_null(False)
    inst = (
        df.filter(is_instant)
        .sort(["ticker", "canonical", "fiscal_end", "priority", "filed"])
        .unique(subset=["ticker", "canonical", "fiscal_end"], keep="first")
        .select(["ticker", "canonical", "fiscal_end", "value", "filed"])
    )
    dur = _quarterly_from_durations(df.filter(~is_instant))
    return pl.concat([inst, dur], how="vertical")


# ---------------------------------------------------------------------------
# Derived factors
# ---------------------------------------------------------------------------

def _safe_div(num: pl.Expr, den: pl.Expr, *, clip: Optional[tuple] = None) -> pl.Expr:
    """Divide, returning null when denominator is null/zero, optional clip."""
    expr = pl.when(den.is_not_null() & (den.abs() > 1e-9)).then(num / den).otherwise(None)
    if clip is not None:
        expr = expr.clip(clip[0], clip[1])
    return expr


def _add_yoy(wide: pl.DataFrame, base: str, out: str, lag_days: int = 365) -> pl.DataFrame:
    """Trailing YoY growth for ``base`` using an as-of join ~1y earlier."""
    if base not in wide.columns:
        return wide.with_columns(pl.lit(None).cast(pl.Float64).alias(out))
    right = wide.select([
        "ticker",
        pl.col("fiscal_end").alias("prev_date"),
        pl.col(base).alias("prev_val"),
    ]).sort(["ticker", "prev_date"])
    left = wide.with_columns(
        (pl.col("fiscal_end") - pl.duration(days=lag_days)).alias("target")
    ).sort(["ticker", "target"])
    merged = left.join_asof(
        right,
        left_on="target",
        right_on="prev_date",
        by="ticker",
        strategy="backward",
        tolerance="75d",
    )
    merged = merged.with_columns(
        _safe_div(pl.col(base), pl.col("prev_val")).sub(1).alias(out)
    )
    return merged.drop(["prev_date", "prev_val", "target"])


def _derive_factors(wide: pl.DataFrame) -> pl.DataFrame:
    """Compute ~55 derived factors from the canonical wide table.

    Every canonical input referenced here is materialised (as null when absent)
    first so the derivation never raises ``ColumnNotFound`` on sparse filers.
    """
    inputs = [
        "operating_income", "pretax_income", "interest_expense", "net_income",
        "income_tax_expense", "depreciation_amortization", "gross_profit",
        "revenue", "cogs", "operating_cash_flow", "capex", "equity", "total_debt",
        "cash", "total_assets", "current_liabilities", "current_assets",
        "shares_diluted", "shares_basic", "rpo", "deferred_revenue",
        "contract_liabilities", "inventory", "receivables", "payables",
        "rd_expense", "sbc_expense", "sga", "dividends_paid", "buybacks",
        "shares_outstanding", "marketable_securities", "long_term_debt",
        "short_term_debt", "goodwill", "intangibles", "ppe_net", "retained_earnings",
        "interest_expense_finance_lease", "interest_expense_deposit",
        "interest_expense_longterm_debt", "interest_expense_shortterm_debt",
        "interest_expense_other",
    ]
    missing = [n for n in inputs if n not in wide.columns]
    if missing:
        wide = wide.with_columns([pl.lit(None).cast(pl.Float64).alias(n) for n in missing])

    # Derive combined total debt when only the current/non-current split is
    # tagged (very common) so leverage / WACC inputs survive.
    wide = wide.with_columns(
        pl.coalesce([
            pl.col("total_debt"),
            pl.when(
                pl.col("long_term_debt").is_not_null() | pl.col("short_term_debt").is_not_null()
            )
            .then(pl.col("long_term_debt").fill_null(0.0) + pl.col("short_term_debt").fill_null(0.0))
            .otherwise(None),
        ]).alias("total_debt")
    )

    # Sum interest-expense components when no consolidated total is tagged, so
    # interest coverage / cost of debt survive for utilities, banks and lessees.
    _int_components = [
        "interest_expense_finance_lease", "interest_expense_deposit",
        "interest_expense_longterm_debt", "interest_expense_shortterm_debt",
        "interest_expense_other",
    ]
    _any_component = pl.any_horizontal([pl.col(c).is_not_null() for c in _int_components])
    _component_sum = pl.sum_horizontal([pl.col(c).fill_null(0.0) for c in _int_components])
    wide = wide.with_columns(
        pl.coalesce([
            pl.col("interest_expense"),
            pl.when(_any_component).then(_component_sum).otherwise(None),
        ]).alias("interest_expense")
    )

    # --- income / EBITDA -------------------------------------------------
    wide = wide.with_columns([
        pl.coalesce([
            pl.col("operating_income"),
            pl.col("pretax_income") + pl.col("interest_expense"),
            pl.col("net_income") + pl.col("income_tax_expense") + pl.col("interest_expense"),
        ]).alias("ebit"),
    ])
    wide = wide.with_columns([
        (pl.col("ebit") + pl.col("depreciation_amortization")).alias("ebitda"),
        pl.coalesce([
            pl.col("gross_profit"), pl.col("revenue") - pl.col("cogs")
        ]).alias("gross_profit"),
        pl.col("income_tax_expense").alias("tax_expense"),
    ])

    # effective tax rate (fallback constant when uninformative)
    wide = wide.with_columns(
        _safe_div(pl.col("tax_expense"), pl.col("pretax_income"), clip=(0.0, 0.5)).alias(
            "effective_tax_rate"
        )
    )
    wide = wide.with_columns(
        pl.col("effective_tax_rate").fill_null(DEFAULT_TAX_RATE).alias("effective_tax_rate")
    )

    # --- cash flow / invested capital -----------------------------------
    wide = wide.with_columns([
        (pl.col("operating_cash_flow") - pl.col("capex")).alias("fcf"),
        (pl.col("ebit") * (1 - pl.col("effective_tax_rate"))).alias("nopat"),
        pl.coalesce([
            pl.col("equity") + pl.col("total_debt") - pl.col("cash"),
            pl.col("total_assets") - pl.col("current_liabilities"),
        ]).alias("invested_capital"),
        (pl.col("total_debt") - pl.col("cash")).alias("net_debt"),
        (pl.col("current_assets") - pl.col("current_liabilities")).alias("working_capital"),
        pl.coalesce([pl.col("shares_diluted"), pl.col("shares_basic")]).alias(
            "shares_weighted_avg"
        ),
    ])

    # RPO balance = disclosed RPO if present else deferred revenue + contract liab.
    wide = wide.with_columns(
        pl.when(pl.col("rpo").is_not_null())
        .then(pl.col("rpo"))
        .otherwise(
            pl.col("deferred_revenue").fill_null(0.0)
            + pl.col("contract_liabilities").fill_null(0.0)
        )
        .alias("rpo_total")
    )
    wide = wide.with_columns(
        pl.when(
            pl.col("rpo").is_null()
            & pl.col("deferred_revenue").is_null()
            & pl.col("contract_liabilities").is_null()
        )
        .then(None)
        .otherwise(pl.col("rpo_total"))
        .alias("rpo_total")
    )

    # --- returns & leverage ---------------------------------------------
    wide = wide.with_columns([
        _safe_div(pl.col("nopat"), pl.col("invested_capital")).alias("roic"),
        _safe_div(pl.col("net_income"), pl.col("equity")).alias("roe"),
        _safe_div(pl.col("net_income"), pl.col("total_assets")).alias("roa"),
        _safe_div(pl.col("fcf"), pl.col("invested_capital")).alias("fcf_roic"),
        _safe_div(pl.col("total_debt"), pl.col("equity")).alias("debt_to_equity"),
        _safe_div(pl.col("total_debt"), pl.col("total_assets")).alias("debt_to_assets"),
        _safe_div(pl.col("net_debt"), pl.col("equity")).alias("net_debt_to_equity"),
        _safe_div(pl.col("net_debt"), pl.col("ebitda")).alias("net_debt_to_ebitda"),
        _safe_div(pl.col("ebit"), pl.col("interest_expense").abs()).alias("interest_coverage"),
        _safe_div(pl.col("ebitda"), pl.col("interest_expense").abs()).alias(
            "ebitda_interest_coverage"
        ),
        _safe_div(pl.col("current_assets"), pl.col("current_liabilities")).alias("current_ratio"),
        _safe_div(
            pl.col("current_assets") - pl.col("inventory"), pl.col("current_liabilities")
        ).alias("quick_ratio"),
        _safe_div(pl.col("cash"), pl.col("current_liabilities")).alias("cash_ratio"),
    ])

    # --- margins & turnover ---------------------------------------------
    wide = wide.with_columns([
        _safe_div(pl.col("gross_profit"), pl.col("revenue")).alias("gross_margin"),
        _safe_div(pl.col("ebit"), pl.col("revenue")).alias("operating_margin"),
        _safe_div(pl.col("net_income"), pl.col("revenue")).alias("net_margin"),
        _safe_div(pl.col("ebit"), pl.col("revenue")).alias("ebit_margin"),
        _safe_div(pl.col("ebitda"), pl.col("revenue")).alias("ebitda_margin"),
        _safe_div(pl.col("fcf"), pl.col("revenue")).alias("fcf_margin"),
        _safe_div(pl.col("fcf"), pl.col("total_assets")).alias("fcf_to_assets"),
        _safe_div(pl.col("revenue"), pl.col("total_assets")).alias("asset_turnover"),
        _safe_div(pl.col("cogs"), pl.col("inventory")).alias("inventory_turnover"),
        _safe_div(pl.col("revenue"), pl.col("receivables")).alias("receivables_turnover"),
        _safe_div(pl.col("cogs"), pl.col("payables")).alias("payables_turnover"),
    ])
    wide = wide.with_columns([
        _safe_div(pl.lit(365.0), pl.col("receivables_turnover")).alias("days_sales_outstanding"),
        _safe_div(pl.lit(365.0), pl.col("inventory_turnover")).alias("days_inventory"),
        _safe_div(pl.lit(365.0), pl.col("payables_turnover")).alias("days_payables"),
    ])
    wide = wide.with_columns(
        (pl.col("days_sales_outstanding") + pl.col("days_inventory") - pl.col("days_payables")).alias(
            "cash_conversion_cycle"
        )
    )

    # --- intensity ratios ------------------------------------------------
    wide = wide.with_columns([
        _safe_div(pl.col("rd_expense"), pl.col("revenue")).alias("rd_to_revenue"),
        _safe_div(pl.col("sbc_expense"), pl.col("revenue")).alias("sbc_to_revenue"),
        _safe_div(pl.col("capex"), pl.col("revenue")).alias("capex_to_revenue"),
        _safe_div(pl.col("depreciation_amortization"), pl.col("revenue")).alias(
            "depreciation_to_revenue"
        ),
        _safe_div(pl.col("sga"), pl.col("revenue")).alias("sga_to_revenue"),
        _safe_div(
            pl.col("capex") - pl.col("depreciation_amortization") + pl.col("rd_expense"),
            pl.col("nopat"),
        ).alias("reinvestment_rate"),
        _safe_div(
            pl.col("dividends_paid").fill_null(0.0) + pl.col("buybacks").fill_null(0.0),
            pl.col("net_income"),
        ).alias("payout_ratio"),
        _safe_div(pl.col("equity"), pl.col("shares_outstanding")).alias("book_value_per_share"),
        _safe_div(pl.col("capex"), pl.col("depreciation_amortization").abs()).alias(
            "capex_to_depreciation"
        ),
    ])
    wide = wide.with_columns(
        (pl.col("reinvestment_rate") * pl.col("roic")).alias("expected_growth")
    )

    # --- WACC components (filing-sourced only, no market inputs) --------
    wide = wide.with_columns([
        _safe_div(pl.col("interest_expense").abs(), pl.col("total_debt"), clip=(0.0, 0.5)).alias(
            "cost_of_debt"
        ),
        _safe_div(pl.col("equity"), pl.col("equity") + pl.col("total_debt"), clip=(0.0, 1.0)).alias(
            "weight_equity"
        ),
    ])
    wide = wide.with_columns(
        (1.0 - pl.col("weight_equity")).clip(0.0, MAX_DEBT_WEIGHT).alias("weight_debt")
    )
    wide = wide.with_columns(
        (pl.col("cost_of_debt") * (1 - pl.col("effective_tax_rate"))).alias("after_tax_cost_of_debt")
    )

    # --- accruals --------------------------------------------------------
    wide = wide.with_columns(
        _safe_div(pl.col("net_income") - pl.col("operating_cash_flow"), pl.col("total_assets")).alias(
            "accruals"
        )
    )

    # --- YoY growth ------------------------------------------------------
    for base, out in [
        ("revenue", "revenue_yoy"),
        ("ebit", "ebit_yoy"),
        ("net_income", "net_income_yoy"),
        ("fcf", "fcf_yoy"),
        ("shares_outstanding", "shares_yoy"),
        ("total_assets", "asset_growth"),
    ]:
        wide = _add_yoy(wide, base, out)

    return wide


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def build_dense(
    mapped_path: Path,
    universe_path: Path,
    out_path: Path,
) -> Dict[str, object]:
    log.info("Loading canonical facts from %s", mapped_path)
    long = _load_canonical_long(mapped_path)
    log.info("Canonical rows after dedup: %s", f"{long.height:,}")

    tidy = _canonical_long(long)
    log.info("Tidy (ticker, canonical, fiscal_end) rows: %s", f"{tidy.height:,}")

    anchors = tidy.group_by(["ticker", "fiscal_end"]).agg(
        pl.col("filed").min().alias("filed_date")
    )
    wide = tidy.pivot(
        index=["ticker", "fiscal_end"],
        on="canonical",
        values="value",
        aggregate_function="last",
    ).join(anchors, on=["ticker", "fiscal_end"], how="left")

    # Drop cover-page-only rows that would otherwise win the as-of tie-break.
    anchor_cols = [c for c in ANCHOR_FACTORS if c in wide.columns]
    if anchor_cols:
        before = wide.height
        wide = wide.filter(
            pl.any_horizontal([pl.col(c).is_not_null() for c in anchor_cols])
        )
        log.info("Dropped %s non-statement rows", f"{before - wide.height:,}")

    # ticker -> cik (zero-padded)
    cik_map = (
        long.select(["ticker", "cik"]).unique(subset=["ticker"], keep="first")
    )
    wide = wide.join(cik_map, on="ticker", how="left")

    wide = _derive_factors(wide)

    # Keep a deterministic column order: keys first, then factors alphabetically.
    factors = sorted([c for c in wide.columns if c not in ("ticker", "cik", "fiscal_end", "filed_date", "fiscal_year")])
    wide = wide.with_columns(pl.col("fiscal_end").dt.year().alias("fiscal_year"))
    wide = wide.select(["ticker", "cik", "fiscal_end", "filed_date", "fiscal_year", *factors]).sort(
        ["ticker", "fiscal_end"]
    )
    log.info("Wide quarterly panel: %s rows x %d cols", f"{wide.height:,}", wide.width)

    # ---- as-of join to the monthly universe ---------------------------
    universe = pl.read_parquet(universe_path).select(["ticker", "rebalance_date"])
    if universe.schema["rebalance_date"] == pl.Utf8:
        universe = universe.with_columns(
            pl.col("rebalance_date").str.to_date("%Y-%m-%d", strict=False).alias("rebalance_date")
        )
    elif universe.schema["rebalance_date"] == pl.Datetime:
        universe = universe.with_columns(pl.col("rebalance_date").dt.date().alias("rebalance_date"))
    universe = universe.filter(pl.col("rebalance_date").is_not_null())
    # One row per (ticker, filed_date): the most recent fiscal period known at
    # that filing.  Multiple fiscal_ends can share a filed date (current period
    # plus prior-year comparative); we keep the newest end.
    right = (
        wide.sort(["ticker", "fiscal_end"])
        .unique(subset=["ticker", "filed_date"], keep="last")
        .sort(["ticker", "filed_date"])
    )
    left = universe.sort(["ticker", "rebalance_date"])
    panel = left.join_asof(
        right,
        left_on="rebalance_date",
        right_on="filed_date",
        by="ticker",
        strategy="backward",
    )

    # ---- coverage + fallback tier -------------------------------------
    coverage_exprs = [
        pl.col(f).is_not_null().cast(pl.Float64) for f in CORE_COVERAGE_FACTORS if f in panel.columns
    ]
    if coverage_exprs:
        panel = panel.with_columns(
            (pl.sum_horizontal(coverage_exprs) / len(coverage_exprs)).alias(
                "concept_coverage_pct"
            )
        )
    else:
        panel = panel.with_columns(pl.lit(0.0).alias("concept_coverage_pct"))

    age = (pl.col("rebalance_date") - pl.col("filed_date")).dt.total_days()
    tickers_with_data = set(wide["ticker"].to_list())
    panel = panel.with_columns([
        age.alias("_filed_age_days"),
        pl.col("ticker").is_in(list(tickers_with_data)).alias("_has_data"),
    ])
    panel = panel.with_columns(
        pl.when(~pl.col("_has_data")).then(TIER_NO_DATA)
        .when(pl.col("filed_date").is_null()).then(TIER_MISSING)
        .when(pl.col("_filed_age_days") > MAX_STALE_DAYS).then(TIER_MISSING)
        .when(pl.col("_filed_age_days") > FRESH_DAYS).then(TIER_FORWARD)
        .otherwise(TIER_FILED)
        .cast(pl.Int8)
        .alias("fallback_tier")
    ).drop(["_filed_age_days", "_has_data"])

    # n_factors_present helper
    present = [c for c in panel.columns if c not in KEY_COLS + ["concept_coverage_pct", "fallback_tier", "fiscal_year"]]
    panel = panel.with_columns(
        pl.sum_horizontal([pl.col(c).is_not_null().cast(pl.Int32) for c in present]).alias(
            "n_factors_present"
        )
    )

    # order columns
    keys = ["ticker", "cik", "fiscal_end", "filed_date", "rebalance_date", "fiscal_year"]
    rest = sorted([c for c in panel.columns if c not in keys])
    panel = panel.select([*keys, *rest])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    panel.write_parquet(out_path, compression="zstd")
    log.info("Wrote dense panel %s -> %s", f"{panel.height:,}", out_path)

    tier_counts = panel["fallback_tier"].value_counts().sort("fallback_tier")
    return {
        "rows": panel.height,
        "columns": panel.width,
        "tickers": panel["ticker"].n_unique(),
        "factors": len(present),
        "date_min": str(panel["rebalance_date"].min()),
        "date_max": str(panel["rebalance_date"].max()),
        "tier_counts": {int(r["fallback_tier"]): int(r["count"]) for r in tier_counts.to_dicts()},
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mapped",
        default="data/pit_fundamentals/xbrl_full/mapped_concepts.parquet",
    )
    parser.add_argument(
        "--universe", default="data/universe/monthly_reconstitution.parquet"
    )
    parser.add_argument(
        "--output", default="data/pit_fundamentals/dense_fundamentals.parquet"
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    def _abs(p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else ROOT / path

    mapped, universe, out = _abs(args.mapped), _abs(args.universe), _abs(args.output)
    for p in (mapped, universe):
        if not p.exists():
            log.error("Required input missing: %s", p)
            return 2

    summary = build_dense(mapped, universe, out)
    write_provenance(
        out,
        source="SEC_EDGAR",
        source_version=SOURCE_VERSION,
        pit_timestamp_column="filed_date",
        entity_key="cik",
        transformations=[
            "xbrl_full_extract",
            "concept_map_usgaap2024",
            "quarterly_align",
            "forward_fill_rebalance",
            "derive_factors",
        ],
        row_count=summary["rows"],
        date_range={"min": summary["date_min"], "max": summary["date_max"]},
        mapping_confidence="high",
        extra={
            "mapped_input": str(mapped).replace("\\", "/"),
            "universe_input": str(universe).replace("\\", "/"),
            "tickers": summary["tickers"],
            "factors": summary["factors"],
            "fallback_tiers": summary["tier_counts"],
        },
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
