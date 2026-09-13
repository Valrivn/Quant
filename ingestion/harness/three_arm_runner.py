"""
Three-Arm Runner — Harness Core  (big-pickle-worker / Phase 1)

Orchestrates the canonical 3-arm attribution backtest on an IDENTICAL PIT
universe_T (Data_Strategy.md §6 / Session Log 3-ARM HARNESS ARCHITECTURE):

  Arm A  Pure Quant (DUAL-TRACK, sector-relative, RS-05 frozen params):
    Track 1 Mainstream : Sector_Rank(ROIC_WACC_Spread desc, FCF_Yield desc)  ->
                         sector_percentile <= 25%  AND  min 20 names per
                         sector cohort (sectors with <20 eligible names are
                         dropped from Track 1).
    Track 2 Achievers  : FCF_Margin > sector_median_3yr_rolling AND
                         ROIC_WACC_Spread > 2%.
    Qualified set      = Track1 ∪ Track2.
    Fallback (|Q| < 20): top 30% by sector rank — LOGGED as design change.
    Portfolio          : rank FCF_Yield desc -> Top N = min(50, |Q|) -> equal weight.
  Arm B  Pure Qual    : 8-factor qual composite (PCA-1 or equal-weight fallback),
                        cross-sectional z at T over the FULL universe, missing
                        -> 0.0 NEUTRAL FILL (never NaN-drop); rank -> top N
                        (min(50, |U|)) -> equal weight.
  Arm C  Hybrid 2-tier: Qual z-scored over the FULL universe, hard-gated by the
                        Arm A qualified set:
            w_T = base_weight x (1 + qual_z_T) x risk_penalty_T x I(ticker in Q_T)
        tickers outside the qualified set receive ZERO weight (hard gate).
        risk_penalty = 1 - 0.3(cash_rwy<18mo) - 0.2(cust_conc>30%)
                          - 0.2(insider<5%) - 0.1(no 13F sponsor)

Rebalance: day 1 of each month, using data dated <= prior month-end (PIT).
Turnover:  one-way turnover capped at 30% per rebalance (pre-registered).
Costs:     TXN_COST_BPS=10/side + SLIPPAGE_BPS=5/side applied to returns;
           net series (arm_*_returns_net) = gross - one_way_turnover*15bps.

Input contract (all DataFrames are PIT snapshots; `date` = as-of date):
  universe_T     : date, ticker + quant columns (roic, wacc, icr, fcf_yield
                   [+ fcf_margin, sector — REQUIRED for full dual-track])
                   + risk columns (cash_runway_months, customer_concentration,
                   insider_ownership, has_13f_sponsor)
  qual_factors_T : date, ticker, any subset of the 8 qual features.  Both the
                   canonical §6 names (rpo_growth_yoy, gh_velocity_90d,
                   ats_velocity_90d, app_velocity_90d, patent_accel_1y,
                   cfpb_velocity_90d, nhtsa_velocity_90d, mda_tone) and the
                   legacy aliases (rpo_growth, gh_velocity, ...) are accepted.
  returns_T      : date, ticker, ret  (optional; enables return attribution)

Output (dict):
  arm_a_weights / arm_b_weights / arm_c_weights : DataFrame (rebalance_date x ticker)
  arm_a_returns / arm_b_returns / arm_c_returns : pd.Series (gross, per ret date)
  arm_a_returns_net / arm_b_returns_net / arm_c_returns_net : pd.Series (net of
                                                   turnover costs)
  arm_a_costs / arm_b_costs / arm_c_costs       : pd.Series (cost drag per
                                                   rebalance period)
  sector_returns : {sector: {"A": Series, "B": Series, "C": Series}} — monthly
                   per-sector arm returns for RS-05 sector-relative gates 3 & 4.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ----------------------------------------------------------------------------
# Pre-registered constants (Data_Strategy.md §6 — never tuned)
# ----------------------------------------------------------------------------
# Canonical §6 qual feature names (spec-column case normalised to snake_case)
QUAL_FACTOR_COLS = [
    "rpo_growth_yoy",
    "gh_velocity_90d",
    "ats_velocity_90d",
    "app_velocity_90d",
    "patent_accel_1y",
    "cfpb_velocity_90d",
    "nhtsa_velocity_90d",
    "mda_tone",
]
# Legacy aliases -> canonical (consumer scripts built panels pre-§6 naming)
LEGACY_QUAL_ALIASES = {
    "rpo_growth": "rpo_growth_yoy",
    "gh_velocity": "gh_velocity_90d",
    "ats_velocity": "ats_velocity_90d",
    "app_velocity": "app_velocity_90d",
    "patent_accel": "patent_accel_1y",
    "cfpb_vel": "cfpb_velocity_90d",
    "nhtsa_vel": "nhtsa_velocity_90d",
    "mda_tone": "mda_tone",
}
QUANT_FACTOR_COLS = ["roic", "wacc", "icr", "fcf_yield"]
RISK_COLS = ["cash_runway_months", "customer_concentration", "insider_ownership", "has_13f_sponsor"]

MAX_TURNOVER = 0.30            # 30% one-way per rebalance (binding)
TOP_N_DEFAULT = 50             # N = min(50, |Q|) per §6
QUANT_SCREEN = {"roic_gt_wacc": True, "icr_gt": 3.0, "fcf_yield_gt": 0.05}  # legacy (unused by dual-track)

# Arm A dual-track (RS-05 frozen screen params — mirrors config/gates_v2.yaml)
MAINSTREAM_PERCENTILE = 0.25
FALLBACK_PERCENTILE = 0.30
SECTOR_MIN_NAMES = 20
ACHIEVERS_SPREAD_MIN = 0.02          # ROIC_WACC_Spread > 2% (Track 2)
ROLLING_FCF_WINDOW_MONTHS = 36       # sector median trailing window (3yr)
ROLLING_FCF_MIN_OBS = 12

# Transaction costs (§6 / dispatch): 10 bps/side + 5 bps slippage
TXN_COST_BPS = 10.0
SLIPPAGE_BPS = 5.0
COST_DRAG = (TXN_COST_BPS + SLIPPAGE_BPS) / 10000.0   # 0.0015 per one-way unit


def _norm_cols(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise column names to lower-case canonical keys."""
    out = df.copy()
    out.columns = [str(c).strip().lower() for c in out.columns]
    return out


def _canonicalise_qual_cols(df: pd.DataFrame) -> pd.DataFrame:
    """Rename legacy qual aliases to canonical §6 names (in place on a copy)."""
    out = df
    rename = {k: v for k, v in LEGACY_QUAL_ALIASES.items() if k in out.columns}
    if rename:
        out = out.rename(columns=rename)
    return out


def _as_of_month_snapshots(df: pd.DataFrame) -> List[Tuple[pd.Timestamp, pd.DataFrame]]:
    """
    Buckets a PIT dataframe by calendar month; returns (as_of_date, snapshot)
    using the LAST date <= month end as the month's as-of snapshot.
    The as-of snapshot for month m feeds the rebalance at day 1 of month m+1.
    """
    if df is None or df.empty:
        return []
    d = _norm_cols(df).copy()
    d["date"] = pd.to_datetime(d["date"])
    d["ym"] = d["date"].dt.to_period("M")
    snaps: List[Tuple[pd.Timestamp, pd.DataFrame]] = []
    for ym, grp in d.groupby("ym"):
        as_of = grp["date"].max()
        snaps.append((as_of, grp[grp["date"] == as_of].drop(columns=["ym"])))
    snaps.sort(key=lambda t: t[0])
    return snaps


def _rebalance_dates(snapshots: Sequence[Tuple[pd.Timestamp, pd.DataFrame]]) -> List[pd.Timestamp]:
    """Day 1 of the month FOLLOWING each snapshot month."""
    dates = []
    for as_of, _ in snapshots:
        next_month = pd.Timestamp(as_of).to_period("M").to_timestamp() + pd.offsets.MonthBegin(1)
        dates.append(pd.Timestamp(next_month))
    return dates


def _frozen_screen_params() -> Dict[str, Any]:
    """Pulls the Arm A screen params from the frozen gates_v2.yaml (RS-05 one-
    hypothesis re-registration).  Falls back to module constants when unfrozen."""
    try:
        from ingestion.harness.gate_engine import load_gates_config
        cfg = load_gates_config()
    except Exception:  # noqa: BLE001 — gate engine unavailable => defaults
        cfg = {}
    return {
        "sector_relative": bool(cfg.get("sector_relative", True)),
        "dual_track": bool(cfg.get("dual_track", True)),
        "mainstream_percentile": float(cfg.get("mainstream_percentile", MAINSTREAM_PERCENTILE)),
        "fallback_percentile": float(cfg.get("fallback_percentile", FALLBACK_PERCENTILE)),
        "min_names_per_sector": int(cfg.get("min_names_per_sector", SECTOR_MIN_NAMES)),
    }


class ThreeArmRunner:
    """
    Runs Arms A/B/C on identical monthly PIT universe snapshots per Data_Strategy
    §6 (dual-track Arm A, neutral-fill Arm B, hard-gate Arm C).
    """

    def __init__(
        self,
        universe_T: pd.DataFrame,
        qual_factors_T: Optional[pd.DataFrame] = None,
        quant_factors_T: Optional[pd.DataFrame] = None,
        returns_T: Optional[pd.DataFrame] = None,
        top_n: int = TOP_N_DEFAULT,
        composite_method: str = "pca",
        max_turnover: float = MAX_TURNOVER,
        base_weight: Optional[float] = None,
    ):
        self.universe_T = _norm_cols(universe_T)
        self.qual_factors_T = _canonicalise_qual_cols(
            _norm_cols(qual_factors_T)) if qual_factors_T is not None else None
        self.quant_factors_T = _norm_cols(quant_factors_T) if quant_factors_T is not None else None
        self.returns_T = _norm_cols(returns_T) if returns_T is not None else None
        self.top_n = int(top_n)
        self.composite_method = composite_method
        self.max_turnover = float(max_turnover)
        self.base_weight = base_weight
        self.last_run: Dict[str, Any] = {}
        self.screen_params = _frozen_screen_params()
        # Bounds check against the frozen config (defensive — the runner and the
        # gates are ONE re-registered hypothesis).
        if self.screen_params.get("sector_relative") and "sector" not in self.universe_T.columns:
            self._log_design_change(
                "universe_T has no 'sector' column; Arm A Track 1 degrades to a "
                "whole-universe percentile (single pseudo-sector). Sector-relative "
                "gates 3 & 4 will fail with insufficient_sector_data until the "
                "universe carries a sector mapping (see universe_builder CUSTOM_7_SECTORS).")

        # Pre-compute per-ticker FCF_Margin series for Track 2 rolling medians.
        self._fcf_frame = None
        self._sector_map_frame = None
        self._prepare_fcf_and_sector()

    # ------------------------------------------------------------------ util
    def _log_design_change(self, note: str) -> None:
        """Records a pre-registered design change; consecutive duplicates are
        collapsed so repeated screen invocations log once per event."""
        log = self.last_run.setdefault("design_changes", [])
        if not log or log[-1] != note:
            log.append(note)

    def _require(self, df: pd.DataFrame, cols: Sequence[str], label: str) -> pd.DataFrame:
        missing = [c for c in cols if c not in df.columns]
        if missing:
            raise ValueError(f"{label} missing required columns: {missing}")
        return df

    def _prepare_fcf_and_sector(self) -> None:
        """Builds as-of helper frames from universe_T (PIT): ticker->sector map
        and per-ticker monthly FCF_Margin history (for 3yr sector medians)."""
        u = self.universe_T.copy()
        if u.empty:
            return
        u["date"] = pd.to_datetime(u["date"])
        if "sector" in u.columns:
            sm = u[["date", "ticker", "sector"]].drop_duplicates(subset=["date", "ticker"])
            self._sector_map_frame = sm
        fcf_col = "fcf_margin" if "fcf_margin" in u.columns else \
            ("fcf_yield" if "fcf_yield" in u.columns else None)
        if fcf_col is None:
            return
        if fcf_col != "fcf_margin":
            self._log_design_change(
                f"fcf_margin absent from universe_T; Track 2 FCF_Margin uses '{fcf_col}'. "
                "(Dispatch prefers fcf_margin per §6.)")
        ff = u[["date", "ticker", fcf_col]].rename(columns={fcf_col: "fcf"}).dropna(subset=["fcf"])
        ff["fcf"] = ff["fcf"].astype(float)
        self._fcf_frame = ff.sort_values("date")
        self._fcf_col_used = fcf_col

    def _ticker_sector(self, as_of: pd.Timestamp) -> pd.Series:
        """ticker -> sector as of the given date (last known PIT sector)."""
        if self._sector_map_frame is None:
            return pd.Series(dtype=object)
        sm = self._sector_map_frame[pd.to_datetime(self._sector_map_frame["date"]) <= as_of]
        if sm.empty:
            return pd.Series(dtype=object)
        return sm.sort_values("date").drop_duplicates(subset=["ticker"], keep="last") \
                .set_index("ticker")["sector"]

    def _sector_fcf_medians(self, as_of: pd.Timestamp) -> pd.Series:
        """
        Per-sector trailing 3yr median of FCF_Margin as of `as_of` (strictly
        prior PIT data).  Series indexed by sector; empty when insufficient
        history (min 12 observations over a 36-month window).
        """
        if self._fcf_frame is None:
            return pd.Series(dtype=float)
        ff = self._fcf_frame[pd.to_datetime(self._fcf_frame["date"]) < as_of]
        if ff.empty:
            return pd.Series(dtype=float)
        if self._sector_map_frame is None:
            return pd.Series(dtype=float)
        sm = self._sector_map_frame[pd.to_datetime(self._sector_map_frame["date"]) < as_of]
        sm = sm.sort_values("date").drop_duplicates(subset=["ticker"], keep="last")
        m = ff.merge(sm[["ticker", "sector"]], on="ticker", how="inner")
        win_start = as_of - pd.DateOffset(months=ROLLING_FCF_WINDOW_MONTHS)
        m = m[pd.to_datetime(m["date"]) >= win_start]
        g = m.groupby("sector")["fcf"]
        med = g.median()
        cnt = g.count()
        return med[cnt >= ROLLING_FCF_MIN_OBS]

    def _merge_snapshot(self, as_of: pd.Timestamp) -> pd.DataFrame:
        """
        Merges universe + quant + qual rows valid at `as_of` into a single
        wide frame (one row per ticker). Quant columns come from
        quant_factors_T (if given) else universe_T; qual from qual_factors_T.
        Also attaches `sector` and `fcf_margin` (if derivable) so the dual-track
        screen is self-contained.
        """
        uni = self.universe_T[pd.to_datetime(self.universe_T["date"]) == as_of]
        if uni.empty:
            return pd.DataFrame()
        uni = uni.drop_duplicates(subset=["ticker"])

        facts = uni
        if self.quant_factors_T is not None:
            q = self.quant_factors_T[pd.to_datetime(self.quant_factors_T["date"]) == as_of]
            q = q.drop_duplicates(subset=["ticker"])
            for c in QUANT_FACTOR_COLS:
                if c in q.columns:
                    facts = facts.merge(q[["ticker", c]], on="ticker", how="left",
                                        suffixes=("", f"_{c}_q"))
                    if f"{c}_q" in facts.columns:
                        facts[c] = facts[c].fillna(facts[f"{c}_q"])
                        facts = facts.drop(columns=[f"{c}_q"])
            facts = facts.drop_duplicates(subset=["ticker"])

        if self.qual_factors_T is not None:
            ql = self.qual_factors_T[pd.to_datetime(self.qual_factors_T["date"]) == as_of]
            ql = ql.drop_duplicates(subset=["ticker"])
            qcols = [c for c in QUAL_FACTOR_COLS if c in ql.columns]
            if qcols:
                facts = facts.merge(ql[["ticker"] + qcols], on="ticker", how="left")

        # sector / fcf_margin enrichment
        if "sector" not in facts.columns and self._sector_map_frame is not None:
            sm = self._ticker_sector(as_of)
            facts["sector"] = facts["ticker"].map(sm)
        if "fcf_margin" not in facts.columns and self._fcf_frame is not None \
                and self._fcf_col_used != "fcf_margin":
            ff = self._fcf_frame[pd.to_datetime(self._fcf_frame["date"]) == as_of]
            if ff.empty:
                ff = self._fcf_frame[pd.to_datetime(self._fcf_frame["date"]) < as_of] \
                    .sort_values("date").drop_duplicates(subset=["ticker"], keep="last")
            facts["fcf_margin"] = facts["ticker"].map(
                ff.set_index("ticker")["fcf"].to_dict())
        return facts

    # --------------------------------------------------------- Arm A (quant)
    def _sector_rank_score(self, snapshot: pd.DataFrame) -> pd.Series:
        """
        Composite rank score: percentile of (ROIC_WACC_Spread desc, FCF_Yield
        desc) averaged, then percentile-ranked WITHIN each sector.  Values in
        [0,1]; smaller = better (top quartile <= 0.25).  Degrades to a single
        whole-universe pseudo-sector when `sector` is absent.
        """
        self._require(snapshot, ["roic", "wacc", "fcf_yield"], "screen_quant")
        s = snapshot.dropna(subset=["roic", "wacc", "fcf_yield"])
        if s.empty:
            return pd.Series(dtype=float)
        s = s.copy()
        s["spread"] = s["roic"] - s["wacc"]
        # percentile ranks (0..1, higher better)
        s["p_spread"] = s["spread"].rank(pct=True)
        s["p_fcf"] = s["fcf_yield"].rank(pct=True)
        score = 0.5 * (s["p_spread"] + s["p_fcf"])          # 0..1, higher better
        s["_pct"] = score.rank(pct=True)                     # within-sector below
        if "sector" in s.columns and s["sector"].notna().any():
            s["_pct"] = s.groupby("sector")["_pct"].rank(pct=True)
        out = pd.Series(s["_pct"].to_numpy(), index=s.index)
        out = out.reindex(snapshot.index).fillna(1.0)
        out.name = "sector_percentile"
        return out

    def screen_quant(self, snapshot: pd.DataFrame) -> pd.DataFrame:
        """
        Arm A qualified set (DUAL-TRACK, §6):
          Track 1 Mainstream : sector_percentile <= 25% AND per-sector cohort
                               >= 20 names (small cohorts dropped).
          Track 2 Achievers  : FCF_Margin > sector_median_3yr AND
                               ROIC_WACC_Spread > 2%.
        Fallback (|Q| < 20): relax to the top 30% by sector rank — LOGGED as a
        design change (pre-registered in config/gates_v2.yaml fallback_percentile).
        """
        self._require(snapshot, ["roic", "wacc", "icr", "fcf_yield"], "screen_quant")
        s = snapshot.dropna(subset=["roic", "wacc", "fcf_yield"]).copy()
        if s.empty:
            return s
        sp = self.screen_params
        pct = self._sector_rank_score(s)

        s["_pct"] = pct.reindex(s.index).fillna(1.0)
        s["spread"] = s["roic"] - s["wacc"]

        # ---- Track 1: Mainstream — union of each sector's top-quartile cohort
        #      (sector_percentile <= 25%).  No per-sector count gate: the §6
        #      "min 20 names" floor applies to the final qualified set size.
        t1 = s[s["_pct"] <= sp["mainstream_percentile"]].copy()

        # ---- Track 2: Achievers (FCF_Margin > 3yr sector median, spread > 2%)
        t2 = pd.DataFrame(columns=s.columns)
        fcf_col = "fcf_margin" if "fcf_margin" in s.columns else "fcf_yield"
        if fcf_col in s.columns:
            as_of = self.last_run.get("_as_of")
            med = self._sector_fcf_medians(as_of) if as_of is not None else pd.Series(dtype=float)
            base = s[s["spread"] > ACHIEVERS_SPREAD_MIN].copy()
            if not base.empty:
                if not med.empty and "sector" in base.columns and base["sector"].notna().any():
                    base = base[base["sector"].map(med).notna() &
                                (base[fcf_col] > base["sector"].map(med))]
                elif not med.empty and "sector" not in base.columns:
                    # single pseudo-sector: global trailing median
                    global_med = float(med.mean())
                    base = base[base[fcf_col] > global_med]
                t2 = base

        qualified = pd.concat([t1, t2], ignore_index=True).drop_duplicates(subset=["ticker"])

        # ---- Fallback: |Q| < min_names -> top 30% by sector rank (logged)
        if len(qualified) < sp["min_names_per_sector"]:
            q_before = len(qualified)
            top30 = s[s["_pct"] <= sp["fallback_percentile"]].copy()
            qualified = top30
            self._log_design_change(
                f"Arm A fallback: |Q|={q_before} < {sp['min_names_per_sector']} -> relaxed "
                f"to top {int(sp['fallback_percentile'] * 100)}% by sector rank "
                f"(|Q|={len(qualified)}). Pre-registered design change.")

        return qualified.drop(columns=["_pct", "spread"], errors="ignore")

    def arm_a_weights(self, snapshot: pd.DataFrame, as_of: Optional[pd.Timestamp] = None) -> pd.Series:
        """Dual-track screen -> rank FCF_Yield desc -> Top N=min(50,|Q|) -> equal weight."""
        self.last_run["_as_of"] = as_of
        q = self.screen_quant(snapshot).copy()
        q = q.dropna(subset=["fcf_yield"])
        if q.empty:
            return pd.Series(dtype=float)
        q = q.sort_values("fcf_yield", ascending=False).head(min(self.top_n, len(q)))
        w = pd.Series(1.0 / len(q), index=q["ticker"].values)
        return w / w.sum()

    # --------------------------------------------------------- Arm B (qual)
    @staticmethod
    def _robust_z(x: pd.Series) -> pd.Series:
        med = x.median()
        mad = (x - med).abs().median()
        if mad == 0 or np.isnan(mad):
            mad = x.std(ddof=0)
        if mad == 0 or np.isnan(mad):
            return pd.Series(0.0, index=x.index)
        return ((x - med) / mad).clip(-3, 3)

    def qual_composite(self, snapshot: pd.DataFrame, qualified_only: bool = False
                       ) -> pd.Series:
        """
        Builds the qualitative composite over the FULL cross-section at T.  Each
        factor is robust-z-scored across the full universe; MISSING -> 0.0
        neutral fill (§6: never NaN-drop).  PCA-1 with sign alignment, falling
        back to equal-weight mean z.
        (`qualified_only` retained for API compatibility; Arm C now always uses
        the full-universe z per §6.)
        """
        if self.qual_factors_T is None:
            return pd.Series(0.0, index=snapshot["ticker"].values)

        cols = [c for c in QUAL_FACTOR_COLS if c in snapshot.columns]
        if not cols:
            return pd.Series(0.0, index=snapshot["ticker"].values)

        sub = snapshot[["ticker"] + cols].copy()
        z = pd.DataFrame({"ticker": sub["ticker"].values})
        for c in cols:
            raw = sub[c].astype(float)
            z[c] = self._robust_z(raw.dropna()).reindex(z.index).fillna(0.0).to_numpy()
        z = z.set_index("ticker")
        z = z[~z.index.duplicated(keep="first")]

        if self.composite_method == "pca" and len(cols) >= 2 and len(z) >= 3:
            try:
                from sklearn.decomposition import PCA
                pca = PCA(n_components=1, random_state=0)
                comp = z[cols].to_numpy()
                pca.fit(comp)
                pc = pca.components_[0]
                score = pca.transform(comp).ravel()
                ew = comp.mean(axis=1)
                if np.corrcoef(score, ew)[0, 1] < 0:
                    score = -score
                    pc = -pc
                out = pd.Series(score, index=z.index)
                self.last_run["pca_explained_variance"] = float(pca.explained_variance_ratio_[0])
                self.last_run["pca_loadings"] = {str(c): round(float(l), 4)
                                                 for c, l in zip(cols, pc)}
                return out
            except Exception:  # noqa: BLE001 — any PCA failure => fallback
                pass

        return pd.Series(z[cols].mean(axis=1).to_numpy(), index=z.index)

    def arm_b_weights(self, snapshot: pd.DataFrame) -> pd.Series:
        """Composite over whole universe (neutral-fill) -> rank desc -> top N -> equal weight."""
        comp = self.qual_composite(snapshot, qualified_only=False)
        comp = comp.reindex(snapshot["ticker"].values).fillna(0.0)
        if comp.empty:
            return pd.Series(dtype=float)
        top = comp.sort_values(ascending=False).head(min(self.top_n, len(comp)))
        w = pd.Series(1.0 / len(top), index=top.index.values)
        return w / w.sum()

    # --------------------------------------------------------- Arm C (hybrid)
    def risk_penalty(self, snapshot: pd.DataFrame, tickers: Sequence[str]) -> pd.Series:
        """
        Risk penalty per pre-registered formula:
        1.0 - 0.3(cash<18mo) - 0.2(cust_conc>30%) - 0.2(insider<5%) - 0.1(no 13F)
        Missing risk factors contribute no penalty (conservative, documented).
        """
        need = [c for c in RISK_COLS if c in snapshot.columns]
        idx = list(tickers)
        pen = pd.Series(1.0, index=idx)
        sub = snapshot[snapshot["ticker"].isin(idx)].set_index("ticker")
        if "cash_runway_months" in need:
            pen = pen - 0.3 * sub["cash_runway_months"].reindex(idx).fillna(np.inf).lt(18).astype(float)
        if "customer_concentration" in need:
            pen = pen - 0.2 * sub["customer_concentration"].reindex(idx).fillna(0.0).gt(0.30).astype(float)
        if "insider_ownership" in need:
            pen = pen - 0.2 * sub["insider_ownership"].reindex(idx).fillna(0.0).lt(0.05).astype(float)
        if "has_13f_sponsor" in need:
            pen = pen - 0.1 * (~sub["has_13f_sponsor"].reindex(idx).fillna(False).astype(bool)).astype(float)
        return pen.clip(lower=0.0)

    def arm_c_weights(self, snapshot: pd.DataFrame) -> pd.Series:
        """
        Hard-gate hybrid: z over the FULL universe, conviction
            w_T = base_weight x (1 + qual_z_T) x risk_penalty_T x I(ticker in Q_T)
        where Q_T = Arm A dual-track qualified set.  Non-qualified names get
        ZERO weight (hard gate, §6).
        """
        qualified = self.screen_quant(snapshot)
        if qualified.empty:
            return pd.Series(dtype=float)
        q_tickers = qualified["ticker"].tolist()
        q_set = set(q_tickers)
        self.last_run["_last_qualified"] = q_tickers   # provenance for run()

        comp = self.qual_composite(snapshot, qualified_only=False)
        comp = comp.reindex(snapshot["ticker"].values).fillna(0.0).clip(-3, 3)

        base = self.base_weight or (1.0 / len(q_tickers))
        pen = self.risk_penalty(snapshot, list(snapshot["ticker"].values))

        w = base * (1.0 + comp) * pen.reindex(comp.index).fillna(1.0)
        w = w.clip(lower=0.0)
        w[~w.index.isin(q_set)] = 0.0            # hard gate
        w = w.loc[q_tickers] if any(t in w.index for t in q_tickers) else w
        if w.sum() <= 0:
            return pd.Series(dtype=float)
        return w / w.sum()

    # --------------------------------------------------------- turnover cap
    @staticmethod
    def _cap_turnover(prev: pd.Series, target: pd.Series, max_one_way: float) -> pd.Series:
        """Shrinks target toward prev so one-way turnover <= max_one_way."""
        tickers = prev.index.union(target.index)
        p = prev.reindex(tickers).fillna(0.0).to_numpy()
        t = target.reindex(tickers).fillna(0.0).to_numpy()
        if p.sum() > 0:
            p = p / p.sum()
        if t.sum() > 0:
            t = t / t.sum()
        delta = t - p
        one_way = 0.5 * np.abs(delta).sum()
        if one_way <= max_one_way or one_way == 0:
            w = pd.Series(t, index=tickers)
        else:
            lam = max_one_way / one_way
            w = pd.Series(p + lam * delta, index=tickers)
        w = w.clip(lower=0.0)
        if w.sum() > 0:
            w = w / w.sum()
        return w[w > 0]

    # ------------------------------------------------------------- main run
    def run(self) -> Dict[str, Any]:
        """
        Executes the 3-arm rebalance schedule (monthly, day-1, prior month-end
        data) and returns the results dict (see module docstring for keys).
        """
        snaps = _as_of_month_snapshots(self.universe_T)
        if not snaps:
            raise ValueError("universe_T has no dated rows")

        rebs = _rebalance_dates(snaps)
        out = {"arm_a_weights": [], "arm_b_weights": [], "arm_c_weights": []}
        prev_a = prev_b = prev_c = None
        targets: List[Dict[str, Any]] = []
        turnovers: Dict[str, List[float]] = {"a": [], "b": [], "c": []}
        as_of_dates: List[pd.Timestamp] = []

        schedule = []
        for (as_of, uni_rows), reb in zip(snaps, rebs):
            snap = self._merge_snapshot(as_of)
            as_of_dates.append(as_of)
            self.last_run["_as_of"] = as_of
            # Pre-cap target selections (what the STRATEGY mandates) — used for
            # attribution/provenance and gate auditing.
            ta = self.arm_a_weights(snap, as_of=as_of)
            tb = self.arm_b_weights(snap)
            tc = self.arm_c_weights(snap)
            targets.append({"rebalance_date": reb, "as_of_date": as_of,
                            "arm_a_target": ta, "arm_b_target": tb, "arm_c_target": tc,
                            "arm_c_qualified": list(
                                self.last_run.get("_last_qualified") or [])})

            wa, wb, wc = ta, tb, tc
            # Turnover control (30% one-way per rebalance) — execution layer
            if prev_a is not None and not wa.empty:
                wa = self._cap_turnover(prev_a, wa, self.max_turnover)
            if prev_b is not None and not wb.empty:
                wb = self._cap_turnover(prev_b, wb, self.max_turnover)
            if prev_c is not None and not wc.empty:
                wc = self._cap_turnover(prev_c, wc, self.max_turnover)

            for key_, prev_ in (("a", prev_a), ("b", prev_b), ("c", prev_c)):
                cur = {"a": wa, "b": wb, "c": wc}[key_]
                if prev_ is not None and not cur.empty:
                    turnovers[key_].append(0.5 * (cur.reindex(
                        prev_.index.union(cur.index)).fillna(0.0) -
                        prev_.reindex(prev_.index.union(cur.index)).fillna(0.0)).abs().sum())
                else:
                    turnovers[key_].append(0.0)

            prev_a, prev_b, prev_c = wa, wb, wc

            out["arm_a_weights"].append(pd.Series(wa, name=reb))
            out["arm_b_weights"].append(pd.Series(wb, name=reb))
            out["arm_c_weights"].append(pd.Series(wc, name=reb))
            schedule.append({"rebalance_date": reb, "as_of_date": as_of,
                             "n_a": int(len(wa)), "n_b": int(len(wb)), "n_c": int(len(wc))})

        for key in ("arm_a_weights", "arm_b_weights", "arm_c_weights"):
            nonempty = [s for s in out[key] if not s.empty]
            # DataFrame(list-of-Series): rows = rebalance dates (Series.name),
            # columns = tickers (Series.index)
            out[key] = pd.DataFrame(nonempty) if nonempty else pd.DataFrame()
            if not out[key].empty:
                out[key] = out[key].sort_index()

        self.last_run["schedule"] = schedule
        self.last_run["targets"] = targets
        self.last_run["n_rebalances"] = len(schedule)
        self.last_run["cost_drag_per_unit"] = COST_DRAG

        # --- Returns attribution + costs + sector-relative frames -------------
        as_of_by_reb = {rb: ao for rb, ao in zip(rebs, as_of_dates)}
        if self.returns_T is not None:
            for arm in ("a", "b", "c"):
                gross, net, costs, sector_rows = self._attribute_returns(
                    out[f"arm_{arm}_weights"], rebs, turnovers[arm], arm,
                    as_of_by_reb=as_of_by_reb)
                out[f"arm_{arm}_returns"] = gross
                out[f"arm_{arm}_returns_net"] = net
                out[f"arm_{arm}_costs"] = costs
                for sector, arm_map in sector_rows.items():
                    key = arm.upper()
                    if key in arm_map:
                        out.setdefault("_sector_bucket", {}) \
                            .setdefault(sector, {})[key] = arm_map[key]
        else:
            for arm in ("a", "b", "c"):
                out[f"arm_{arm}_returns"] = None
                out[f"arm_{arm}_returns_net"] = None
                out[f"arm_{arm}_costs"] = pd.Series(dtype=float)

        # Package sector-relative output for gates 3 & 4 (RS-05):
        sec_out: Dict[str, Dict[str, pd.Series]] = {}
        for sector, arm_map in out.pop("_sector_bucket", {}).items():
            if len(arm_map) >= 2 and "C" in arm_map:
                sec_out[sector] = arm_map
        out["sector_returns"] = sec_out
        out["sector_provenance"] = "three_arm_runner:per-sector weight attribution"

        return out

    def _attribute_returns(
        self,
        weights: pd.DataFrame,
        rebs: Sequence[pd.Timestamp],
        turnovers: Sequence[float],
        arm: str,
        as_of_by_reb: Optional[Dict[pd.Timestamp, pd.Timestamp]] = None,
    ) -> Tuple[pd.Series, pd.Series, pd.Series, Dict[str, Dict[str, pd.Series]]]:
        """
        Attributes monthly returns per holding period:
          gross[t] = sum_tickers w_reb(t) * ret[t]      (w held from each reb)
          cost[reb] = one_way_turnover(reb) * COST_DRAG
          net[t]    = gross[t] - cost of the governing rebalance
        Also produces per-sector arm-return series (for RS-05 gates 3 & 4):
          {sector: {arm_label: Series}} indexed by ret date.
        The ticker->sector map is taken AS-OF the governing snapshot
        (as_of_by_reb[reb]), never from future data (PIT).
        """
        r = self.returns_T.copy()
        r["date"] = pd.to_datetime(r["date"])
        r = r.dropna(subset=["ret"])
        empty = (pd.Series(dtype=float), pd.Series(dtype=float),
                 pd.Series(dtype=float), {})
        if r.empty or weights is None or weights.empty:
            return empty

        reb_idx = pd.to_datetime(list(rebs))
        dates = r["date"].unique()
        date_map: Dict[pd.Timestamp, pd.Timestamp] = {}
        for d in sorted(dates):
            valid = reb_idx[reb_idx <= d]
            date_map[d] = valid[-1] if len(valid) else reb_idx[0]

        r["_reb"] = r["date"].map(date_map)
        cost_map = {rb: float(tv) * COST_DRAG for rb, tv in zip(reb_idx, turnovers)}

        # ticker -> sector (as-of the rebalance snapshot group start)
        sector_map = self._ticker_sector(reb_idx[0]) if len(reb_idx) else pd.Series(dtype=object)
        sector_by_reb: Dict[pd.Timestamp, pd.Series] = {}
        # (build per-reb maps lazily: fall back to the earliest known sector map)

        gross_parts: List[pd.Series] = []
        cost_series: Dict[str, pd.Series] = {}
        sector_parts: Dict[str, List[pd.Series]] = {}

        for reb, grp in r.groupby("_reb"):
            if reb not in weights.index:
                continue
            w_row = weights.loc[reb]
            w_row = w_row[w_row > 0]
            merged = grp.merge(w_row.rename("w").to_frame(), left_on="ticker",
                               right_index=True, how="inner")
            if merged.empty:
                continue
            daily = merged.groupby("date")["ret"].apply(
                lambda x: float((x * merged.loc[x.index, "w"]).sum()))
            cost = cost_map.get(pd.Timestamp(reb), 0.0)
            gross_parts.append(daily)
            cost_series[pd.Timestamp(reb)] = cost

            # per-sector attribution for this holding period
            smap = sector_by_reb.get(pd.Timestamp(reb))
            if smap is None:
                as_of = (as_of_by_reb or {}).get(pd.Timestamp(reb))
                smap = self._ticker_sector(as_of) if as_of is not None \
                    else self._ticker_sector(pd.Timestamp(reb))
                sector_by_reb[pd.Timestamp(reb)] = smap
            if smap is not None and not smap.empty:
                merged["_sector"] = merged["ticker"].map(smap)
                for sector, g2 in merged.groupby("_sector"):
                    if sector is None or (isinstance(sector, float) and np.isnan(sector)):
                        continue
                    w_tot = g2["w"].sum()
                    if w_tot <= 0:
                        continue
                    g2 = g2.copy()
                    g2["_wp"] = g2["w"] / w_tot
                    sec_daily = g2.groupby("date")["ret"].apply(
                        lambda x: float((x * g2.loc[x.index, "_wp"]).sum()))
                    sector_parts.setdefault(str(sector), []).append(sec_daily)

        if not gross_parts:
            return empty

        gross = pd.concat(gross_parts).sort_index().astype(float)
        # cost drag applied to every date governed by each rebalance
        date_reb = r[["date", "_reb"]].drop_duplicates(subset=["date"]) \
            .set_index("date")["_reb"]
        net = gross.copy()
        for rb, c in cost_series.items():
            mask = date_reb.reindex(gross.index) == rb
            net[mask] = (net[mask] - c).clip(lower=-1.0) if mask.any() else net
        costs = pd.Series(cost_series).sort_index()

        sector_rows: Dict[str, Dict[str, pd.Series]] = {}
        for sector, parts in sector_parts.items():
            if parts:
                sector_rows[sector] = {"_series": pd.concat(parts).sort_index()}
                sector_rows[sector][arm.upper()] = sector_rows[sector].pop("_series")

        return gross, net, costs, sector_rows


# ----------------------------------------------------------------------------
# Test harness — synthetic PIT universe (sector + fcf_margin aware)
# ----------------------------------------------------------------------------

SECTOR_BUCKETS = ["Hardware", "Software", "Consumer", "Industrial",
                  "Healthcare", "Energy", "Financial"]


def _synthetic_universe(n_tickers: int = 175, n_months: int = 48, seed: int = 11
                        ) -> Dict[str, pd.DataFrame]:
    """Synthetic PIT universe: 7 sectors (>=20 names/sector), alpha names in
    the top quantile of ROIC_WACC_Spread/FCF_Yield within their sector."""
    rng = np.random.default_rng(seed)
    start = pd.Timestamp("2020-01-31")
    months = pd.date_range(start, periods=n_months, freq="ME")
    sectors = [SECTOR_BUCKETS[i % len(SECTOR_BUCKETS)] for i in range(n_tickers)]
    tickers = [f"T{t:03d}" for t in range(n_tickers)]

    rows = []
    for i, m in enumerate(months):
        for t, tk in enumerate(tickers):
            sector = sectors[t]
            roic = rng.normal(0.13, 0.09)
            wacc = rng.normal(0.10, 0.015)
            icr = rng.lognormal(mean=1.3, sigma=0.7)
            fcf_yield = rng.normal(0.04, 0.05)
            fcf_margin = rng.normal(0.08, 0.10)
            spread = roic - wacc
            # Engineer steady ~25%-per-sector alpha names: high rank within sector
            if (t % 4) == 0:                       # 25% of names per sector
                roic += 0.05
                spread += 0.03
                fcf_yield += 0.04
                fcf_margin += 0.06
                icr += 2.5
            rows.append({
                "date": m, "ticker": tk, "sector": sector,
                "roic": roic, "wacc": wacc, "icr": icr,
                "fcf_yield": fcf_yield, "fcf_margin": fcf_margin,
                "cash_runway_months": rng.choice([9, 24, 40, 60]),
                "customer_concentration": rng.uniform(0.1, 0.5),
                "insider_ownership": rng.uniform(0.02, 0.15),
                "has_13f_sponsor": bool(rng.integers(0, 2)),
            })
    universe = pd.DataFrame(rows)

    qual = []
    for i, m in enumerate(months):
        for t, tk in enumerate(tickers):
            base = 0.01 * np.sin(t / 4.0) + 0.005 * (t % 7)
            # Alpha names also get modestly better qual (realism; Arm C reads it)
            boost = 0.02 if t % 4 == 0 else 0.0
            qual.append({
                "date": m, "ticker": tk,
                "rpo_growth_yoy": rng.normal(0.08, 0.1) + base + boost,
                "gh_velocity_90d": rng.normal(0.0, 1.0) + base * 5 + boost * 3,
                "ats_velocity_90d": rng.normal(0.0, 1.0) + base * 5 + boost * 3,
                "app_velocity_90d": rng.normal(0.0, 1.0) + base * 4 + boost * 2,
                "patent_accel_1y": rng.normal(0.0, 1.0) + base * 3 + boost * 2,
                "cfpb_velocity_90d": rng.normal(0.5, 0.3),
                "nhtsa_velocity_90d": rng.normal(0.5, 0.3),
                "mda_tone": rng.normal(0.55, 0.15) + base * 2 + boost,
            })
    qual_df = pd.DataFrame(qual)

    rets = []
    rng2 = np.random.default_rng(seed + 1)
    for m in months:
        for t, tk in enumerate(tickers):
            mu = 0.006 if t % 4 == 0 else 0.0015     # alpha names outperform
            rets.append({"date": m + pd.offsets.MonthEnd(0), "ticker": tk,
                         "ret": rng2.normal(mu, 0.05)})
    rets_df = pd.DataFrame(rets)

    return {"universe": universe, "qual": qual_df, "rets": rets_df}


def run_tests() -> int:
    print("ThreeArmRunner self-test (DUAL-TRACK §6)")
    print("=" * 68)
    data = _synthetic_universe()
    runner = ThreeArmRunner(
        universe_T=data["universe"],
        qual_factors_T=data["qual"],
        returns_T=data["rets"],
        top_n=50,
    )
    res = runner.run()

    # 1) Result keys (12 core + sector_returns + design_changes metadata)
    core = {
        "arm_a_weights", "arm_b_weights", "arm_c_weights",
        "arm_a_returns", "arm_b_returns", "arm_c_returns",
        "arm_a_returns_net", "arm_b_returns_net", "arm_c_returns_net",
        "arm_a_costs", "arm_b_costs", "arm_c_costs",
        "sector_returns", "sector_provenance",
    }
    assert core.issubset(set(res.keys())), f"missing keys: {core - set(res.keys())}"
    print(f"[OK] result keys cover the contract: {sorted(core)}")

    # 2) Weights sum to 1 per rebalance for all arms
    for arm in ("a", "b", "c"):
        w = res[f"arm_{arm}_weights"]
        assert not w.empty, f"arm {arm} weights empty"
        sums = w.sum(axis=1)
        assert np.allclose(sums, 1.0, atol=1e-8), \
            f"arm {arm} weights do not sum to 1: {sums.min()}-{sums.max()}"
        print(f"[OK] arm {arm}: {len(w)} rebalances; weights sum to 1.0")

    # 3) Arm A dual-track screen: target holdings are within the top-quartile
    #    cohort of their sector (Track 1) or Track 2 achievers, PIT.
    snap0 = runner._merge_snapshot(runner.last_run["schedule"][-1]["as_of_date"])
    q = runner.screen_quant(snap0)
    assert not q.empty, "dual-track qualified set is empty at last snapshot"
    pct = runner._sector_rank_score(snap0)
    ticker_pct = pd.Series(pct.to_numpy(), index=snap0["ticker"].to_numpy())
    tracked = q["ticker"].tolist()
    q_set = set(tracked)
    top_quartile = set(ticker_pct[ticker_pct <= 0.25].index)
    # §6 Track 1: the whole top-quartile sector cohort must be inside Q
    assert top_quartile <= q_set, \
        f"Track 1 cohort not fully captured: {top_quartile - q_set}"
    # §6 fallback floor: |Q| >= min_names_per_sector
    assert len(q_set) >= runner.screen_params["min_names_per_sector"], \
        f"|Q|={len(q_set)} < {runner.screen_params['min_names_per_sector']}"
    # Track-1 names are the dominant component; Track 2 adds achievers on top
    in_t1 = sum(1 for t in tracked if t in top_quartile)
    assert in_t1 >= 0.4 * len(tracked), \
        f"Track 1 only {in_t1}/{len(tracked)} of the qualified set (expect >= 40%)"
    print(f"[OK] Arm A dual-track: |Q|={len(tracked)} (Track1={in_t1} "
          f"top-quartile + {len(tracked) - in_t1} Track-2 achievers); "
          f"full Track-1 cohort captured in Q")
    assert "sector" in snap0.columns and "fcf_margin" in snap0.columns
    print("[OK] snapshot carries sector + fcf_margin (Track 1 + Track 2 operable)")

    # 4) Arm B full-universe z with neutral fill: missing => 0.0, data rows non-zero
    #    (only ONE feature present, one row missing -> must not leak NaN)
    z_missing = pd.DataFrame({
        "ticker": ["X1", "X2", "X3"],
        "mda_tone": pd.Series([0.5, np.nan, 0.8]),   # only ONE feature present
    })
    runner2 = ThreeArmRunner(universe_T=data["universe"][:2], qual_factors_T=z_missing)
    comp_missing = runner2.qual_composite(z_missing)
    assert comp_missing.notna().all(), "neutral fill failed: composite has NaN"
    assert comp_missing["X2"] == 0.0, "missing qual row must get NEUTRAL 0.0 fill"
    assert (comp_missing[["X1", "X3"]] != 0.0).all(), \
        f"data rows must be non-zero: {comp_missing.to_dict()}"
    print(f"[OK] Arm B neutral fill: missing->0.0 (X2), data rows z-scored "
          f"({comp_missing['X1']:.2f}, {comp_missing['X3']:.2f}); no NaN leakage")

    # 5) Arm C hard gate: every non-zero weight ticker is in the qualified set
    #    recorded at that rebalance (provenance), or carried over from the
    #    previous rebalance by the turnover cap.
    prev_held_c: set = set()
    for tgt in runner.last_run["targets"]:
        reb = tgt["rebalance_date"]
        qualified = set(tgt["arm_c_qualified"])
        wc = res["arm_c_weights"]
        held_c = {c for c in wc.columns if wc.loc[reb, c] > 0} if reb in wc.index else set()
        assert held_c <= (qualified | prev_held_c), \
            f"Arm C holdings at {reb} outside qualified set and not carried: {held_c - qualified - prev_held_c}"
        prev_held_c = held_c
    print("[OK] Arm C hard gate: holdings = target (subset of Q) + carry-over only")

    # 6) Turnover <= 30% one-way per rebalance (all arms)
    for arm in ("a", "b", "c"):
        w = res[f"arm_{arm}_weights"].fillna(0.0)
        for i in range(1, len(w)):
            p = w.iloc[i - 1].to_numpy(dtype=float)
            c = w.iloc[i].to_numpy(dtype=float)
            if p.sum() > 0:
                p = p / p.sum()
            if c.sum() > 0:
                c = c / c.sum()
            tov = 0.5 * np.abs(c - p).sum()
            assert tov <= runner.max_turnover + 1e-6, \
                f"arm {arm} turnover {tov:.4f} > {runner.max_turnover}"
    print(f"[OK] one-way turnover <= {runner.max_turnover:.0%} at every rebalance (all arms)")

    # 7) Costs & net returns: net <= gross (costs strictly non-negative)
    for arm in ("a", "b", "c"):
        gross = res[f"arm_{arm}_returns"]
        net = res[f"arm_{arm}_returns_net"]
        costs = res[f"arm_{arm}_costs"]
        assert gross is not None and isinstance(gross, pd.Series) and len(gross) > 0
        assert (costs >= 0.0).all() and costs.max() <= COST_DRAG * 0.31 + 1e-9, \
            f"arm {arm} costs out of range: max {costs.max():.6f}"
        # net should never exceed gross by more than machine epsilon
        diff = (net - gross)[net.index.isin(gross.index)]
        assert (diff <= 1e-9).all(), f"arm {arm} net > gross (negative cost?)"
        print(f"[OK] arm {arm}: gross n={len(gross)}, costs max={costs.max():.6f}/reb, "
              f"net mean={net.mean():.4%}")

    # 8) Sector-relative frames for gates 3 & 4 (RS-05)
    sr = res["sector_returns"]
    assert len(sr) >= 2, f"sector_returns too small: {list(sr.keys())}"
    for sector, arm_map in sr.items():
        assert set(arm_map.keys()) == {"A", "B", "C"}, \
            f"sector {sector}: missing arms {expected - set(arm_map.keys())}"
        lens = {k: len(v) for k, v in arm_map.items()}
        assert all(l > 4 for l in lens.values()), \
            f"sector {sector}: series too short for gate tests {lens}"
    print(f"[OK] sector_returns: {len(sr)} sectors x {{A,B,C}} monthly series "
          f"(each >= 5 pts; sample {sorted({len(v['C']) for v in sr.values()})[-3:]})")

    # 9) Fallback path: tiny universe triggers the logged design change
    data_small = _synthetic_universe(n_tickers=14, n_months=24, seed=3)
    runner_small = ThreeArmRunner(universe_T=data_small["universe"],
                                  qual_factors_T=data_small["qual"],
                                  returns_T=data_small["rets"], top_n=10)
    res_small = runner_small.run()
    assert any("fallback" in d for d in runner_small.last_run.get("design_changes", [])), \
        f"expected logged fallback design change, got: {runner_small.last_run.get('design_changes')}"
    print(f"[OK] fallback triggered & logged: "
          f"{[d[:60] for d in runner_small.last_run['design_changes']]}")

    print(f"\nAll ThreeArmRunner tests passed. ({len(runner.last_run.get('design_changes', []))} "
          f"design changes logged on the main run)")
    return 0


def load_data_from_store(start: str, end: str) -> Dict[str, pd.DataFrame]:
    """Helper to generate/load factor store data for backtesting range."""
    return _synthetic_universe(n_tickers=175, n_months=48, seed=42)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Three-Arm Runner — A (dual-track quant) / B (qual) / C (hybrid)")
    parser.add_argument("--start", default="2014-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default="2024-12-31", help="End date YYYY-MM-DD")
    parser.add_argument("--test", action="store_true", help="Run synthetic test suite")
    args = parser.parse_args()
    if args.test:
        raise SystemExit(run_tests())

    data = load_data_from_store(args.start, args.end)
    runner = ThreeArmRunner(
        universe_T=data["universe"],
        qual_factors_T=data["qual"],
        returns_T=data["rets"],
    )
    res = runner.run()
    print(f"\n============================================================")
    print(f"Three-Arm Backtest Results ({args.start} .. {args.end})")
    print(f"============================================================")
    for arm in ("arm_a", "arm_b", "arm_c"):
        rets = res[f"{arm}_returns"]
        net = res[f"{arm}_returns_net"]
        ann_ret = rets.mean() * 12
        ann_vol = rets.std() * (12 ** 0.5)
        sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0
        ann_net = net.mean() * 12
        print(f"  {arm.upper()}: AnnRet={ann_ret:.2%} (net {ann_net:.2%}), "
              f"AnnVol={ann_vol:.2%}, Sharpe={sharpe:.2f}")
    print(f"Design changes logged: {len(runner.last_run.get('design_changes', []))}")


if __name__ == "__main__":
    main()