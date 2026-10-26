"""
wacc_engine_v2.py — Phase 8 §2.2 WACC Engine v2 (daily PIT panel, WS-5 Problem A-C re-spec)
Pre-Registration Config: config/pre_registration_phase8.yaml (frozen 2026-09-20, CEO Ruling B-20260920-001)

Rebuilds the high-frequency cost-of-capital layer per the bigpickle §5 re-spec and
§2.2 "wacc_panel_daily" table.  Formulas are bound to the frozen config + the
Plan §5 + phase8_bigpickle_analysis.md WS-5/WS-6 clauses:

  1. **Unlevered sector/industry beta (β_U)**: Damodaran unlevered-beta via GICS L2
     mapping (config/sector_beta.yaml v1 sidecar SHA-256, emitted by
     scripts/refresh_damodaran.py --emit-sector-beta). No lookahead: lookup uses
     the mapped Damodaran industry as-of T−1. Appendix B fallback chain →
     100% coverage of `beta_source` (drives the §2.1 coverage gate):
       L2 mapped     → β_U table value            [sector_bottom_up]
       L2 unmapped   → median of mapped sectors   [sector_median_fallback]
       empty         → universe median            [universe_median_fallback]
     Firm 60-mo regression is an explicit override (≥ 36 valid obs) →
     `firm_regression_60m` (beta used AS levered β, no re-levering).
  2. **Hamada net-debt re-levering with cash term** (§5.1, CEO Ruling WS-5/A):
     β_L = β_U × (1 + (1−τ)·D_net/E) − β_cash·(Cash/E)
     with D_net = max(Debt − Cash, 0)  (net debt, cash excluded), E = market cap,
     and D/E capped at the sector P95 (net-debt levering cap; §5.1; fallback
     D_E_NET_CAP_P95 = 2.5 documented as universe-P95 proxy).
  3. **Financials carve-out** (§5.2 / Appendix B): firms in Financials GICS L2 use
     the RAW sector/industry β (no Hamada levering; no debt-weight build-up).
  4. **Total beta** (§5.3, frozen total_beta block): raw = β_L/max(ρ, ρ_floor);
     Total_Beta_adj = 5.0 × sigmoid(raw / 3.0) = 5.0/(1+e^(−raw/3)) (Plan L239-240,
     CEO Ruling Defect 3: ceiling 5.0, transition 3.0).  total_beta is a STORED
     column consumed by the position-sizing/WS-6 layer — it is NOT the ke driver.
  5. **ke_v2 = R_f + β_L × ERP** (Plan §5.6 edge table "Debt = 0 → WACC = K_e =
     R_f + β_L·ERP").  ERP = 0.70×Damodaran + 0.30×ERP_option with
     ERP_option = (VIX/100)/√12 − R_f    <— VIX in points, /100 is REQUIRED
     (an 18-pt VIX ⇒ 5.2% monthly vol, NOT 520%).  Clamped to frozen erp_bounds
     [0.005, 0.08]; NO distress cheapening (the kd spread already reflects
     distress).  Missing VIX/R_f/ERP components → carry-forward last valid with
     staleness flag (Appendix B rule 3) — never NaN.
  6. **Synthetic credit rating + spread** (§5.3 Problem C): ICR → Damodaran
     synthetic rating via industry_beta.yaml default_ratings tiers; ICR missing →
     neutral AAA (cap ICR at 100 for lookup); distressed branch (EBIT≤0) → CCC
     base spread = frozen distressed_spread 0.0885 (CEO Ruling #11, table match).
     Ω_stress multiplies the base spread and the result is clamped to frozen
     spread_bounds [0.0063, 0.1900] (CEO Ruling #10: full 14-tier coverage).
  7. **Ω_stress sensitivity multiplier** (Plan L279-286, frozen omega_stress block):
     Ω = 1 + w_burn·burn_ratio·(w_icr·(1 − ICR_comp))·Sector_Stress_Factor
     burn_ratio = max(0, −OCF)/max(Rev, ε)      (cash burn vs revenue)
     ICR_comp   = clamp(ICR, 0.1, 100)          (1 − ICR_comp ≥ 0; missing ICR → 1.0 neutral)
     Sector_Stress_Factor ∈ {High 1.5, Medium 1.0, Low 0.5} (by β_U level)
     Ω ∈ [1.0, 3.0]; Adjusted_Spread = Base_Spread × Ω
     components + {omega_stress, components, adjusted_spread} exposed for the
     trade_audit log (Plan L286).
  8. **Tax rules** (§5.4): τ_eff ∈ [0, 0.35] else fallback 0.21; EBIT≤0 →
     (1−τ)=1.0 in WACC (tax_shield_if_ebit_negative: 1.0); Hamada re-levering
     uses the applied PIT τ (fallback 0.21) — documented ruling input.
  9. **WACC v2**: WACC_v2 = w_ke·ke + w_kd·kd_pre_tax·(1−τ_eff), with
     w_d = D/E_net/(1+D/E_net) (Financials → 0), WACC_v2 ∈ [0, 0.5]
     (validated by valuation_schema.validate_wacc_panel).
  10. **Analytical duration nowcast** (§2.2): IV_nowcast = IV_baseline ×
      (1 − D_WACC·ΔWACC) via Quantitative/iv_nowcast.analytical_duration_adjustment
      (frozen `iv_nowcast` field, ≥ 0.01 floor).
  11. **Daily PIT panel** → wacc_panel_daily partition (asof_date, ticker) PK,
      PIT anchors: filed_date_ref ≤ asof_date ≤ retrieval_ts; ke_v2/wacc_v2 ∈
      [0, 0.5]; erp_blend ∈ [0.005, 0.08]; beta_source non-null (coverage gate).
      Emitted to data/wacc/wacc_panel_daily/month=YYYY-MM/ (15 cols, parquet +
      provenance sidecar SHA-256; §2.2 persistence-granularity correction).
      Optional audit: pass a TradeAuditLedger to write_panel_partitioned → one
      chained block per panel row (stage_gate=1 VALUATION context).

PIT contract (mirrors valuation_schema.validate_wacc_panel §2.2):
  - PK = (asof_date, ticker); filed_date_ref <= asof_date; asof <= retrieval_ts date
  - ke_v2 / wacc_v2 ∈ [0, 0.5]; iv_nowcast >= 0.01 floor; erp_blend ∈ [0.005, 0.08]
  - beta_source NOT NULL; beta_source enum coverage drives the wacc panel gate.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd
import yaml

from Quantitative.iv_nowcast import analytical_duration_adjustment

try:
    from ingestion.valuation_schema import (
        load_pre_registration_config,
        WACC_PANEL_COLUMNS,
        validate_wacc_panel,
    )
    _HAS_SCHEMA = True
except ImportError:  # pragma: no cover
    WACC_PANEL_COLUMNS = None
    validate_wacc_panel = None
    _HAS_SCHEMA = False

logger = logging.getLogger("wacc_engine_v2")

FROZEN_CFG = "config/pre_registration_phase8.yaml"
SECTOR_BETA_CFG = "config/sector_beta.yaml"
INDUSTRY_BETA_CFG = "config/industry_beta.yaml"
DEFAULT_WACC_OUT_DIR = Path("data/wacc/wacc_panel_daily")  # month=YYYY-MM/ partitions
DEFAULT_PROV_DIR = Path("data/wacc/provenance")

# Frozen defaults (pre_registration_phase8.yaml · wacc_engine)
DEFAULT_ERP_DAMODARAN = 0.0450
DEFAULT_VIX_PROXY_WEIGHT = 0.30
DEFAULT_DAMODARAN_WEIGHT = 0.70
ERP_BOUNDS = (0.005, 0.08)
DISTRESSED_SPREAD = 0.0885
SPREAD_BOUNDS = (0.0063, 0.19)
RHO_FLOOR = 0.25
RHO_NEUTRAL_MISSING = 0.5          # Appendix B rule 4: ρ missing/<36 obs → neutral 0.5
TAX_FALLBACK = 0.21
TAX_BOUNDS = (0.0, 0.35)
SECTOR_STRESS_FACTORS = {"High": 1.5, "Medium": 1.0, "Low": 0.5}
OMEGA_MAX_MULTIPLIER = 3.0
ICR_COMP_CLAMP = (0.1, 100.0)      # Damodaran ICR lookup clamp (Interest=0 → 100)
MAX_TOTAL_BETA = 5.0
TOTAL_BETA_TRANSITION = 3.0
BETA_CASH_DEFAULT = 0.0            # Damodaran cash-holder unlevered beta ≈ 0
D_E_NET_CAP_P95 = 2.5              # net-debt/equity sector P95 cap fallback
FIRM_REGRESSION_MIN_OBS = 36       # §5.1: firm 60-mo regression needs ≥ 36 valid obs
BURN_REVENUE_EPS = 1e-9
VIX_DEFAULT_POINTS = 20.0
RF_DEFAULT = 0.04
# staleness thresholds (calendar days) for carry-forward flags (Appendix B rule 3)
RF_STALE_DAYS = 5
VIX_STALE_DAYS = 5
ERP_DM_STALE_DAYS = 95

FINANCIALS_GICS_L2 = {            # Financials → raw sector beta (carve-out)
    "401010", "401020", "402010", "402020", "402030",
    "403010", "403020", "403030",
}

# Damodaran table rating strings → valuation_schema synthetic_rating enum:
# ["NONE","AAA","AA","A","BBB","BB","B","CCC","CC","C","D"]
RATING_ENUM_KEYS = ("NONE", "AAA", "AA", "A", "BBB", "BB", "B", "CCC", "CC", "C", "D")


def _clamp(value: float, lo: float, hi: float) -> float:
    return float(max(lo, min(hi, value)))


def _map_rating_to_schema(rating: Optional[str]) -> str:
    """Map a Damodaran table rating ('Aaa/AAA', 'Caa/CCC', ...) to the coarse
    valuation_schema synthetic_rating enum.  Unknown/None → 'NONE'."""
    if not rating:
        return "NONE"
    key = str(rating).split("/")[-1].strip().upper()
    for k in RATING_ENUM_KEYS:
        if key == k:
            return k
    # 'AA+' / 'B-' / 'CC' style: strip +/- then longest-prefix match
    stripped = key.rstrip("+-")
    if stripped == "":
        return "NONE"
    best = ""
    for k in ("AAA", "AA", "BBB", "BB", "CCC", "CC", "A", "B", "C", "D"):
        if stripped.startswith(k) and len(k) > len(best):
            best = k
    return best if best else "NONE"


def _sigmoid_ceiling_beta(total_beta_raw: float, ceiling: float = MAX_TOTAL_BETA,
                          transition: float = TOTAL_BETA_TRANSITION) -> float:
    """Total-Beta sigmoid ceiling (Plan L239-240, frozen total_beta block):
    Total_Beta_adj = ceiling × sigmoid(raw / transition) = ceiling / (1+e^(−raw/τ)).
    Hard clamp at `ceiling` (sigmoid → 1.0 as raw → ∞) and at 0 (never negative).
    """
    if not np.isfinite(total_beta_raw) or total_beta_raw <= 0.0:
        return 0.0
    x = float(total_beta_raw)
    out = float(ceiling) / (1.0 + np.exp(-(x / float(transition))))
    return float(min(max(out, 0.0), ceiling))


class DamodaranSectorBeta:
    """PIT sector/industry unlevered beta lookup with Appendix B fallback chain.

    Chain (Appendix B, pre-registered):
      1. GICS L2 → mapped industry        → (β_U, 'sector_bottom_up')
      2. else median over mapped sectors  → (median, 'sector_median_fallback')
      3. else universe median             → (fallback, 'universe_median_fallback')
    Every call returns a non-None β_U → 100% coverage of beta_source.
    """

    def __init__(
        self,
        sector_beta_path: str = SECTOR_BETA_CFG,
        industry_beta_path: str = INDUSTRY_BETA_CFG,
        fallback_beta: float = 0.743,
    ):
        self.fallback_beta = float(fallback_beta)
        self._sector: Dict[str, Dict[str, Any]] = {}
        self._load(sector_beta_path)
        self._industry: Dict[str, Dict[str, Any]] = {}
        self._load_industry(industry_beta_path)
        self._all_betas: List[float] = []

    def _load(self, path: str) -> None:
        p = Path(path)
        if not p.exists():
            logger.warning("sector_beta config %s missing; using fallback β_U", path)
            return
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        sb = doc.get("sector_beta", doc) if isinstance(doc, dict) else doc
        inds = sb.get("industries", {}) if isinstance(sb, dict) else {}
        for gics_l2, meta in inds.items():
            if isinstance(meta, dict):
                self._sector[str(gics_l2)] = {
                    "gics_l2_name": meta.get("gics_l2_name"),
                    "damodaran_industry": meta.get("damodaran_industry"),
                    "beta_unlevered": float(meta.get("beta_unlevered", self.fallback_beta)),
                }
        self._all_betas = [m["beta_unlevered"] for m in self._sector.values()] or [self.fallback_beta]

    def _load_industry(self, path: str) -> None:
        """Ratings tiers for synthetic credit (other fields retained for beta fallback)."""
        p = Path(path)
        if not p.exists():
            logger.warning("industry_beta config %s missing", path)
            return
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        self._industry = doc if isinstance(doc, dict) else {}

    def lookup_unlevered(self, gics_l2: Optional[str]) -> Tuple[float, str]:
        """β_U + source per Appendix B fallback chain. Never returns None."""
        if gics_l2 is not None and str(gics_l2) in self._sector:
            return float(self._sector[str(gics_l2)]["beta_unlevered"]), "sector_bottom_up"
        if self._all_betas:
            return float(np.median(self._all_betas)), "sector_median_fallback"
        return float(self.fallback_beta), "universe_median_fallback"

    def sector_median(self) -> float:
        return float(np.median(self._all_betas)) if self._all_betas else self.fallback_beta


class SyntheticRatingTable:
    """Damodaran ICR → synthetic credit rating tier lookup (§5.3 Problem C)."""

    def __init__(self, industry_beta_path: str = INDUSTRY_BETA_CFG):
        self._tiers: List[Dict[str, Any]] = []
        doc = {}
        p = Path(industry_beta_path)
        if p.exists():
            doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        dr = doc.get("default_ratings", {}) if isinstance(doc, dict) else {}
        self._tiers = [
            t for t in dr.get("tiers", [])
            if isinstance(t, dict) and "icr_threshold" in t and "spread" in t
        ]
        # sort descending by icr_threshold so worst-tier-first lookup returns first match
        self._tiers.sort(key=lambda t: float(t["icr_threshold"]), reverse=True)
        if not self._tiers:
            self._tiers = [{"icr_threshold": -1e9, "rating": "D2/D", "spread": DISTRESSED_SPREAD}]

    def lookup(self, icr: Optional[float]) -> Tuple[str, float]:
        """(raw rating, base spread) from interest coverage ratio.
        ICR missing → neutral AAA (analysis §5.2: cap ICR at 100 for lookup);
        ICR below worst tier → the worst tier itself (already D at −100k)."""
        if icr is None:
            return str(self._tiers[0]["rating"]), float(self._tiers[0]["spread"])
        icr_val = float(_clamp(float(icr), *ICR_COMP_CLAMP))
        for t in self._tiers:
            if icr_val >= float(t["icr_threshold"]):
                return str(t["rating"]), float(t["spread"])
        return str(self._tiers[-1]["rating"]), float(self._tiers[-1]["spread"])


class WACCEngineV2:
    """Phase 8 §2.2 daily WACC engine v2 (Hamada net-debt, β_L-driven ke,
    total-beta sigmoid ceiling, ERP blend, synthetic credit, Ω_stress, tax
    rules, Appendix B fallback, analytical duration nowcast)."""

    def __init__(self, config: Optional[Union[str, Dict[str, Any]]] = None):
        cfg = self._load_cfg(config)

        w = ((cfg.get("phase8") or {}).get("wacc_engine", {})
             if cfg else {}) or (cfg.get("wacc_engine", {}) if cfg else {})

        self.erp_damodaran = float(w.get("erp_damodaran", DEFAULT_ERP_DAMODARAN))
        self.vix_proxy_weight = float(w.get("vix_proxy_weight", DEFAULT_VIX_PROXY_WEIGHT))
        self.damodaran_weight = float(w.get("damodaran_weight", DEFAULT_DAMODARAN_WEIGHT))
        self.erp_bounds = tuple(float(x) for x in w.get("erp_bounds", ERP_BOUNDS))
        self.distressed_spread = float(w.get("distressed_spread", DISTRESSED_SPREAD))
        self.spread_bounds = tuple(float(x) for x in w.get("spread_bounds", SPREAD_BOUNDS))
        self.rho_floor = float(w.get("rho_floor", RHO_FLOOR))
        self.tax_fallback = float(w.get("tax_fallback", TAX_FALLBACK))
        self.tax_shield_if_ebit_negative = float(
            w.get("tax_shield_if_ebit_negative", 1.0))
        # omega stress block (frozen)
        om = w.get("omega_stress", {}) or {}
        self.omega_enabled = bool(om.get("enabled", True))
        self.omega_max_multiplier = float(om.get("max_multiplier", OMEGA_MAX_MULTIPLIER))
        self.omega_cash_burn_weight = float(om.get("cash_burn_weight", 1.0))
        self.omega_icr_weight = float(om.get("icr_weight", 1.0))
        self.omega_stress_factors = dict(om.get("sector_stress_factors", SECTOR_STRESS_FACTORS))
        # total_beta block (frozen)
        tb = (cfg.get("phase8", {}).get("total_beta", {}) if cfg and "phase8" in cfg
              else w.get("total_beta", {})) or {}
        self.max_total_beta = float(tb.get("max_total_beta", MAX_TOTAL_BETA))
        self.sigmoid_transition_point = float(tb.get("sigmoid_transition_point",
                                                     TOTAL_BETA_TRANSITION))

        # data layers
        self.sector_beta = DamodaranSectorBeta()
        self.ratings = SyntheticRatingTable()

        # daily input series (PIT market data, as-of T−1)
        self.rf_series: Dict[pd.Timestamp, float] = {}        # 10Y Treasury (fraction)
        self.vix_series: Dict[pd.Timestamp, float] = {}       # VIX, POINTS (18.0 = 18 pts)
        self.erp_damodaran_series: Dict[pd.Timestamp, float] = {}
        self.beta_60m: Dict[Tuple[str, pd.Timestamp], float] = {}   # firm regression β
        self.rho_60m: Dict[Tuple[str, pd.Timestamp], float] = {}    # firm regression ρ
        self.reg_beta_n: Dict[Tuple[str, pd.Timestamp], int] = {}   # regression obs count

        # per-ticker PIT fundamentals (from filing rows, anchor = filed_date)
        self.cap_struct: Dict[Tuple[str, pd.Timestamp], float] = {}   # D/E net
        self.cash_ratio: Dict[Tuple[str, pd.Timestamp], float] = {}   # Cash/E
        self.icr: Dict[Tuple[str, pd.Timestamp], float] = {}          # ICR
        self.ocf: Dict[Tuple[str, pd.Timestamp], float] = {}          # operating cash flow
        self.revenue: Dict[Tuple[str, pd.Timestamp], float] = {}      # revenue
        self.tax_rate: Dict[Tuple[str, pd.Timestamp], float] = {}     # effective τ
        self.is_financial: Dict[Tuple[str, pd.Timestamp], bool] = {}  # Financials carve-out
        self.iv_baseline: Dict[Tuple[str, pd.Timestamp], float] = {}  # IV from last filing panel
        self.d_wacc_baseline: Dict[Tuple[str, pd.Timestamp], float] = {}
        self.wacc_estimate_baseline: Dict[Tuple[str, pd.Timestamp], float] = {}
        self.filed_date_ref: Dict[Tuple[str, pd.Timestamp], pd.Timestamp] = {}

        self._last_erp_blend_log: float = 0.05

    @staticmethod
    def _load_cfg(config: Optional[Union[str, Dict[str, Any]]]) -> Dict[str, Any]:
        if config is None:
            cfg = load_pre_registration_config() if _HAS_SCHEMA else {}
        elif isinstance(config, dict):
            cfg = config
        else:
            cfg = load_pre_registration_config(config) if _HAS_SCHEMA else {}
        return cfg or {}

    # ------------------------------------------------------------------
    # Input loading (PIT)
    # ------------------------------------------------------------------
    def load_rf(self, series: Optional[Union[Dict, pd.Series, pd.DataFrame]] = None) -> Dict[pd.Timestamp, float]:
        if series is not None:
            _populate_series(self.rf_series, series, scale=100.0 if not _is_frac(series) else 1.0)
        return self.rf_series

    def load_vix(self, series: Optional[Union[Dict, pd.Series, pd.DataFrame]] = None) -> Dict[pd.Timestamp, float]:
        if series is not None:
            # VIX stored in POINTS: fraction input (0.20) is scaled ×100 → 20.0
            _populate_series(self.vix_series, series, scale=100.0 if _is_frac(series) else 1.0)
        return self.vix_series

    def load_erp(self, series: Optional[Union[Dict, pd.Series, pd.DataFrame]] = None) -> Dict[pd.Timestamp, float]:
        if series is not None:
            _populate_series(self.erp_damodaran_series, series,
                             scale=100.0 if not _is_frac(series) else 1.0)
        return self.erp_damodaran_series

    def load_beta_60m(self, beta_map: Optional[Dict[Tuple[str, Any], float]] = None,
                      n_obs: Optional[Dict[Tuple[str, Any], int]] = None,
                      rho_map: Optional[Dict[Tuple[str, Any], float]] = None) -> None:
        """Firm 60-month regression data (override path, ≥36 obs gate).
        n_obs keyed the same way as beta_map; rho_map optional (ρ_60m)."""
        if beta_map:
            for (t, d), v in beta_map.items():
                self.beta_60m[(t, pd.Timestamp(d))] = float(v)
        if n_obs:
            for (t, d), v in n_obs.items():
                self.reg_beta_n[(t, pd.Timestamp(d))] = int(v)
        if rho_map:
            for (t, d), v in rho_map.items():
                self.rho_60m[(t, pd.Timestamp(d))] = float(v)

    def load_rho_60m(self, rho_map: Optional[Dict[Tuple[str, Any], float]] = None) -> Dict[Tuple[str, pd.Timestamp], float]:
        if rho_map:
            for (t, d), v in rho_map.items():
                self.rho_60m[(t, pd.Timestamp(d))] = float(v)
        return self.rho_60m

    # ------------------------------------------------------------------
    # PIT fundamentals
    # ------------------------------------------------------------------
    def update_on_filing(
        self,
        ticker: str,
        filed_date: Union[str, pd.Timestamp],
        row: Dict[str, Any],
    ) -> None:
        """Register one PIT filing row for a ticker (anchor = filed_date)."""
        fd = pd.Timestamp(filed_date)
        self.cap_struct[(ticker, fd)] = float(row.get("d_e_market_net", 0.0))
        self.cash_ratio[(ticker, fd)] = float(row.get("cash_ratio", row.get("cash_over_e", 0.0)))
        self.icr[(ticker, fd)] = float(row.get("icr_v2", row.get("interest_coverage", 0.0)))
        self.ocf[(ticker, fd)] = float(row.get("operating_cash_flow", 0.0))
        self.revenue[(ticker, fd)] = float(row.get("revenue", 0.0))
        self.tax_rate[(ticker, fd)] = float(row.get("tax_rate_applied", self.tax_fallback))
        self.is_financial[(ticker, fd)] = bool(row.get("is_financial_kd", False))
        self.iv_baseline[(ticker, fd)] = float(row.get("iv_baseline", row.get("iv_per_share", 0.0)))
        self.d_wacc_baseline[(ticker, fd)] = float(row.get("duration_wacc", 0.0))
        self.wacc_estimate_baseline[(ticker, fd)] = float(
            row.get("wacc_estimate", row.get("wacc_v2", 0.0)))
        self.filed_date_ref[(ticker, fd)] = fd

    def _pit_lookup(self, table: Dict[Tuple[str, pd.Timestamp], Any], ticker: str, d: pd.Timestamp) -> Optional[Any]:
        """Most recent filing value with filed_date <= d (point-in-time)."""
        if not table:
            return None
        candidates = [v for (t, f), v in table.items() if t == ticker and f <= d]
        return candidates[-1] if candidates else None

    def _is_financial(self, ticker: str, d: pd.Timestamp, gics_l2: Optional[str]) -> bool:
        """Financials carve-out: GICS L2 membership OR explicit filing flag."""
        if self._gics_financials(gics_l2):
            return True
        flag = self._pit_lookup(self.is_financial, ticker, d)
        return bool(flag) if flag is not None else False

    # ------------------------------------------------------------------
    # Core math
    # ------------------------------------------------------------------
    def _unlevered_beta(self, gics_l2: Optional[str]) -> Tuple[float, str]:
        """Appendix B chain → (β_U, source); never a null beta."""
        return self.sector_beta.lookup_unlevered(gics_l2)

    def _firm_regression(self, ticker: str, d: pd.Timestamp) -> Optional[Tuple[float, int, Optional[float]]]:
        """Firm 60-mo regression override when available with ≥ 36 valid obs:
        (β_firm, n_obs, ρ_firm) — None if absent or below minimum."""
        beta = self._pit_lookup(self.beta_60m, ticker, d)
        if beta is None:
            return None
        n = self._pit_lookup(self.reg_beta_n, ticker, d)
        if n is None or int(n) < FIRM_REGRESSION_MIN_OBS:
            return None
        rho = self._pit_lookup(self.rho_60m, ticker, d)
        return float(beta), int(n), (float(rho) if rho is not None else None)

    def _omega_stress(self, icr: Optional[float], ocf: Optional[float],
                      revenue: Optional[float], gics_l2: Optional[str]) -> Tuple[float, Dict[str, Any]]:
        """Ω_stress = 1 + w_burn·burn_ratio·(w_icr·(1−ICR_comp))·Stress, Ω∈[1,3].
        (Plan L279-286 + frozen omega_stress block.)"""
        if not self.omega_enabled:
            return 1.0, {"omega_stress": 1.0, "enabled": False}
        burn = 0.0
        rev = float(revenue) if revenue is not None else 0.0
        if rev > BURN_REVENUE_EPS:
            ocf_v = float(ocf) if ocf is not None else 0.0
            burn = max(0.0, -ocf_v) / rev
        if icr is None:
            icr_comp = 1.0  # neutral — no penalty, no benefit
        else:
            icr_comp = float(_clamp(float(icr), *ICR_COMP_CLAMP))
        icr_term = max(0.0, 1.0 - icr_comp) * self.omega_icr_weight
        beta_prov, _ = self._unlevered_beta(gics_l2)
        stress_tier = "High" if beta_prov >= 1.2 else ("Low" if beta_prov <= 0.7 else "Medium")
        sf = float(self.omega_stress_factors.get(stress_tier, 1.0))
        omega = 1.0 + self.omega_cash_burn_weight * burn * icr_term * sf
        omega = _clamp(omega, 1.0, self.omega_max_multiplier)
        prov = {
            "omega_stress": omega,
            "burn_ratio": float(burn),
            "icr_comp": float(icr_comp),
            "icr_term": float(icr_term),
            "stress_tier": stress_tier,
            "stress_factor": sf,
            "cash_burn_weight": self.omega_cash_burn_weight,
            "icr_weight": self.omega_icr_weight,
            "enabled": True,
        }
        return omega, prov

    def _cost_of_debt_spread(self, ticker: str, d: pd.Timestamp,
                             gics_l2: Optional[str],
                             ebit: Optional[float], icr: Optional[float]) -> Tuple[float, str, bool, Dict[str, Any]]:
        """(spread_adj, rating_schema, distress_flag, prov) from ICR/EBIT.

        - distressed (EBIT≤0): base = frozen distressed_spread (Caa/CCC 8.85%,
          CEO Ruling #11) — then × Ω, clamped to spread_bounds.
        - else: table lookup by ICR (missing → neutral AAA 4bp base).
        - Ω_stress always multiplies the base; final clamp to frozen spread_bounds
          [0.0063, 0.1900] with bounds-flag when the clamp binds (Ruling #10)."""
        rating_raw: str
        base_spread: float
        distress = bool(ebit is not None and ebit <= 0.0)
        icr_val = icr
        if icr_val is None:
            icr_val = self._pit_lookup(self.icr, ticker, d)
        if distress:
            rating_raw, base_spread = "Caa/CCC", float(self.distressed_spread)
        else:
            rating_raw, base_spread = self.ratings.lookup(icr_val)

        ocf = self._pit_lookup(self.ocf, ticker, d)
        rev = self._pit_lookup(self.revenue, ticker, d)
        omega, om_prov = self._omega_stress(icr_val, ocf, rev, gics_l2)
        spread_adj = base_spread * omega
        bounds_flag = spread_adj > self.spread_bounds[1]
        spread_adj = _clamp(spread_adj, *self.spread_bounds)
        prov = {
            "rating_raw": rating_raw,
            "rating": _map_rating_to_schema(rating_raw),
            "base_spread": float(base_spread),
            "spread_adj": float(spread_adj),
            "distress": distress,
            "bounds_flag": bool(bounds_flag),
            "spread_bounds": list(self.spread_bounds),
            "omega": om_prov,
        }
        return float(spread_adj), prov["rating"], distress, prov

    def _effective_tax(self, ticker: str, d: pd.Timestamp, ebit: Optional[float]) -> float:
        """τ_eff for the WACC tax shield: EBIT≤0 → 0.0 ⇒ (1−τ)=1.0 (frozen
        tax_shield_if_ebit_negative: 1.0); else applied τ clamped [0,0.35],
        fallback 0.21 (τ_eff<0 / >0.35 → 0.21, per §5.4 + ruling note)."""
        if ebit is not None and ebit <= 0.0:
            return 0.0
        tau = self._pit_lookup(self.tax_rate, ticker, d)
        if tau is None:
            tau = self.tax_fallback
        tau = float(tau)
        if tau < TAX_BOUNDS[0] or tau > TAX_BOUNDS[1]:
            return float(self.tax_fallback)
        return float(_clamp(tau, *TAX_BOUNDS))

    def _hamada_tax(self, ticker: str, d: pd.Timestamp) -> float:
        """Re-levering τ: applied PIT tax rate if valid else 0.21 (Hamada uses
        marginal; documented ruling input, analysis §5.4)."""
        tau = self._pit_lookup(self.tax_rate, ticker, d)
        if tau is None:
            return float(self.tax_fallback)
        tau = float(tau)
        if tau < TAX_BOUNDS[0] or tau > TAX_BOUNDS[1]:
            return float(self.tax_fallback)
        return tau

    def _debt_weight(self, ticker: str, d: pd.Timestamp, gics_l2: Optional[str]) -> float:
        """w_d via net-debt/equity (Hamada). Financials → 0 (no levering)."""
        if self._is_financial(ticker, d, gics_l2):
            return 0.0
        d_e = self._pit_lookup(self.cap_struct, ticker, d)
        if d_e is None:
            d_e = 0.0
        d_e = float(min(max(0.0, d_e), D_E_NET_CAP_P95))
        return d_e / (1.0 + d_e)

    @staticmethod
    def _gics_financials(gics_l2: Optional[str]) -> bool:
        return gics_l2 is not None and str(gics_l2) in FINANCIALS_GICS_L2

    # ------------------------------------------------------------------
    # RHO resolution (Appendix B rule 4)
    # ------------------------------------------------------------------
    def _rho_used(self, ticker: str, d: pd.Timestamp,
                  rho_override: Optional[float] = None) -> Tuple[float, bool]:
        """ρ_used = max(ρ, ρ_floor) with ρ_floor 0.25 for valid regressions;
        missing / <36 obs → 0.5 neutral + rho_degraded flag (Appendix B rule 4)."""
        rho = rho_override if rho_override is not None else self._pit_lookup(self.rho_60m, ticker, d)
        if rho is None:
            return float(RHO_NEUTRAL_MISSING), True
        rho = float(rho)
        if not np.isfinite(rho):
            return float(RHO_NEUTRAL_MISSING), True
        return float(_clamp(rho, self.rho_floor, 1.0)), False

    def compute_total_beta(
        self,
        beta_or_unlevered: float,
        ticker: str,
        d: pd.Timestamp,
        gics_l2: Optional[str] = None,
        is_financial: Optional[bool] = None,
        source: str = "sector_bottom_up",
        firm_override: Optional[Tuple[float, int, Optional[float]]] = None,
    ) -> Tuple[float, float, float, Dict[str, Any]]:
        """(beta_levered_v2, rho_used, total_beta, prov).

        Hamada net-debt re-levering + cash term; financials carve-out → raw β;
        firm regression override (≥36 obs) → β used as-is.  Total beta stored
        (sigmoid ceiling 5.0 at transition 3.0) — ke uses β_L, not total_beta."""
        fin = self._is_financial(ticker, d, gics_l2) if is_financial is None else is_financial

        if firm_override is not None:
            beta_firm, n_obs, rho_f = firm_override
            beta_L = float(beta_firm)
            source = "firm_regression_60m"
            rho, rho_degraded = self._rho_used(ticker, d, rho_f)
            beta_degraded = False
            debt_missing = False
            d_e_used = float(self._pit_lookup(self.cap_struct, ticker, d) or 0.0)
        elif fin:
            beta_L = float(beta_or_unlevered)
            rho, rho_degraded = self._rho_used(ticker, d)
            beta_degraded = False
            debt_missing = False
            d_e_used = 0.0
        else:
            tau = self._hamada_tax(ticker, d)
            d_e = self._pit_lookup(self.cap_struct, ticker, d)
            debt_missing = d_e is None
            d_e = float(min(max(0.0, d_e if d_e is not None else 0.0), D_E_NET_CAP_P95))
            cash_e = self._pit_lookup(self.cash_ratio, ticker, d) or 0.0
            beta_L = beta_or_unlevered * (1.0 + (1.0 - tau) * d_e) - BETA_CASH_DEFAULT * cash_e
            rho, rho_degraded = self._rho_used(ticker, d)
            beta_degraded = source in ("sector_median_fallback", "universe_median_fallback")
            d_e_used = d_e

        raw = beta_L / max(rho, 1e-9)
        total_beta = _sigmoid_ceiling_beta(raw, ceiling=self.max_total_beta,
                                           transition=self.sigmoid_transition_point)
        prov = {
            "beta_source": source,
            "debt_equity_net": float(d_e_used),
            "debt_missing": bool(debt_missing),
            "rho_used": rho,
            "rho_degraded": bool(rho_degraded),
            "beta_degraded": bool(beta_degraded),
            "is_financial": fin,
            "firm_regression_n": firm_override[1] if firm_override else None,
            "ceiling": self.max_total_beta,
            "transition": self.sigmoid_transition_point,
            "total_beta_raw": float(raw),
        }
        return float(beta_L), rho, float(total_beta), prov

    def _carry_forward(self, table: Dict[pd.Timestamp, float], d: pd.Timestamp,
                       default: float, stale_days: int) -> Tuple[float, bool]:
        """Last valid value with key ≤ d (carry-forward, Appendix B rule 3);
        staleness flag when the carry is older than `stale_days`."""
        keys = [k for k in table if k <= d]
        if not keys:
            return float(default), True
        last_k = max(keys)
        value = float(table[last_k])
        stale = (d - last_k).days > stale_days
        return value, stale

    def compute_erp_blend(self, d: pd.Timestamp, vix: Optional[float] = None) -> Tuple[float, Dict[str, Any]]:
        """ERP = w_dm·ERP_Damodaran + w_vix·ERP_option; ERP_option = (VIX/100)/√12 − R_f.
        No distress cheapening — the kd spread already prices distress.  Missing
        components carry forward with staleness flags; blend clamped to frozen
        erp_bounds [0.005, 0.08].  Never NaN (Appendix B rule 3)."""
        rf, rf_stale = self._carry_forward(self.rf_series, d, RF_DEFAULT, RF_STALE_DAYS)
        erp_dm, erp_stale = self._carry_forward(self.erp_damodaran_series, d,
                                                self.erp_damodaran, ERP_DM_STALE_DAYS)
        vix_t: float
        if vix is not None:
            vix_t = float(vix)
        else:
            vix_t, vix_stale = self._carry_forward(self.vix_series, d,
                                                    VIX_DEFAULT_POINTS, VIX_STALE_DAYS)
        # VIX is in POINTS (18.0 → 18%); convert to decimal then /√12 → monthly vol
        erp_option = max(0.0, (vix_t / 100.0) / np.sqrt(12.0) - rf)
        w_sum = self.damodaran_weight + self.vix_proxy_weight
        blend_exact = (self.damodaran_weight * erp_dm + self.vix_proxy_weight * erp_option) / w_sum
        blend = _clamp(blend_exact, *self.erp_bounds)
        self._last_erp_blend_log = float(blend)
        return float(blend), {
            "erp_damodaran": float(erp_dm),
            "erp_option": float(erp_option),
            "vix": float(vix_t),
            "rf_10y": float(rf),
            "blend_exact": float(blend_exact),
            "vix_stale": bool(vix_stale) if vix is None else False,
            "rf_stale": bool(rf_stale),
            "erp_damodaran_stale": bool(erp_stale),
            "blend_clamped": bool(abs(blend_exact - blend) > 1e-12),
        }

    def _row_for(self, ticker: str, d: pd.Timestamp, gics_l2: Optional[str],
                 ebit: Optional[float], icr: Optional[float],
                 iv_baseline: Optional[float], d_wacc: Optional[float],
                 retrieval_ts: pd.Timestamp) -> Dict[str, Any]:
        """Computes one full WACC panel row (+ extended prov for sidecar/audit)."""
        beta_u, src = self._unlevered_beta(gics_l2)
        firm = self._firm_regression(ticker, d)
        beta_L, rho, total_beta, beta_prov = self.compute_total_beta(
            beta_u, ticker, d, gics_l2, source=src, firm_override=firm)

        spread, rating_schema, distress, spread_prov = self._cost_of_debt_spread(
            ticker, d, gics_l2, ebit, icr)
        erp, erp_prov = self.compute_erp_blend(d)
        rf = erp_prov["rf_10y"]
        ke = rf + beta_L * erp

        tau = self._effective_tax(ticker, d, ebit)
        kd_pre = rf + spread
        w_d = self._debt_weight(ticker, d, gics_l2)
        wacc = (1.0 - w_d) * ke + w_d * kd_pre * (1.0 - tau)

        # analytical duration nowcast (§2.2): IV_now = IV_base×(1 − D_WACC·ΔWACC)
        iv_base = iv_baseline
        if iv_base is None:
            iv_base = self._pit_lookup(self.iv_baseline, ticker, d)
        d_wacc_val = d_wacc
        if d_wacc_val is None:
            d_wacc_val = self._pit_lookup(self.d_wacc_baseline, ticker, d)
        if d_wacc_val is None:
            d_wacc_val = 0.0
        baseline = self._pit_lookup(self.wacc_estimate_baseline, ticker, d)
        wacc_baseline = float(baseline) if baseline is not None else float(wacc)
        iv_now = 0.01
        if iv_base is not None and iv_base >= 0.01 and d_wacc_val is not None:
            iv_now = analytical_duration_adjustment(float(iv_base), float(d_wacc_val),
                                                    float(wacc) - wacc_baseline)

        filed_ref = self._pit_lookup(self.filed_date_ref, ticker, d)
        if filed_ref is None:
            filed_ref = d

        row = {
            "asof_date": d,
            "ticker": ticker,
            "filed_date_ref": filed_ref,
            "rf_10y": float(rf),
            "erp_damodaran": float(erp_prov["erp_damodaran"]),
            "erp_option": float(erp_prov["erp_option"]),
            "erp_blend": float(erp),
            "ke_v2": float(_clamp(ke, 0.0, 0.5)),
            "kd_pre_tax_v2": float(_clamp(kd_pre, 0.0, 0.5)),
            "wacc_v2": float(_clamp(wacc, 0.0, 0.5)),
            "beta_source": src if firm is None else "firm_regression_60m",
            "wacc_baseline": float(wacc_baseline),
            "duration_wacc": float(d_wacc_val),
            "iv_nowcast": float(max(0.01, iv_now)),
            "retrieval_ts": retrieval_ts,
        }
        prov = {
            "beta_levered_v2": float(beta_L),
            "rho_used": float(rho),
            "total_beta": float(total_beta),
            "synthetic_rating": rating_schema,
            "credit_spread_bps": float(spread * 10000.0),
            "kd_pre_exact": float(kd_pre),
            "ke_exact": float(ke),
            "wacc_exact": float(wacc),
            "tau_effective": float(tau),
            "w_d": float(w_d),
            "omega": spread_prov["omega"],
            "distress": distress,
            "bounds_flag": spread_prov["bounds_flag"],
            "debt_missing": beta_prov["debt_missing"],
            "rho_degraded": beta_prov["rho_degraded"],
            "beta_degraded": beta_prov["beta_degraded"],
            "erp": erp_prov,
            "beta_prov": beta_prov,
        }
        return row, prov

    def compute_daily_wacc(self, ticker: str, date: Union[str, pd.Timestamp],
                           gics_l2: Optional[str] = None,
                           ebit: Optional[float] = None,
                           icr: Optional[float] = None,
                           iv_baseline: Optional[float] = None,
                           d_wacc: Optional[float] = None) -> float:
        """Daily WACC v2 (PIT). Returns wacc_v2 float."""
        d = pd.Timestamp(date)
        row, _ = self._row_for(ticker, d, gics_l2, ebit, icr, iv_baseline, d_wacc,
                               pd.Timestamp.now(tz="UTC").tz_localize(None))
        return float(row["wacc_v2"])

    # ------------------------------------------------------------------
    # Daily panel
    # ------------------------------------------------------------------
    def build_daily_panel(
        self,
        tickers: List[str],
        dates: List[Union[str, pd.Timestamp]],
        gics_l2_map: Optional[Dict[str, Optional[str]]] = None,
        ebit_map: Optional[Dict[str, Optional[float]]] = None,
        icr_map: Optional[Dict[str, Optional[float]]] = None,
        iv_baseline_map: Optional[Dict[str, Optional[float]]] = None,
        d_wacc_map: Optional[Dict[str, Optional[float]]] = None,
        retrieval_ts: Optional[pd.Timestamp] = None,
    ) -> pd.DataFrame:
        """Builds the 15-column daily WACC panel (PIT, PK = asof+ticker)."""
        rows: List[Dict[str, Any]] = []
        retr = retrieval_ts if retrieval_ts is not None else pd.Timestamp.now(tz="UTC").tz_localize(None)
        for ticker in tickers:
            gics = (gics_l2_map or {}).get(ticker)
            for date in dates:
                d = pd.Timestamp(date)
                row, _ = self._row_for(
                    ticker, d, gics,
                    (ebit_map or {}).get(ticker),
                    (icr_map or {}).get(ticker),
                    (iv_baseline_map or {}).get(ticker),
                    (d_wacc_map or {}).get(ticker),
                    retr)
                rows.append(row)

        df = pd.DataFrame(rows, columns=WACC_PANEL_COLUMNS if _HAS_SCHEMA else list(
            rows[0].keys()) if rows else [])
        return df

    def build_daily_panel_with_prov(
        self,
        tickers: List[str],
        dates: List[Union[str, pd.Timestamp]],
        gics_l2_map: Optional[Dict[str, Optional[str]]] = None,
        ebit_map: Optional[Dict[str, Optional[float]]] = None,
        icr_map: Optional[Dict[str, Optional[float]]] = None,
        iv_baseline_map: Optional[Dict[str, Optional[float]]] = None,
        d_wacc_map: Optional[Dict[str, Optional[float]]] = None,
        retrieval_ts: Optional[pd.Timestamp] = None,
    ) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
        """Panel + per-row extended prov (sidecar aggregates, trade_audit payload)."""
        rows: List[Dict[str, Any]] = []
        provs: List[Dict[str, Any]] = []
        retr = retrieval_ts if retrieval_ts is not None else pd.Timestamp.now(tz="UTC").tz_localize(None)
        for ticker in tickers:
            gics = (gics_l2_map or {}).get(ticker)
            for date in dates:
                d = pd.Timestamp(date)
                row, prov = self._row_for(
                    ticker, d, gics,
                    (ebit_map or {}).get(ticker),
                    (icr_map or {}).get(ticker),
                    (iv_baseline_map or {}).get(ticker),
                    (d_wacc_map or {}).get(ticker),
                    retr)
                rows.append(row)
                provs.append(prov)
        df = pd.DataFrame(rows, columns=WACC_PANEL_COLUMNS if _HAS_SCHEMA else list(
            rows[0].keys()) if rows else [])
        return df, provs

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def write_panel_partitioned(
        self,
        df: pd.DataFrame,
        out_dir: Union[str, Path] = DEFAULT_WACC_OUT_DIR,
        provs: Optional[List[Dict[str, Any]]] = None,
        audit: Any = None,
    ) -> List[Path]:
        """Writes wacc_panel_daily as month=YYYY-MM/ parquet + per-month provenance
        sidecar (SHA-256, degraded-flag aggregates for the coverage gate), and
        optionally appends one chained trade_audit block per row (batched)."""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        df = df.copy()
        df["asof_date"] = pd.to_datetime(df["asof_date"])
        df["month"] = df["asof_date"].dt.strftime("%Y-%m")
        written: List[Path] = []

        # row-aligned provenance: bucket each prov by the month of ITS row
        # (rows interleave months when built per-ticker × date).
        prov_by_row = list(zip(df.itertuples(index=False), provs)) if provs is not None else None
        audit_batches: Dict[str, List[Dict[str, Any]]] = {}

        for month, grp in df.groupby("month"):
            mdir = out_dir / f"month={month}"
            mdir.mkdir(parents=True, exist_ok=True)
            col_df = grp.drop(columns=["month"])
            path = mdir / "wacc_panel.parquet"
            col_df.to_parquet(path, index=False)
            written.append(path)

            # per-month degraded aggregates (coverage gate: 100% non-NaN & ≥99% non-degraded)
            deg_agg: Dict[str, Any] = {}
            if prov_by_row is not None:
                month_provs = [p for r, p in prov_by_row
                               if pd.Timestamp(r.asof_date).strftime("%Y-%m") == month]
                n_degraded = sum(1 for p in month_provs
                                 if p.get("beta_degraded") or p.get("rho_degraded"))
                deg_agg = {
                    "rows": int(len(grp)),
                    "beta_degraded_rows": n_degraded,
                    "degraded_pct": round(100.0 * n_degraded / max(len(grp), 1), 4),
                }
            self._write_sidecar(path, col_df, **deg_agg)

            if audit is not None:
                # one audit block per panel row (batched per month by the ledger)
                for _, r in col_df.iterrows():
                    payload = self._audit_payload(row_dict=r)
                    audit_batches.setdefault(month, []).append(payload)

        if audit is not None and audit_batches:
            audit.append_batch([p for ps in audit_batches.values() for p in ps])
        return written

    @staticmethod
    def _audit_payload(row_dict: Any) -> Dict[str, Any]:
        """Maps one panel row → trade_audit block payload (stage_gate=1 valuation).
        trigger_reason is the minimum VALID Appendix A valuation object."""
        r = row_dict
        asof = pd.Timestamp(r["asof_date"])
        filed = pd.Timestamp(r["filed_date_ref"]) if pd.notna(r["filed_date_ref"]) else asof
        retr = pd.Timestamp(r["retrieval_ts"]) if pd.notna(r["retrieval_ts"]) else (
            pd.Timestamp.now(tz="UTC").tz_localize(None))
        return {
            "ticker": str(r["ticker"]),
            "asof": asof,
            "wacc_v2": float(r["wacc_v2"]),
            "ke_v2": float(r["ke_v2"]),
            "kd_pre_tax_v2": float(r["kd_pre_tax_v2"]),
            "erp_blend": float(r["erp_blend"]),
            "rf_10y": float(r["rf_10y"]),
            "beta_source": str(r["beta_source"]),
            "retrieval_ts": retr,
            "action": "PANEL_WRITE",
            "gate_name": "WACC_PANEL",
            "stage_gate": 1,  # valuation stage
            "trigger_reason": {
                "valuation": {
                    "iv_p25": float(max(0.0, r["iv_nowcast"])),
                    "price": float(max(0.0, r["iv_nowcast"])),
                    "discount_pct": 0.0,
                    "iv_method": "dcf_v2_nowcast",
                    "iv_basis_filed_date": str(filed.date()),
                    "iv_nowcast_ts": retr.isoformat(),
                }
            },
        }

    def _write_sidecar(self, parquet_path: Path, df: pd.DataFrame, **extra: Any) -> None:
        import hashlib as _hl
        raw = parquet_path.read_bytes()
        digest = _hl.sha256(raw).hexdigest()
        meta: Dict[str, Any] = {
            "table": "wacc_panel_daily",
            "rows": int(len(df)),
            "columns": list(df.columns),
            "pk": ["asof_date", "ticker"],
            "sha256": digest,
            "filed_date_ref_le_asof": bool((df["filed_date_ref"].dt.normalize() <= df["asof_date"].dt.normalize()).all()),
            "retrieval_ts": str(df["retrieval_ts"].iloc[0]) if len(df) else None,
            "wacc_bounds_ok": bool((df["wacc_v2"].between(0.0, 0.5)).all()),
            "erp_bounds_ok": bool((df["erp_blend"].between(0.005, 0.08)).all()),
            "beta_source_100pct": bool(df["beta_source"].notna().all()),
            "written_at": datetime.now(timezone.utc).isoformat(),
        }
        meta.update(extra)
        sidecar = parquet_path.with_suffix(parquet_path.suffix + ".provenance.json")
        sidecar.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def validate_panel(self, df: pd.DataFrame) -> List[str]:
        if _HAS_SCHEMA and validate_wacc_panel is not None:
            return validate_wacc_panel(df)
        return _fallback_validate_panel(df)


def _populate_series(target: Dict[pd.Timestamp, float], src, scale: float = 1.0) -> None:
    if isinstance(src, dict):
        items = src.items()
    elif isinstance(src, pd.Series):
        items = src.items()
    else:
        items = []
    for k, v in items:
        try:
            target[pd.Timestamp(k)] = float(v) * scale
        except (TypeError, ValueError):
            continue


def _is_frac(series) -> bool:
    """Heuristic: values ≤ 0.5 treated as fractions (rf/erp/vix-in-decimal), not pc/points."""
    if isinstance(series, dict):
        vals = [v for v in series.values() if v is not None]
    elif isinstance(series, pd.Series):
        vals = series.dropna().tolist()
    else:
        return False
    return bool(vals) and max(vals) <= 0.5


def clamp(value: float, lo: float, hi: float) -> float:
    return float(max(lo, min(hi, value)))


def _fallback_validate_panel(df: pd.DataFrame) -> List[str]:
    violations: List[str] = []
    if df["asof_date"].duplicated().any():
        violations.append("duplicate asof_date rows (expected all distinct)")
    if df.duplicated(subset=["asof_date", "ticker"]).any():
        violations.append("duplicate (asof_date, ticker) rows — PK violated")
    if not (df["filed_date_ref"].dt.normalize() <= df["asof_date"].dt.normalize()).all():
        violations.append("filed_date_ref > asof_date (lookahead)")
    if (df["wacc_v2"] < 0.0).any() or (df["wacc_v2"] > 0.5).any():
        violations.append("wacc_v2 outside [0,0.5]")
    if (df["erp_blend"] < 0.005).any() or (df["erp_blend"] > 0.08).any():
        violations.append("erp_blend outside [0.005, 0.08]")
    if (df["iv_nowcast"] < 0.01).any():
        violations.append("iv_nowcast below 0.01 floor")
    if df["beta_source"].isnull().any():
        violations.append("null beta_source (coverage gate violation)")
    return violations


def _self_test() -> int:
    print("WACCEngineV2 self-test")
    print("=" * 60)

    # 0. schema contract present
    assert _HAS_SCHEMA, "valuation_schema must be importable"
    assert len(WACC_PANEL_COLUMNS) == 15, WACC_PANEL_COLUMNS

    # 1. VIX-in-points bug guard: 18 pts → ERP_option ≈ 5.2% − R_f, NOT 520%
    erp_vix: WACCEngineV2 = WACCEngineV2()
    erp_vix.load_rf({"2024-11-01": 0.042})
    erp_vix.load_vix({"2024-11-01": 18.0})
    erp_vix.load_erp({"2024-11-01": 0.045})
    blend, prov = erp_vix.compute_erp_blend(pd.Timestamp("2024-11-01"))
    assert 0.0 <= prov["erp_option"] <= 0.30, prov
    assert abs(prov["erp_option"] - ((18.0 / 100.0) / np.sqrt(12.0) - 0.042)) < 1e-9
    print(f"[OK] VIX/sqrt(12) with /100: VIX=18pts -> ERP_option={prov['erp_option']:.4f} "
          f"(blend={blend:.4f})")

    # 2. ERP clamp: VIX=80 + R_f=0.005 → blend clamps to 0.08 (EXT-10)
    erp_vix.load_rf({"2024-11-01": 0.005})
    erp_vix.load_vix({"2024-11-01": 80.0})
    blend2, prov2 = erp_vix.compute_erp_blend(pd.Timestamp("2024-11-01"))
    assert abs(blend2 - 0.08) < 1e-9, prov2
    print(f"[OK] ERP clamp (EXT-10): VIX=80 -> blend={blend2:.4f} (frozen erp_bounds 5%-8%)")

    # 3. sigmoid total-beta formula: 5.0·sigmoid(raw/3.0); EXT-07 raw=7.2 → ~4.58
    tb = _sigmoid_ceiling_beta(7.2)
    assert abs(tb - 5.0 / (1.0 + np.exp(-7.2 / 3.0))) < 1e-12
    assert 4.4 < tb < 5.0, tb
    assert _sigmoid_ceiling_beta(1e9) == 5.0 and _sigmoid_ceiling_beta(-1.0) == 0.0
    print(f"[OK] total_beta sigmoid: raw=7.2 -> {tb:.4f} (5.0*sigmoid(x/3); ceiling 5.0)")

    # 4. rating mapping to schema enum
    assert _map_rating_to_schema("Aaa/AAA") == "AAA"
    assert _map_rating_to_schema("Baa2/BBB") == "BBB"
    assert _map_rating_to_schema("Caa/CCC") == "CCC"
    assert _map_rating_to_schema("D2/D") == "D"
    assert _map_rating_to_schema("A1/A+") == "A"
    print("[OK] Damodaran ratings map to valuation_schema synthetic_rating enum")

    # 5. Ω_stress closed-form: burn 0.5, ICR −2, High → 1 + 0.5·0.9·1.5 = 1.675
    eng = WACCEngineV2()
    om, om_prov = eng._omega_stress(-2.0, -500.0, 1000.0, "151020")  # beta 0.93 -> Medium
    bu_sec, _s = eng.sector_beta.lookup_unlevered("151020")
    tier = "High" if bu_sec >= 1.2 else ("Low" if bu_sec <= 0.7 else "Medium")
    om_exp = 1.0 + 0.5 * 0.9 * float(eng.omega_stress_factors[tier])
    assert abs(om - om_exp) < 1e-9, om_prov  # exact vs actual tier factor
    omh, om_prov_h = eng._omega_stress(-2.0, -500.0, 1000.0, "451020")  # beta 1.55 -> High
    assert abs(omh - (1.0 + 0.5 * 0.9 * 1.5)) < 1e-9, om_prov_h
    assert 1.0 <= omh <= 3.0
    print(f"[OK] Omega_stress: burn=0.5/ICR=-2 -> Omega={om:.4f} (tier={tier}), {omh:.4f} (High) "
          f"(plan EXT-01 notes Omega>=2.5 is NOT reachable under frozen formula - documented)")

    # 6. ke uses β_L: financials → β_L=β_U and ke=rf+β_L·erp (not total_beta)
    asof = pd.Timestamp("2024-11-01")
    eng.load_rf({str(asof.date()): 0.042})
    eng.load_vix({str(asof.date()): 18.0})
    eng.load_erp({str(asof.date()): 0.045})
    beta_u, src = eng._unlevered_beta("401010")
    beta_l, rho, total_beta, prov6 = eng.compute_total_beta(
        beta_u, "JPM", asof, gics_l2="401010")
    assert prov6["is_financial"] is True and abs(beta_l - beta_u) < 1e-9
    assert total_beta >= beta_l  # sigmoid ceiling ≥ raw for levered names
    blend6, _ = eng.compute_erp_blend(asof)
    ke6 = 0.042 + beta_l * blend6
    print(f"[OK] Financials carve-out: beta_L==beta_U={beta_l:.3f}; ke=rf+beta_L*ERP={ke6:.4f} "
          f"vs ke-with-total_beta={0.042 + total_beta * blend6:.4f} (stored separately)")

    # 7. tax shield: EBIT≤0 → (1−τ)=1.0 (τ_eff 0.0); Hamada still uses applied τ
    eng.update_on_filing("X", "2024-10-28", {
        "d_e_market_net": 0.5, "cash_ratio": 0.02, "icr_v2": -1.0,
        "tax_rate_applied": 0.16, "ocf_v2": -100.0, "revenue": 200.0,
        "iv_baseline": 12.0, "duration_wacc": 3.0, "wacc_estimate": 0.10})
    tau_dist = eng._effective_tax("X", asof, ebit=-5.0)
    tau_hm = eng._hamada_tax("X", asof)
    assert tau_dist == 0.0 and tau_hm == 0.16, (tau_dist, tau_hm)
    spread_x, rating_x, distress_x, sprov_x = eng._cost_of_debt_spread(
        "X", asof, "452010", ebit=-5.0, icr=-1.0)
    assert distress_x and rating_x == "CCC"
    assert sprov_x["base_spread"] == DISTRESSED_SPREAD
    print(f"[OK] distressed: EBIT<=0 -> tau_eff=0 ({1.0 - tau_dist:.0%} shield), "
          f"rating={rating_x}, spread={spread_x:.4f} (Omega={sprov_x['omega']['omega_stress']:.3f})")

    # 8. Appendix B fallback: unmapped GICS → median fallback; never null
    bu_m, src_m = eng._unlevered_beta("999999")
    assert src_m in ("sector_median_fallback", "universe_median_fallback"), src_m
    assert bu_m > 0.0
    bu_all, src_all = eng._unlevered_beta(None)
    assert bu_all > 0.0 and src_all in ("sector_bottom_up", "sector_median_fallback",
                                        "universe_median_fallback"), src_all
    print(f"[OK] Appendix B fallback: unmapped -> {bu_m:.3f} ({src_m}); always non-null")

    # 9. daily panel integrity (2 tickers, incl. structural path + firm regression)
    eng.update_on_filing("AAPL", "2024-10-28", {
        "d_e_market_net": 0.15, "cash_ratio": 0.05, "icr_v2": 4.2,
        "tax_rate_applied": 0.16, "operating_cash_flow": 1200.0, "revenue": 2000.0,
        "iv_baseline": 118.0, "duration_wacc": 7.5, "wacc_estimate": 0.090})
    eng.update_on_filing("MSFT", "2024-10-30", {
        "d_e_market_net": 0.08, "cash_ratio": 0.12, "icr_v2": 6.3,
        "tax_rate_applied": 0.20, "operating_cash_flow": 800.0, "revenue": 1500.0,
        "iv_baseline": 96.0, "duration_wacc": 6.2, "wacc_estimate": 0.085})
    panel, provs = eng.build_daily_panel_with_prov(
        ["AAPL", "MSFT"], [asof],
        gics_l2_map={"AAPL": "452010", "MSFT": "451030"},
        ebit_map={"AAPL": 2000.0, "MSFT": 1500.0})
    assert len(panel.columns) == 15, panel.columns
    viol = eng.validate_panel(panel)
    assert not viol, viol
    assert panel["beta_source"].notna().all()
    # firm regression override path (≥36 obs)
    eng.load_beta_60m({("MSFT", asof): 1.10}, n_obs={("MSFT", asof): 60})
    panel2, provs2 = eng.build_daily_panel_with_prov(["MSFT"], [asof], gics_l2_map={"MSFT": "451030"})
    assert panel2["beta_source"].iloc[0] == "firm_regression_60m"
    print("[OK] daily panel passes §2.2 + PIT checks; firm_regression_60m override works")

    # 10. persistence + sidecar (tmp dir)
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "wacc"
        paths = eng.write_panel_partitioned(panel, out, provs=provs)
        assert len(paths) >= 1 and paths[0].exists()
        sidecar = json.loads(Path(str(paths[0]) + ".provenance.json").read_text(encoding="utf-8"))
        assert sidecar["beta_source_100pct"] is True
        import hashlib as _hl
        assert sidecar["sha256"] == _hl.sha256(paths[0].read_bytes()).hexdigest()
    print("[OK] partitioned parquet + SHA-256 sidecar written & verified")

    print("\nAll WACCEngineV2 tests passed.")
    return 0


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Phase 8 WACC Engine v2 (daily PIT panel)")
    parser.add_argument("--self-test", action="store_true", help="Run test suite")
    parser.add_argument("--write-snapshot", type=str, default=None,
                        help="Build + write a demo daily panel to a directory")
    args = parser.parse_args()
    if args.self_test:
        return _self_test()
    if args.write_snapshot:
        eng = WACCEngineV2()
        asof = pd.Timestamp("2024-11-01")
        eng.load_rf({str(asof.date()): 0.042})
        eng.load_vix({str(asof.date()): 18.0})
        eng.load_erp({str(asof.date()): 0.045})
        panel, provs = eng.build_daily_panel_with_prov(
            ["AAPL", "MSFT"], [asof],
            gics_l2_map={"AAPL": "452010", "MSFT": "451030"},
            iv_baseline_map={"AAPL": 118.0, "MSFT": 96.0})
        paths = eng.write_panel_partitioned(panel, Path(args.write_snapshot), provs=provs)
        print(f"[OK] wrote {len(paths)} partition(s): " + ", ".join(str(p) for p in paths))
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys_exit_code = main()
    import sys as _sys
    _sys.exit(sys_exit_code)