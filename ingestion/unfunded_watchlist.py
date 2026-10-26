"""
Unfunded-Growth Watchlist (Quarterly) — bigpickle-worker

Quarterly query that flags names whose projected growth requires reinvestment
capital the firm cannot fund, using the FROZEN sc_constraint identities
(config/pre_registration_phase8.yaml phase8.sc_constraint, WS-7.3):

    Required          = DeltaRev / S_C                    (annualized, PIT)
    Available         = OCF + Cash + Credit_Capacity
    Credit_Capacity   = sector_target_debt_ratio x Total_Assets - Total_Debt
    Unfunded          <=> Required > 1.2 x Available

The credit capacity leg is REGIME-CONDITIONED (Phase 8 WS-7 tuning):

    Credit_Capacity_regime = Credit_Capacity x regime_multiplier
    regime_multiplier: EXPANSION 1.0, RECOVERY 0.8, SHOCK 0.7, CRISIS 0.5

    (Mirrors Quantitative.sizing_rebalancing.KELLY_REGIME_MULTIPLIERS so the
    watchlist and the sizing engine condition on the SAME regime scale: a
    CRISIS regime shrinks the funding assumption BEFORE the 1.2x threshold is
    applied, so names that are funded in EXPANSION can flag in CRISIS.)

Single source of truth: Quantitative.narrative_valuation.credit_capacity and
.unfunded_growth_check implement the frozen formulas — this module only maps
PIT rows onto them.  Sector target D/(D+E) comes from the frozen Damodaran
custom-sector table in validation.spike_guards.SC_DEFAULTS (betas_latest.csv
2026.09).  Ticker -> custom sector resolution chain:

    data/damodaran/members_latest.csv (industry_group)
        -> config/industry_beta.yaml (sub_area)
        -> scripts/refresh_damodaran.SUB_AREA_TO_CUSTOM_SECTOR (custom sector)
        -> SC_DEFAULTS["sector_target_debt_ratio"][sector]   (fallback 0.25)

Cadence: quarterly (YYYYQN).  The orchestrator runs this stage only when
is_quarterly_due() is true; --force bypasses for backfills.

Output artifact (PIT estate, carries source_timestamp for the PIT auditor):
    data/pit_fundamentals/unfunded_watchlist_YYYYQN.parquet
    + .provenance.json sidecar (manifest contract)

CLI:
    python -m ingestion.unfunded_watchlist --run --quarter 2026Q3 --regime EXPANSION
    python -m ingestion.unfunded_watchlist --run --force
    python -m ingestion.unfunded_watchlist --test
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from config.logging_config import college_event

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# Frozen constants
# ----------------------------------------------------------------------------

DEFAULT_FUNDAMENTALS = Path("data/pit_fundamentals/valuation_fundamentals.parquet")
DEFAULT_MEMBERS = Path("data/damodaran/members_latest.csv")
DEFAULT_INDUSTRY_BETA = Path("config/industry_beta.yaml")
OUTPUT_DIR = Path("data/pit_fundamentals")
CHECKPOINT_DIR = Path("data/checkpoints/unfunded_watchlist")

# Frozen regime-conditioned credit-capacity multipliers.  Same scale as
# Quantitative.sizing_rebalancing.KELLY_REGIME_MULTIPLIERS (consistent
# conditioning across sizing and the funding watchlist).
REGIME_CREDIT_MULTIPLIERS: Dict[str, float] = {
    "EXPANSION": 1.0,
    "RECOVERY": 0.8,
    "SHOCK": 0.7,
    "CRISIS": 0.5,
}
DEFAULT_REGIME = "EXPANSION"

# Fallback mirror of scripts/refresh_damodaran.SUB_AREA_TO_CUSTOM_SECTOR
# (frozen 2026.09 taxonomy).  Used only when the scripts module cannot be
# imported; the live import is preferred so the mapping stays single-sourced.
SUB_AREA_TO_CUSTOM_SECTOR_FALLBACK: Dict[str, str] = {
    "chip_design": "Hardware", "chip_equipment": "Hardware",
    "hardware_oem": "Hardware", "telecom": "Hardware",
    "application_software": "Software", "it_services": "Software",
    "data_services": "Software", "retail": "Consumer",
    "food_beverage": "Consumer", "hospitality": "Consumer",
    "autos": "Consumer", "media_content": "Consumer",
    "aero_defense": "Industrial", "construction": "Industrial",
    "machinery_equipment": "Industrial", "transport": "Industrial",
    "environment": "Industrial", "diversified": "Industrial",
    "materials": "Industrial", "agri": "Industrial",
    "pharma": "Healthcare", "biotech": "Healthcare",
    "healthcare": "Healthcare", "energy": "Energy",
    "financials": "Financial", "insurance": "Financial",
    "utilities": "Energy", "real_estate": "Financial",
    "ad_services": "Software",
}

# Minimal config passed to the frozen narrative helpers: only the clamp that
# the S/C winsorize needs (schema: 0 < sc_ratio_used <= 10).
SC_CLAMPS = {"sc_ratio_min": 1e-6, "sc_ratio_max": 10.0}


# ----------------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------------

def quarter_of(d: pd.Timestamp) -> str:
    """Calendar quarter label YYYYQN for a timestamp."""
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def is_quarterly_due(as_of: pd.Timestamp, last_quarter: Optional[str]) -> bool:
    """True when the as-of date has advanced into a NEW calendar quarter
    relative to the last recorded run (or there is no recorded run).  Runs do
    not re-fire inside the same quarter unless forced."""
    if last_quarter is None:
        return True
    return quarter_of(as_of) != str(last_quarter)


def regime_multiplier(regime: str) -> float:
    return float(REGIME_CREDIT_MULTIPLIERS.get(str(regime).upper(), 1.0))


def load_sub_area_to_custom_sector() -> Dict[str, str]:
    """Live import of the frozen sub_area taxonomy; falls back to the frozen
    mirror copy only if the scripts module is unavailable."""
    try:
        from scripts.refresh_damodaran import SUB_AREA_TO_CUSTOM_SECTOR  # type: ignore
        return dict(SUB_AREA_TO_CUSTOM_SECTOR)
    except Exception:  # noqa: BLE001
        return dict(SUB_AREA_TO_CUSTOM_SECTOR_FALLBACK)


def load_industry_map(path: Path = DEFAULT_INDUSTRY_BETA) -> Dict[str, str]:
    """industry_group -> sub_area from config/industry_beta.yaml."""
    if not path.exists():
        return {}
    import yaml
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}
    out: Dict[str, str] = {}
    for name, info in (cfg.get("industries") or {}).items():
        if isinstance(info, dict) and info.get("sub_area"):
            out[str(name)] = str(info["sub_area"])
    return out


def load_member_industries(path: Path = DEFAULT_MEMBERS) -> Dict[str, str]:
    """ticker -> industry_group from the Damodaran members file.  Only plain
    US-style ticker symbols participate (no exchange-prefixed listings)."""
    if not path.exists():
        return {}
    try:
        df = pd.read_csv(path, encoding="utf-8")
    except Exception:  # noqa: BLE001
        return {}
    out: Dict[str, str] = {}
    t = df["ticker"].astype(str)
    mask = t.str.match(r"^[A-Z0-9.\-]{1,6}$")
    for ticker, industry in zip(t[mask], df.loc[mask, "industry_group"].astype(str)):
        if industry and str(industry) != "nan":
            out[str(ticker)] = str(industry)
    return out


def custom_sector_for_ticker(ticker: str) -> Optional[str]:
    """Resolve ticker -> 7 custom sector via the frozen Damodaran chain
    (members industry_group -> industry_beta sub_area -> custom sector)."""
    members = load_member_industries()
    industry = members.get(str(ticker).upper())
    if not industry:
        return None
    sub = load_industry_map().get(industry)
    if not sub:
        return None
    return load_sub_area_to_custom_sector().get(sub)


def target_debt_ratio_for_ticker(ticker: str) -> Tuple[float, Optional[str]]:
    """(target_debt_ratio, sector) using the frozen SC_DEFAULTS table;
    falls back to the frozen default 0.25 when the sector is unresolvable."""
    # local import: validation.spike_guards is a light module (numpy/pandas)
    from validation.spike_guards import SC_DEFAULTS
    sector = custom_sector_for_ticker(ticker)
    table = SC_DEFAULTS.get("sector_target_debt_ratio") or {}
    if sector and sector in table:
        return float(table[sector]), sector
    return float(SC_DEFAULTS.get("default_target_debt_ratio", 0.25)), sector


# ----------------------------------------------------------------------------
# Result containers
# ----------------------------------------------------------------------------

@dataclass
class WatchlistRow:
    ticker: str
    fiscal_end_date: Optional[str]
    filed_date: Optional[str]
    revenue: Optional[float]
    revenue_yoy: Optional[float]
    delta_rev: float
    sc_ratio: float
    required_reinvestment: Optional[float]
    operating_cash_flow: Optional[float]
    cash: Optional[float]
    total_assets: Optional[float]
    total_debt: Optional[float]
    sector: Optional[str]
    target_debt_ratio_used: float
    credit_capacity: Optional[float]
    regime: str
    regime_multiplier: float
    credit_capacity_regime: Optional[float]
    available_funding: Optional[float]
    unfunded_flag: bool
    gap_pct: Optional[float]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "fiscal_end_date": self.fiscal_end_date,
            "filed_date": self.filed_date,
            "revenue": self.revenue,
            "revenue_yoy": self.revenue_yoy,
            "delta_rev": self.delta_rev,
            "sc_ratio": self.sc_ratio,
            "required_reinvestment": self.required_reinvestment,
            "operating_cash_flow": self.operating_cash_flow,
            "cash": self.cash,
            "total_assets": self.total_assets,
            "total_debt": self.total_debt,
            "sector": self.sector,
            "target_debt_ratio_used": self.target_debt_ratio_used,
            "credit_capacity": self.credit_capacity,
            "regime": self.regime,
            "regime_multiplier": self.regime_multiplier,
            "credit_capacity_regime": self.credit_capacity_regime,
            "available_funding": self.available_funding,
            "unfunded_flag": self.unfunded_flag,
            "gap_pct": self.gap_pct,
        }


@dataclass
class WatchlistResult:
    quarter: str
    as_of: str
    regime: str
    rows: List[WatchlistRow] = field(default_factory=list)
    unfunded_tickers: List[str] = field(default_factory=list)
    source: str = "SEC_EDGAR_xbrl_filtered + valuation_fundamentals"
    output_path: Optional[Path] = None
    error: Optional[str] = None

    def to_frame(self) -> pd.DataFrame:
        if not self.rows:
            return pd.DataFrame()
        df = pd.DataFrame([r.to_dict() for r in self.rows])
        df["source_timestamp"] = pd.Timestamp(self.as_of)
        df = df.sort_values(["unfunded_flag", "ticker"], ascending=[False, True])
        return df

    def to_dict(self) -> Dict[str, Any]:
        return {
            "quarter": self.quarter,
            "as_of": self.as_of,
            "regime": self.regime,
            "source": self.source,
            "screened_firms": len(self.rows),
            "unfunded_count": len(self.unfunded_tickers),
            "unfunded_tickers": self.unfunded_tickers,
            "output_path": str(self.output_path) if self.output_path else None,
            "error": self.error,
        }


# ----------------------------------------------------------------------------
# Watchlist executor
# ----------------------------------------------------------------------------

class UnfundedWatchlist:
    """Quarterly unfunded-growth watchlist over the PIT valuation
    fundamentals.  Pure query + frozen formula application; no global state."""

    def __init__(self,
                 fundamentals_path: Path = DEFAULT_FUNDAMENTALS,
                 output_dir: Path = OUTPUT_DIR,
                 checkpoint_dir: Path = CHECKPOINT_DIR) -> None:
        self.fundamentals_path = Path(fundamentals_path)
        self.output_dir = Path(output_dir)
        self.checkpoint_dir = Path(checkpoint_dir)

    # -- loading -----------------------------------------------------------

    def load_recent_filings(self, as_of: pd.Timestamp) -> pd.DataFrame:
        """Latest PIT filing row per ticker (filed <= as_of) with a positive
        revenue base, from the annualized valuation fundamentals frame."""
        if not self.fundamentals_path.exists():
            raise FileNotFoundError(
                f"valuation fundamentals missing: {self.fundamentals_path}")
        df = pd.read_parquet(self.fundamentals_path)
        # PIT filter
        filed = pd.to_datetime(df["filed_date"], errors="coerce")
        as_of_ts = pd.Timestamp(as_of)
        df = df.loc[filed.notna() & (filed <= as_of_ts)].copy()
        df["revenue"] = pd.to_numeric(df.get("revenue"), errors="coerce")
        df = df.loc[df["revenue"].notna() & (df["revenue"] > 0)].copy()
        if df.empty:
            return df
        # keep the latest filing row per ticker (tie-break: latest fiscal end)
        df["_filed"] = pd.to_datetime(df["filed_date"], errors="coerce")
        df = (df.sort_values(["_filed", "fiscal_end_date"])
                .groupby("ticker", as_index=False).tail(1))
        return df

    # -- evaluation ----------------------------------------------------------

    def evaluate(self, df: pd.DataFrame, regime: str) -> List[WatchlistRow]:
        """Map rows through the frozen sc_constraint helpers.  The credit
        capacity leg is multiplied by the regime multiplier BEFORE the 1.2x
        unfunded threshold is applied."""
        from Quantitative.narrative_valuation import (  # local: no circulars
            credit_capacity,
            unfunded_growth_check,
        )
        mult = regime_multiplier(regime)
        rows: List[WatchlistRow] = []
        for _, r in df.iterrows():
            ticker = str(r["ticker"])
            revenue = float(r["revenue"]) if pd.notna(r.get("revenue")) else None
            revenue_yoy = float(r["revenue_yoy"]) if pd.notna(r.get("revenue_yoy")) else None
            delta_rev = revenue * revenue_yoy if (revenue and revenue_yoy and revenue_yoy > 0) else 0.0

            # S/C = Revenue / Invested_Capital, winsorized to the schema clamp
            ic = r.get("invested_capital")
            sc_raw = None
            if revenue and ic is not None and pd.notna(ic) and float(ic) > 0:
                sc_raw = revenue / float(ic)
            sc = min(max((sc_raw if sc_raw is not None else 0.0),
                         SC_CLAMPS["sc_ratio_min"]), SC_CLAMPS["sc_ratio_max"])
            ocf = float(r["operating_cash_flow"]) if pd.notna(r.get("operating_cash_flow")) else None
            cash = float(r["cash"]) if pd.notna(r.get("cash")) else None
            ta = float(r["total_assets"]) if pd.notna(r.get("total_assets")) else None
            td = float(r["total_debt"]) if pd.notna(r.get("total_debt")) else None

            ratio, sector = target_debt_ratio_for_ticker(ticker)
            cc, _ = credit_capacity(ratio, ta, td)
            cc_regime = None if cc is None else cc * mult

            flag, gap, _, _, available = unfunded_growth_check(
                delta_rev, sc, ocf, cash, cc_regime)

            rows.append(WatchlistRow(
                ticker=ticker,
                fiscal_end_date=(str(r["fiscal_end_date"]) if pd.notna(r.get("fiscal_end_date")) else None),
                filed_date=(str(r["filed_date"]) if pd.notna(r.get("filed_date")) else None),
                revenue=revenue, revenue_yoy=revenue_yoy, delta_rev=delta_rev,
                sc_ratio=sc,
                required_reinvestment=(delta_rev / sc if sc > 0 and delta_rev > 0 else None),
                operating_cash_flow=ocf, cash=cash, total_assets=ta, total_debt=td,
                sector=sector, target_debt_ratio_used=ratio,
                credit_capacity=cc, regime=str(regime).upper(),
                regime_multiplier=mult, credit_capacity_regime=cc_regime,
                available_funding=available,
                unfunded_flag=bool(flag), gap_pct=gap,
            ))
        return rows

    # -- persistence ---------------------------------------------------------

    def persist(self, result: WatchlistResult) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        out = self.output_dir / f"unfunded_watchlist_{result.quarter}.parquet"
        df = result.to_frame()
        df.to_parquet(out, index=False)
        # provenance sidecar (manifest contract)
        sidecar = out.with_name(out.name + ".provenance.json")
        sidecar.write_text(json.dumps({
            "source": result.source,
            "retrieval_timestamp": datetime.now(timezone.utc).isoformat(),
            "source_version": "valuation_fundamentals.parquet (annualized PIT)",
            "pit_timestamp_column": "source_timestamp",
            "entity_key": "ticker",
            "transformations": [
                "PIT filter filed_date <= as_of",
                "latest filing per ticker",
                "delta_rev = revenue * revenue_yoy",
                "sc_ratio = revenue / invested_capital, clamp [1e-6, 10]",
                "credit_capacity regime x " + str(result.regime_multiplier)
                if False else ("credit_capacity regime-conditioned by " + result.regime),
                "unfunded flag via narrative_valuation.unfunded_growth_check (1.2x)",
            ],
            "row_count": len(df),
            "quarter": result.quarter,
            "as_of": result.as_of,
            "regime": result.regime,
        }, indent=2, sort_keys=True, default=str), encoding="utf-8")
        result.output_path = out
        return out

    def save_last_run(self, result: WatchlistResult) -> None:
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        (self.checkpoint_dir / "last_run.json").write_text(json.dumps({
            "quarter": result.quarter, "as_of": result.as_of,
            "regime": result.regime,
            "unfunded_count": len(result.unfunded_tickers),
            "ran_at": datetime.now(timezone.utc).isoformat(),
        }, indent=2, sort_keys=True, default=str), encoding="utf-8")

    def load_last_run(self) -> Dict[str, Any]:
        p = self.checkpoint_dir / "last_run.json"
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    # -- main entry --------------------------------------------------------

    def run(self, as_of: Optional[Any] = None, regime: str = DEFAULT_REGIME,
            quarter: Optional[str] = None, force: bool = False) -> WatchlistResult:
        """Quarterly watchlist query.  Returns the result; a not-yet-due run
        (same quarter, not forced) returns an empty result with the prior
        output_path preserved."""
        as_of_ts = pd.Timestamp(as_of) if as_of is not None else pd.Timestamp(date.today())
        quarter = quarter or quarter_of(as_of_ts)
        last = self.load_last_run()
        if not force and not is_quarterly_due(as_of_ts, last.get("quarter")):
            logger.info("Unfunded watchlist not due (last run quarter=%s, as_of=%s)",
                        last.get("quarter"), quarter_of(as_of_ts))
            prior = self.output_dir / f"unfunded_watchlist_{quarter}.parquet"
            return WatchlistResult(
                quarter=quarter, as_of=as_of_ts.date().isoformat(),
                regime=str(regime).upper(), rows=[], unfunded_tickers=[],
                output_path=prior if prior.exists() else None,
            )

        df = self.load_recent_filings(as_of_ts)
        rows = self.evaluate(df, regime)
        unfunded = sorted({r.ticker for r in rows if r.unfunded_flag})
        result = WatchlistResult(
            quarter=quarter, as_of=as_of_ts.date().isoformat(),
            regime=str(regime).upper(), rows=rows, unfunded_tickers=unfunded,
        )
        out = self.persist(result)
        self.save_last_run(result)

        college_event(
            "unfunded_watchlist_complete",
            quarter=quarter, as_of=result.as_of, regime=result.regime,
            screened=len(rows), unfunded_count=len(unfunded),
            output=str(out),
        )
        logger.info("Unfunded watchlist %s: %d/%d unfunded -> %s",
                    quarter, len(unfunded), len(rows), out)
        return result


# ----------------------------------------------------------------------------
# Self-test / CLI
# ----------------------------------------------------------------------------

def _self_test() -> bool:
    from Quantitative.narrative_valuation import credit_capacity, unfunded_growth_check

    # 1. quarter cadence
    assert quarter_of(pd.Timestamp("2026-09-30")) == "2026Q3"
    assert quarter_of(pd.Timestamp("2026-10-01")) == "2026Q4"
    assert is_quarterly_due(pd.Timestamp("2026-09-30"), "2026Q3") is False
    assert is_quarterly_due(pd.Timestamp("2026-09-30"), "2026Q2") is True
    assert is_quarterly_due(pd.Timestamp("2026-09-30"), None) is True

    # 2. regime-conditioned credit capacity (frozen formula reused)
    cc, ratio = credit_capacity(0.20, 1000.0, 100.0)
    assert abs(cc - 100.0) < 1e-9 and abs(ratio - 0.20) < 1e-9
    assert abs(cc * regime_multiplier("EXPANSION") - 100.0) < 1e-9
    assert abs(cc * regime_multiplier("SHOCK") - 70.0) < 1e-9
    assert abs(cc * regime_multiplier("CRISIS") - 50.0) < 1e-9
    assert abs(regime_multiplier("bogus_regime") - 1.0) < 1e-9

    # 3. unfunded threshold flips under regime conditioning
    #    required=250, avail(EXPANSION, cc=100, ocf=50, cash=0) = 150 -> 250 > 1.2*150
    flag_exp, _, _, _, avail_exp = unfunded_growth_check(250.0, 1.0, 50.0, 0.0, 100.0)
    assert flag_exp and abs(avail_exp - 150.0) < 1e-9
    #    CRISIS halves the credit leg: available = 50 + 50 = 100 -> even deeper
    flag_crisis, _, _, _, avail_crisis = unfunded_growth_check(250.0, 1.0, 50.0, 0.0, 50.0)
    assert flag_crisis and abs(avail_crisis - 100.0) < 1e-9
    #    funded case: required=100 <= 1.2 * 150
    flag_ok, gap_ok, _, _, _ = unfunded_growth_check(100.0, 1.0, 50.0, 0.0, 100.0)
    assert not flag_ok and gap_ok is not None

    # 4. sector resolution chain (deterministic on the frozen fallback table)
    ratio_default, sector = target_debt_ratio_for_ticker("ZZZZ_NOT_A_REAL_TICKER")
    assert ratio_default == 0.25 and sector is None
    assert set(REGIME_CREDIT_MULTIPLIERS) == {"EXPANSION", "RECOVERY", "SHOCK", "CRISIS"}

    # 5. end-to-end on a synthetic fundamentals frame
    wl = UnfundedWatchlist()
    frame = pd.DataFrame([{
        "ticker": "SYN1", "filed_date": "2026-08-15", "fiscal_end_date": "2026-06-30",
        "revenue": 500.0, "revenue_yoy": 0.30, "invested_capital": 250.0,
        "operating_cash_flow": 20.0, "cash": 5.0, "total_assets": 800.0,
        "total_debt": 400.0,
    }])
    rows = wl.evaluate(frame, "CRISIS")
    assert len(rows) == 1
    r = rows[0]
    assert abs(r.sc_ratio - (500.0 / 250.0)) < 1e-9
    assert abs(r.delta_rev - 150.0) < 1e-9
    # cc = 0.25*800 - 400 = -200 -> regime -100; available = 20+5-100 = -75
    # any positive Required on negative funding flags
    assert r.unfunded_flag and r.credit_capacity_regime == -100.0
    assert ratio_default == 0.25  # SYN1 unresolvable -> default debt ratio

    # 6. not-due short-circuit preserves the prior path
    res_due = WatchlistResult(quarter="2026Q3", as_of="2026-09-01", regime="EXPANSION")
    assert not res_due.unfunded_tickers

    print("unfunded_watchlist self-test: OK (6 groups)")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Quarterly unfunded-growth watchlist (phase8 sc_constraint)")
    parser.add_argument("--run", action="store_true",
                        help="Run the quarterly watchlist query")
    parser.add_argument("--quarter", type=str, default=None,
                        help="Quarter label YYYYQN (default: from --as-of/today)")
    parser.add_argument("--regime", type=str, default=DEFAULT_REGIME,
                        help="HMM regime for credit-capacity conditioning")
    parser.add_argument("--as-of", type=str, default=None,
                        help="PIT as-of date (default: today)")
    parser.add_argument("--force", action="store_true",
                        help="Bypass quarterly-due check (backfills)")
    parser.add_argument("--test", action="store_true", help="Self-test")
    args = parser.parse_args()

    if args.test:
        ok = _self_test()
        return 0 if ok else 1

    if args.run:
        as_of = pd.Timestamp(args.as_of) if args.as_of else None
        result = UnfundedWatchlist().run(
            as_of=as_of, regime=args.regime, quarter=args.quarter, force=args.force)
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True, default=str))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())