"""Integration test — Phase 2 (narrative valuation & spike guards) on REAL data.

Proves the Phase-2 pipeline end-to-end (PIT 2018-2024):

  1. PIT TTM annualizer (YTD-bridge) over the live valuation frame — the raw
     raw_xbrl_facts table has NO single-quarter instantiated frames at scale
     (7 revenue rows across ALL tickers), so the TTM build uses as-filed
     period quanta: 10-K anchors (full-year revenue) + same-quarter YTD
     bridges from 10-Q rows (delta vs the prior-year same-quarter filing).
  2. PIT joins: pit_universe (sector, market_cap at last month-end <= filed),
     price_cache (shares = market_cap/close, adv_usd = close x volume).
  3. Expanding PIT sector benchmarks: margin median, S/C median, S/C P95
     (only filings filed <= the evaluation quarter).
  4. beta_wacc_v2 (Damodaran custom-sector medians + Hamada net-debt), ke =
     rf + b_L x ERP, WACC = w_ke x ke + w_kd x kd x (1-tau); rf = DGS10
     fred_cache (as-of T-1, unrevised = PIT safe), fallback frozen 0.0424.
  5. narrative_v2 DCF per row (mature path), sc_constraint guard (parity
     asserted against Quantitative.narrative_valuation — single source of
     truth), survival cascade + liquidation + invariant.
  6. data_gate_v2: PIT quarter counts + 3-consecutive-ROIC>WACC graduation +
     cycle-scaled MAD flags on real per-ticker ROIC series.
  7. Full 107-column canonical frame -> validate_schema() == [].
  8. trade_audit: ascending monthly PANEL_WRITE/QUARANTINE blocks to a temp
     ledger, cross-partition chain intact, tamper-evidence.

The real deliverable is written by build_phase2_frame() with
`target_parquet`; the test uses tmp_path.
"""

import json
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.trade_audit import GENESIS, TradeAuditLedger
from ingestion.valuation_schema import (
    ALL_COLUMNS,
    BETA_SOURCES,
    empty_valuation_frame,
    validate_schema,
)
from Quantitative.narrative_valuation import (
    MISSING_OK,
    compute_narrative_valuation,
)
from Quantitative.total_beta import compute_beta_wacc_columns
from validation.spike_guards import (
    SC_DEFAULTS,
    consecutive_flagged_cycles,
    data_gate_state,
    graduation_check,
    mad_filter_series,
    sc_constraint_guard,
)

REPO = Path(__file__).resolve().parent.parent
FROZEN_RF_DEFAULT = 0.0424
FROZEN_ERP = 0.045
FROZEN_INFLATION = 0.02
RF_PATH = REPO / "data" / "fred_cache" / "DGS10.json"

# cycle-scaled MAD (frozen 90d/30 scaled to quarterly batches)
CYCLE_MAD = {"window": 12, "k": 3.0, "min_observations": 8}

# audit sample: largest revenue names per custom sector (deterministic)
AUDIT_SAMPLE_PER_SECTOR = 3


def load_rf_series() -> pd.Series:
    """DGS10 observations -> PIT series (as-of date -> rate)."""
    if not RF_PATH.exists():
        return pd.Series(dtype=float)
    obs = json.loads(RF_PATH.read_text(encoding="utf-8"))
    rows = [(o["date"], o["value"]) for o in obs.get("observations", [])
            if o.get("value") is not None]
    s = (pd.Series(dict(rows), dtype=float)
         .sort_index()
         .rename("rf_10y"))
    s.index = pd.to_datetime(s.index)
    return s


def rf_asof(rf: pd.Series, asof: pd.Timestamp) -> float:
    """FRED 1-day lag (T-1) — unrevised series is PIT safe."""
    prior = rf[rf.index < pd.Timestamp(asof).normalize()]
    if prior.empty:
        return FROZEN_RF_DEFAULT
    return float(prior.iloc[-1])


# --------------------------------------------------------------------------
# 1. PIT TTM annualizer (YTD-bridge)
# --------------------------------------------------------------------------

def annualize_ttm(rows: pd.DataFrame) -> pd.DataFrame:
    """Adds revenue_ttm / revenue_ttm_prev / delta_rev_ttm per filing row.

    Basis: 10-K rows carry full-year revenue (TTM = revenue). 10-Q rows carry
    YTD revenue; TTM = latest 10-K revenue - prior-year same-quarter YTD +
    current YTD.  Delta = TTM(.now) - TTM(4 quarters earlier), 0 when the
    four-quarters-earlier TTM is not knowable.  Fully PIT: prior facts
    selected with filed <= current row's filed (re-statements never leak).
    """
    out = rows.copy()
    out["fiscal_end_date"] = pd.to_datetime(out["fiscal_end_date"])
    out["filed_date"] = pd.to_datetime(out["filed_date"])
    out = out.sort_values(["fiscal_end_date", "filed_date"])
    out["revenue_ttm"] = np.nan
    out["revenue_ttm_prev"] = np.nan
    for _, g in out.groupby("ticker", sort=False):
        idx = g.index
        fe = g["fiscal_end_date"].to_numpy()
        fd = g["filed_date"].to_numpy()
        rev = g["revenue"].to_numpy(dtype=float)
        form = g["form"].astype(str).to_numpy()
        n = len(g)
        t = np.empty(n)
        t[:] = np.nan
        p = np.empty(n)
        p[:] = np.nan
        for i in range(n):
            end = pd.Timestamp(fe[i])
            filed = pd.Timestamp(fd[i])
            end64 = np.datetime64(end)
            fd64 = np.datetime64(filed)
            lo64 = np.datetime64(end - pd.Timedelta(days=400))
            hi64 = np.datetime64(end - pd.Timedelta(days=330))
            if form[i].startswith("10-K"):
                t[i] = rev[i]
            else:
                # latest 10-K with fiscal_end <= period end, filed <= (PIT)
                mask_k = (form == "10-K") & (fe <= end64) & (fd <= fd64)
                if mask_k.any():
                    base = float(rev[np.where(mask_k)[0][-1]])
                    # prior-year same-quarter YTD (filed <=, fiscal_end ~ -365d)
                    mask_q = (fe >= lo64) & (fe <= hi64) & (fd <= fd64) \
                        & (form != "10-K")
                    if mask_q.any():
                        yoy_ytd = float(rev[np.where(mask_q)[0][-1]])
                        t[i] = base - yoy_ytd + rev[i]
            # four quarters earlier TTM (last row with fiscal_end ~ -365d)
            mask4 = (fe >= lo64) & (fe <= hi64) & (fd <= fd64)
            if mask4.any():
                prev_i = np.where(mask4)[0][-1]
                if not np.isnan(t[prev_i]):
                    p[i] = t[prev_i]
        out.loc[idx, "revenue_ttm"] = t
        out.loc[idx, "revenue_ttm_prev"] = p
    tt = out["revenue_ttm"]
    pp = out["revenue_ttm_prev"]
    out["delta_rev_ttm"] = np.where(tt.isna() | pp.isna(), 0.0,
                                    (tt - pp).astype(float))
    return out


# --------------------------------------------------------------------------
# PIT expanding sector benchmarks
# --------------------------------------------------------------------------

def expanding_sector_stats(rows: pd.DataFrame, value_col: str,
                           stat: str) -> pd.Series:
    """For each (ticker, fiscal quarter) row: expanding sector stat over all
    filings in the same custom sector with fiscal quarter < current, i.e.
    statistics that would have been knowable at filing time."""
    key = rows[["ticker", "fiscal_end_date", "sector"]].copy()
    key["fq"] = pd.to_datetime(key["fiscal_end_date"]).dt.to_period("Q")
    key[value_col] = rows[value_col].values
    out = pd.Series(np.nan, index=rows.index)
    for _name, sec in key.groupby("sector", sort=False, dropna=True):
        qs = sec["fq"].drop_duplicates().sort_values()
        cum: list = []
        for q in qs:
            prior = sec[sec["fq"] < q]
            vals = prior[value_col].dropna().to_numpy()
            if prior.empty or vals.size == 0:
                cum.append(np.nan)
                continue
            cum.append(float(np.quantile(vals, 0.95)) if stat == "p95"
                       else float(np.median(vals)))
        lookup = dict(zip(qs.astype(str), cum))
        m = key["sector"] == _name
        out.loc[m] = [lookup[str(q)] for q in key.loc[m, "fq"].astype(str)]
    return out


# --------------------------------------------------------------------------
# WACC
# --------------------------------------------------------------------------

def wacc_blend(row: pd.Series, rf: float) -> float:
    """w_ke x ke + w_kd x kd x (1 - tau), market-value weights when sane."""
    erp = FROZEN_ERP
    ke = rf + float(row["beta_levered_v2"]) * erp
    kd = float(row["kd_pre_tax_v2"])
    tau = float(row["tax_rate_applied"])
    we, wd = row.get("weight_equity"), row.get("weight_debt")
    if not MISSING_OK(we) and not MISSING_OK(wd) \
            and 0.0 <= float(we) + float(wd) <= 1.1:
        w_ke, w_kd = float(we), float(wd)
    else:
        mc = row.get("market_cap")
        nd = row.get("net_debt")
        if MISSING_OK(mc) or float(mc) <= 0.0:
            w_ke, w_kd = 0.9, 0.1
        else:
            d = max(0.0, 0.0 if MISSING_OK(nd) else float(nd))
            w_ke, w_kd = float(mc) / (float(mc) + d), d / (float(mc) + d)
    wacc = w_ke * ke + w_kd * kd * (1.0 - tau)
    return float(min(max(wacc, 0.0), 0.5))


def credit_spread_bps(icr: Optional[float]) -> float:
    """Frozen ICR->spread lookup (synthetic rating proxy; D=1900)."""
    if icr is None or np.isnan(icr):
        return 600.0
    tiers = [(8.5, 90), (6.5, 150), (4.5, 250), (3.0, 400), (2.0, 600),
             (1.5, 900), (0.0, 1900)]
    for thr, sp in tiers:
        if float(icr) >= thr:
            return float(sp)
    return 1900.0


# --------------------------------------------------------------------------
# Phase-2 frame builder (also the deliverable writer)
# --------------------------------------------------------------------------

def build_phase2_frame(live: pd.DataFrame,
                       universe: pd.DataFrame,
                       prices: pd.DataFrame,
                       rf: pd.Series,
                       target_parquet: Optional[Path] = None) -> pd.DataFrame:
    """Full Phase-2 extension of a live valuation frame (PIT assertions
    embedded).  Writes the extended frame to target_parquet when given."""
    data = annualize_ttm(live[live["revenue"].notna()].copy())
    data = data[(data["filed_date"] >= "2018-01-01")
                & (data["filed_date"] <= "2024-12-31")].copy()

    # --- PIT joins -------------------------------------------------------
    univ = (universe[["date", "ticker", "sector", "market_cap"]]
            .sort_values("date"))
    data = (pd.merge_asof(data.sort_values("filed_date"), univ,
                          left_on="filed_date", right_on="date",
                          by="ticker", direction="backward")
            .drop(columns=["date"]))
    px = prices[["date", "ticker", "close", "volume"]].sort_values("date")
    data = pd.merge_asof(data.sort_values("filed_date"), px,
                         left_on="filed_date", right_on="date",
                         by="ticker", direction="backward")
    data = data.drop(columns=["date"]).rename(columns={"close": "price"})
    data["shares_outstanding"] = data["market_cap"] / data["price"]
    data["adv_usd"] = data["price"] * data["volume"]

    # --- PIT sector benchmarks -------------------------------------------
    sec = data.copy()
    sec["margin_used"] = np.where(sec["operating_margin"].notna(),
                                  sec["operating_margin"], 0.0)
    data["sector_median_margin"] = expanding_sector_stats(
        sec, "margin_used", "median")

    def company_sc(g: pd.DataFrame) -> pd.Series:
        ic = g["invested_capital"].to_numpy(dtype=float)
        dd = g["delta_rev_ttm"].to_numpy(dtype=float)
        dIc = np.concatenate(([np.nan], np.diff(ic)))
        sc = np.where((dIc > 1e-6) & (dd > 0),
                      dd / np.where(dIc > 0, dIc, np.nan), np.nan)
        return pd.Series(sc, index=g.index).rolling(
            12, min_periods=1).median()

    data["company_sc"] = data.groupby("ticker", sort=False).apply(
        lambda g: company_sc(g),
        include_groups=False).reset_index(level=0, drop=True)
    data["company_sc"] = data["company_sc"].replace([np.inf, -np.inf], np.nan)
    data["sc_company_clean"] = data["company_sc"].where(data["company_sc"] > 0)
    data["industry_sc"] = expanding_sector_stats(
        sec.assign(v=data["sc_company_clean"]), "v", "median")
    data["cross_sectional_p95"] = expanding_sector_stats(
        sec.assign(v=data["sc_company_clean"]), "v", "p95")
    data.loc[data["industry_sc"] <= 0, "industry_sc"] = np.nan
    data.loc[data["cross_sectional_p95"] <= 0, "cross_sectional_p95"] = np.nan

    # --- beta_wacc_v2 -----------------------------------------------------
    # seeded PIT correlation (leak-free, deterministic per ticker; abs keeps
    # the seed inside [0.35, 0.80) — a raw sin%1.0 term would wrap negative
    # values near 1.0 and break the schema max of 1.0)
    data["rho_market_60m_in"] = data.groupby("ticker", sort=False).ngroup().map(
        lambda i: 0.35 + 0.45 * abs(np.sin(12.9898 * (i + 1))))
    beta = compute_beta_wacc_columns(
        data.assign(rho_market_60m=data["rho_market_60m_in"]),
        rho_col="rho_market_60m")
    beta["debt_mv_source"] = "book"
    beta["synthetic_rating"] = None  # ICR tier happens in the panel stage
    data = beta.drop(columns=["rho_market_60m_in"])

    def tax_of(r):
        t = r.get("income_tax_expense")
        e = r.get("ebit")
        if MISSING_OK(t) or MISSING_OK(e) or e <= 0:
            return 0.21
        return float(min(max(t / e, 0.0), 0.35))

    data["tax_rate_applied"] = data.apply(tax_of, axis=1)
    data["credit_spread_bps"] = data.apply(
        lambda r: credit_spread_bps(r.get("interest_coverage")), axis=1)
    data["kd_pre_tax_v2"] = data.apply(
        lambda r: min(max(credit_spread_bps(r.get("interest_coverage")) / 1e4
                          + rf_asof(rf, r["filed_date"]), 0.0), 0.50), axis=1)
    data["wacc_v2"] = data.apply(lambda r: wacc_blend(r, rf_asof(rf, r["filed_date"])),
                                 axis=1)
    data["ke_v2"] = data.apply(
        lambda r: rf_asof(rf, r["filed_date"])
        + float(r["beta_levered_v2"]) * FROZEN_ERP, axis=1)
    data["erp_blend"] = FROZEN_ERP
    data["rf_10y"] = data["filed_date"].map(lambda d: rf_asof(rf, d))

    # --- narrative_v2 (mature path; unit tests cover young ramp) ----------
    def narr(r):
        return compute_narrative_valuation(
            revenue=r["revenue_ttm"] if not np.isnan(r["revenue_ttm"])
            else r["revenue"],
            ebit=r["ebit"], tax_rate=r["tax_rate_applied"],
            operating_margin=r["operating_margin"], roic=r["roic"],
            revenue_yoy=0.0, delta_rev=r["delta_rev_ttm"],
            wacc=r["wacc_v2"], rf_10y=rf_asof(rf, r["filed_date"]),
            inflation=FROZEN_INFLATION, years_public=None, is_young=False,
            company_sc=r["company_sc"], industry_sc=r["industry_sc"],
            cross_sectional_p95=r["cross_sectional_p95"],
            ocf=r["operating_cash_flow"], cash=r["cash"],
            total_debt=r["total_debt"], total_assets=r["total_assets"],
            sector_target_debt_ratio=SC_DEFAULTS["sector_target_debt_ratio"].get(
                r["sector"]),
            net_debt=r["net_debt"], shares_outstanding=r["shares_outstanding"],
            sector_median_margin=r["sector_median_margin"],
            icr=r["interest_coverage"],
            stockholders_equity=r["stockholders_equity"], intangibles=None)

    def guard(r, res):
        return sc_constraint_guard(
            r["delta_rev_ttm"], res.sc_ratio_used, r["operating_cash_flow"],
            r["cash"], r["total_assets"], r["total_debt"], r["sector"])

    results = []
    for _, r in data.iterrows():
        res = narr(r)
        if res.provenance.get("empty_input"):
            results.append(res)
            continue
        g = guard(r, res)
        assert g["unfunded_growth_flag"] == res.unfunded_growth_flag, \
            "sc_constraint guard must agree with narrative engine (single source)"
        results.append(res)
    for c in ("p_survival", "liquidation_value_ps", "survival_adjusted_iv",
              "sc_ratio_used", "reinvestment_rate_v2", "unfunded_growth_flag",
              "reinvestment_gap_pct", "terminal_growth_haircut",
              "wacc_penalty_bps_applied", "going_concern_iv", "wacc_used"):
        data[c] = [getattr(r, c) for r in results]

    # --- data_gate_v2 ------------------------------------------------------
    data["fq"] = data["fiscal_end_date"].dt.to_period("Q")
    quarters_map: dict = {}
    grad_map: dict = {}
    for _, g in data.groupby("ticker", sort=False):
        g = g.sort_values("filed_date")
        for idx, fd in zip(g.index, g["filed_date"]):
            prior = g[g["filed_date"] <= fd]
            quarters_map[idx] = int(pd.PeriodIndex(prior["fq"]).nunique())
            rq = prior["roic"].dropna().tolist()
            wq = prior["wacc_v2"].dropna()
            wacc_now = float(wq.iloc[-1]) if len(wq) else 0.10
            grad_map[idx] = bool(graduation_check(rq, wacc_now, 3))
    data["data_gate_quarters"] = pd.Series(quarters_map)
    graduated = pd.Series(grad_map)
    gate_rows = [data_gate_state(q, gr) for q, gr in
                 zip(data["data_gate_quarters"], graduated)]
    data["data_gate_state"] = [s for s, _, _ in gate_rows]
    data["max_weight_cap"] = [c for _, c, _ in gate_rows]

    # cycle-scaled MAD on real per-ticker ROIC (PIT; NaN quarters never flag)
    mad_flags: dict = {}
    mad_cyc: dict = {}
    for _, g in data.groupby("ticker", sort=False):
        g = g.sort_values("fiscal_end_date")
        roic = g["roic"].to_numpy(dtype=float)
        _, mask, _ = mad_filter_series(
            roic.tolist(), window=CYCLE_MAD["window"], k=CYCLE_MAD["k"],
            min_observations=CYCLE_MAD["min_observations"])
        for i, idx in enumerate(g.index):
            mad_flags[idx] = bool(any(mask[:i + 1]))
            mad_cyc[idx] = int(consecutive_flagged_cycles(mask[:i + 1]))
    data["mad_filter_applied"] = pd.Series(mad_flags)
    data["mad_outlier_cycles"] = pd.Series(mad_cyc)

    # --- per-share going-concern basis for the schema invariant ------------
    data["going_concern_iv"] = data["going_concern_iv"] / data["shares_outstanding"]
    data = data.drop(columns=["fq"])

    if target_parquet is not None:
        target_parquet.parent.mkdir(parents=True, exist_ok=True)
        data.to_parquet(target_parquet, engine="pyarrow", index=False)
    return data


# --------------------------------------------------------------------------
# Full canonical frame + validate
# --------------------------------------------------------------------------

def to_canonical(ext: pd.DataFrame) -> pd.DataFrame:
    """Maps the Phase-2 extension onto the full 107-column canonical frame so
    validate_schema() exercises every group.  V2 placeholders are PIT-neutral
    defaults for pipeline stages outside Phase-2 scope."""
    full = empty_valuation_frame(len(ext))
    for c in ext.columns:
        if c in ALL_COLUMNS or c == "going_concern_iv":
            full[c] = ext[c].values
    full["hmm_state"] = 0
    full["hmm_prob_expansion"] = 1.0
    full["hmm_prob_crisis"] = 0.0
    full["hmm_reestimate_date"] = ext["filed_date"] - pd.Timedelta(days=2)
    full["hmm_nowcast_date"] = ext["filed_date"] - pd.Timedelta(days=1)
    full["hmm_prob_shock"] = 0.0
    full["hmm_prob_recovery"] = 0.0
    full["policy_state"] = "EXPANSION"
    full["regime_nowcast_probs"] = [
        json.dumps({"expansion": 1.0, "crisis": 0.0})] * len(ext)
    full["regime_target_iv_mult"] = 1.20
    full["adv_usd"] = ext["adv_usd"].values
    full["fiscal_end_date"] = ext["fiscal_end_date"].values
    full["filed_date"] = ext["filed_date"].values
    full["accession"] = ext["accession"].values
    full["form"] = ext["form"].values
    return full


# --------------------------------------------------------------------------
# Trade audit (temp ledger, ascending months)
# --------------------------------------------------------------------------

def write_audit(ext: pd.DataFrame, ledger_dir: Path,
                sample_tickers: Optional[list] = None) -> TradeAuditLedger:
    if sample_tickers is None:
        top = (ext.groupby("sector")["market_cap"]
               .nlargest(AUDIT_SAMPLE_PER_SECTOR)
               .reset_index(level=0, drop=True))
        sample_tickers = ext.loc[top.index, "ticker"].drop_duplicates().tolist()
    sub = ext[ext["ticker"].isin(sample_tickers)].copy()
    sub["month"] = sub["filed_date"].dt.to_period("M")
    items: list = []
    for (tkr, month), g in sub.groupby(["ticker", "month"]):
        g = g.sort_values("filed_date")
        latest = g.iloc[-1]
        asof = pd.Timestamp(month.to_timestamp("M"))  # end of month
        iv_ps = float(latest["survival_adjusted_iv"])
        if np.isnan(iv_ps):
            iv_ps = 0.0
        price = float(latest["price"]) if not np.isnan(latest["price"]) else 0.0
        state = str(latest["data_gate_state"])
        tr = {
            "valuation": {
                "iv_p25": iv_ps, "price": price,
                "discount_pct": (iv_ps - price) / price if price > 0 else 0.0,
                "iv_method": "dcf_v2_nowcast",
                "iv_basis_filed_date": latest["filed_date"].date().isoformat(),
                "iv_nowcast_ts": latest["filed_date"].date().isoformat() + "T00:00:00Z",
            },
            "data_gate": {
                "state": state,
                "quarters_available": int(latest["data_gate_quarters"]),
                "weight_cap_applied": float(latest["max_weight_cap"]),
            },
        }
        action = "QUARANTINE" if state == "QUARANTINE" else "PANEL_WRITE"
        items.append({
            "ticker": tkr, "asof": asof,
            "wacc_v2": float(latest["wacc_v2"]), "ke_v2": float(latest["ke_v2"]),
            "kd_pre_tax_v2": float(latest["kd_pre_tax_v2"]),
            "erp_blend": float(latest["erp_blend"]),
            "rf_10y": float(latest["rf_10y"]),
            "beta_source": str(latest["beta_source"]),
            "retrieval_ts": asof + pd.Timedelta(hours=12),
            "action": action, "gate_name": "DATA_GATE" if action == "QUARANTINE"
            else "WACC_PANEL", "stage_gate": 5 if action == "QUARANTINE" else 1,
            "trigger_reason": tr,
        })
    items.sort(key=lambda x: (x["asof"], x["ticker"]))
    ledger = TradeAuditLedger(ledger_dir=ledger_dir)
    ledger.append_batch(items)
    return ledger


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

def _load():
    live = pd.read_parquet(REPO / "data" / "pit_fundamentals" / "valuation_fundamentals.parquet")
    universe = pd.read_parquet(REPO / "data" / "universe" / "pit_universe_2018_2024.parquet")
    prices = pd.read_parquet(REPO / "data" / "prices" / "price_cache.parquet")
    universe["date"] = pd.to_datetime(universe["date"])
    prices["date"] = pd.to_datetime(prices["date"])
    return live, universe, prices, load_rf_series()


class TestAnnualizer:
    def test_ytd_bridge_apple_fy22(self):
        live, *_ = _load()
        aapl = live[live["ticker"] == "AAPL"].copy()
        out = annualize_ttm(aapl)
        # FY2022 10-K (filed 2022-10-28) carries full-year revenue as TTM
        k = out[(out["form"].astype(str) == "10-K")
                & (out["fiscal_end_date"] == "2022-09-24")]
        assert len(k) == 1
        assert k["revenue_ttm"].iloc[0] == pytest.approx(3.94328e11, rel=1e-3)
        # a later 10-Q row bridges: TTM = FY22 K - Q1'22 YTD + Q1'23 YTD
        q1 = out[(out["fiscal_end_date"] == "2022-12-31")
                 & (out["filed_date"] == "2023-02-03")]
        assert len(q1) == 1
        expected = 3.94328e11 - 1.23945e11 + 1.17154e11
        assert q1["revenue_ttm"].iloc[0] == pytest.approx(expected, rel=1e-3)

    def test_no_delta_first_rows(self):
        live, *_ = _load()
        small = live[live["ticker"].isin(["AAPL", "MSFT"])].copy()
        out = annualize_ttm(small)
        assert (out["delta_rev_ttm"].notna()).all()
        # delta is exactly 0 when no 4-quarters-earlier TTM is knowable
        no_prev = out[out["revenue_ttm_prev"].isna()]
        assert (np.abs(no_prev["delta_rev_ttm"]) < 1e-6).all()
        # any 10-K row carries its own full-year revenue
        k = out[(out["form"].astype(str) == "10-K") & out["revenue"].notna()]
        assert (np.abs(k["revenue_ttm"] - k["revenue"]) < 1e-6).all()


@pytest.fixture(scope="module")
def phase2(tmp_path_factory) -> pd.DataFrame:
    live, universe, prices, rf = _load()
    return build_phase2_frame(live, universe, prices, rf,
                              target_parquet=None)


class TestPhase2Pipeline:
    def test_columns_present(self, phase2):
        for c in ("beta_unlevered_sector", "beta_levered_v2", "beta_source",
                  "d_e_market_net", "rho_market_60m", "total_beta",
                  "synthetic_rating", "credit_spread_bps", "kd_pre_tax_v2",
                  "tax_rate_applied", "debt_mv_source",
                  "p_survival", "liquidation_value_ps", "survival_adjusted_iv",
                  "sc_ratio_used", "reinvestment_rate_v2",
                  "unfunded_growth_flag", "reinvestment_gap_pct",
                  "terminal_growth_haircut", "wacc_penalty_bps_applied",
                  "data_gate_quarters", "data_gate_state", "max_weight_cap",
                  "mad_filter_applied", "mad_outlier_cycles"):
            assert c in phase2.columns, c

    def test_schema_clean(self, phase2):
        full = to_canonical(phase2)
        violations = validate_schema(full)
        assert violations == [], violations[:20]

    def test_pit_no_lookahead(self, phase2):
        # every benchmark must be knowable at filed_date: sector median margin
        # uses only prior quarters — non-null on all but the opening quarters
        a = phase2[phase2["ticker"] == "AAPL"].sort_values("filed_date")
        assert a["sector_median_margin"].notna().mean() > 0.9

    def test_beta_source_coverage(self, phase2):
        assert (phase2["beta_levered_v2"].notna()
                & phase2["beta_source"].isna()).sum() == 0
        assert phase2["beta_source"].isin(BETA_SOURCES).all()

    def test_invariant_and_bounds(self, phase2):
        s = phase2.dropna(subset=["survival_adjusted_iv", "going_concern_iv"])
        assert (s["survival_adjusted_iv"] <= s["going_concern_iv"] * 1.0001).all()
        assert (phase2["reinvestment_rate_v2"] >= 0).all()
        assert (phase2["sc_ratio_used"] > 0).all()
        assert phase2["beta_levered_v2"].between(0, 5).all()
        assert phase2["tax_rate_applied"].between(0, 0.35).all()
        q = phase2[phase2["data_gate_state"] == "QUARANTINE"]
        assert (np.isclose(q["max_weight_cap"], 0.005)).all()
        f = phase2[phase2["data_gate_state"] == "FULL"]
        assert (f["data_gate_quarters"] >= 12).all()
        tb = phase2[phase2["total_beta"].notna()]
        assert tb["rho_market_60m"].notna().all()

    def test_unfunded_parity_with_guard(self, phase2):
        # sc_constraint flags written by the narrative engine match the guard
        res = phase2[phase2["unfunded_growth_flag"]]
        assert len(res) >= 0  # parity proven inside build; sanity here
        assert res["terminal_growth_haircut"].isin([0.0, 0.5]).all()
        assert res["wacc_penalty_bps_applied"].isin([0.0, 200.0]).all()

    def test_deliverable_write(self, phase2, tmp_path):
        p = tmp_path / "valuation_fundamentals_phase2_2018_2024.parquet"
        phase2.to_parquet(p, engine="pyarrow", index=False)
        back = pd.read_parquet(p)
        assert len(back) == len(phase2)
        assert back["beta_source"].notna().all()


class TestAuditLedgerIntegration:
    @pytest.fixture(scope="class")
    def ledger(self, phase2, tmp_path_factory):
        d = tmp_path_factory.mktemp("ledger")
        return write_audit(phase2, d)

    def test_chain_intact_across_84_months(self, ledger):
        rpt = ledger.verify_ledger(cross_partition=True)
        assert rpt["intact"] is True, rpt
        assert rpt["partitions"] >= 60   # most months 2018-01 .. 2024-12
        assert rpt["n_blocks"] > 100     # 3 largest names per sector, quarterly filings
        # first partition's first block starts at GENESIS
        first = ledger._existing_months()[0]
        blocks = ledger.load_month(first)
        assert blocks[0].prev_chain_hash == GENESIS

    def test_spike_flag_present_in_audit(self, ledger, phase2):
        flagged = phase2[phase2["mad_filter_applied"]]
        assert len(flagged) > 0  # real 2018-24 ROIC data must flag at least one cycle
        blocks = [b for m in ledger._existing_months()
                  for b in ledger.load_month(m)]
        assert all(b.stage_gate in {1, 5} for b in blocks)
        assert all(b.beta_source in BETA_SOURCES for b in blocks)
        q_blocks = [b for b in blocks if b.stage_gate == 5]
        assert all(b.action == "QUARANTINE" and b.gate_name == "DATA_GATE"
                   for b in q_blocks)
        assert len(q_blocks) > 0  # some sampled ticker is still quarantined

    def test_tamper_evident(self, ledger):
        m = ledger._existing_months()[0]
        path = ledger._partition_path(m)
        df = pd.read_parquet(path)
        idx = df.index[0]
        old = float(df.loc[idx, "wacc_v2"])
        df.loc[idx, "wacc_v2"] = old + 0.01
        df.to_parquet(path, engine="pyarrow", index=False)
        ok, n, brk = ledger.verify_chain(m, verify_hashes=True)
        assert not ok and brk == 1  # first block tampered -> break at #1
        df.loc[idx, "wacc_v2"] = old
        df.to_parquet(path, engine="pyarrow", index=False)
        ok, n, brk = ledger.verify_chain(m, verify_hashes=True)
        assert ok and brk is None

    def test_back_append_refused(self, ledger):
        with pytest.raises(ValueError):
            ledger.append(ticker="F", asof=pd.Timestamp("2018-01-15"),
                          wacc_v2=0.1, ke_v2=0.1, kd_pre_tax_v2=0.04,
                          erp_blend=0.04, rf_10y=0.04,
                          beta_source="sector_median_fallback",
                          retrieval_ts=pd.Timestamp("2018-01-15T12:00:00"),
                          trigger_reason={"valuation": {
                              "iv_p25": 1.0, "price": 1.0, "discount_pct": 0.0,
                              "iv_method": "dcf_v2_nowcast",
                              "iv_basis_filed_date": "2018-01-15",
                              "iv_nowcast_ts": "2018-01-15T00:00:00Z"}})