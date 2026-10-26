"""
Valuation Schema — Hybrid Fractal Architecture + Phase 8 Redesign (bigpickle-worker)

Canonical PIT column contract for `data/pit_fundamentals/valuation_fundamentals.parquet`
and the Phase 8 daily WACC panel contract for
`data/wacc/wacc_panel_daily/month=YYYY-MM/wacc_panel.parquet`.

Layout (SCHEMA_VERSION 3.0)
---------------------------
    N_CORE_KEYS  = 6    (ticker, fiscal_end_date, filed_date, accession, form, fiscal_year)
    N_EXISTING   = 51   (verified LIVE from the 57-column parquet minus keys)
    N_ADDED_V2   = 22   (Phase 0-1: t-HMM state, fractal vol, swap barrier, execution, regime)
    N_ADDED_V3   = 28   (Phase 8, per phase8_bigpickle_analysis §2.1:
                         beta_wacc_v2 11, narrative_v2 9, data_gate_v2 5,
                         regime policy-state 3)
    N_TOTAL      = 107

NOTE ON COUNT RECONCILIATION:
    The Phase 8 dispatch brief quotes "79 -> 95 cols (add 16)".  The binding
    enumeration in phase8_bigpickle_analysis.md §2.1 lists 25 added filing-row
    columns (11 + 9 + 5) PLUS the 4-label regime policy-state extension
    (hmm_prob_shock, hmm_prob_recovery, policy_state) required by the same
    section's "validate_schema additions" block.  This module implements the
    FULL enumerated §2.1 set (104 = 79 + 25) + the 3 regime policy-state
    columns = 107.  The "95" figure predates the final §2.1 group tables and
    is retained here only as historical context; column presence is the
    contract.

    Daily-mutable K_e / WACC columns do NOT go in the filing-row parquet.
    They live in the separate `wacc_panel_daily` table (bigpickle §2.2) keyed
    by (asof_date, ticker) — avoids 63x filing-row duplication and the FM-2
    staleness trap.

New column groups (all PIT-compliant, anchored on `filed_date`):

    beta_wacc_v2    (11)  bottom-up unlevered beta, Hamada levering, total
                          beta, synthetic credit rate (Phase 8 WACC)
    narrative_v2    (9)   survival probability, liquidation value,
                          S/C reinvestment ratio (Phase 8 WS-6)
    data_gate_v2    (5)   quarantine/probation/full state + MAD filter
    regime_policy   (3)   4-label policy state (EXPANSION/CRISIS/SHOCK/RECOVERY)

PIT discipline (enforced by `validate_schema`):
    * `filed_date` is the row-level availability anchor.
    * `hmm_reestimate_date <= hmm_nowcast_date <= filed_date`.
    * `beta_source IS NOT NULL` whenever `beta_levered_v2` is present (100%
      coverage gate support; every row must carry a beta source).
    * `total_beta` non-null implies `rho_market_60m` non-null.
    * `data_gate_state = 'FULL'` -> quarters >= 12; `'QUARANTINE'` -> cap 0.005.
    * `reinvestment_rate_v2 >= 0`; `sc_ratio_used > 0` (P95 clamp enforced).
    * 4-label regime: `policy_state` in enum, probabilities in [0,1]; when all
      four hmm_prob_* are non-null they must sum to ~1.

Daily WACC panel (`wacc_panel_daily`, §2.2):
    * PK (asof_date, ticker); `filed_date_ref <= asof_date` (basis filing
      predates trade date — no lookahead).
    * `rf_10y` / `erp_blend` are as-of T-1 inputs (FRED 1-day lag) — the
      engine stamps them at T-1; the panel PIT check asserts the retrieval
      stamp semantics in `validate_wacc_panel`.
    * `ke_v2`, `wacc_v2` in [0, 0.5]; `iv_nowcast` non-negative.
    * `iv_nowcast = iv_baseline * (1 - D * dWACC)` (Quantitative/iv_nowcast.py).

CLI:
    python -m ingestion.valuation_schema --list
    python -m ingestion.valuation_schema --test
    python -m ingestion.valuation_schema --check data/pit_fundamentals/valuation_fundamentals.parquet
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

SCHEMA_VERSION = "3.0"
SCHEMA_CREATED = "2026-09-20"
CONFIG_PATH = "config/pre_registration_phase8.yaml"

# ----------------------------------------------------------------------------
# Column identity
# ----------------------------------------------------------------------------

CORE_KEY_COLUMNS: List[str] = [
    "ticker", "fiscal_end_date", "filed_date", "accession", "form", "fiscal_year",
]

# 51 existing *data* columns (57 total baseline minus the 6 core keys above).
# Order preserves the live parquet column order.
EXISTING_COLUMNS: List[str] = [
    "cash", "stockholders_equity", "capex", "depreciation_amortization",
    "income_tax_expense", "operating_cash_flow", "operating_income", "rd_expense",
    "sbc_expense", "current_assets", "current_liabilities", "interest_expense",
    "inventory", "long_term_debt", "payables", "receivables", "total_assets",
    "total_liabilities", "net_income", "revenue", "total_debt", "ebit", "nopat",
    "invested_capital", "roic", "fcf", "revenue_yoy", "ebit_yoy", "fcf_margin",
    "operating_margin", "debt_to_equity", "interest_coverage", "rd_to_revenue",
    "sbc_to_revenue", "capex_to_revenue", "cost_of_debt", "weight_equity",
    "weight_debt", "reinvestment_rate", "expected_growth", "fcf_roic", "net_debt",
    "rd_cap_asset", "rd_amort_exp", "ebit_cap_rd", "nopat_cap_rd",
    "invested_capital_cap_rd", "roic_cap_rd", "omr", "cost_of_equity_est",
    "wacc_estimate",
]

# --- V2 added groups (Phase 0-1, unchanged by Phase 8) ---------------------

HMM_STATE_COLUMNS: List[str] = [
    "hmm_state", "hmm_prob_expansion", "hmm_prob_crisis",
    "hmm_reestimate_date", "hmm_nowcast_date",
]

FRACTAL_VOL_COLUMNS: List[str] = [
    "sigma_fractal_5d", "sigma_fractal_10d", "sigma_fractal_20d",
    "sector_tau", "sector_k",
]

SWAP_BARRIER_COLUMNS: List[str] = [
    "swap_barrier_bps", "t_held_months", "moat_penalty_bps",
    "swap_allowed", "exit_reason",
]

EXECUTION_COLUMNS: List[str] = [
    "tca_est_bps", "tca_realized_bps", "adv_usd", "fill_rate", "queue_position",
]

REGIME_COLUMNS: List[str] = [
    "regime_target_iv_mult", "regime_nowcast_probs",
]

ADDED_V2_COLUMNS: List[str] = (
    HMM_STATE_COLUMNS + FRACTAL_VOL_COLUMNS + SWAP_BARRIER_COLUMNS
    + EXECUTION_COLUMNS + REGIME_COLUMNS
)

# --- V3 added groups (Phase 8, phase8_bigpickle_analysis §2.1) -------------

# beta_wacc_v2 — 11 rows per §2.1 table (per-filing, PIT = filed_date)
BETA_WACC_V2_COLUMNS: List[str] = [
    "beta_unlevered_sector",      # Damodaran 94-industry beta_U via GICS L2
    "beta_levered_v2",            # Hamada-relevered firm beta (net debt + cash term)
    "beta_source",                # enum — drives 100% coverage gate
    "d_e_market_net",             # Max(net debt,0)/market cap, D/E cap at sector P95
    "rho_market_60m",             # rolling 60-mo correlation with SPY (as-of T-1)
    "total_beta",                 # beta_L/max(rho, rho_floor), sigmoid ceiling 5.0
    "synthetic_rating",           # enum NONE..D from ICR lookup
    "credit_spread_bps",          # spread from ratings table (bps)
    "kd_pre_tax_v2",              # R_f + spread (book-debt approximation flagged)
    "tax_rate_applied",           # tau_eff if 0<=tau_eff<=0.35 else 0.21
    "debt_mv_source",             # book / marked
]

# narrative_v2 — 9 rows per §2.1 table (per-filing)
NARRATIVE_V2_COLUMNS: List[str] = [
    "p_survival",                 # clamped [0.05, 0.95]; priority cascade §5.7
    "liquidation_value_ps",       # tangible assets - claims + net cash, per share
    "survival_adjusted_iv",       # p*IV_going_concern + (1-p)*liquidation
    "sc_ratio_used",              # industry S/C or 3yr median, P95 clamp
    "reinvestment_rate_v2",       # (dRev/S_C)/EBIT(1-tau) — corrected formula §5.7
    "unfunded_growth_flag",       # Required_Reinvestment > Available_Funding*1.2
    "reinvestment_gap_pct",       # (Required - Available)/Available
    "terminal_growth_haircut",    # 0.0 or 0.50 on unfunded flag
    "wacc_penalty_bps_applied",   # 0 or 200 on unfunded flag
]

# data_gate_v2 — 5 rows per §2.1 table
DATA_GATE_V2_COLUMNS: List[str] = [
    "data_gate_quarters",         # PIT SEC filing count with filed_date <= as-of
    "data_gate_state",            # QUARANTINE (<8q) / PROBATION (8-11q) / FULL (>=12q)
    "max_weight_cap",             # 0.005 / 0.010 / unconstrained
    "mad_filter_applied",         # True if any alt-data metric replaced by RollMedian_90d
    "mad_outlier_cycles",         # consecutive flagged cycles
]

# regime policy-state — 4-label extension (§2.1 validate_schema additions)
REGIME_POLICY_COLUMNS: List[str] = [
    "hmm_prob_shock",             # filtered P(SHOCK | obs<=nowcast_date)
    "hmm_prob_recovery",          # filtered P(RECOVERY | obs<=nowcast_date)
    "policy_state",               # EXPANSION / CRISIS / SHOCK / RECOVERY
]

ADDED_V3_COLUMNS: List[str] = (
    BETA_WACC_V2_COLUMNS + NARRATIVE_V2_COLUMNS + DATA_GATE_V2_COLUMNS
    + REGIME_POLICY_COLUMNS
)

ALL_COLUMNS: List[str] = CORE_KEY_COLUMNS + EXISTING_COLUMNS + ADDED_V2_COLUMNS + ADDED_V3_COLUMNS

N_CORE_KEYS = len(CORE_KEY_COLUMNS)
N_EXISTING = len(EXISTING_COLUMNS)          # 51 data cols (+6 keys = 57 baseline)
N_BASELINE = N_CORE_KEYS + N_EXISTING       # 57 — verified against live parquet
N_ADDED_V2 = len(ADDED_V2_COLUMNS)          # 22
N_ADDED_V3 = len(ADDED_V3_COLUMNS)          # 28
N_ADDED = N_ADDED_V2 + N_ADDED_V3           # 50
N_TOTAL = len(ALL_COLUMNS)                  # 107

# Frozen enums / bounds (config/pre_registration_phase8.yaml, NOT configurable)
EXIT_REASONS: List[str] = ["floor", "target", "trailing", "swap", "regime_shift"]
HMM_STATES: Dict[int, str] = {0: "EXPANSION", 1: "CRISIS"}
POLICY_STATES: List[str] = ["EXPANSION", "CRISIS", "SHOCK", "RECOVERY"]
BETA_SOURCES: List[str] = [
    "sector_bottom_up", "firm_regression_60m",
    "sector_median_fallback", "universe_median_fallback",
]
SYNTHETIC_RATINGS: List[str] = ["NONE", "AAA", "AA", "A", "BBB", "BB", "B", "CCC", "CC", "C", "D"]
DATA_GATE_STATES: List[str] = ["QUARANTINE", "PROBATION", "FULL"]
DEBT_MV_SOURCES: List[str] = ["book", "marked"]


# ----------------------------------------------------------------------------
# WACC daily panel (§2.2) — separate table, NOT part of the filing-row parquet
# ----------------------------------------------------------------------------
WACC_PANEL_COLUMNS: List[str] = [
    "asof_date",         # PIT anchor (trade date)
    "ticker",            # VARCHAR
    "filed_date_ref",    # FCF-matrix basis row (PIT join key)
    "rf_10y",            # FRED DGS10, as-of = T-1 (6am ET lag)
    "erp_damodaran",     # frozen estimator (E1, quarterly)
    "erp_option",        # frozen estimator (E3: VIX/sqrt(12) - R_f), as-of T-1
    "erp_blend",         # 0.70*damodaran + 0.30*option
    "ke_v2",             # CHECK 0.0..0.5
    "kd_pre_tax_v2",     # R_f + spread
    "wacc_v2",           # CHECK 0.0..0.5
    "beta_source",       # mirrors valuation row
    "wacc_baseline",     # from last filing (delta for duration nowcast)
    "duration_wacc",     # analytical duration (RS-24)
    "iv_nowcast",        # iv_baseline * (1 - D*dWACC)
    "retrieval_ts",      # TIMESTAMP
]

WACC_PANEL_DDL_SQL: str = """
CREATE TABLE wacc_panel_daily (
    asof_date        DATE    NOT NULL,          -- PIT anchor (trade date)
    ticker           VARCHAR NOT NULL,
    filed_date_ref   DATE,                      -- FCF-matrix basis row (PIT join key)
    rf_10y           DOUBLE,                    -- FRED DGS10, as-of = T-1 (6am ET lag)
    erp_damodaran    DOUBLE,
    erp_option       DOUBLE,                    -- frozen estimator (E3 VIX proxy)
    erp_blend        DOUBLE,                    -- 0.70 x damodaran + 0.30 x option
    ke_v2            DOUBLE  CHECK (ke_v2 BETWEEN 0.0 AND 0.5),
    kd_pre_tax_v2    DOUBLE,
    wacc_v2          DOUBLE  CHECK (wacc_v2 BETWEEN 0.0 AND 0.5),
    beta_source      VARCHAR,                   -- mirrors valuation row
    wacc_baseline    DOUBLE,                    -- from last filing (delta for duration nowcast)
    duration_wacc    DOUBLE,                    -- analytical duration (RS-24)
    iv_nowcast       DOUBLE,                    -- iv_baseline * (1 - D x dWACC)
    retrieval_ts     TIMESTAMP,
    PRIMARY KEY (asof_date, ticker)
);
"""


# ----------------------------------------------------------------------------
# Column specification
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class ColumnSpec:
    name: str
    dtype: str                       # "str" | "float" | "int" | "bool" | "date" | "json_text"
    group: str
    description: str
    required: bool = True
    enum: Optional[List[str]] = None
    min: Optional[float] = None
    max: Optional[float] = None

    def check_value(self, v: Any) -> Optional[str]:
        """Returns a violation message or None."""
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return None  # nulls allowed for non-key columns
        if self.dtype == "str":
            if self.enum is not None and str(v) not in self.enum:
                return f"'{v}' not in enum {self.enum}"
            return None
        if self.dtype == "bool":
            if not isinstance(v, (bool, np.bool_)):
                return f"expected bool, got {type(v).__name__}"
            return None
        if self.dtype == "date":
            return None
        try:
            fv = float(v)
        except (TypeError, ValueError):
            return f"non-numeric value {v!r}"
        if np.isnan(fv):
            return None
        if self.min is not None and fv < self.min:
            return f"{fv} < min {self.min}"
        if self.max is not None and fv > self.max:
            return f"{fv} > max {self.max}"
        return None


def _spec(name: str, dtype: str, group: str, description: str,
          enum: Optional[List[str]] = None,
          min: Optional[float] = None, max: Optional[float] = None,
          required: bool = True) -> ColumnSpec:
    return ColumnSpec(name, dtype, group, description, required, enum, min, max)


COLUMN_SPECS: Dict[str, ColumnSpec] = {}

# Core keys
for _c, _d in [
    ("ticker", "ticker symbol"),
    ("fiscal_end_date", "fiscal period end date"),
    ("filed_date", "SEC EDGAR filing date — row-level PIT availability anchor"),
    ("accession", "SEC accession number"),
    ("form", "SEC form type (10-K/10-Q)"),
    ("fiscal_year", "fiscal year of the reporting period"),
]:
    COLUMN_SPECS[_c] = _spec(_c, "str" if _c in ("ticker", "accession", "form") else
                             ("date" if _c.endswith("_date") else "int"),
                             "core_key", _d)

# Existing valuation data columns (all float64, PIT on filed_date)
for _c in EXISTING_COLUMNS:
    COLUMN_SPECS[_c] = _spec(_c, "float", "valuation", f"existing valuation column '{_c}' (57-col baseline)")

# --- V2 groups (unchanged) --------------------------------------------------

for _c, _d in [
    ("hmm_state", "2-state t-HMM nowcast: 0=EXPANSION, 1=CRISIS (daily filter step, fixed params)"),
    ("hmm_prob_expansion", "filtered P(EXPANSION | obs<=nowcast_date)"),
    ("hmm_prob_crisis", "filtered P(CRISIS | obs<=nowcast_date)"),
    ("hmm_reestimate_date", "date of the monthly MLE re-estimation whose params produced this nowcast"),
    ("hmm_nowcast_date", "as-of date of the daily filter step (PIT anchor for hmm_* columns)"),
]:
    if _c == "hmm_state":
        COLUMN_SPECS[_c] = _spec(_c, "int", "t_hmm_state", _d, min=0, max=1)
    elif _c.endswith("_date"):
        COLUMN_SPECS[_c] = _spec(_c, "date", "t_hmm_state", _d)
    else:
        COLUMN_SPECS[_c] = _spec(_c, "float", "t_hmm_state", _d, min=0.0, max=1.0)

for _c, _d in [
    ("sigma_fractal_5d", "HAR-|r| fitted vol over next 5 trading days (decimal, e.g. 0.05 = 5%)"),
    ("sigma_fractal_10d", "HAR-|r| fitted vol over next 10 trading days"),
    ("sigma_fractal_20d", "HAR-|r| fitted vol over next 20 trading days"),
    ("sector_tau", "sector trailing horizon tau from frozen multifractal.sector_overrides"),
    ("sector_k", "sector trailing multiplier k from frozen multifractal.sector_overrides"),
]:
    if _c == "sector_tau":
        COLUMN_SPECS[_c] = _spec(_c, "int", "fractal_vol", _d, min=1, max=252)
    elif _c == "sector_k":
        COLUMN_SPECS[_c] = _spec(_c, "float", "fractal_vol", _d, min=0.0, max=10.0)
    else:
        COLUMN_SPECS[_c] = _spec(_c, "float", "fractal_vol", _d, min=0.0)

for _c, _d in [
    ("swap_barrier_bps", "barrier = C*exp(gamma*t_target/(t_held+eps)) + moat_penalty, in bps"),
    ("t_held_months", "months the current position has been held at as-of date"),
    ("moat_penalty_bps", "100 if moat condition holds on CURRENT holding, else 0"),
    ("swap_allowed", "True iff candidate edge delta (bps) > swap_barrier_bps"),
    ("exit_reason", "reason for exit when a swap/exit is triggered"),
]:
    if _c == "swap_allowed":
        COLUMN_SPECS[_c] = _spec(_c, "bool", "swap_barrier", _d)
    elif _c == "exit_reason":
        COLUMN_SPECS[_c] = _spec(_c, "str", "swap_barrier", _d, enum=EXIT_REASONS)
    elif _c == "t_held_months":
        COLUMN_SPECS[_c] = _spec(_c, "float", "swap_barrier", _d, min=0.0)
    else:
        COLUMN_SPECS[_c] = _spec(_c, "float", "swap_barrier", _d, min=0.0)

for _c, _d in [
    ("tca_est_bps", "pre-trade estimated one-way transaction cost for this exit leg (bps)"),
    ("tca_realized_bps", "realized one-way transaction cost after fill (bps)"),
    ("adv_usd", "average daily dollar volume at as-of date (USD)"),
    ("fill_rate", "fraction of intended size filled (0..1)"),
    ("queue_position", "percentile queue position for limit orders (0=head of queue, 1=tail)"),
]:
    if _c == "fill_rate":
        COLUMN_SPECS[_c] = _spec(_c, "float", "execution", _d, min=0.0, max=1.0)
    elif _c == "queue_position":
        COLUMN_SPECS[_c] = _spec(_c, "float", "execution", _d, min=0.0, max=1.0)
    elif _c == "adv_usd":
        COLUMN_SPECS[_c] = _spec(_c, "float", "execution", _d, min=0.0)
    else:
        COLUMN_SPECS[_c] = _spec(_c, "float", "execution", _d)

for _c, _d in [
    ("regime_target_iv_mult", "target intrinsic-value multiple for the nowcast regime (EXP 1.20 / CRISIS 1.05)"),
    ("regime_nowcast_probs", "JSON snapshot {expansion: p, crisis: 1-p} from the daily HMM filter"),
]:
    if _c == "regime_target_iv_mult":
        COLUMN_SPECS[_c] = _spec(_c, "float", "regime", _d, min=0.0, max=5.0)
    else:
        COLUMN_SPECS[_c] = _spec(_c, "json_text", "regime", _d)

# --- V3: beta_wacc_v2 (§2.1) ------------------------------------------------
for _c, _d, _mn, _mx in [
    ("beta_unlevered_sector", "Damodaran 94-industry unlevered beta mapped via GICS L2 (versioned YAML, SHA-256)", 0.0, 3.0),
    ("beta_levered_v2", "Hamada-relevered firm beta (net debt + cash term); financials carve-out", 0.0, 5.0),
    ("d_e_market_net", "Max(net debt, 0)/market cap; financials carve-out cap at sector P95", 0.0, 20.0),
    ("rho_market_60m", "rolling 60-mo correlation with SPY (as-of T-1)", -1.0, 1.0),
    ("total_beta", "beta_L/max(rho, rho_floor); sigmoid ceiling 5.0 at transition 3.0 (frozen)", 0.0, 5.0),
    ("credit_spread_bps", "spread from ratings table (bps; full 14-tier, D=1900)", 0.0, 2000.0),
    ("kd_pre_tax_v2", "R_f + spread (book-debt approximation flagged)", 0.0, 0.50),
    ("tax_rate_applied", "tau_eff if 0<=tau_eff<=0.35 else 0.21 (frozen)", 0.0, 0.35),
]:
    COLUMN_SPECS[_c] = _spec(_c, "float", "beta_wacc_v2", _d, min=_mn, max=_mx)

COLUMN_SPECS["beta_source"] = _spec("beta_source", "str", "beta_wacc_v2",
                                    "fallback chain source (Appendix B; drives 100% coverage gate)",
                                    enum=BETA_SOURCES)
COLUMN_SPECS["synthetic_rating"] = _spec("synthetic_rating", "str", "beta_wacc_v2",
                                         "synthetic credit rating from ICR lookup / distressed branch",
                                         enum=SYNTHETIC_RATINGS)
COLUMN_SPECS["debt_mv_source"] = _spec("debt_mv_source", "str", "beta_wacc_v2",
                                       "market-value debt approximation flag", enum=DEBT_MV_SOURCES)

# --- V3: narrative_v2 (§2.1) ------------------------------------------------
for _c, _d, _mn, _mx in [
    ("p_survival", "survival probability, clamped [0.05, 0.95]; priority cascade §5.7", 0.0, 1.0),
    ("liquidation_value_ps", "tangible assets - claims + net cash, per share (no double-count)", 0.0, None),
    ("survival_adjusted_iv", "p*IV_going_concern + (1-p)*liquidation; must be <= going-concern IV*1.0001", 0.0, None),
    ("sc_ratio_used", "industry S/C or company 3-yr median; P95 clamp (>0)", 1e-6, 10.0),
    ("reinvestment_rate_v2", "(dRev/S_C)/EBIT(1-tau) — corrected identity basis §5.7", 0.0, None),
    ("reinvestment_gap_pct", "(Required - Available)/Available; negative = funded", None, None),
    ("terminal_growth_haircut", "0.0 or 0.50 on unfunded flag", 0.0, 0.50),
    ("wacc_penalty_bps_applied", "0 or 200 on unfunded flag (must be logged, not inferred)", 0.0, 200.0),
]:
    COLUMN_SPECS[_c] = _spec(_c, "float", "narrative_v2", _d, min=_mn, max=_mx)

_COLUMN_SPECS_ = COLUMN_SPECS  # (no-op alias for lint readability)
COLUMN_SPECS["unfunded_growth_flag"] = _spec(
    "unfunded_growth_flag", "bool", "narrative_v2",
    "Required_Reinvestment > Available_Funding x 1.2")

# --- V3: data_gate_v2 (§2.1) -----------------------------------------------
COLUMN_SPECS["data_gate_quarters"] = _spec(
    "data_gate_quarters", "int", "data_gate_v2",
    "count of PIT SEC filings with filed_date <= as-of", min=0)
COLUMN_SPECS["data_gate_state"] = _spec(
    "data_gate_state", "str", "data_gate_v2",
    "QUARANTINE (<8q) / PROBATION (8-11q) / FULL (>=12q)", enum=DATA_GATE_STATES)
COLUMN_SPECS["max_weight_cap"] = _spec(
    "max_weight_cap", "float", "data_gate_v2",
    "0.005 / 0.010 / unconstrained", min=0.0, max=1.0)
COLUMN_SPECS["mad_filter_applied"] = _spec(
    "mad_filter_applied", "bool", "data_gate_v2",
    "True if any alt-data metric replaced by Roll_Median_90d this cycle")
COLUMN_SPECS["mad_outlier_cycles"] = _spec(
    "mad_outlier_cycles", "int", "data_gate_v2",
    "consecutive cycles flagged (enables '2 consecutive cycles' audit)", min=0)

# --- V3: regime policy-state (§2.1) ----------------------------------------
for _c, _d in [
    ("hmm_prob_shock", "filtered P(SHOCK | obs<=nowcast_date)"),
    ("hmm_prob_recovery", "filtered P(RECOVERY | obs<=nowcast_date)"),
]:
    COLUMN_SPECS[_c] = _spec(_c, "float", "regime_policy", _d, min=0.0, max=1.0)
COLUMN_SPECS["policy_state"] = _spec(
    "policy_state", "str", "regime_policy",
    "4-label policy state (derivable at nowcast time only)", enum=POLICY_STATES)


# ----------------------------------------------------------------------------
# Config loading (frozen pre-registration)
# ----------------------------------------------------------------------------

def load_pre_registration_config(path: str = CONFIG_PATH) -> Dict[str, Any]:
    """Loads the FROZEN pre-registration config (never modified)."""
    import yaml
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"pre-registration config not found: {path}")
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return cfg


def regime_target_iv_mult(regime: str, config: Optional[Dict[str, Any]] = None) -> float:
    """Returns the frozen regime target IV multiple for a regime string."""
    cfg = config if config is not None else load_pre_registration_config()
    targets = cfg.get("phase8", {}).get("regime_thresholds", {})
    return float(targets.get(regime, {}).get("target_multiple", targets.get("EXPANSION", {}).get("target_multiple", 1.20)))


# ----------------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------------

def validate_schema(df: pd.DataFrame) -> List[str]:
    """
    Structural + PIT validation of a valuation frame against the canonical
    schema. Returns a list of violation strings (empty = clean).

    Checks:
      1. All canonical columns present (superset allowed).
      2. Core keys non-null; filed_date never after fiscal_end_date + lookahead
         guard for as-of columns.
      3. Column-level constraints (enums, bounds, bools).
      4. PIT ordering: hmm_reestimate_date <= hmm_nowcast_date <= filed_date.
      5. Phase 8 invariants (§2.1):
         - beta_source NOT NULL when beta_levered_v2 NOT NULL
         - total_beta non-null -> rho_market_60m non-null
         - data_gate_state 'FULL' -> quarters >= 12; 'QUARANTINE' -> cap 0.005
         - reinvestment_rate_v2 >= 0; sc_ratio_used > 0
         - 4-label probabilities in [0,1], sum ~1 when all present
         - survival_adjusted_iv <= going_concern_iv*1.0001 when the
           going-concern IV column is also present (WS-6 producer supplies it)
    """
    violations: List[str] = []

    missing = [c for c in ALL_COLUMNS if c not in df.columns]
    if missing:
        violations.append(f"missing columns: {missing}")

    # Core key null checks
    if "ticker" in df.columns and df["ticker"].isnull().any():
        violations.append("null values in key column 'ticker'")

    date_cols = [c for c in ("fiscal_end_date", "filed_date", "hmm_nowcast_date",
                             "hmm_reestimate_date") if c in df.columns]
    for c in date_cols:
        if df[c].isnull().any():
            violations.append(f"null values in date column '{c}'")

    if "fiscal_end_date" in df.columns and "filed_date" in df.columns:
        bad = (pd.to_datetime(df["filed_date"]) < pd.to_datetime(df["fiscal_end_date"])).sum()
        if bad:
            violations.append(f"{bad} rows have filed_date < fiscal_end_date")

    # Column-level constraints
    for c in ALL_COLUMNS:
        spec = COLUMN_SPECS.get(c)
        if spec is None or c not in df.columns:
            continue
        col = df[c]
        if spec.dtype == "str" and spec.enum is not None:
            non_null = col.dropna().astype(str)
            bad = [v for v in non_null if v not in spec.enum]
            if bad:
                violations.append(f"column '{c}': values {bad[:5]} not in enum {spec.enum}")
        if spec.dtype == "bool":
            bad = col.dropna()[~col.dropna().isin([True, False, 0, 1, np.bool_(True), np.bool_(False)])]
            if len(bad):
                violations.append(f"column '{c}': non-bool values present")
            continue
        if (spec.min is not None or spec.max is not None) and spec.dtype in ("float", "int"):
            num = pd.to_numeric(col, errors="coerce").dropna()
            if spec.min is not None and (num < spec.min).any():
                violations.append(f"column '{c}': values below min {spec.min}")
            if spec.max is not None and (num > spec.max).any():
                violations.append(f"column '{c}': values above max {spec.max}")

    # PIT ordering of HMM stamps
    if all(c in df.columns for c in ("hmm_reestimate_date", "hmm_nowcast_date", "filed_date")):
        re = pd.to_datetime(df["hmm_reestimate_date"])
        nc = pd.to_datetime(df["hmm_nowcast_date"])
        fd = pd.to_datetime(df["filed_date"])
        if (re > nc).any():
            violations.append("PIT violation: hmm_reestimate_date > hmm_nowcast_date "
                              "(params cannot be known before the filter step)")
        if (nc > fd).any():
            violations.append("PIT violation: hmm_nowcast_date > filed_date "
                              "(nowcast must be knowable at filing time)")

    # Probabilities sum to ~1 (2-label legacy rows only; 4-label rows are
    # checked below once all four labels are present)
    if ("hmm_prob_expansion" in df.columns and "hmm_prob_crisis" in df.columns
            and "hmm_prob_shock" in df.columns and "hmm_prob_recovery" in df.columns):
        # mixed frame: only rows where shock+recovery are both null are
        # legacy 2-label rows and must satisfy expansion+crisis == 1
        two_label_rows = df["hmm_prob_shock"].isna() & df["hmm_prob_recovery"].isna()
        if two_label_rows.any():
            total = (df.loc[two_label_rows, "hmm_prob_expansion"]
                     + df.loc[two_label_rows, "hmm_prob_crisis"]).dropna()
            bad = (total - 1.0).abs() > 1e-6
            if bad.any():
                violations.append("hmm probabilities do not sum to 1 (2-label rows)")
    elif "hmm_prob_expansion" in df.columns and "hmm_prob_crisis" in df.columns:
        total = (df["hmm_prob_expansion"] + df["hmm_prob_crisis"]).dropna()
        bad = (total - 1.0).abs() > 1e-6
        if bad.any():
            violations.append("hmm probabilities do not sum to 1")

    # Regime JSON blob parseable + keys complete
    if "regime_nowcast_probs" in df.columns:
        for i, v in enumerate(df["regime_nowcast_probs"].dropna()):
            if not isinstance(v, str):
                violations.append(f"regime_nowcast_probs row {i}: expected JSON string")
                break
            try:
                blob = json.loads(v)
                if not {"expansion", "crisis"} <= set(blob):
                    violations.append(f"regime_nowcast_probs row {i}: missing expansion/crisis keys")
                    break
            except json.JSONDecodeError as exc:
                violations.append(f"regime_nowcast_probs row {i}: invalid JSON ({exc})")
                break

    # --- Phase 8 §2.1 invariants ------------------------------------------
    if {"beta_levered_v2", "beta_source"}.issubset(df.columns):
        n_lev = df["beta_levered_v2"].notna()
        n_src = df["beta_source"].notna()
        bad = (n_lev & ~n_src).sum()
        if bad:
            violations.append(f"{bad} rows have beta_levered_v2 without beta_source "
                              "(coverage gate violation)")

    if {"total_beta", "rho_market_60m"}.issubset(df.columns):
        bad = (df["total_beta"].notna() & df["rho_market_60m"].isna()).sum()
        if bad:
            violations.append(f"{bad} rows have total_beta but null rho_market_60m")

    if {"data_gate_state", "data_gate_quarters", "max_weight_cap"}.issubset(df.columns):
        full = df["data_gate_state"] == "FULL"
        q = pd.to_numeric(df["data_gate_quarters"], errors="coerce")
        bad = (full & (q < 12)).sum()
        if bad:
            violations.append(f"{bad} rows data_gate_state=FULL with quarters<12")
        quarantine = df["data_gate_state"] == "QUARANTINE"
        cap = pd.to_numeric(df["max_weight_cap"], errors="coerce")
        bad_q = (quarantine & ~np.isclose(cap, 0.005)).sum()
        if bad_q:
            violations.append(f"{bad_q} rows data_gate_state=QUARANTINE with max_weight_cap != 0.005")

    if "reinvestment_rate_v2" in df.columns:
        bad = (pd.to_numeric(df["reinvestment_rate_v2"], errors="coerce") < 0).sum()
        if bad:
            violations.append(f"{bad} rows have negative reinvestment_rate_v2")

    if "sc_ratio_used" in df.columns:
        bad = (pd.to_numeric(df["sc_ratio_used"], errors="coerce") <= 0).sum()
        if bad:
            violations.append(f"{bad} rows have sc_ratio_used <= 0 (P95 clamp violated)")

    # 4-label regime probabilities
    four = [c for c in ("hmm_prob_expansion", "hmm_prob_crisis",
                        "hmm_prob_shock", "hmm_prob_recovery") if c in df.columns]
    if len(four) == 4:
        total = df[four].sum(axis=1).dropna()
        bad = (total - 1.0).abs() > 1e-3
        if bad.any():
            violations.append(f"{bad.sum()} rows: 4-label hmm probabilities do not sum to ~1")

    if "policy_state" in df.columns:
        non_null = df["policy_state"].dropna().astype(str)
        bad = [v for v in non_null if v not in POLICY_STATES]
        if bad:
            violations.append(f"policy_state: values {bad[:5]} not in {POLICY_STATES}")

    # survival_adjusted_iv <= going-concern IV (when producer supplies basis)
    if {"survival_adjusted_iv", "going_concern_iv"}.issubset(df.columns):
        s = pd.to_numeric(df["survival_adjusted_iv"], errors="coerce")
        g = pd.to_numeric(df["going_concern_iv"], errors="coerce")
        mask = df["survival_adjusted_iv"].notna() & df["going_concern_iv"].notna()
        bad = (s[mask] > g[mask] * 1.0001).sum()
        if bad:
            violations.append(f"{bad} rows: survival_adjusted_iv > going_concern_iv*1.0001")

    return violations


def validate_wacc_panel(df: pd.DataFrame) -> List[str]:
    """
    Phase 8 §2.2 daily WACC panel validation.

    Checks:
      1. All 15 canonical panel columns present.
      2. PK (asof_date, ticker) uniqueness.
      3. No lookahead: filed_date_ref <= asof_date (basis filing precedes
         trade date); asof_date <= retrieval_ts date (panel never written
         with a future as-of).
      4. ke_v2 / wacc_v2 in [0, 0.5]; iv_nowcast non-negative and >= 0.01
         floor; erp_blend in [0.005, 0.08] (frozen erp_bounds).
      5. beta_source non-null (coverage gate support).
    """
    violations: List[str] = []
    missing = [c for c in WACC_PANEL_COLUMNS if c not in df.columns]
    if missing:
        violations.append(f"missing wacc panel columns: {missing}")
        return violations

    if df.empty:
        return violations

    if df.duplicated(subset=["asof_date", "ticker"]).any():
        violations.append("duplicate (asof_date, ticker) rows — PK violated")

    asof = pd.to_datetime(df["asof_date"], errors="coerce")
    filed_ref = pd.to_datetime(df["filed_date_ref"], errors="coerce")
    retr = pd.to_datetime(df["retrieval_ts"], errors="coerce")
    m = filed_ref.notna()
    bad = (filed_ref[m] > asof[m]).sum()
    if bad:
        violations.append(f"{bad} rows: filed_date_ref > asof_date (lookahead)")
    m2 = retr.notna()
    # compare calendar dates only; tolerate tz-aware retrieval_ts (UTC) by
    # stripping tz before normalizing, so naive-asof vs aware-retrieval works
    asof_d = asof.dt.normalize()
    retr_d = pd.to_datetime(retr[m2]).dt.tz_localize(None).dt.normalize()
    bad2 = (asof_d[m2] > retr_d).sum()
    if bad2:
        violations.append(f"{bad2} rows: asof_date after retrieval_ts date")

    for c in ("ke_v2", "wacc_v2"):
        num = pd.to_numeric(df[c], errors="coerce").dropna()
        if (num < 0.0).any() or (num > 0.5).any():
            violations.append(f"column '{c}': values outside [0, 0.5]")

    iv = pd.to_numeric(df["iv_nowcast"], errors="coerce").dropna()
    if (iv < 0.01).any():
        violations.append("iv_nowcast below 0.01 floor")

    erp = pd.to_numeric(df["erp_blend"], errors="coerce").dropna()
    if (erp < 0.005).any() or (erp > 0.08).any():
        violations.append("erp_blend outside frozen erp_bounds [0.005, 0.08]")

    if "beta_source" in df.columns and df["beta_source"].isnull().any():
        violations.append("null beta_source in wacc panel (coverage gate violation)")

    return violations


# ----------------------------------------------------------------------------
# Empty frames
# ----------------------------------------------------------------------------

def empty_valuation_frame(n: int = 0) -> pd.DataFrame:
    """Creates an empty (n-row) canonical valuation frame with correct dtypes."""
    df = pd.DataFrame(index=range(n))
    for c in ALL_COLUMNS:
        spec = COLUMN_SPECS[c]
        if spec.dtype == "str" or spec.dtype == "json_text":
            df[c] = pd.Series([None] * n, dtype="object")
        elif spec.dtype == "bool":
            df[c] = pd.Series([False] * n, dtype="bool")
        elif spec.dtype == "date":
            df[c] = pd.Series([pd.NaT] * n, dtype="datetime64[ns]")
        elif spec.dtype == "int":
            # valid PIT defaults per column (producers overwrite with real values)
            if c in ("data_gate_quarters", "mad_outlier_cycles"):
                df[c] = pd.Series([0] * n, dtype="int64")
            elif c == "sector_tau":
                # sector_tau has spec min=1; 0 is invalid, so leave unset
                # (float NaN) until the multifractal producer fills it.
                df[c] = pd.Series([np.nan] * n, dtype="float64")
            else:
                df[c] = pd.Series([0] * n, dtype="Int64" if c == "hmm_state" else "int64")
        else:
            df[c] = pd.Series([np.nan] * n, dtype="float64")
    return df


def empty_wacc_panel_frame(n: int = 0) -> pd.DataFrame:
    """Creates an empty (n-row) canonical daily WACC panel frame (§2.2)."""
    df = pd.DataFrame(index=range(n))
    df["asof_date"] = pd.Series([pd.NaT] * n, dtype="datetime64[ns]")
    df["ticker"] = pd.Series([None] * n, dtype="object")
    df["filed_date_ref"] = pd.Series([pd.NaT] * n, dtype="datetime64[ns]")
    for c in ("rf_10y", "erp_damodaran", "erp_option", "erp_blend", "ke_v2",
              "kd_pre_tax_v2", "wacc_v2", "wacc_baseline", "duration_wacc",
              "iv_nowcast"):
        df[c] = pd.Series([np.nan] * n, dtype="float64")
    df["beta_source"] = pd.Series([None] * n, dtype="object")
    df["retrieval_ts"] = pd.Series([pd.NaT] * n, dtype="datetime64[us]")
    return df


# ----------------------------------------------------------------------------
# Walk-forward backtest artifact schema (Phase 4, CEO Ruling B-20260920-001)
# ----------------------------------------------------------------------------
# Contract for `data/backtests/walkforward_hybrid_fractal_v1.parquet` — the
# consolidated fold artifact the Phase-4 16-gate engine consumes.  Returns-grain
# (one row per fold weekly bar), enriched where the fold artifacts provide it.
# Enrichment columns (fractal_vol_tau*, qcs_score, omega_stress) are honest-null
# unless the producer fold artifacts carry them — the schema NEVER fabricates.

WALKFORWARD_SCHEMA_VERSION = "1.0"
WALKFORWARD_DEFAULT_OUT = "data/backtests/walkforward_hybrid_fractal_v1.parquet"

WALKFORWARD_FOLD_RANGE = (1, 4)          # pre-registered 4-fold walk-forward
WALKFORWARD_REGIME_STATES = ["EXPANSION", "SHOCK", "CRISIS", "RECOVERY"]

WALKFORWARD_COLUMNS: List[str] = [
    # core identity
    "date",                 # DATE  — weekly bar obs date (PIT anchor)
    "stratum",              # VARCHAR — Fold_1..Fold_4 arm-C stratum label
    "fold",                 # INTEGER — walkforward_fold 1..4 (independent variable)
    # portfolio state (returns.parquet)
    "portfolio_value",      # DOUBLE
    "cash",                 # DOUBLE
    "equity_value",         # DOUBLE
    "bil_value",            # DOUBLE
    "gld_value",            # DOUBLE
    "period_return",        # DOUBLE — weekly arm-C period return
    "cum_return",           # DOUBLE
    "drawdown",             # DOUBLE — depth, negative
    "n_positions",          # INTEGER
    # regime enrichment (regime_states.parquet -> detected_state)
    "regime_state",         # VARCHAR — EXPANSION / SHOCK / CRISIS / RECOVERY
    # walk-forward diagnostic columns (dispatch contract §Gate registration)
    "fractal_vol_tau5",     # DOUBLE — HAR-|r| 5d (null unless producer supplies)
    "fractal_vol_tau10",    # DOUBLE — HAR-|r| 10d
    "fractal_vol_tau20",    # DOUBLE — HAR-|r| 20d
    "swap_barrier_level",   # DOUBLE — bps barrier (null unless swap_barriers exists)
    "exit_reason",          # VARCHAR — last exit trigger in the bar (null = none)
    "tca_cost_bps",         # DOUBLE — realized TCA on the bar (null unless tca_costs)
    "qcs_score",            # DOUBLE — quality composite 0..1 (null unless present)
    "omega_stress",         # DOUBLE — omega stress multiplier 1..3 (null unless present)
]

# Walk-forward diagnostic column specs — SEPARATE dict (the canonical
# COLUMN_SPECS stays 107 = N_TOTAL for the valuation frame; walk-forward
# artifacts live in their own consolidated parquet and are NOT part of the
# filing-row contract).
WALKFORWARD_SPECS: Dict[str, ColumnSpec] = {
    "walkforward_fold": _spec(
        "walkforward_fold", "int", "walkforward",
        "walk-forward fold index 1..4 (independent variable)", min=1, max=4, required=False),
    "stratum": _spec("stratum", "str", "walkforward", "arm-C stratum label", required=False),
    "regime_state": _spec(
        "regime_state", "str", "walkforward", "persisted HMM regime state",
        enum=WALKFORWARD_REGIME_STATES, required=False),
    "exit_reason": _spec(
        "exit_reason", "str", "walkforward",
        "last trade reason in the bar (raw producer reason preserved — e.g. "
        "swap_barrier_breached_*, macro_shield_crisis_exit, roic_decay); "
        "documented as free-form to avoid fabricating canonical labels",
        required=False),
}
for _c, _mn, _mx in [
    ("fractal_vol_tau5", 0.0, None),
    ("fractal_vol_tau10", 0.0, None),
    ("fractal_vol_tau20", 0.0, None),
    ("swap_barrier_level", 0.0, None),
    ("tca_cost_bps", 0.0, None),
    ("qcs_score", 0.0, 1.0),
    ("omega_stress", 1.0, 3.0),
]:
    WALKFORWARD_SPECS[_c] = _spec(
        _c, "float", "walkforward", f"walk-forward {_c}", min=_mn, max=_mx, required=False)


def validate_walkforward(df: pd.DataFrame) -> List[str]:
    """
    Structural + PIT validation of a consolidated walk-forward frame
    (`walkforward_hybrid_fractal_v1.parquet`).

    Checks:
      1. Required returns-grain columns present (superset allowed).
      2. `fold` in 1..4 (pre-registered walkforward_folds).
      3. `regime_state` in enum when present; `exit_reason` in enum.
      4. Bounds: qcs_score in [0,1], omega_stress in [1,3], vol/barrier/tca >= 0.
      5. No forward-fill markers: period_return never null on the arm-C path.
      6. PIT: date is the bar anchor; no date beyond the declared end.
    """
    violations: List[str] = []
    required = [c for c in WALKFORWARD_COLUMNS
                if c not in ("fractal_vol_tau5", "fractal_vol_tau10",
                             "fractal_vol_tau20", "swap_barrier_level",
                             "exit_reason", "tca_cost_bps", "qcs_score",
                             "omega_stress")]
    missing = [c for c in required if c not in df.columns]
    if missing:
        violations.append(f"missing walk-forward columns: {missing}")
        return violations

    if df.empty:
        return violations

    if "fold" in df.columns:
        f = pd.to_numeric(df["fold"], errors="coerce").dropna()
        if (f < WALKFORWARD_FOLD_RANGE[0]).any() or (f > WALKFORWARD_FOLD_RANGE[1]).any():
            violations.append(f"fold outside {WALKFORWARD_FOLD_RANGE}")

    if "regime_state" in df.columns:
        non_null = df["regime_state"].dropna().astype(str)
        bad = [v for v in non_null if v not in WALKFORWARD_REGIME_STATES]
        if bad:
            violations.append(f"regime_state: values {bad[:5]} not in {WALKFORWARD_REGIME_STATES}")

    if "exit_reason" in df.columns:
        non_null = df["exit_reason"].dropna().astype(str)
        bad = [v for v in non_null if not str(v).strip()]
        if bad:
            violations.append("exit_reason: empty-string raw reasons present "
                              "(producer must log a reason or leave null)")

    for _c, _mn, _mx in [("fractal_vol_tau5", 0.0, None),
                         ("fractal_vol_tau10", 0.0, None),
                         ("fractal_vol_tau20", 0.0, None),
                         ("swap_barrier_level", 0.0, None),
                         ("tca_cost_bps", 0.0, None),
                         ("qcs_score", 0.0, 1.0),
                         ("omega_stress", 1.0, 3.0)]:
        if _c not in df.columns:
            continue
        num = pd.to_numeric(df[_c], errors="coerce").dropna()
        if _mn is not None and (num < _mn).any():
            violations.append(f"column '{_c}': values below min {_mn}")
        if _mx is not None and (num > _mx).any():
            violations.append(f"column '{_c}': values above max {_mx}")

    if "period_return" in df.columns and df["period_return"].isnull().any():
        violations.append("null values in period_return (must be complete on arm-C path)")
    return violations


def empty_walkforward_frame(n: int = 0) -> pd.DataFrame:
    """Empty (n-row) consolidated walk-forward frame with correct dtypes."""
    df = pd.DataFrame(index=range(n))
    df["date"] = pd.Series([pd.NaT] * n, dtype="datetime64[ns]")
    df["stratum"] = pd.Series([None] * n, dtype="object")
    df["fold"] = pd.Series([0] * n, dtype="int64")
    for c in ("portfolio_value", "cash", "equity_value", "bil_value", "gld_value",
              "period_return", "cum_return", "drawdown"):
        df[c] = pd.Series([np.nan] * n, dtype="float64")
    df["n_positions"] = pd.Series([0] * n, dtype="int64")
    df["regime_state"] = pd.Series([None] * n, dtype="object")
    for c in ("fractal_vol_tau5", "fractal_vol_tau10", "fractal_vol_tau20",
              "swap_barrier_level", "tca_cost_bps", "qcs_score", "omega_stress"):
        df[c] = pd.Series([np.nan] * n, dtype="float64")
    df["exit_reason"] = pd.Series([None] * n, dtype="object")
    return df


def build_walkforward_parquet(fold_dir: str, fold: int, out_path: Optional[str] = None,
                              write_sidecar: bool = True) -> pd.DataFrame:
    """
    Builds the consolidated returns-grain walk-forward frame for ONE fold dir
    (`20260919-{strategy}-fold{K}`) and appends it to the consolidated parquet.

    Joins, at the weekly-bar grain:
      * returns.parquet      -> portfolio state + period_return (primary)
      * regime_states.parquet-> detected_state -> regime_state (same-date)
      * trades.parquet       -> last exit_reason within the bar (best-effort)
      * swap_barriers/tca_costs -> honest null (v1 artifacts carry no data)

    Returns the fold frame; when `out_path` is given, appends to that parquet
    (creating it and the `.provenance.json` sidecar on first fold) and returns
    the frame.
    """
    from pathlib import Path as _P
    root = _P(fold_dir)
    rp = root / "returns.parquet"
    if not rp.exists():
        rp = root / (root.name + ".returns.parquet")
    if not rp.exists():
        raise FileNotFoundError(f"fold returns artifact missing: {rp}")

    ret = pd.read_parquet(rp)
    fp_ = _P(fold_dir)
    suffix = "" if fp_.name.startswith(root.name) else root.name

    ret["date"] = pd.to_datetime(ret["date"])
    ret = ret.sort_values("date").reset_index(drop=True)
    ret["fold"] = int(fold)
    if "stratum" not in ret.columns:
        ret["stratum"] = f"Fold_{fold}"
    if "period_return" not in ret.columns:
        ret["period_return"] = np.nan

    out = pd.DataFrame()
    for c in WALKFORWARD_COLUMNS:
        if c in ret.columns and c not in ("regime_state", "exit_reason", "tca_cost_bps"):
            out[c] = ret[c]
        else:
            out[c] = np.nan if c in ("portfolio_value", "cash", "equity_value",
                                     "bil_value", "gld_value", "cum_return",
                                     "drawdown", "fractal_vol_tau5",
                                     "fractal_vol_tau10", "fractal_vol_tau20",
                                     "swap_barrier_level", "tca_cost_bps",
                                     "qcs_score", "omega_stress") else None

    # fold / identity columns
    out["fold"] = ret["fold"]
    out["date"] = ret["date"]
    if "stratum" in ret.columns:
        out["stratum"] = ret["stratum"]
    out["period_return"] = ret["period_return"]
    for c in ("portfolio_value", "cash", "equity_value", "bil_value", "gld_value",
              "cum_return", "drawdown", "n_positions"):
        if c in ret.columns:
            out[c] = ret[c]
    if "n_positions" not in ret.columns:
        out["n_positions"] = 0

    # regime enrichment (same-date join)
    rsp = root / "regime_states.parquet"
    if rsp.exists():
        rs = pd.read_parquet(rsp)
        rs["obs_date"] = pd.to_datetime(rs["obs_date"])
        rs = rs[["obs_date", "detected_state"]].rename(
            columns={"obs_date": "date", "detected_state": "regime_state"})
        rs["regime_state"] = rs["regime_state"].astype(str).str.upper()
        out = out.drop(columns=["regime_state"])
        out = out.merge(rs, on="date", how="left")
        out["regime_state"] = out["regime_state"].where(
            out["regime_state"].notna(), None)
    # exit/tca enrichment from trades (last action in bar)
    trp = root / "trades.parquet"
    if trp.exists() and pd.read_parquet(trp).shape[1] > 0 and "date" in pd.read_parquet(trp).columns:
        tr = pd.read_parquet(trp)
        if len(tr):
            tr["date"] = pd.to_datetime(tr["date"])
            tr["exit_reason"] = tr.get("reason", pd.Series("hold", index=tr.index)).fillna("hold")
            t_last = (tr.sort_values("date")
                        .drop_duplicates(subset=["date"], keep="last")
                        [["date", "exit_reason"]])
            out = out.drop(columns=["exit_reason"])
            out = out.merge(t_last, on="date", how="left")
            out["exit_reason"] = out["exit_reason"].fillna("none")
        else:
            out["exit_reason"] = "none"
    else:
        out["exit_reason"] = "none"

    # enforce column order + types
    for c in WALKFORWARD_COLUMNS:
        if c not in out.columns:
            out[c] = np.nan if c not in ("stratum", "regime_state", "exit_reason") else None
    out = out[WALKFORWARD_COLUMNS].copy()
    out["date"] = pd.to_datetime(out["date"])
    out["fold"] = pd.to_numeric(out["fold"], errors="coerce").astype("Int64")

    if out_path is not None:
        op = _P(out_path)
        op.parent.mkdir(parents=True, exist_ok=True)
        if op.exists():
            prev = pd.read_parquet(op)
            frame = pd.concat([prev, out], ignore_index=True).drop_duplicates(
                subset=["date", "fold", "stratum"], keep="last")
        else:
            frame = out
        frame = frame.sort_values(["fold", "date"]).reset_index(drop=True)
        frame.to_parquet(op, engine="pyarrow", index=False)
        if write_sidecar:
            _write_walkforward_sidecar(op, frame)
        return out
    return out


def _write_walkforward_sidecar(op: Path, frame: pd.DataFrame) -> None:
    """Writes the §8-provenance sidecar (sha256, row_count, date_range)."""
    from pathlib import Path as _Path
    import hashlib as _hl
    from datetime import datetime as _dt, timezone as _tz
    digest = _hl.sha256(op.read_bytes()).hexdigest()
    dates = pd.to_datetime(frame["date"]).dropna()
    prov = {
        "source": "ingestion/valuation_schema.build_walkforward_parquet",
        "retrieval_timestamp": _dt.now(_tz.utc).isoformat(),
        "sha256": digest,
        "row_count": int(len(frame)),
        "date_range": {"min": str(dates.min().date()), "max": str(dates.max().date())}
        if len(dates) else None,
        "schema_version": WALKFORWARD_SCHEMA_VERSION,
        "pit_timestamp_column": "date",
    }
    _Path(str(op) + ".provenance.json").write_text(
        json.dumps(prov, indent=2, sort_keys=True, default=str), encoding="utf-8")


def build_hmm_filter_row(ticker: str, as_of: pd.Timestamp, filed_date: pd.Timestamp,
                         prob_crisis: float, reestimate_date: pd.Timestamp,
                         prob_shock: float = 0.0, prob_recovery: float = 0.0,
                         policy_state: Optional[str] = None) -> Dict[str, Any]:
    """Builds one canonical PIT-conformant HMM/regime row (helper for producers).

    Phase 8 extension: accepts 4-label inputs (shock/recovery/policy_state);
    when shock/recovery are 0 and policy_state is None the row stays in the
    legacy 2-label form (fields left as None).  hmm_state derives from the
    largest posterior among the available labels.
    """
    prob_crisis = float(np.clip(prob_crisis, 0.0, 1.0))
    prob_shock = float(np.clip(prob_shock, 0.0, 1.0)) if prob_shock is not None else 0.0
    prob_recovery = float(np.clip(prob_recovery, 0.0, 1.0)) if prob_recovery is not None else 0.0

    # normalize four-label probs to sum ~1 when any non-2-label prob is set
    if prob_shock > 0.0 or prob_recovery > 0.0:
        total = 1.0 - prob_crisis + prob_crisis + prob_shock + prob_recovery  # = 1 + shock + recovery
        # keep expansion implied: pe + pc + ps + pr = 1
        pe = 1.0 - prob_crisis - prob_shock - prob_recovery
        probs = {"expansion": round(max(0.0, pe), 8), "crisis": round(prob_crisis, 8),
                 "shock": round(prob_shock, 8), "recovery": round(prob_recovery, 8)}
    else:
        probs = {"expansion": round(1.0 - prob_crisis, 8), "crisis": round(prob_crisis, 8)}

    if policy_state is not None:
        assert policy_state in POLICY_STATES, policy_state
    else:
        policy_state = max(probs, key=probs.get)
        policy_state = "EXPANSION" if policy_state == "expansion" else \
            ("CRISIS" if policy_state == "crisis" else
             ("SHOCK" if policy_state == "shock" else "RECOVERY"))

    row = {
        "ticker": ticker,
        "filed_date": pd.Timestamp(filed_date),
        "hmm_state": int(prob_crisis > 0.5),
        "hmm_prob_expansion": probs["expansion"],
        "hmm_prob_crisis": probs["crisis"],
        "hmm_reestimate_date": pd.Timestamp(reestimate_date),
        "hmm_nowcast_date": pd.Timestamp(as_of),
        "regime_nowcast_probs": json.dumps(probs),
        "regime_target_iv_mult": regime_target_iv_mult(policy_state),
    }
    if prob_shock > 0.0 or prob_recovery > 0.0:
        row["hmm_prob_shock"] = probs["shock"]
        row["hmm_prob_recovery"] = probs["recovery"]
    row["policy_state"] = policy_state
    return row


# ----------------------------------------------------------------------------
# CLI / self-test
# ----------------------------------------------------------------------------

def _test_schema() -> int:
    print("ValuationSchema self-test")
    print("=" * 60)
    print(f"columns: baseline={N_BASELINE} added_v2={N_ADDED_V2} "
          f"added_v3={N_ADDED_V3} total={N_TOTAL}")
    assert N_BASELINE == 57, N_BASELINE
    assert N_ADDED_V2 == 22, N_ADDED_V2
    assert N_ADDED_V3 == 28, N_ADDED_V3
    assert N_TOTAL == 107, N_TOTAL
    assert len(COLUMN_SPECS) == N_TOTAL, len(COLUMN_SPECS)
    assert len(set(ALL_COLUMNS)) == N_TOTAL, "duplicate column names"
    assert len(set(BETA_WACC_V2_COLUMNS)) == 11
    assert len(set(NARRATIVE_V2_COLUMNS)) == 9
    assert len(set(DATA_GATE_V2_COLUMNS)) == 5
    assert len(set(REGIME_POLICY_COLUMNS)) == 3
    print("[OK] 57 + 22 + 28 = 107 columns, no duplicates, all spec'd")

    # valid frame passes
    df = empty_valuation_frame(2)
    df["ticker"] = ["AAPL", "MSFT"]
    df["fiscal_end_date"] = pd.to_datetime(["2024-09-28", "2024-06-30"])
    df["filed_date"] = pd.to_datetime(["2024-11-01", "2024-07-30"])
    df["hmm_reestimate_date"] = pd.to_datetime(["2024-10-01", "2024-06-30"])
    df["hmm_nowcast_date"] = pd.to_datetime(["2024-10-31", "2024-07-29"])
    df["hmm_state"] = [0, 1]
    df["hmm_prob_expansion"] = [0.62, 0.13]
    df["hmm_prob_crisis"] = [0.28, 0.77]
    df["hmm_prob_shock"] = [0.06, 0.05]
    df["hmm_prob_recovery"] = [0.04, 0.05]
    df["policy_state"] = ["EXPANSION", "CRISIS"]
    df["regime_nowcast_probs"] = [
        '{"expansion": 0.62, "crisis": 0.28, "shock": 0.06, "recovery": 0.04}',
        '{"expansion": 0.13, "crisis": 0.77, "shock": 0.05, "recovery": 0.05}']
    df["swap_allowed"] = [False, True]
    df["exit_reason"] = [None, "trailing"]
    df["fill_rate"] = [1.0, 0.85]
    df["queue_position"] = [0.0, 0.42]
    # Phase 8 rows
    df["beta_unlevered_sector"] = [1.225, 1.489]
    df["beta_levered_v2"] = [1.05, 1.30]
    df["beta_source"] = ["sector_bottom_up", "sector_bottom_up"]
    df["d_e_market_net"] = [0.0, 0.12]
    df["rho_market_60m"] = [0.62, 0.55]
    df["total_beta"] = [1.05, 1.30]
    df["synthetic_rating"] = ["AAA", "A"]
    df["credit_spread_bps"] = [40.0, 78.0]
    df["kd_pre_tax_v2"] = [0.0430, 0.0528]
    df["tax_rate_applied"] = [0.15, 0.21]
    df["debt_mv_source"] = ["book", "book"]
    df["p_survival"] = [0.95, 0.95]
    df["liquidation_value_ps"] = [8.0, 12.0]
    df["survival_adjusted_iv"] = [118.0, 96.0]
    df["sc_ratio_used"] = [1.2, 1.5]
    df["reinvestment_rate_v2"] = [0.05, 0.08]
    df["unfunded_growth_flag"] = [False, False]
    df["reinvestment_gap_pct"] = [-0.1, 0.2]
    df["terminal_growth_haircut"] = [0.0, 0.0]
    df["wacc_penalty_bps_applied"] = [0.0, 0.0]
    df["data_gate_quarters"] = [24, 14]
    df["data_gate_state"] = ["FULL", "FULL"]
    df["max_weight_cap"] = [1.0, 1.0]
    df["mad_filter_applied"] = [False, False]
    df["mad_outlier_cycles"] = [0, 0]
    df["sector_tau"] = [252, 252]
    df["sector_k"] = [1.0, 1.0]
    viol = validate_schema(df)
    assert not viol, viol
    print("[OK] canonical populated frame passes all PIT + constraint checks")

    # enum + PIT violations detected
    df_bad = df.copy()
    df_bad.loc[0, "exit_reason"] = "banana"
    df_bad.loc[0, "hmm_nowcast_date"] = pd.to_datetime("2024-12-01")  # > filed_date
    df_bad.loc[0, "beta_source"] = None                                # beta w/o source
    df_bad.loc[0, "fill_rate"] = 1.7
    df_bad.loc[0, "data_gate_state"] = "FULL"
    df_bad.loc[0, "data_gate_quarters"] = 6                            # FULL < 12q
    df_bad.loc[1, "total_beta"] = 1.30
    df_bad.loc[1, "rho_market_60m"] = None                             # tb w/o rho
    viol = validate_schema(df_bad)
    joined = "\n".join(viol)
    assert any("enum" in v for v in viol), joined
    assert any("nowcast" in v for v in viol), joined
    assert any("above max" in v for v in viol), joined
    assert any("beta_source" in v and "coverage" in v for v in viol), joined
    assert any("quarters<12" in v for v in viol), joined
    assert any("total_beta" in v and "rho_market_60m" in v for v in viol), joined
    print("[OK] violations detected: enum, PIT ordering, bounds, §2.1 invariants")

    # wacc panel frame passes + violation detection
    p = empty_wacc_panel_frame(2)
    p["asof_date"] = pd.to_datetime(["2024-10-31", "2024-11-01"])
    p["ticker"] = ["AAPL", "MSFT"]
    p["filed_date_ref"] = pd.to_datetime(["2024-10-28", "2024-07-30"])  # ≤ asof (PIT)
    p["rf_10y"] = [0.042, 0.042]
    p["erp_damodaran"] = [0.0450, 0.0450]
    p["erp_option"] = [0.0310, 0.0310]
    p["erp_blend"] = [0.0408, 0.0408]
    p["ke_v2"] = [0.086, 0.095]
    p["kd_pre_tax_v2"] = [0.0480, 0.0528]
    p["wacc_v2"] = [0.081, 0.089]
    p["beta_source"] = ["sector_bottom_up", "sector_bottom_up"]
    p["wacc_baseline"] = [0.0800, 0.0850]
    p["duration_wacc"] = [8.0, 9.0]
    p["iv_nowcast"] = [112.4, 98.7]
    p["retrieval_ts"] = pd.to_datetime(["2024-11-01T12:00:00Z", "2024-11-02T12:00:00Z"])
    pviol = validate_wacc_panel(p)
    assert not pviol, pviol
    print("[OK] daily wacc_panel_daily frame passes §2.2 validation")

    pb = p.copy()
    pb.loc[0, "filed_date_ref"] = pd.to_datetime("2024-12-01")  # > asof (lookahead)
    pb.loc[1, "wacc_v2"] = 0.75                                  # > 0.5
    pb.loc[1, "beta_source"] = None
    pviol = validate_wacc_panel(pb)
    pj = "\n".join(pviol)
    assert any("lookahead" in v for v in pviol), pj
    assert any("outside [0, 0.5]" in v for v in pviol), pj
    assert any("beta_source" in v for v in pviol), pj
    print("[OK] wacc panel violations detected: lookahead, bounds, coverage")

    # regime target helper against frozen config
    cfg = load_pre_registration_config()
    assert abs(regime_target_iv_mult("EXPANSION", cfg) - 1.20) < 1e-9
    assert abs(regime_target_iv_mult("CRISIS", cfg) - 1.05) < 1e-9
    assert abs(regime_target_iv_mult("SHOCK", cfg) - 1.10) < 1e-9
    assert abs(regime_target_iv_mult("RECOVERY", cfg) - 1.15) < 1e-9
    print("[OK] regime_target_iv_mult reads frozen config (4-label)")

    # build_hmm_filter_row produces a clean row when merged
    row = build_hmm_filter_row("AAPL", pd.Timestamp("2024-10-31"),
                               pd.Timestamp("2024-11-01"), 0.27,
                               pd.Timestamp("2024-10-01"),
                               prob_shock=0.08, prob_recovery=0.05)
    assert row["policy_state"] == "EXPANSION"
    assert abs(row["hmm_prob_expansion"] + row["hmm_prob_crisis"]
               + row["hmm_prob_shock"] + row["hmm_prob_recovery"] - 1.0) < 1e-8
    print("[OK] build_hmm_filter_row 4-label PIT-conformant")

    # DDL is valid SQL fragment (sanity: contains PK + CHECKs)
    assert "PRIMARY KEY (asof_date, ticker)" in WACC_PANEL_DDL_SQL
    assert "CHECK (ke_v2 BETWEEN 0.0 AND 0.5)" in WACC_PANEL_DDL_SQL
    print("[OK] wacc_panel_daily DDL matches §2.2")

    # walk-forward consolidated frame: clean + violation detection
    wf = empty_walkforward_frame(4)
    wf["date"] = pd.to_datetime(["2022-07-01", "2022-07-08", "2022-07-15", "2022-07-22"])
    wf["stratum"] = ["Fold_1"] * 4
    wf["fold"] = [1, 1, 1, 1]
    wf["period_return"] = [0.01, -0.02, 0.015, 0.005]
    wf["portfolio_value"] = [1e6, 1.01e6, 0.99e6, 1.005e6]
    wf["n_positions"] = [40, 40, 41, 41]
    wf["regime_state"] = ["EXPANSION", "EXPANSION", "SHOCK", "SHOCK"]
    wf["qcs_score"] = [0.6, 0.6, 0.65, 0.65]
    wf["omega_stress"] = [1.0, 1.0, 1.2, 1.2]
    wf["exit_reason"] = ["hold", "hold", "trailing", "hold"]
    wviol = validate_walkforward(wf)
    assert not wviol, wviol
    print("[OK] consolidated walk-forward frame passes WALKFORWARD_SCHEMA contract")

    wb = wf.copy()
    wb.loc[0, "fold"] = 9                       # outside 1..4
    wb.loc[1, "regime_state"] = "BANANA"        # bad enum
    wb.loc[2, "qcs_score"] = 1.7                # > 1.0
    wb.loc[3, "omega_stress"] = 4.0             # > 3.0
    wb.loc[0, "exit_reason"] = ""               # empty raw reason
    wb.loc[1, "period_return"] = None
    wviol = validate_walkforward(wb)
    joined = "\n".join(wviol)
    assert any("fold outside" in v for v in wviol), joined
    assert any("regime_state" in v for v in wviol), joined
    assert any("qcs_score" in v for v in wviol), joined
    assert any("omega_stress" in v for v in wviol), joined
    assert any("exit_reason" in v for v in wviol), joined
    assert any("period_return" in v for v in wviol), joined
    print("[OK] walk-forward violations detected: fold range, enums, bounds, completeness")

    print("\nAll ValuationSchema tests passed.")
    return 0


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="Canonical 107-column PIT valuation schema + daily WACC panel (Phase 8, §2.1/§2.2)")
    parser.add_argument("--list", action="store_true", help="Print all columns by group")
    parser.add_argument("--test", action="store_true", help="Run self-test suite")
    parser.add_argument("--check", type=str, default=None, metavar="PARQUET",
                        help="Validate a parquet/csv frame against the schema")
    parser.add_argument("--walkforward-check", type=str, default=None, metavar="PARQUET",
                        help="Validate a consolidated walk-forward frame")
    parser.add_argument("--walkforward-build", action="store_true",
                        help="Build consolidated walk-forward parquet from fold dirs")
    parser.add_argument("--fold-dirs", type=str, default=None, nargs="+",
                        help="Fold dirs for --walkforward-build (default: auto-discover "
                             "data/backtests/20260919-hybrid_fractal_v1-fold{1..4})")
    parser.add_argument("--out", type=str, default=WALKFORWARD_DEFAULT_OUT,
                        help="Output path for --walkforward-build")
    args = parser.parse_args()

    if args.test:
        return _test_schema()
    if args.walkforward_check:
        df = pd.read_parquet(args.walkforward_check)
        viol = validate_walkforward(df)
        if viol:
            print(f"walk-forward schema violations ({len(viol)}):")
            for v in viol:
                print(f"  - {v}")
            return 1
        print(f"[OK] {args.walkforward_check}: walk-forward schema clean "
              f"({len(df)} rows, folds {sorted(df['fold'].dropna().unique().tolist())})")
        return 0
    if args.walkforward_build:
        from pathlib import Path as _P
        if args.fold_dirs:
            dirs = [str(_P(d)) for d in args.fold_dirs]
        else:
            base = _P("data/backtests")
            dirs = [str(base / f"20260919-hybrid_fractal_v1-fold{k}") for k in range(1, 5)]
        parts = []
        for k, d in enumerate(dirs, start=1):
            if _P(d).exists():
                parts.append(build_walkforward_parquet(d, k))
            else:
                print(f"[warn] fold dir missing: {d}")
        if not parts:
            print("no fold dirs found; nothing to build")
            return 1
        frame = pd.concat(parts, ignore_index=True)
        op = _P(args.out)
        op.parent.mkdir(parents=True, exist_ok=True)
        frame = frame.drop_duplicates(subset=["date", "fold", "stratum"], keep="last")
        frame = frame.sort_values(["fold", "date"]).reset_index(drop=True)
        frame.to_parquet(op, engine="pyarrow", index=False)
        _write_walkforward_sidecar(op, frame)
        viol = validate_walkforward(frame)
        if viol:
            print(f"[FAIL] consolidated walk-forward frame has {len(viol)} violations")
            for v in viol:
                print(f"  - {v}")
            return 1
        print(f"[OK] walk-forward consolidated parquet written: {op}")
        print(f"     rows={len(frame)} folds={sorted(frame['fold'].dropna().unique().tolist())}")
        print(f"     sidecar: {op}.provenance.json")
        return 0
    if args.check:
        path = args.check
        if path.endswith(".parquet"):
            df = pd.read_parquet(path)
        else:
            df = pd.read_csv(path)
        if "hmm_nowcast_date" in df.columns:
            df["hmm_nowcast_date"] = pd.to_datetime(df["hmm_nowcast_date"])
        viol = validate_schema(df)
        if viol:
            print(f"schema violations ({len(viol)}):")
            for v in viol:
                print(f"  - {v}")
            return 1
        print(f"[OK] {path}: all {N_TOTAL} canonical columns validated, no violations")
        return 0
    if args.list:
        print(f"Valuation Schema v{SCHEMA_VERSION} — {N_TOTAL} columns "
              f"(baseline {N_BASELINE} + v2 {N_ADDED_V2} + v3 {N_ADDED_V3}), "
              f"PIT anchor: filed_date")
        groups = ["core_key", "valuation", "t_hmm_state", "fractal_vol",
                  "swap_barrier", "execution", "regime", "beta_wacc_v2",
                  "narrative_v2", "data_gate_v2", "regime_policy"]
        for grp in groups:
            cols = [c for c, s in COLUMN_SPECS.items() if s.group == grp]
            print(f"\n[{grp}] ({len(cols)})")
            for c in cols:
                s = COLUMN_SPECS[c]
                enum = f" enum={s.enum}" if s.enum else ""
                print(f"  {c:<28} {s.dtype:<9} {s.description}{enum}")
        print(f"\n[wacc_panel_daily] ({len(WACC_PANEL_COLUMNS)} cols, separate table)")
        for c in WACC_PANEL_COLUMNS:
            print(f"  {c}")
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())