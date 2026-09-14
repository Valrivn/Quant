"""
Screen Engine — Dual-Track Screening Harness  (MODULE 2)

Implements Arm A dual-track screening logic per upgraded specification:
- Track 1: Top 25% per sector by ROIC_WACC_Spread desc, then FCF_Yield desc.
           Min sector size: 20 names (sectors with < 20 names excluded).
- Track 2: FCF_Margin > 3yr sector median AND ROIC_WACC_Spread > 2%.
- Union = Mainstream (Track 1) ∪ Achievers (Track 2).
- Fallback: If |Qualified| < 20 → Relax to Top 30% per sector (Track 1 logic only).
- Logs all design changes.

Output: data/harness/qualified_set_{window}.parquet
"""

from __future__ import annotations

import datetime
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Union

# Ensure project root is in sys.path when executed as script
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import duckdb
import numpy as np
import pandas as pd

from ingestion.sector_stats import (
    SECTOR_TAXONOMY,
    as_of_latest,
    load_universe_as_of,
    monthly_rebalance_dates,
    read_factor_panel,
)

DEFAULT_FACTOR_STORE = "data/harness/factors.duckdb"
DEFAULT_UNIVERSE = "data/universe/monthly_reconstitution.parquet"
OUTPUT_DIR = Path("data/harness")

# Parameters per specification
MAINSTREAM_PCTL = 0.25
FALLBACK_PCTL = 0.30
MIN_SECTOR_SIZE = 20
ACHIEVERS_SPREAD_MIN = 0.02
QUALIFIED_FLOOR = 20


class ScreenEngine:
    """
    Dual-track screening engine for Arm A.
    """

    def __init__(
        self,
        factor_store_path: Union[str, Path] = DEFAULT_FACTOR_STORE,
        universe_path: Union[str, Path] = DEFAULT_UNIVERSE,
        output_dir: Union[str, Path] = OUTPUT_DIR,
    ):
        self.factor_store_path = Path(factor_store_path)
        self.universe_path = Path(universe_path)
        self.output_dir = Path(output_dir)
        self.design_changes_log: List[Dict[str, Any]] = []

    def log_design_change(
        self, date: pd.Timestamp, window: str, reason: str, details: Dict[str, Any]
    ) -> None:
        entry = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "rebalance_date": str(date.date()) if isinstance(date, pd.Timestamp) else str(date),
            "window": window,
            "reason": reason,
            "details": details,
        }
        self.design_changes_log.append(entry)

    def run_screen_at_date(
        self,
        as_of: pd.Timestamp,
        window_name: str,
        sector_medians: Optional[pd.DataFrame] = None,
    ) -> pd.DataFrame:
        """
        Executes Arm A dual-track screening at a single rebalance date `as_of`.
        """
        universe = load_universe_as_of(self.universe_path, as_of)
        if universe.empty or "sector" not in universe.columns:
            return pd.DataFrame()

        # Read required factor snapshots
        spread_panel = read_factor_panel(self.factor_store_path, ["ROIC_WACC_Spread"])
        yield_panel = read_factor_panel(self.factor_store_path, ["FCF_Yield"])
        margin_panel = read_factor_panel(self.factor_store_path, ["FCF_Margin"])

        spread_snap = as_of_latest(spread_panel, as_of, max_staleness_days=400)
        yield_snap = as_of_latest(yield_panel, as_of, max_staleness_days=400)
        margin_snap = as_of_latest(margin_panel, as_of, max_staleness_days=400)

        # Merge factor snapshots into universe
        df = universe[["ticker", "sector"]].copy()
        df = df.merge(spread_snap[["ticker", "value"]].rename(columns={"value": "ROIC_WACC_Spread"}), on="ticker", how="inner")
        df = df.merge(yield_snap[["ticker", "value"]].rename(columns={"value": "FCF_Yield"}), on="ticker", how="inner")
        df = df.merge(margin_snap[["ticker", "value"]].rename(columns={"value": "FCF_Margin"}), on="ticker", how="left")

        df = df.dropna(subset=["ROIC_WACC_Spread", "FCF_Yield"])
        if df.empty:
            return pd.DataFrame()

        # Sector sizes in available data
        sector_counts = df.groupby("sector")["ticker"].count().to_dict()

        # --- Track 1: Top 25% per sector by ROIC_WACC_Spread desc, then FCF_Yield desc
        # Exclude sectors with count < MIN_SECTOR_SIZE (20)
        track1_rows = []
        for sector, grp in df.groupby("sector"):
            if len(grp) < MIN_SECTOR_SIZE:
                continue
            sorted_grp = grp.sort_values(
                by=["ROIC_WACC_Spread", "FCF_Yield", "ticker"],
                ascending=[False, False, True],
            )
            cutoff = int(np.ceil(len(grp) * MAINSTREAM_PCTL))
            track1_rows.append(sorted_grp.iloc[:cutoff])

        t1_df = pd.concat(track1_rows, ignore_index=True) if track1_rows else pd.DataFrame()

        # --- Track 2: FCF_Margin > 3yr sector median AND ROIC_WACC_Spread > 2% (0.02)
        t2_df = pd.DataFrame()
        if sector_medians is not None and not sector_medians.empty:
            # sector_medians has columns: sector, value (which is FCF_Margin_median_3yr)
            med_dict = sector_medians.set_index("sector")["value"].to_dict()
            t2_cond = (
                (df["ROIC_WACC_Spread"] > ACHIEVERS_SPREAD_MIN)
                & df["FCF_Margin"].notna()
                & (df["FCF_Margin"] > df["sector"].map(med_dict))
            )
            t2_df = df[t2_cond].copy()
        else:
            # Fallback when sector_medians not pre-computed: cross-sectional trailing window query or current median
            med_dict = df.groupby("sector")["FCF_Margin"].median().to_dict()
            t2_cond = (
                (df["ROIC_WACC_Spread"] > ACHIEVERS_SPREAD_MIN)
                & df["FCF_Margin"].notna()
                & (df["FCF_Margin"] > df["sector"].map(med_dict))
            )
            t2_df = df[t2_cond].copy()

        # Union = Mainstream ∪ Achievers
        if not t1_df.empty and not t2_df.empty:
            qualified = pd.concat([t1_df, t2_df], ignore_index=True).drop_duplicates(subset=["ticker"])
        elif not t1_df.empty:
            qualified = t1_df.drop_duplicates(subset=["ticker"]).copy()
        elif not t2_df.empty:
            qualified = t2_df.drop_duplicates(subset=["ticker"]).copy()
        else:
            qualified = pd.DataFrame()

        # Check floor rule: If |Qualified| < 20 -> Relax to Top 30% per sector (Track 1 logic only)
        if len(qualified) < QUALIFIED_FLOOR:
            q_before = len(qualified)
            relaxed_rows = []
            for sector, grp in df.groupby("sector"):
                if len(grp) < MIN_SECTOR_SIZE:
                    continue
                sorted_grp = grp.sort_values(
                    by=["ROIC_WACC_Spread", "FCF_Yield", "ticker"],
                    ascending=[False, False, True],
                )
                cutoff = int(np.ceil(len(grp) * FALLBACK_PCTL))
                relaxed_rows.append(sorted_grp.iloc[:cutoff])

            qualified = pd.concat(relaxed_rows, ignore_index=True).drop_duplicates(subset=["ticker"]) if relaxed_rows else pd.DataFrame()
            
            self.log_design_change(
                date=as_of,
                window=window_name,
                reason="Qualified set floor relaxation (|Q| < 20)",
                details={
                    "original_count": q_before,
                    "relaxed_count": len(qualified),
                    "fallback_percentile": FALLBACK_PCTL,
                    "track": "Track 1 only (Top 30%)",
                },
            )

        if not qualified.empty:
            qualified["rebalance_date"] = as_of
            qualified["window"] = window_name

        return qualified

    def run_window(
        self,
        start_date: str,
        end_date: str,
        window_name: str,
    ) -> pd.DataFrame:
        """
        Runs dual-track screening across all rebalance dates in range [start_date, end_date]
        and writes output to data/harness/qualified_set_{window}.parquet.
        """
        rebalance_dates = monthly_rebalance_dates(self.universe_path, start=start_date, end=end_date)
        all_qualified = []

        # Read sector medians from sector_stats table if available
        sector_stats_df = pd.DataFrame()
        if self.factor_store_path.exists():
            with duckdb.connect(str(self.factor_store_path), read_only=True) as conn:
                tables = [r[0] for r in conn.execute("SHOW TABLES").fetchall()]
                if "sector_stats" in tables:
                    sector_stats_df = conn.execute(
                        "SELECT date, sector, value FROM sector_stats WHERE stat_name = 'FCF_Margin_median_3yr'"
                    ).df()

        for d in rebalance_dates:
            med_sub = None
            if not sector_stats_df.empty:
                sub = sector_stats_df[pd.to_datetime(sector_stats_df["date"]) == d]
                if not sub.empty:
                    med_sub = sub[["sector", "value"]]

            q_df = self.run_screen_at_date(d, window_name, sector_medians=med_sub)
            if not q_df.empty:
                all_qualified.append(q_df)

        if all_qualified:
            res_df = pd.concat(all_qualified, ignore_index=True)
        else:
            res_df = pd.DataFrame(columns=["rebalance_date", "ticker", "sector", "ROIC_WACC_Spread", "FCF_Yield", "FCF_Margin", "window"])

        self.output_dir.mkdir(parents=True, exist_ok=True)
        out_file = self.output_dir / f"qualified_set_{window_name}.parquet"
        res_df.to_parquet(out_file, index=False)

        return res_df


def run_tests() -> int:
    """Self-test for ScreenEngine."""
    from pathlib import Path
    import tempfile
    
    tmp_dir = Path(tempfile.mkdtemp())
    store_path = tmp_dir / "test_factors.duckdb"
    univ_path = tmp_dir / "test_univ.parquet"

    # Synthetic universe & factors
    rng = np.random.default_rng(42)
    ticks = [f"T{i:03d}" for i in range(140)]  # 20 per sector across 7 sectors
    sectors = [SECTOR_TAXONOMY[i % 7] for i in range(140)]
    dates = pd.date_range("2020-01-31", "2020-03-31", freq="ME")

    rows_u = []
    rows_f = []
    for d in dates:
        for t, sec in zip(ticks, sectors):
            rows_u.append({"rebalance_date": d, "ticker": t, "sector": sec, "market_cap": 1e9})
            rows_f.append({"date": d, "ticker": t, "factor_name": "ROIC_WACC_Spread", "value": rng.uniform(-0.05, 0.15)})
            rows_f.append({"date": d, "ticker": t, "factor_name": "FCF_Yield", "value": rng.uniform(0.01, 0.10)})
            rows_f.append({"date": d, "ticker": t, "factor_name": "FCF_Margin", "value": rng.uniform(0.05, 0.25)})

    pd.DataFrame(rows_u).to_parquet(univ_path)
    pd.DataFrame(rows_f).to_parquet(tmp_dir / "f.parquet")

    with duckdb.connect(str(store_path)) as conn:
        conn.execute("CREATE TABLE factor_panel (date DATE, ticker VARCHAR, factor_name VARCHAR, value DOUBLE)")
        conn.execute(f"COPY factor_panel FROM '{(tmp_dir / 'f.parquet').as_posix()}' (FORMAT PARQUET)")

    engine = ScreenEngine(factor_store_path=store_path, universe_path=univ_path, output_dir=tmp_dir)
    res = engine.run_window("2020-01-01", "2020-03-31", "test_window")

    assert not res.empty, "ScreenEngine returned empty result"
    assert (tmp_dir / "qualified_set_test_window.parquet").exists(), "Output parquet missing"
    assert "rebalance_date" in res.columns
    print(f"[OK] ScreenEngine self-test passed. Generated {len(res)} rows.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        raise SystemExit(run_tests())
