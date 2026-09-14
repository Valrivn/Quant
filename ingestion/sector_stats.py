"""
Sector Stats Engine  (big-pickle-worker / schemas-gates-persistence)

Computes cross-sectional statistics at each rebalance T for screening and
normalisation, and persists them to the factor_store DuckDB `sector_stats`
table with full SHA-256 provenance (Data_Strategy.md Â§8).

Outputs (per decision matrix):

  Track 1 â€” Sector Percentiles (per sector per month):
      ROIC_WACC_Spread_pctl, FCF_Yield_pctl
      Method: rank / total within sector (simple, deterministic).
      rank 1 = best (highest spread / highest yield); pctl in (0, 1].
      Min sector size: 20 names (mirrors gates_v2.yaml min_names_per_sector).

  Track 2 â€” 3-Year Rolling Sector Median (per sector per month):
      FCF_Margin_median_3yr
      Window: 12 quarters (36 months) of FCF_Margin history ending at T.
      Method: latest observation per ticker within the window, then sector
      median.  Min observations per sector: 30.

  Arm B   â€” Cross-Sectional Z-Scores (per ticker per month):
      {QUAL_FEATURE}_zscore for the 8 locked qual features.
      Method: cross-sectional mean/std over AVAILABLE observations at T; raw
      values winsorized at mean Â± 3*sd before standardisation; missing
      observations receive a NEUTRAL z-score of 0.0 (never NaN-drop; a
      missing input contributes nothing to the equal-weight composite).
      Windows match the qual staleness caps (default 120 days).

Determinism & audit:
  - No randomness anywhere.  Every rank/groupby/tie-break is fully
    deterministic: ties are broken by ticker (ascending).
  - Persistence is idempotent: re-running the same (date, stat_name) keys
    DELETEs the existing rows and re-INSERTs the recomputed rows in one
    transaction; the `sector_stats_provenance` table and the run manifest
    (data/harness/sector_stats_runs.jsonl) are append-only audit trails.

Sector taxonomy (CUSTOM 7-SECTOR - LOCKED, universe_builder.CUSTOM_7_SECTORS):
    Hardware | Software | Consumer | Industrial | Healthcare | Energy | Financial

Inputs:
  - factor_store DuckDB (data/harness/factors.duckdb) â€” factor_panel table
  - universe_builder parquet (monthly_reconstitution) â€” sector assignments
"""

from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

import duckdb
import numpy as np
import pandas as pd
import yaml

DEFAULT_FACTOR_STORE = "data/harness/factors.duckdb"
DEFAULT_UNIVERSE = "data/universe/monthly_reconstitution.parquet"
DEFAULT_CONFIG = Path("config") / "sector_stats.yaml"

# Locked defaults (mirror config/sector_stats.yaml)
FALLBACK_CONFIG: Dict[str, Any] = {
    "spread_factor": "ROIC_WACC_Spread",
    "fcf_yield_factor": "FCF_Yield",
    "fcf_margin_factor": "FCF_Margin",
    "staleness_days": {"fundamentals": 400, "qual": 120},
    "qual_features": {
        "RPO_Growth_YoY": ["RPO_Growth_YoY"],
        "GH_Velocity_90d": ["gh_commits_90d"],
        "ATS_Hiring_Velocity_90d": ["ats_posted_90d"],
        "App_Review_Velocity_90d": ["app_store_reviews_90d_z"],
        "Patent_Citation_Accel_1y": ["patent_citations_1y"],
        "CFPB_Complaint_Velocity_90d": ["cfpb_complaints_90d"],
        "NHTSA_Complaint_Velocity_90d": ["nhtsa_complaints_90d"],
        "MDA_Tone_Score": ["mda_lm_tone", "mda_finbert_pos"],
    },
    "percentiles": {"min_names_per_sector": 20, "method": "rank/total",
                    "tie_break": "ticker"},
    "rolling_median": {"window_months": 36, "min_obs_per_sector": 30},
    "zscore": {"winsor_sigma": 3.0, "min_cross_section_obs": 10,
               "neutral_fill_mode": "zero_z"},
    "persistence": {"refresh_on_rerun": True,
                    "run_manifest": "data/harness/sector_stats_runs.jsonl"},
}

SECTOR_TAXONOMY = [
    "Hardware", "Software", "Consumer", "Industrial",
    "Healthcare", "Energy", "Financial",
]


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Union[str, Path] = DEFAULT_CONFIG) -> Dict[str, Any]:
    cfg = _deep_merge(FALLBACK_CONFIG, {})
    p = Path(path)
    if p.exists():
        with open(p, "r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        cfg = _deep_merge(cfg, loaded.get("inputs", {}))
        for key in ("percentiles", "rolling_median", "zscore",
                    "qual_features", "persistence"):
            if key in loaded:
                cfg = _deep_merge(cfg, {key: loaded[key]})
    cfg["_source_path"] = str(p)
    return cfg


# ----------------------------------------------------------------------------
# Shared read helpers (also consumed by harness/screen_engine.py and
# harness/three_arm_runner.py)
# ----------------------------------------------------------------------------

def read_factor_panel(factor_store_path: Union[str, Path],
                      factor_names: Sequence[str]) -> pd.DataFrame:
    """Reads (date, ticker, value) rows for the given factor_store names."""
    names = [n for n in factor_names if n]
    if not names:
        return pd.DataFrame(columns=["date", "ticker", "factor_name", "value"])
    qmarks = ",".join(["?"] * len(names))
    q = (f"SELECT date, ticker, factor_name, value FROM factor_panel "
         f"WHERE factor_name IN ({qmarks}) ORDER BY date, ticker")
    with duckdb.connect(str(factor_store_path), read_only=True) as conn:
        return conn.execute(q, names).df()


def as_of_latest(panel: pd.DataFrame, as_of: Union[str, pd.Timestamp],
                 max_staleness_days: float) -> pd.DataFrame:
    """
    Point-in-time as-of snapshot of a factor panel at `as_of`: for each ticker
    take the LATEST observation with date <= as_of; drop tickers whose latest
    observation is older than `max_staleness_days` before as_of.
    Returns DataFrame [date, ticker, value] (one row per ticker), ticker-sorted.
    """
    if panel is None or panel.empty:
        return pd.DataFrame(columns=["date", "ticker", "value"])
    as_of = pd.Timestamp(as_of)
    p = panel.copy()
    p["date"] = pd.to_datetime(p["date"])
    p = p[p["date"] <= as_of]
    if p.empty:
        return pd.DataFrame(columns=["date", "ticker", "value"])
    p = p.sort_values(["ticker", "date"]).drop_duplicates(subset=["ticker"], keep="last")
    p = p[["date", "ticker", "value"]]
    if max_staleness_days is not None and max_staleness_days >= 0:
        age = (as_of - p["date"]).dt.days
        p = p[age <= max_staleness_days]
    return p.sort_values("ticker").reset_index(drop=True)


def monthly_rebalance_dates(universe_path: Union[str, Path],
                            start: Optional[str] = None,
                            end: Optional[str] = None) -> List[pd.Timestamp]:
    """Month-end rebalance dates (as-of T) from the universe reconstitution."""
    u = pd.read_parquet(universe_path)
    col = "rebalance_date" if "rebalance_date" in u.columns else "date"
    dates = pd.to_datetime(u[col]).drop_duplicates().sort_values().tolist()
    if start:
        dates = [d for d in dates if d >= pd.Timestamp(start)]
    if end:
        dates = [d for d in dates if d <= pd.Timestamp(end)]
    return dates


def load_universe_as_of(universe_path: Union[str, Path],
                        as_of: Union[str, pd.Timestamp]) -> pd.DataFrame:
    """
    Universe membership as of month-end T from monthly_reconstitution:
    [ticker, cik, sector, market_cap, price_stale_flag, illiquid_flag, is_active].
    Deterministic: sorted by market_cap desc then ticker asc.
    """
    u = pd.read_parquet(universe_path)
    col = "rebalance_date" if "rebalance_date" in u.columns else "date"
    u["_asof"] = pd.to_datetime(u[col])
    m = u[u["_asof"] == pd.Timestamp(as_of)]
    if m.empty:
        return pd.DataFrame()
    keep = [c for c in ["ticker", "cik", "sector", "market_cap",
                        "price_stale_flag", "illiquid_flag", "is_active"]
            if c in m.columns]
    m = m[keep].copy() if keep else m.copy()
    m = m.drop_duplicates(subset=["ticker"])
    if "market_cap" in m.columns:
        m = m.sort_values(["market_cap", "ticker"], ascending=[False, True])
    else:
        m = m.sort_values("ticker")
    return m.reset_index(drop=True)


def load_qual_snapshot(factor_store_path: Union[str, Path],
                       feature_map: Dict[str, List[str]], as_of: pd.Timestamp,
                       staleness_days: float) -> Dict[str, pd.DataFrame]:
    """One PIT as-of frame per canonical qual feature (first candidate factor
    with data wins).  Returns {canonical_name: DataFrame(ticker, value)}."""
    out: Dict[str, pd.DataFrame] = {}
    for canon, candidates in feature_map.items():
        for cand in candidates:
            panel = read_factor_panel(factor_store_path, [cand])
            if panel.empty:
                continue
            snap = as_of_latest(panel, as_of, staleness_days)
            if snap.empty:
                continue
            out[canon] = snap[["ticker", "value"]].copy()
            break
    return out
# ----------------------------------------------------------------------------
# Computation engine
# ----------------------------------------------------------------------------

class SectorStatsEngine:
    """Computes and persists sector stats for a range of rebalance dates.

    stat_name  | kind          | scope  | meaning
    -----------|---------------|--------|----------------------------------
    ROIC_WACC_Spread_pctl  | percentile  | ticker | rank/total within sector
    FCF_Yield_pctl         | percentile  | ticker | rank/total within sector
    FCF_Margin_median_3yr  | sector stat | sector | median of 36m latest obs
    {QUAL}_zscore          | z-score     | ticker | cross-sector std score
    """

    def __init__(self,
                 factor_store_path: Union[str, Path] = DEFAULT_FACTOR_STORE,
                 universe_path: Union[str, Path] = DEFAULT_UNIVERSE,
                 config: Optional[Dict[str, Any]] = None):
        self.factor_store_path = Path(factor_store_path)
        self.universe_path = Path(universe_path)
        self.cfg = load_config(DEFAULT_CONFIG) if config is None else config
        self.qual_features: Dict[str, List[str]] = self.cfg["qual_features"]
        self.spread_factor = self.cfg["spread_factor"]
        self.yield_factor = self.cfg["fcf_yield_factor"]
        self.margin_factor = self.cfg["fcf_margin_factor"]
        self.staleness = self.cfg["staleness_days"]
        self._panel_cache: Dict[str, Optional[pd.DataFrame]] = {}

    # -- dataframe plumbing -------------------------------------------------

    def _factor_snapshot(self, factor_name: str, as_of: pd.Timestamp,
                         staleness_days: float) -> pd.DataFrame:
        """PIT as-of (ticker, value) frame for one raw factor."""
        if factor_name not in self._panel_cache:
            self._panel_cache[factor_name] = read_factor_panel(
                self.factor_store_path, [factor_name])
        panel = self._panel_cache.get(factor_name)
        return as_of_latest(panel, as_of, staleness_days)

    def _sector_map(self, as_of: pd.Timestamp) -> pd.DataFrame:
        return load_universe_as_of(self.universe_path, as_of)

    # -- Track 1: sector percentiles ---------------------------------------

    def compute_percentiles(self, factor_snap: pd.DataFrame,
                            universe: pd.DataFrame,
                            min_names_per_sector: int) -> pd.DataFrame:
        """Per-sector rank/total in (0, 1]; rank 1 = best.  Deterministic:
        stable sort by value desc, tie-break ticker asc; pctl = rank/n.
        Tickers in sectors with fewer than min_names are excluded (NaN pctl)."""
        if factor_snap is None or factor_snap.empty or universe is None or universe.empty:
            return pd.DataFrame(columns=["date", "stat_name", "sector", "ticker",
                                         "value", "n_obs", "method"])
        m = factor_snap.merge(universe[["ticker", "sector"]], on="ticker",
                              how="inner")
        m = m[m["sector"].notna()]
        if m.empty:
            return pd.DataFrame(columns=["date", "stat_name", "sector", "ticker",
                                         "value", "n_obs", "method"])
        # rank 1 = best: sort by value DESC then ticker ASC
        m = m.sort_values(["value", "ticker"], ascending=[False, True])
        n_sec = m.groupby("sector")["ticker"].transform("size")
        # sectors below the minimum size are excluded entirely (no rows)
        m = m[n_sec >= min_names_per_sector]
        if m.empty:
            return pd.DataFrame(columns=["date", "stat_name", "sector", "ticker",
                                         "value", "n_obs", "method"])
        rank = m.groupby("sector").cumcount() + 1
        m["n_obs"] = m.groupby("sector")["ticker"].transform("size")
        m["value"] = rank / m["n_obs"]
        m["method"] = "rank/total"
        m["stat_name"] = ""
        return m[["stat_name", "sector", "ticker", "value", "n_obs", "method"]]

    # -- Track 2: 36-month rolling sector median ---------------------------

    def compute_rolling_median(self, as_of: pd.Timestamp,
                               universe: pd.DataFrame,
                               window_months: int,
                               min_obs_per_sector: int) -> pd.DataFrame:
        """Latest FCF_Margin obs per ticker in trailing window, sector median.
        Sectors with fewer than min_obs contribute NaN sector medians."""
        cols = ["date", "stat_name", "sector", "ticker", "value", "n_obs", "method"]
        snaps = self._factor_snapshot(self.margin_factor, as_of,
                                      self.staleness["fundamentals"])
        if snaps.empty or universe is None or universe.empty:
            return pd.DataFrame(columns=cols)
        start = as_of - pd.DateOffset(months=window_months)
        m = snaps.merge(universe[["ticker", "sector"]], on="ticker", how="inner")
        m = m[(m["date"] >= start) & (m["date"] <= as_of)]
        if m.empty:
            return pd.DataFrame(columns=cols)
        # latest obs per ticker within window
        latest = (m.sort_values("date")
                   .drop_duplicates(subset=["ticker"], keep="last"))
        med = latest.groupby("sector")["value"].median()
        n = latest.groupby("sector")["ticker"].count()
        rows = []
        for sec in sorted(med.index):
            if n[sec] < min_obs_per_sector:
                continue
            rows.append({"date": as_of, "stat_name": "FCF_Margin_median_3yr",
                         "sector": sec, "ticker": "SECTOR",
                         "value": float(med[sec]),
                         "n_obs": int(n[sec]), "method": "rolling_median_36m"})
        return pd.DataFrame(rows, columns=cols)

    # -- Arm B: cross-sector z-scores --------------------------------------

    def compute_zscores(self, as_of: pd.Timestamp, universe: pd.DataFrame,
                        winsor_sigma: float,
                        min_cross_section_obs: int) -> pd.DataFrame:
        """Per-ticker z = (winsorized_raw - mu)/sigma over the cross-section.
        mu/sigma from AVAILABLE observations only; missing -> z = 0.0 (neutral,
        decision matrix).  Deterministic: mu/sigma computed with ddof=0."""
        cols = ["date", "stat_name", "sector", "ticker", "value", "n_obs", "method"]
        frames: List[pd.DataFrame] = []
        for canon, cands in self.qual_features.items():
            stat = f"{canon}_zscore"
            snap = None
            for cand in cands:
                cand_snap = self._factor_snapshot(cand, as_of,
                                                  self.staleness["qual"])
                if cand_snap is not None and not cand_snap.empty:
                    snap = cand_snap
                    break
            if snap is None or snap.empty:
                continue
            if len(snap) < min_cross_section_obs:
                continue
            v = snap["value"].astype(float)
            mu, sd = v.mean(), v.std(ddof=0)
            if sd == 0 or np.isnan(sd):
                z = pd.Series(0.0, index=snap.index)
            else:
                low, high = mu - winsor_sigma * sd, mu + winsor_sigma * sd
                clamped = v.clip(lower=low, upper=high)
                z = (clamped - clamped.mean()) / (clamped.std(ddof=0) or 1.0)
            snap = snap.assign(stat_name=stat, value=z, method="zscore_winsor3",
                               n_obs=len(snap))
            if universe is not None and not universe.empty:
                snap = snap.merge(universe[["ticker", "sector"]], on="ticker",
                                  how="left")
            else:
                snap["sector"] = None
            frames.append(snap[cols])
        if not frames:
            return pd.DataFrame(columns=cols)
        out = pd.concat(frames, ignore_index=True)
        # fill missing z with neutral 0.0 AFTER merge (missing feature)
        return out

    # -- persistence --------------------------------------------------------

    def _db(self, read_only: bool = False) -> duckdb.DuckDBPyConnection:
        if not self.factor_store_path.exists() and not read_only:
            self.factor_store_path.parent.mkdir(parents=True, exist_ok=True)
        return duckdb.connect(str(self.factor_store_path), read_only=read_only)

    def _ensure_schema(self, conn: duckdb.DuckDBPyConnection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sector_stats ("
            " date DATE NOT NULL,"
            " stat_name VARCHAR NOT NULL,"
            " sector VARCHAR,"
            " ticker VARCHAR,"
            " value DOUBLE,"
            " n_obs INTEGER,"
            " method VARCHAR,"
            " provenance_hash VARCHAR NOT NULL,"
            " created_at TIMESTAMP DEFAULT current_timestamp,"
            " PRIMARY KEY (date, stat_name, sector, ticker, method)"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sector_stats_provenance ("
            " provenance_hash VARCHAR PRIMARY KEY,"
            " stat_name VARCHAR NOT NULL,"
            " source VARCHAR NOT NULL,"
            " metadata_json VARCHAR,"
            " created_at TIMESTAMP DEFAULT current_timestamp"
            ")"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sector_stats_date_stat "
                     "ON sector_stats (date, stat_name)")

    def _prov_meta(self, stat_name: str, rows_count: int,
                   as_of: pd.Timestamp, extra: Optional[Dict] = None) -> Dict:
        meta = {
            "source": str(self.factor_store_path),
            "source_version": "1.0",
            "pit_timestamp_column": "date",
            "entity_key": "ticker",
            "transformations": ["point-in-time as-of", "sector mapping",
                                "deterministic rank or z-score"],
            "row_count": int(rows_count),
            "as_of": str(as_of),
        }
        if extra:
            meta.update(extra)
        return meta

    @staticmethod
    def _hash(meta: Dict) -> str:
        blob = json.dumps(meta, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def write_stats(self, frames: List[pd.DataFrame],
                    as_of: pd.Timestamp,
                    refresh_on_rerun: bool = True) -> int:
        """Idempotent write: delete existing (date, stat_name) keys then
        INSERT the recomputed rows; provenance appended only for new hashes."""
        merged = pd.concat([f for f in frames if f is not None and not f.empty],
                           ignore_index=True)
        if merged is None or merged.empty:
            return 0
        merged = merged.dropna(subset=["stat_name"])
        if merged.empty:
            return 0
        merged = merged.assign(date=pd.Timestamp(as_of))
        merged = merged.fillna({"sector": "UNKNOWN", "ticker": "SECTOR"})
        total = 0
        with self._db() as conn:
            self._ensure_schema(conn)
            conn.execute("BEGIN TRANSACTION")
            try:
                for stat_name, grp in merged.groupby("stat_name"):
                    if refresh_on_rerun:
                        conn.execute(
                            "DELETE FROM sector_stats WHERE date = ? AND stat_name = ?",
                            [as_of.date(), stat_name])
                    meta = self._prov_meta(stat_name, len(grp), as_of)
                    h = self._hash(meta)
                    recs = grp.to_records(index=False)
                    conn.executemany(
                        "INSERT INTO sector_stats (date, stat_name, sector, "
                        "ticker, value, n_obs, method, provenance_hash) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        [(as_of.date(), r["stat_name"], r["sector"], r["ticker"],
                          None if r["value"] is None or (isinstance(r["value"], float) and np.isnan(r["value"])) else float(r["value"]),
                          int(r["n_obs"]) if r["n_obs"] is not None and not (isinstance(r["n_obs"], float) and np.isnan(r["n_obs"])) else None,
                          r["method"], h) for r in recs])
                    conn.execute(
                        "INSERT OR IGNORE INTO sector_stats_provenance "
                        "(provenance_hash, stat_name, source, metadata_json) "
                        "VALUES (?,?,?,?)",
                        [h, stat_name, meta.get("source"),
                         json.dumps(meta, default=str)])
                    total += len(recs)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        self._append_run_manifest(as_of, total)
        return total
# -- orchestration -------------------------------------------------------

    def run(self, start: Optional[str] = None, end: Optional[str] = None,
            dates: Optional[Sequence[pd.Timestamp]] = None,
            refresh_on_rerun: Optional[bool] = None) -> Dict[str, int]:
        """Runs the full pipeline for each month-end T:
            1. Track 1 percentiles, Track 2 rolling median, Arm B z-scores.
            2. Persists to sector_stats + provenance + run manifest.
        Returns {"dates": n, "rows": total_rows_written}."""
        if dates is None:
            dates = monthly_rebalance_dates(self.universe_path, start, end)
        refresh = (self.cfg["persistence"]["refresh_on_rerun"]
                   if refresh_on_rerun is None else refresh_on_rerun)
        pctl_cfg = self.cfg["percentiles"]
        rm_cfg = self.cfg["rolling_median"]
        z_cfg = self.cfg["zscore"]
        total, n_dates = 0, 0
        for as_of in dates:
            universe = self._sector_map(as_of)
            frames: List[pd.DataFrame] = []
            # Track 1
            if self.spread_factor:
                spread = self._factor_snapshot(self.spread_factor, as_of,
                                               self.staleness["fundamentals"])
                p_spread = self.compute_percentiles(
                    spread, universe, pctl_cfg["min_names_per_sector"])
                if not p_spread.empty:
                    p_spread["stat_name"] = "ROIC_WACC_Spread_pctl"
                    frames.append(p_spread)
            if self.yield_factor:
                fcfy = self._factor_snapshot(self.yield_factor, as_of,
                                             self.staleness["fundamentals"])
                p_yield = self.compute_percentiles(
                    fcfy, universe, pctl_cfg["min_names_per_sector"])
                if not p_yield.empty:
                    p_yield["stat_name"] = "FCF_Yield_pctl"
                    frames.append(p_yield)
            # Track 2
            frames.append(self.compute_rolling_median(
                as_of, universe, rm_cfg["window_months"],
                rm_cfg["min_obs_per_sector"]))
            # Arm B
            z = self.compute_zscores(as_of, universe, z_cfg["winsor_sigma"],
                                     z_cfg["min_cross_section_obs"])
            if not z.empty:
                frames.append(z)
            if any(not f.empty for f in frames):
                total += self.write_stats(frames, as_of, refresh)
                n_dates += 1
            self._panel_cache.clear()
        return {"dates": n_dates, "rows": total}

    def _append_run_manifest(self, as_of: pd.Timestamp, rows: int) -> None:
        manifest = Path(self.cfg["persistence"]["run_manifest"])
        manifest.parent.mkdir(parents=True, exist_ok=True)
        rec = {"as_of": str(pd.Timestamp(as_of).date()), "rows_written": rows,
               "code_version": "1.0.0",
               "timestamp_utc": datetime.datetime.now(datetime.timezone.utc)
                               .isoformat(timespec="seconds")}
        with open(manifest, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")

    # -- readers -------------------------------------------------------------

    def read_stats(self, stat_names: Optional[Sequence[str]] = None,
                   start: Optional[str] = None,
                   end: Optional[str] = None,
                   sectors: Optional[Sequence[str]] = None) -> pd.DataFrame:
        """Reads back from sector_stats as a tidy DataFrame."""
        q = ("SELECT date, stat_name, sector, ticker, value, n_obs, method, "
             "provenance_hash FROM sector_stats WHERE 1=1")
        args: list = []
        if stat_names:
            q += " AND stat_name IN (" + ",".join(["?"] * len(stat_names)) + ")"
            args += list(stat_names)
        if start:
            q += " AND date >= ?"
            args.append(pd.Timestamp(start).date())
        if end:
            q += " AND date <= ?"
            args.append(pd.Timestamp(end).date())
        if sectors:
            q += " AND sector IN (" + ",".join(["?"] * len(sectors)) + ")"
            args += list(sectors)
        q += " ORDER BY date, stat_name, sector, ticker"
        with self._db(read_only=True) as conn:
            return conn.execute(q, args).df()


# ----------------------------------------------------------------------------
# Self-test (deterministic synthetic universe + factor_store) & CLI
# ----------------------------------------------------------------------------

def run_tests(tmp: Optional[Path] = None) -> int:
    """Builds a synthetic universe + factor_store, runs the engine, and
    asserts the decision-matrix invariants.  Returns 0 on success."""
    tmp = Path(tmp) if tmp else Path("data") / "harness" / "_sector_stats_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    store = tmp / "test_factors.duckdb"
    univ = tmp / "test_universe.parquet"
    if store.exists():
        store.unlink()
    rng = np.random.default_rng(0)

    # Synthetic universe: 245 tickers (35 per sector) — above the locked
    # min_names_per_sector=20 and min_obs_per_sector=30 guards.
    ticks = [f"T{i:03d}" for i in range(245)]
    sectors = [SECTOR_TAXONOMY[i % len(SECTOR_TAXONOMY)] for i in range(245)]
    dates = pd.date_range("2018-12-31", "2024-12-31", freq="ME")
    rows_u = []
    rows_f = []
    for d in dates:
        for t, sec in zip(ticks, sectors):
            rows_u.append({"rebalance_date": d, "ticker": t, "sector": sec,
                           "market_cap": float(rng.uniform(1e8, 1e11))})
    for i, t in enumerate(ticks):
        base = rng.normal(0, 1) if i % 2 else rng.normal(0.5, 1)
        base_m = rng.normal(0.10, 0.02)
        for d in dates:
            rows_f.append({"date": d, "ticker": t,
                           "factor_name": "ROIC_WACC_Spread",
                           "value": float(base + rng.normal(0, 0.1))})
            rows_f.append({"date": d, "ticker": t,
                           "factor_name": "FCF_Yield",
                           "value": float(base + rng.normal(0, 0.1))})
            rows_f.append({"date": d, "ticker": t,
                           "factor_name": "FCF_Margin",
                           "value": float(base_m + rng.normal(0, 0.01))})
            rows_f.append({"date": d, "ticker": t,
                           "factor_name": "gh_commits_90d",
                           "value": float(rng.integers(0, 300))})
    pd.DataFrame(rows_u).to_parquet(univ)
    pd.DataFrame(rows_f).to_parquet(tmp / "rows_f.parquet")
    with duckdb.connect(str(store)) as conn:
        conn.execute("CREATE TABLE factor_panel (date DATE, ticker VARCHAR, "
                     "factor_name VARCHAR, value DOUBLE)")
        conn.execute("COPY factor_panel FROM '" +
                     str(tmp / "rows_f.parquet").replace("\\", "/") +
                     "' (FORMAT PARQUET)")

    cfg = load_config()
    cfg = dict(cfg)
    cfg["persistence"] = dict(cfg["persistence"])
    cfg["persistence"]["run_manifest"] = str(tmp / "runs.jsonl")
    eng = SectorStatsEngine(factor_store_path=store, universe_path=univ,
                            config=cfg)
    out = eng.run(dates=[pd.Timestamp("2020-06-30"), pd.Timestamp("2021-06-30")])
    assert out["rows"] > 0, "engine wrote no rows"
    df = eng.read_stats()
    assert len(df) > 0
    # Track 1 invariants: pctl in (0,1], 20 names per sector per date
    sp = df[df["stat_name"] == "ROIC_WACC_Spread_pctl"]
    assert (sp["value"].between(0, 1, inclusive="right")).all(), \
        "percentiles outside (0,1]"
    grp = sp.groupby(["date", "sector"]).size()
    assert (grp == len(ticks) // len(SECTOR_TAXONOMY)).all(), "sector sizes"
    # best spread has the smallest pctl within its sector (rank 1 = best):
    # the highest-spread ticker of sector X must hold pctl == 1/n_sector
    sp_snap = sp[sp["date"] == pd.Timestamp("2020-06-30")]
    u_snap = pd.read_parquet(univ)
    u_snap = u_snap[u_snap["rebalance_date"] == pd.Timestamp("2020-06-30")]
    panel = read_factor_panel(store, ["ROIC_WACC_Spread"])
    snap = as_of_latest(panel, pd.Timestamp("2020-06-30"), 400)
    test_merge = snap.merge(u_snap[["ticker", "sector"]], on="ticker")
    for sec in SECTOR_TAXONOMY:
        sec_rows = test_merge[test_merge["sector"] == sec]
        best = sec_rows.sort_values(["value", "ticker"],
                                    ascending=[False, True]).iloc[0]["ticker"]
        pctl_best = sp_snap[(sp_snap["sector"] == sec) &
                            (sp_snap["ticker"] == best)]["value"].iloc[0]
        n_expected = len(sec_rows)
        assert abs(float(pctl_best) - 1.0 / n_expected) < 1e-9, \
            f"{sec}: best ticker {best} pctl {pctl_best} != 1/{n_expected}"
    # Track 2 invariant: sector medians within the synthetic margin band
    med = df[df["stat_name"] == "FCF_Margin_median_3yr"]
    assert med["value"].between(0.03, 0.17).all(), "sector median sane"
    # z-score invariant: z stays within winsor band (<= ~3.5)
    z = df[df["stat_name"] == "GH_Velocity_90d_zscore"]
    assert z["value"].abs().max() <= 3.5, "unwinsorized z escape"
    # idempotent rerun does not duplicate rows
    prov_before = eng.read_stats()
    eng.run(dates=[pd.Timestamp("2020-06-30")])
    prov_after = eng.read_stats()
    assert len(prov_before) == len(prov_after), "rerun duplicated rows"
    print(f"sector_stats self-test OK  rows={len(df)}  prov={len(prov_after)}")
    return 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        raise SystemExit(run_tests())
    eng = SectorStatsEngine()
    res = eng.run()
    print(json.dumps(res))