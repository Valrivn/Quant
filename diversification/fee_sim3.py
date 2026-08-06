"""Phase-1/Phase-2 $10k multi-asset simulation (D-20260803-003 / D-20260803-004).

Compares strategies over 2018-01-31 -> 2026-07-31 (monthly rebalances) on a
$10,000 account, with real share accounting, turnover-proportional fees, and
dividend accrual:

  1. BASELINE  : SPY buy-and-hold.
  2. MACRO     : Phase-1 strategy — macro-state sleeve tilt (bull -> bonds/
                 alternatives, bear -> cheap stocks) + within-sleeve risk-
                 minimized ETF mix, rebalanced ONLY when the expected variance
                 improvement clears the transaction fee (D-20260803-002
                 opportunistic liquidate-only rule).
  3. MINVAR    : the theoretically-better strategy — global min-variance
                 allocation across all four sleeves, same friction rule
                 (pre-picked in the implementation plan, not after results).
  4. DIVIDEND  : Phase-2 strategy (D-20260803-004) — the equity sleeve holds
                 the OOS stable-dividend basket (dividend_audit, 5y window /
                 >=3% yield / no big cut / REIT-BDC-MLP excluded, bills
                 fallback below the minimum-candidates floor) with the
                 opportunistic oversold tilt in the bear state; bonds/bills/
                 gold follow the same macro-state + within-sleeve risk-
                 minimized mix as MACRO.

Reports end value, total gain, total fees, fees as % of gains, dividend income
vs reallocation fees (the CEO's coverage hypothesis), Sharpe, maxDD, IR, trades,
and average sleeve weights. Also demonstrates multi-source data: the Nasdaq
public API (second-vendor price cross-check) alongside yfinance, SEC XBRL
cross-checks the stable-dividend basket (dividend_audit.xbrl_crosscheck_all),
plus FRED (BAA10Y/DGS10 macro + GOLDPMGBD228NLBM gold fix) when reachable —
with a documented pre-registered HYG/LQD price-proxy fallback for the macro
state when FRED is down.

All bounds/thresholds/yields come from ``sleeves``/``macro_state``/
``risk_minimizer``/``dividend_audit``/``opportunistic`` module constants —
nothing is fitted or hardcoded to outcomes.
"""

import numpy as np
import pandas as pd
import requests

from diversification import allocator
from diversification.allocator import (
    cash_shortfall_relocation,
    fit_static_ml_weights,
    load_config,
    optimize_weights,
    profit_change_trigger,
    sleeve_return_series,
)
from diversification.datastore import (
    fetch_dividend_history,
    fetch_fred_series,
    fetch_nasdaq,
    fetch_sleeve_prices,
)
from diversification.dividend_audit import (
    MIN_CANDIDATES,
    audit_basket,
    xbrl_crosscheck_all,
)
from diversification.macro_state import classify_state, classify_state_price, macro_target_weights
from diversification.markov_momentum import momentum_overweight
from diversification.return_max import (
    ORDER as RM_ORDER,
    complex_return_series,
    fit_static_return_weights,
    optimize_return_weights,
)
from diversification.opportunistic import (
    TILT_MULT,
    _trailing_z,
    absolute_buying_opportunity,
    opportunistic_equity_weights,
)
from diversification.risk_minimizer import _estimate_cov, gradient_descent
from diversification.sleeves import (
    ALL_TICKERS,
    DIVIDEND_CANDIDATES,
    DIVIDEND_EXCLUDED_TICKERS,
    DIVIDEND_YIELDS,
    P3_TICKERS,
    SLEEVE_BOUNDS,
    SLEEVES,
)

START = "2015-01-01"
END = "2026-07-31"
REBAL_START = "2018-01-31"
INITIAL = 10000.0
FEE_RATE = 0.005
MIN_TURNOVER = 0.05
ANNUALIZE = 252.0
OUT = r"C:\Users\Hayden\AppData\Local\Temp\opencode"
MACRO_TICKERS = ["HYG", "LQD"]
FRED_PROBE_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10"
NASDAQ_CROSSCHECK = ["SPY", "VCSH", "VCIT", "BIL", "SHY", "SGOV", "GLD", "IAU"]


def _localize(idx):
    if getattr(idx, "tz", None) is not None:
        return idx.tz_localize(None)
    return idx


def _fred_reachable():
    """Fast HTTP-level probe so a down FRED costs seconds, not minutes."""
    try:
        r = requests.get(
            FRED_PROBE_URL,
            timeout=(5, 10),
            headers={"User-Agent": "Mozilla/5.0"},
        )
        return r.status_code == 200 and len(r.text) > 10
    except Exception:
        return False


def fetch_all():
    prices = fetch_sleeve_prices(P3_TICKERS + MACRO_TICKERS, START, END)
    if prices.empty:
        raise RuntimeError("sleeve price fetch failed")
    prices.index = _localize(prices.index)
    fred_ok = _fred_reachable()
    if fred_ok:
        baa10y = fetch_fred_series("BAA10Y", START, END)
        dgs10 = fetch_fred_series("DGS10", START, END)
        gold_fix = fetch_fred_series("GOLDPMGBD228NLBM", START, END)
    else:
        baa10y = dgs10 = gold_fix = pd.Series(dtype=float)
    credit_ratio = prices["HYG"] / prices["LQD"]
    return prices, baa10y, dgs10, gold_fix, credit_ratio


def _fetch_div_hist(traded):
    """Dividend histories for every traded ticker (candidates + ETFs), so the
    sim accrues REAL ex-date events instead of the static DIVIDEND_YIELDS map."""
    syms = list(dict.fromkeys(list(DIVIDEND_CANDIDATES) + list(traded)))
    return fetch_dividend_history(syms, START, END)


def sleeve_target(date, rets, spread_series, credit_ratio, within_fx):
    """Asset weight dict from macro state + within-sleeve risk minimizer.

    Uses the FRED BAA10Y spread classifier when FRED data is present; otherwise
    the price-based HYG/LQD credit proxy fallback (documented degradation).
    """
    equity = rets.get("SPY", pd.Series(dtype=float))
    if spread_series is not None and not spread_series.empty:
        state = classify_state(spread_series, equity, date)
    else:
        state = classify_state_price(equity, credit_ratio, date)
    sleeve_t = macro_target_weights(state)
    target = {}
    for sleeve, sw in sleeve_t.items():
        tickers = [t for t in SLEEVES[sleeve] if t in rets.columns]
        avail = [t for t in tickers if _has_price(rets, t, date)]
        if not avail:
            continue
        ew = within_fx(sleeve, avail, rets, date)
        for i, t in enumerate(avail):
            target[t] = sw * ew[i]
    return target, state


def _has_price(rets, t, date):
    s = rets[t]
    prior = s[s.index <= date]
    return not prior.empty and prior.iloc[-1] == prior.iloc[-1]


def _equal_within(sleeve, avail, rets, date):
    return np.full(len(avail), 1.0 / len(avail))


def _riskmin_within(sleeve, avail, rets, date, bounds=None):
    if len(avail) < 2:
        return np.full(len(avail), 1.0 / len(avail))
    window = _trailing_window(rets[avail], date)
    if window.empty:
        return np.full(len(avail), 1.0 / len(avail))
    b = bounds or [(0.0, 1.0)] * len(avail)
    w = gradient_descent(window, b)
    if w is None:
        return np.full(len(avail), 1.0 / len(avail))
    return w


def _trailing_window(rets_sub, date, window=252, embargo=21):
    cutoff = date - pd.Timedelta(days=embargo)
    wd = rets_sub[rets_sub.index <= cutoff].tail(window)
    if len(wd) < 60:
        return pd.DataFrame()
    return wd.dropna(axis=0, how="any")


class Portfolio:
    """Share-accounting portfolio with dividends; executes rebalances on the
    first trading day strictly after each calendar rebalance date."""

    def __init__(self, prices, initial=INITIAL, div_hist=None):
        self.prices = prices
        self.idx = prices.index
        self.initial = initial
        self.rets = prices.pct_change(fill_method=None)
        self._div_events = {
            c: s for c, s in (div_hist or {}).items() if s is not None and len(s)
        }

    def _price(self, c, d):
        s = self.prices[c].asof(d)
        return float(s) if s == s else 0.0

    def _value(self, d, shares, cash):
        return cash + sum(sh * self._price(c, d) for c, sh in shares.items())

    def _avail(self, date):
        """Tradable columns: those with a non-NaN return strictly at/under date."""
        prior = self.rets[self.rets.index <= date]
        out = []
        for c in self.rets.columns:
            if c not in prior.columns:
                continue
            last = prior[c].dropna()
            out.append(c) if not last.empty else None
        return out

    def run(self, rebal_dates, target_fn, fee_rate=FEE_RATE, min_turnover=MIN_TURNOVER,
            gate="variance"):
        """target_fn(d) -> (asset_weight_dict, meta) or (None, meta) to hold.

        ``gate``: "variance" (Phase-1/2/3) trades only when the rebalance lowers
        expected variance enough to clear the fee; "turnover" (Discovery
        B-20260804-001 return-max) trades whenever the target differs by at
        least ``min_turnover`` — the fee is charged honestly and the strategy
        itself decides when moving is worth it.
        """
        assets = [c for c in self.prices.columns]
        shares = {c: 0.0 for c in assets}
        cash = self.initial
        vpath = []
        fees = 0.0
        dividends = 0.0
        trades = 0
        skipped = 0
        states = []
        rebal_items = sorted(rebal_dates)
        ri = 0
        pending = None

        for d in self.idx:
            while ri < len(rebal_items) and d > rebal_items[ri]:
                pending = rebal_items[ri]
                ri += 1

            if pending is not None:
                V = self._value(d, shares, cash)
                w_cur = {}
                for c in assets:
                    p = self._price(c, d)
                    w_cur[c] = shares[c] * p / V if V > 0 else 0.0
                target, meta = target_fn(pending, w_cur, V)
                states.append((pending, meta))
                if target is not None and V > 0:
                    turnover = 0.5 * sum(abs(target.get(c, 0.0) - w_cur[c]) for c in assets)
                    fully_cash = V >= self.initial * (1 - 1e-9) and all(
                        w_cur[c] <= 1e-9 for c in assets
                    )
                    if fully_cash:
                        V_after = V - fee_rate * 0.5 * V
                        cash = V_after - sum(target.get(c, 0.0) * V_after for c in assets)
                        for c in assets:
                            p = self._price(c, d)
                            shares[c] = target.get(c, 0.0) * V_after / p if p else 0.0
                        fees += fee_rate * 0.5 * V
                        trades += 1
                    elif turnover >= min_turnover:
                        fee = fee_rate * turnover * V
                        if gate == "turnover":
                            approved = True
                        else:
                            var_cur = self._variance(w_cur, d)
                            var_tar = self._variance(target, d)
                            improvement = (var_cur - var_tar) * ANNUALIZE * V
                            approved = improvement > fee
                        if approved:
                            V_after = V - fee
                            cash = V_after - sum(target.get(c, 0.0) * V_after for c in assets)
                            for c in assets:
                                p = self._price(c, d)
                                shares[c] = target.get(c, 0.0) * V_after / p if p else 0.0
                            fees += fee
                            trades += 1
                        else:
                            skipped += 1
                    else:
                        skipped += 1
                pending = None

            # dividend accrual (real ex-date events from dividend history when
            # available; static DIVIDEND_YIELDS fallback otherwise)
            for c in assets:
                if shares[c] <= 0:
                    continue
                ev = self._div_events.get(c)
                if ev is not None and d in ev.index:
                    inc = shares[c] * float(np.sum(np.atleast_1d(ev.loc[d])))
                    cash += inc
                    dividends += inc
                    continue
                p = self._price(c, d)
                if p > 0:
                    y = DIVIDEND_YIELDS.get(c, 0.0)
                    if y > 0:
                        inc = shares[c] * p * y / ANNUALIZE
                        cash += inc
                        dividends += inc
            vpath.append(self._value(d, shares, cash))
        return pd.Series(vpath, index=self.idx), {"fees": fees, "trades": trades,
                                                  "dividends": dividends,
                                                  "skipped": skipped,
                                                  "states": states}

    def _variance(self, w, d):
        assets = self._avail(d)
        wv = np.array([w.get(c, 0.0) for c in assets], dtype=float)
        if assets:
            window = _trailing_window(self.rets[assets], d)
        else:
            window = pd.DataFrame()
        if window.empty or wv.sum() <= 0:
            return 0.0
        cov = _estimate_cov(window)
        if cov is None:
            return 0.0
        return float(wv @ cov @ wv)


def summarize(label, vpath, info, start_idx, end_idx, rets, spread_series):
    seg = vpath.loc[(vpath.index >= start_idx) & (vpath.index <= end_idx)]
    if len(seg) < 2:
        return None
    r = seg.pct_change(fill_method=None).dropna()
    ann = float(r.mean()) * ANNUALIZE
    vol = float(r.std(ddof=1)) * np.sqrt(ANNUALIZE)
    sharpe = ann / vol if vol > 0 else np.nan
    cum = seg / seg.iloc[0]
    mdd = float((cum / cum.cummax() - 1).min())
    end = float(seg.iloc[-1])
    gain = end - INITIAL
    bench = _sp500_bench(rets, start_idx, end_idx)
    ir = np.nan
    if bench is not None and len(bench):
        aligned = r.reindex(bench.index).dropna()
        bb = bench.reindex(aligned.index)
        te = float((aligned - bb).std(ddof=1)) * np.sqrt(ANNUALIZE) if len(aligned) > 1 else np.nan
        ir = (ann - float(bb.mean()) * ANNUALIZE) / te if te and te > 0 else np.nan
    fees = info["fees"]
    dividends = info["dividends"]
    coverage = dividends / fees if fees > 0 else np.inf
    return {
        "strategy": label,
        "end_value": end,
        "gain": gain,
        "total_return": end / INITIAL - 1.0,
        "ann_return": ann,
        "ann_vol": vol,
        "sharpe": sharpe,
        "maxdd": mdd,
        "ir": ir,
        "fees": fees,
        "fees_pct_of_gain": (fees / gain * 100 if gain != 0 else np.nan),
        "dividends": dividends,
        "coverage": coverage,
        "trades": info["trades"],
    }


def _sp500_bench(rets, start_idx, end_idx):
    s = (1 + rets.get("SPY", pd.Series(dtype=float)).fillna(0.0)).cumprod()
    s = s.loc[(s.index >= start_idx) & (s.index <= end_idx)]
    return s.pct_change(fill_method=None).dropna()


def run_sim():
    prices, baa10y, dgs10, gold_fix, credit_ratio = fetch_all()
    rets = prices.pct_change(fill_method=None)
    rets.index = _localize(rets.index)
    rebal = pd.date_range(REBAL_START, END, freq="ME")
    start_idx = prices.index[prices.index > pd.Timestamp(REBAL_START)][0]
    traded = [c for c in prices.columns if c in ALL_TICKERS or c in DIVIDEND_CANDIDATES]

    div_hist = _fetch_div_hist(traded)

    pf = Portfolio(prices[traded], div_hist=div_hist)

    def target_baseline(date, w_cur, V):
        target = {c: 0.0 for c in traded}
        target["SPY"] = 1.0
        return target, {}

    def _macro_state_at(date):
        equity = rets.get("SPY", pd.Series(dtype=float))
        if baa10y is not None and not baa10y.empty:
            return classify_state(baa10y, equity, date)
        return classify_state_price(equity, credit_ratio, date)

    def _other_sleeves(date, sleeve_t, target):
        for sleeve, sw in sleeve_t.items():
            if sleeve == "equity":
                continue
            tickers = [t for t in SLEEVES[sleeve] if t in rets.columns]
            avail = [t for t in tickers if _has_price(rets, t, date)]
            if not avail:
                continue
            ew = _riskmin_within(sleeve, avail, rets, date)
            for i, t in enumerate(avail):
                target[t] = sw * ew[i]

    def target_macro(date, w_cur, V):
        target, state = sleeve_target(date, rets, baa10y, credit_ratio,
                                       within_fx=_riskmin_within)
        return (target if target else None), {"state": state}

    def target_dividend(date, w_cur, V):
        state = _macro_state_at(date)
        sleeve_t = macro_target_weights(state)
        basket, rejected, fallback = audit_basket(
            DIVIDEND_CANDIDATES, div_hist, prices, date,
            excluded_tickers=DIVIDEND_EXCLUDED_TICKERS,
        )
        meta = {"state": state, "basket_n": len(basket), "fallback": fallback}
        target = {}
        if fallback or not basket:
            target["SHY"] = sleeve_t["equity"]
            meta["equity_alloc"] = "bills fallback"
        else:
            avail_eq = [t for t in basket if _has_price(rets, t, date)]
            if not avail_eq:
                target["SHY"] = sleeve_t["equity"]
                meta["equity_alloc"] = "bills fallback"
            else:
                ew = _riskmin_within("equity", avail_eq, rets, date)
                base = {t: sleeve_t["equity"] * ew[i] for i, t in enumerate(avail_eq)}
                ow = opportunistic_equity_weights(avail_eq, prices, date, state)
                if ow is not None and ow != base:
                    base = {t: sleeve_t["equity"] * ow[t] for t in avail_eq}
                    meta["opportunistic"] = True
                meta["equity_alloc"] = "stable-dividend basket"
                for t, v in base.items():
                    target[t] = v
        _other_sleeves(date, sleeve_t, target)
        return (target if target else None), meta

    def target_minvar(date, w_cur, V):
        sleeve_names = list(SLEEVES)
        avail_all = pf._avail(date)
        window = _trailing_window(rets[avail_all], date)
        if window.empty:
            return None, {"state": "n/a"}
        sleeve_ret = {}
        for s in sleeve_names:
            cols = [t for t in SLEEVES[s] if t in avail_all]
            if not cols:
                continue
            sleeve_ret[s] = window[cols].sum(axis=1) / len(cols)
        sr = pd.DataFrame(sleeve_ret)
        sr = sr.dropna(axis=0, how="any")
        if len(sr) < 60 or not set(sleeve_names).issubset(sr.columns):
            return None, {"state": "n/a"}
        bounds = [SLEEVE_BOUNDS[s] for s in sleeve_names]
        wv = gradient_descent(sr, bounds)
        if wv is None:
            return None, {"state": "n/a"}
        target = {}
        for i, s in enumerate(sleeve_names):
            cols = [t for t in SLEEVES[s] if t in avail_all]
            for t in cols:
                target[t] = wv[i] / len(cols)
        return target, {"state": "minvar"}

    results = []
    div_info = None
    for label, fn in [("BASELINE SPY", target_baseline),
                      ("MACRO (state+risk, opportunistic)", target_macro),
                      ("MINVAR (theoretically-better)", target_minvar),
                      ("DIVIDEND (stable-div + opportunistic)", target_dividend)]:
        vpath, info = pf.run(rebal, fn)
        if label.startswith("DIVIDEND"):
            div_info = info
        results.append(summarize(label, vpath, info, start_idx, prices.index[-1], rets, baa10y))

    meta = {"fred_source": "FRED" if not baa10y.empty else "PRICE FALLBACK"}
    return (pd.DataFrame([r for r in results if r]), prices, rets, gold_fix, baa10y,
            div_hist, div_info, meta)


# ---------------------------------------------------------------------------
# Phase-3 (D-20260803-005): risk-constrained ML allocator — STATIC-40/20/20/20,
# OPPORTUNISTIC-ONLY, STATIC-after-ML, ADAPTIVE.
# ---------------------------------------------------------------------------

def _p3_basket_members(date, prices, div_hist):
    basket, rejected, fallback = audit_basket(
        DIVIDEND_CANDIDATES, div_hist, prices, date,
        excluded_tickers=DIVIDEND_EXCLUDED_TICKERS,
    )
    return basket if basket and not fallback else []


def _p3_equity_tickers(date, rets, prices, div_hist):
    eq = ["SPY", "MDY", "IWM"] + _p3_basket_members(date, prices, div_hist)
    return [t for t in eq if t in rets.columns and _has_price(rets, t, date)]


def _p3_sleeve_target(sleeve_w, date, rets, prices, div_hist, cfg):
    """Map a sleeve-weight dict (sums to 1) to a ticker-weight dict for a date."""
    target = {}
    sw = sleeve_w.get("spy", 0.0)
    if sw > 0 and _has_price(rets, "SPY", date):
        target["SPY"] = sw
    sw = sleeve_w.get("small_mid", 0.0)
    if sw > 0:
        sm = [t for t in cfg["sleeves"]["small_mid"] if _has_price(rets, t, date)]
        if sm:
            ew = sw / len(sm)
            for t in sm:
                target[t] = ew
    sw = sleeve_w.get("dividend", 0.0)
    if sw > 0:
        basket = _p3_basket_members(date, prices, div_hist)
        avail = [t for t in basket if _has_price(rets, t, date)]
        if avail:
            ew = sw / len(avail)
            for t in avail:
                target[t] = ew
        else:
            target["SHY"] = target.get("SHY", 0.0) + sw
    sw = sleeve_w.get("bonds", 0.0)
    if sw > 0:
        b = [t for t in cfg["sleeves"]["bonds"] if _has_price(rets, t, date)]
        if b:
            ew = sw / len(b)
            for t in b:
                target[t] = target.get(t, 0.0) + ew
    return target


def _p3_opportunistic_tilt(eq_tickers, prices, date, state, base, pc_fired, cfg):
    """OR-gate tilt inside the equity complex: Phase-2 z-gate OR profit-change.

    Preserves the total equity weight; only rebalances which member holds it.
    Falls back to a tilt toward the relative-cheapest member when the profit-
    change leg fires without any member at the z-gate threshold.
    """
    if state != "bear" or not eq_tickers or base is None:
        return base
    z_fired = any(absolute_buying_opportunity(prices[t], date) for t in eq_tickers)
    if not (pc_fired or z_fired):
        return base
    ow = opportunistic_equity_weights(eq_tickers, prices, date, state, base_weights=base)
    if ow is not None and ow != base:
        return ow
    zs = {}
    for t in eq_tickers:
        z = _trailing_z(prices[t], date)
        zs[t] = z if z == z else np.inf
    if not zs or min(zs.values()) == np.inf:
        return base
    lo = min(zs.values())
    n = len(eq_tickers)
    ew = 1.0 / n
    w = {t: (TILT_MULT * ew if zs[t] == lo else ew) for t in eq_tickers}
    s = sum(w.values())
    return {t: v / s for t, v in w.items()} if s > 0 else base


def run_sim_phase3():
    """Phase-3 five-strategy comparison (D-20260803-005 MODIFY ruling).

    Strategies: BASELINE SPY, STATIC-40/20/20/20 (CEO's fixed mix),
    OPPORTUNISTIC-ONLY (static mix + profit-change/z OR-gate tilt),
    STATIC-after-ML (optimizer fit once on the pre-registered train segment,
    held statically), ADAPTIVE (weights re-optimized each rebalance on a
    trailing window with the same risk-constrained objective).
    """
    cfg = load_config()
    prices, baa10y, dgs10, gold_fix, credit_ratio = fetch_all()
    rets = prices.pct_change(fill_method=None)
    rets.index = _localize(rets.index)
    rebal = pd.date_range(REBAL_START, END, freq="ME")
    start_idx = prices.index[prices.index > pd.Timestamp(REBAL_START)][0]
    oos_start = pd.Timestamp(cfg["optimizer"]["oos_start"])
    oos_idx = prices.index[prices.index >= oos_start]
    oos_start_idx = oos_idx[0] if len(oos_idx) else prices.index[-1]

    div_hist = _fetch_div_hist(P3_TICKERS)
    traded = [c for c in prices.columns if c in P3_TICKERS]
    pf = Portfolio(prices[traded], div_hist=div_hist)
    order = allocator._sleeve_order(cfg)

    def _state(date):
        equity = rets.get("SPY", pd.Series(dtype=float))
        if baa10y is not None and not baa10y.empty:
            return classify_state(baa10y, equity, date)
        return classify_state_price(equity, credit_ratio, date)

    def target_baseline(date, w_cur, V):
        target = {c: 0.0 for c in traded}
        target["SPY"] = 1.0
        return target, {"state": _state(date), "strategy": "baseline"}

    def _static_target_with_overlay(date, w_cur, V, apply_tilt, cfg):
        state = _state(date)
        sleeve_w = dict(cfg["static_targets"])
        target = _p3_sleeve_target(sleeve_w, date, rets, prices, div_hist, cfg)
        meta = {"state": state, "relocated": False, "profit_change": False, "z_gate": False}
        if apply_tilt:
            eq = _p3_equity_tickers(date, rets, prices, div_hist)
            eq_w = {t: target.get(t, 0.0) for t in eq}
            eq_sum = sum(eq_w.values())
            if eq and eq_sum > 0:
                base = {t: eq_w[t] / eq_sum for t in eq}
                trail = rets[rets.index <= date].tail(cfg["profit_change"]["window_days"])
                pc = profit_change_trigger(trail, w_cur, cfg)
                tilt = _p3_opportunistic_tilt(eq, prices, date, state, base, pc, cfg)
                meta["profit_change"] = bool(pc)
                meta["z_gate"] = tilt is not None and tilt != base and not pc
                for t in eq:
                    target[t] = tilt.get(t, 0.0) * eq_sum
        target, relocated = cash_shortfall_relocation(target, w_cur, state, cfg)
        meta["relocated"] = relocated
        return target, meta

    def target_static(date, w_cur, V):
        return _static_target_with_overlay(date, w_cur, V, apply_tilt=False, cfg=cfg)

    def target_opportunistic(date, w_cur, V):
        return _static_target_with_overlay(date, w_cur, V, apply_tilt=True, cfg=cfg)

    ml_weights = fit_static_ml_weights(rets, cfg, lambda d: _p3_basket_members(d, prices, div_hist),
                                       cfg["optimizer"]["train_end"])

    def target_static_ml(date, w_cur, V):
        sleeve_w = ml_weights if ml_weights else dict(cfg["static_targets"])
        state = _state(date)
        target = _p3_sleeve_target(sleeve_w, date, rets, prices, div_hist, cfg)
        target, relocated = cash_shortfall_relocation(target, w_cur, state, cfg)
        return target, {"state": state, "relocated": relocated, "strategy": "static-ml"}

    def target_adaptive(date, w_cur, V):
        state = _state(date)
        sleeve_w = dict(cfg["static_targets"])
        win_days = cfg["optimizer"]["trailing_window_years"] * 252
        sr = sleeve_return_series(rets, date, cfg,
                                  lambda d: _p3_basket_members(d, prices, div_hist),
                                  window_days=win_days)
        if sr is not None:
            wv = optimize_weights(sr, cfg)
            if wv is not None:
                sleeve_w = dict(zip(order, wv))
        target = _p3_sleeve_target(sleeve_w, date, rets, prices, div_hist, cfg)
        target, relocated = cash_shortfall_relocation(target, w_cur, state, cfg)
        return target, {"state": state, "relocated": relocated, "strategy": "adaptive",
                        "weights": sleeve_w}

    results = []
    infos = {}
    for label, fn in [
        ("BASELINE SPY", target_baseline),
        ("STATIC-40/20/20/20 (CEO)", target_static),
        ("OPPORTUNISTIC-ONLY", target_opportunistic),
        ("STATIC-after-ML", target_static_ml),
        ("ADAPTIVE (risk-constrained)", target_adaptive),
    ]:
        vpath, info = pf.run(rebal, fn)
        infos[label] = info
        results.append(summarize(label, vpath, info, start_idx, prices.index[-1], rets, baa10y))
        if label == "STATIC-after-ML":
            oos_row = summarize(f"{label} [OOS {cfg['optimizer']['oos_start']}+]",
                                vpath, info, oos_start_idx, prices.index[-1], rets, baa10y)
            if oos_row:
                oos_row["strategy"] = f"{label} OOS segment"
                results.append(oos_row)

    meta = {"fred_source": "FRED" if not baa10y.empty else "PRICE FALLBACK",
            "ml_weights": ml_weights, "train_end": cfg["optimizer"]["train_end"],
            "oos_start": cfg["optimizer"]["oos_start"]}
    return (pd.DataFrame([r for r in results if r]), prices, rets, gold_fix, baa10y,
            div_hist, infos, meta)


NASDAQ_CROSSCHECK = ["SPY", "VCSH", "VCIT", "BIL", "SHY", "SGOV", "GLD", "IAU"]


def _nasdaq_crosscheck(prices, rets):
    """Cross-validate each sleeve price against the non-yfinance Nasdaq feed."""
    nasdaq = fetch_nasdaq(NASDAQ_CROSSCHECK, START, END)
    if nasdaq.empty:
        print("  nasdaq cross-check: SKIPPED (feed unreachable this run)")
        return
    rows = []
    for sym in NASDAQ_CROSSCHECK:
        if sym not in nasdaq.columns:
            continue
        yf_series = rets.get(sym, pd.Series(dtype=float))
        nq = nasdaq[sym].pct_change(fill_method=None)
        joined = pd.concat([yf_series.rename("yf"), nq.rename("nasdaq")], axis=1).dropna()
        if len(joined) < 30:
            continue
        corr = float(joined["yf"].corr(joined["nasdaq"]))
        level_note = ""
        if corr < 0.9:
            # Ultra-short bill funds move in tiny steps: adjusted-vs-raw daily
            # returns and levels are dominated by the dividend adjustment and
            # rounding. Reconcile on RAW closes (both feeds, same instrument).
            level_note = _raw_level_recon(sym, nasdaq[sym])
        rows.append((sym, corr, len(joined), level_note))
    if not rows:
        print("  nasdaq cross-check: no overlapping series")
        return
    print("  ticker | yfinance vs nasdaq return corr | n | note")
    for sym, corr, n, note in rows:
        print(f"  {sym:6s} | {corr:0.3f} | {n} | {note}")


def _raw_level_recon(sym, nasdaq_series):
    """Compare RAW closes from yfinance (auto_adjust=False) vs Nasdaq."""
    import yfinance as yf

    try:
        raw = yf.download(sym, start=START, end=END, progress=False, auto_adjust=False)
        if raw is None or raw.empty:
            return "  (no raw data)"
        raw_close = raw["Close"].dropna()
        if raw_close.ndim > 1:
            raw_close = raw_close.iloc[:, 0]
        lvl = pd.concat([raw_close.rename("y"), nasdaq_series.rename("n")], axis=1).dropna()
        if len(lvl) < 30:
            return "  (no overlap)"
        level_corr = float(lvl["y"].corr(lvl["n"]))
        rel = float((lvl["n"] / lvl["y"] - 1).abs().mean())
        return f"  (raw-level corr {level_corr:.3f}, mean rel diff {rel:.4f})"
    except Exception:
        return "  (raw recon failed)"


def main():
    out, prices, rets, gold_fix, baa10y, div_hist, div_info, meta = run_sim()
    pd.set_option("display.width", 220)
    print("=== Phase-1/2 (D-20260803-003/004) ===")
    print(out.to_string(float_format=lambda x: f"{x:,.2f}"))
    out.to_csv(f"{OUT}\\fee_sim3_results.csv", index=False)

    print("\n=== Phase-3 (D-20260803-005): risk-constrained ML allocator ===")
    out3, prices3, rets3, gold_fix3, baa10y3, div_hist3, infos3, meta3 = run_sim_phase3()
    print(out3.to_string(float_format=lambda x: f"{x:,.2f}"))
    out3.to_csv(f"{OUT}\\fee_sim3_phase3_results.csv", index=False)

    mw = meta3.get("ml_weights")
    if mw:
        print(f"\n  STATIC-after-ML weights (fit <= {meta3['train_end']}, "
              f"held statically):")
        for s, w in mw.items():
            print(f"    {s:11s} {w:0.4f}")

    print(f"\n  Adaptive ablation counts (all strategies, {len(rebal_dates(infos3))} rebalances):")
    for label, info in infos3.items():
        if not info["states"]:
            continue
        st = info["states"]
        n = len(st)
        reloc = sum(1 for _, m in st if m.get("relocated"))
        pc = sum(1 for _, m in st if m.get("profit_change"))
        zg = sum(1 for _, m in st if m.get("z_gate"))
        state = sum(1 for _, m in st if m.get("state") == "bear")
        print(f"    {label:26s} decisions {n:3d} | bear {state:3d} | "
              f"profit-change {pc:2d} | z-gate {zg:2d} | relocations {reloc:2d}")

    states = div_info["states"] if div_info else []
    if states:
        n = len(states)
        fb = sum(1 for _, m in states if m.get("fallback"))
        bk = sum(1 for _, m in states if m.get("equity_alloc") == "stable-dividend basket")
        op = sum(1 for _, m in states if m.get("opportunistic"))
        avg_n = sum(m.get("basket_n", 0) for _, m in states) / n if n else 0
        print(f"\n  DIVIDEND decisions: {n} monthly rebalances | bills-fallback {fb} | "
              f"basket {bk} | opportunistic tilt {op} | avg basket size {avg_n:.1f}")
        n_bear = sum(1 for _, m in states if m.get("state") == "bear")
        print(f"  macro states across decisions: bear {n_bear} / "
              f"{n - n_bear} non-bear")

    print("\n--- Multi-source check (non-yfinance) ---")
    print(f"  macro state input: {meta['fred_source']}"
          f"   (FRED BAA10Y obs: {len(baa10y)})")
    gld = rets.get("GLD", pd.Series(dtype=float))
    if not gold_fix.empty:
        gf = gold_fix.pct_change(fill_method=None)
        joined = pd.concat([gld.rename("gld"), gf.rename("fix")], axis=1).dropna()
        if len(joined) > 30:
            corr = float(joined["gld"].corr(joined["fix"]))
            print(f"  GLD vs FRED gold-fix daily return corr: {corr:.3f} "
                  f"(n={len(joined)}) — gold sleeve cross-validated on a second source")
    else:
        print("  gold cross-check (FRED GOLDPMGBD228NLBM): SKIPPED (FRED unreachable this run)")
    print("  --- Nasdaq (second vendor) price integrity ---")
    _nasdaq_crosscheck(prices, rets)
    print("\n  --- SEC XBRL stable-dividend cross-check (second dividend source) ---")
    from valuation_alpha.datastore import xbrl_financials
    from valuation_alpha.universe import cik_resolver

    rows = xbrl_crosscheck_all(
        DIVIDEND_CANDIDATES, div_hist, pd.Timestamp(END),
        resolve_cik=cik_resolver.resolve_cik,
        fetch_companyfacts=xbrl_financials.fetch_companyfacts,
        extract=xbrl_financials.extract_quarterly_financials,
    )
    n_na = sum(1 for _, s, _ in rows if s == "NA")
    if rows and n_na == len(rows):
        print("  SEC EDGAR / CIK resolution unavailable this run - XBRL"
              " cross-check SKIPPED (documented degradation, same pattern as"
              " the FRED fallback)")
        return
    print("  ticker | status | detail")
    for name, status, detail in rows:
        print(f"  {name:6s} | {status:4s} | {detail}")


def rebal_dates(infos):
    """Return the number of monthly rebalance decisions from any info dict."""
    for info in infos.values():
        if info["states"]:
            return info["states"]
    return []


# ---------------------------------------------------------------------------
# Discovery B-20260804-001: return-max pivot (all params pre-registered in
# config/weights_diversification.yaml return_max block; nothing fit to outcomes).
# ---------------------------------------------------------------------------

def run_sim_discovery():
    """Discovery comparison: return-max rules + ML variants vs SPY and Phase-3.

    Strategies (all params pre-registered):
      BASELINE SPY, STATIC-after-ML (Phase-3 ref), RM-STATIC (CEO rules, static
      within-equity split), RM-ML-STATIC (ML fit once <= train_end, held),
      RM-ML-ADAPTIVE-HIGH / LOW (trailing re-fit, diversified vs equity-heavy
      bounds), RM-GUARD (adaptive-high + crisis de-risk engine; Test-2 bar),
      RM-FINAL (guard + event-driven fee discipline; Final bar).
    """
    cfg = load_config()
    rm = cfg["return_max"]
    prices, baa10y, dgs10, gold_fix, credit_ratio = fetch_all()
    rets = prices.pct_change(fill_method=None)
    rets.index = _localize(rets.index)
    rebal = pd.date_range(REBAL_START, END, freq="ME")
    start_idx = prices.index[prices.index > pd.Timestamp(REBAL_START)][0]
    oos_idx = prices.index[prices.index >= pd.Timestamp(rm["oos_start"])]
    oos_start_idx = oos_idx[0] if len(oos_idx) else prices.index[-1]

    div_hist = _fetch_div_hist(P3_TICKERS)
    traded = [c for c in prices.columns if c in P3_TICKERS]
    pf = Portfolio(prices[traded], div_hist=div_hist)

    def basket_fx(d):
        return _p3_basket_members(d, prices, div_hist)

    rm_static = fit_static_return_weights(rets, cfg, basket_fx, rm["train_end"])
    p3_ml = fit_static_ml_weights(rets, cfg, basket_fx, cfg["optimizer"]["train_end"])

    def _state(date):
        equity = rets.get("SPY", pd.Series(dtype=float))
        if baa10y is not None and not baa10y.empty:
            return classify_state(baa10y, equity, date)
        return classify_state_price(equity, credit_ratio, date)

    def _crisis_pulse(date):
        s = prices.get("SPY", pd.Series(dtype=float))
        prior = s[s.index <= pd.Timestamp(date)]
        if len(prior) < 22:
            return None
        cur = float(prior.iloc[-1])
        past = float(prior.iloc[-22])
        return cur / past - 1.0 if past > 0 else None

    def _within_split(date, state, mode, low_div):
        if state == "bear":
            bb = rm["bear_buy_more"]
            return {"spy": float(bb["spy_share"]),
                    "small_mid": float(bb["small_mid_share"]),
                    "dividend": float(bb["basket_share"])}
        if mode == "static":
            return dict(rm["static_within"])
        if mode == "ml-static" and rm_static:
            return dict(rm_static)
        if mode.startswith("ml"):
            win_days = int(rm["trailing_window_years"]) * 252
            sr = complex_return_series(rets, date, cfg, basket_fx, window_days=win_days)
            w = optimize_return_weights(sr, cfg, low_div=low_div) if sr is not None else None
            if w is not None:
                return dict(zip(RM_ORDER, w))
        return dict(rm["static_within"])

    def _build_target(date, state, sleeve_w, low_div, engine_armed, apply_momentum):
        E = float(rm["state_equity"][state])
        if engine_armed:
            E = min(E, float(rm["downside_engine"]["max_equity_when_de_risked"]))
        if low_div:
            E = max(E, 1.0 - float(rm["low_diversification_bonds_max"]))
        eq = {}
        shy_fb = 0.0
        sw = sleeve_w.get("spy", 0.0)
        if sw > 0 and _has_price(rets, "SPY", date):
            eq["SPY"] = sw
        sw = sleeve_w.get("small_mid", 0.0)
        if sw > 0:
            sm = [t for t in cfg["sleeves"]["small_mid"] if _has_price(rets, t, date)]
            if sm:
                for t in sm:
                    eq[t] = sw / len(sm)
        sw = sleeve_w.get("dividend", 0.0)
        if sw > 0:
            avail = [t for t in basket_fx(date) if _has_price(rets, t, date)]
            if avail:
                for t in avail:
                    eq[t] = sw / len(avail)
            else:
                shy_fb = sw
        tilted = False
        if apply_momentum and state == rm["momentum"]["gate"] and eq:
            before = dict(eq)
            eq = momentum_overweight(list(eq), prices, date, cfg, eq)
            tilted = eq != before
        total = sum(eq.values()) + shy_fb
        if total <= 0:
            return None, False
        target = {}
        for t, v in eq.items():
            target[t] = v / total * E
        if shy_fb > 0:
            target["SHY"] = target.get("SHY", 0.0) + shy_fb / total * E
        bonds_w = 1.0 - E
        if bonds_w > 0:
            b = [t for t in cfg["sleeves"]["bonds"] if _has_price(rets, t, date)]
            if b:
                for t in b:
                    target[t] = target.get(t, 0.0) + bonds_w / len(b)
        return target, tilted

    def _make_target(mode, low_div, engine, fee):
        last = {"state": None, "armed": False, "last_trade": None}
        de = rm["downside_engine"]

        def fn(date, w_cur, V):
            state = _state(date)
            pulse = _crisis_pulse(date)
            armed = last["armed"]
            if pulse is not None:
                if not armed and pulse <= float(de["arm"]):
                    armed = True
                elif armed and pulse > float(de["disarm"]):
                    armed = False
            if fee and last["state"] is not None:
                event = (state != last["state"]) or (armed != last["armed"]) or (
                    date.month in (1, 4, 7, 10) and date != last["last_trade"])
                if not event:
                    last["state"] = state
                    last["armed"] = armed
                    return None, {"state": state, "hold": True}
            sleeve_w = _within_split(date, state, mode, low_div)
            target, tilted = _build_target(date, state, sleeve_w, low_div,
                                           armed and engine,
                                           rm["momentum"].get("enabled", False))
            last["state"] = state
            last["armed"] = armed
            if target is not None:
                last["last_trade"] = date
            return target, {"state": state, "armed": armed, "mode": mode,
                            "low_div": low_div, "engine": engine, "fee": fee,
                            "tilted": tilted, "weights": sleeve_w}
        return fn

    def target_baseline(date, w_cur, V):
        target = {c: 0.0 for c in traded}
        target["SPY"] = 1.0
        return target, {"state": _state(date), "strategy": "baseline"}

    def target_static_ml_ref(date, w_cur, V):
        sleeve_w = p3_ml if p3_ml else dict(cfg["static_targets"])
        state = _state(date)
        target = _p3_sleeve_target(sleeve_w, date, rets, prices, div_hist, cfg)
        return target, {"state": state, "strategy": "static-ml-ref"}

    strategies = [
        ("BASELINE SPY", target_baseline, False, "variance"),
        ("STATIC-after-ML (P3 ref)", target_static_ml_ref, False, "variance"),
        ("RM-STATIC", _make_target("static", False, False, False), False, "turnover"),
        ("RM-ML-STATIC", _make_target("ml-static", False, False, False), True, "turnover"),
        ("RM-ML-ADAPTIVE-HIGH", _make_target("ml", False, False, False), False, "turnover"),
        ("RM-ML-ADAPTIVE-LOW", _make_target("ml", True, False, False), False, "turnover"),
        ("RM-GUARD (Test-2 bar)", _make_target("ml", False, True, False), False, "turnover"),
        ("RM-FINAL (Final bar)", _make_target("ml", False, True, True), False, "turnover"),
    ]

    results = []
    infos = {}
    for label, fn, report_oos, gate in strategies:
        vpath, info = pf.run(rebal, fn, gate=gate)
        infos[label] = info
        results.append(summarize(label, vpath, info, start_idx, prices.index[-1], rets, baa10y))
        if report_oos:
            oos_row = summarize(f"{label} [OOS {rm['oos_start']}+]", vpath, info,
                                oos_start_idx, prices.index[-1], rets, baa10y)
            if oos_row:
                oos_row["strategy"] = f"{label} OOS segment"
                results.append(oos_row)

    meta = {"fred_source": "FRED" if not baa10y.empty else "PRICE FALLBACK",
            "rm_static": rm_static, "train_end": rm["train_end"],
            "oos_start": rm["oos_start"], "p3_ml": p3_ml}
    return (pd.DataFrame([r for r in results if r]), prices, rets, gold_fix, baa10y,
            div_hist, infos, meta)


def main_discovery():
    """Discovery report: return-max variants vs SPY, the three success bars,
    head-to-head vs STATIC-after-ML, and decision counts."""
    out, prices, rets, gold_fix, baa10y, div_hist, infos, meta = run_sim_discovery()
    pd.set_option("display.width", 240)
    print("=== Discovery B-20260804-001: return-max pivot ===")
    print(out.to_string(float_format=lambda x: f"{x:,.2f}"))
    out.to_csv(f"{OUT}\\discovery_results.csv", index=False)

    rows = {r["strategy"]: r for r in out.to_dict("records")}
    baseline = rows.get("BASELINE SPY")
    if baseline:
        spy_ret = baseline["total_return"]
        print(f"\n  SPY total return: {spy_ret:.1%}")
        print("  Success bars (Discovery brief):")
        for label in ["RM-STATIC", "RM-ML-STATIC", "RM-ML-ADAPTIVE-HIGH",
                      "RM-ML-ADAPTIVE-LOW", "RM-GUARD (Test-2 bar)",
                      "RM-FINAL (Final bar)"]:
            if label not in rows:
                continue
            r = rows[label]
            budget = float(load_config()["return_max"]["fee_discipline"]["target_fee_budget"])
            t1 = r["total_return"] > spy_ret
            t2 = t1 and r["sharpe"] > 0.70 and -r["maxdd"] < 0.40
            final = t2 and r["fees"] < budget
            print(f"  {label:22s} T1-beatSPY={'Y' if t1 else 'N'} "
                  f"T2(+Sharpe>0.70,DD<40%)={'Y' if t2 else 'N'} "
                  f"FINAL(+fees<${budget:.0f})={'Y' if final else 'N'} "
                  f"fees=${r['fees']:.2f} trades={r['trades']}")

    print("\n  Head-to-head vs STATIC-after-ML (Phase-3):")
    for label in ["RM-ML-STATIC", "RM-ML-ADAPTIVE-HIGH", "RM-GUARD (Test-2 bar)",
                  "RM-FINAL (Final bar)"]:
        if label in rows and "STATIC-after-ML (P3 ref)" in rows:
            ref = rows["STATIC-after-ML (P3 ref)"]
            r = rows[label]
            print(f"  {label:22s} gain {r['gain']:>10,.2f} vs {ref['gain']:>10,.2f} "
                  f"| Sharpe {r['sharpe']:5.2f} vs {ref['sharpe']:5.2f} "
                  f"| maxDD {r['maxdd']:6.1%} vs {ref['maxdd']:6.1%}")

    print("\n  Decision counts (monthly rebalances):")
    for label, info in infos.items():
        if not info["states"]:
            continue
        st = info["states"]
        n = len(st)
        bear = sum(1 for _, m in st if m.get("state") == "bear")
        bull = sum(1 for _, m in st if m.get("state") == "bull")
        armed = sum(1 for _, m in st if m.get("armed"))
        holds = sum(1 for _, m in st if m.get("hold"))
        tilted = sum(1 for _, m in st if m.get("tilted"))
        print(f"  {label:24s} n {n:3d} | bull {bull:3d} bear {bear:3d} | "
              f"armed {armed:3d} | tilted {tilted:3d} | holds {holds:3d}")

    return out, infos, meta


DEGRADED_BASELINE_DISCOVERY = {
    # Degraded-reference results (B-20260804-001, static DIVIDEND_YIELDS +
    # price-proxy macro): used to report % deltas for the Discovery suite.
    "BASELINE SPY": 31700.0, "STATIC-after-ML (P3 ref)": 21882.0,
    "RM-STATIC": 26933.0, "RM-ML-STATIC": 26802.0,
    "RM-ML-ADAPTIVE-HIGH": 26841.0, "RM-ML-ADAPTIVE-LOW": 27144.0,
    "RM-GUARD (Test-2 bar)": 25579.0, "RM-FINAL (Final bar)": 27016.0,
}


def data_status(prices, rets, gold_fix, baa10y, div_hist, meta):
    """Print the DATA CHALLENGES report for this run: every degradation is
    tagged, never silent (S1). Order: data challenges first, per CEO."""
    rows = []

    def note(metric, status, detail):
        rows.append((metric, status, detail))

    fred_ok = not baa10y.empty
    note("FRED macro (BAA10Y, gold)", "OK" if fred_ok else "DEGRADED",
         f"{len(baa10y)} BAA10Y obs" if fred_ok else
         "FRED unreachable; HYG/LQD price-proxy fallback (DEGRADED tag). "
         "No ALFRED vintages (fredapi not installed) - macro state is not PIT.")

    import urllib.request as _ur
    edgar_ok = False
    try:
        _ur.urlopen(
            "https://data.sec.gov/api/xbrl/companyconcept/CIK0000320193/"
            "us-gaap/Revenues.json", timeout=15)
        edgar_ok = True
    except Exception:
        edgar_ok = False
    note("SEC EDGAR (second dividend source)", "OK" if edgar_ok else "DEGRADED",
         "XBRL reachable; cross-check running live" if edgar_ok else
         "EDGAR unreachable; yfinance dividends are the sole dividend source")

    real, static = [], []
    for c in prices.columns:
        if c in div_hist and len(div_hist.get(c, pd.Series(dtype=float))):
            real.append(c)
        elif DIVIDEND_YIELDS.get(c, 0.0) > 0:
            static.append(c)
    note("Dividend source (S4/S6)", "PARTIAL" if static else "OK",
         f"REAL ex-date events for {len(real)} tickers ({', '.join(real)}); "
         f"STATIC DIVIDEND_YIELDS fallback still applied to {len(static)} "
         f"({', '.join(static)}) - real-events coverage is not total.")

    survivors = [c for c in prices.columns if c not in ("SPY", "MDY", "IWM")]
    note("Survivor-free universe (S4)", "LIMITED",
         "yfinance history ends at today's constituents; delisted names are "
         "absent. Basket membership is expanding-window OOS, but the price "
         "universe itself is survivorship-biased (no fja05680/sp500 PIT list).")

    note("Fill discipline (S3/P6)", "OK",
         "Execution uses the first trading day strictly after the calendar "
         "rebalance date (portfolio prices at next day; signals at month-end). "
         "No same-close fills.")

    note("Multi-source prices (S2)", "CHECKING",
         "Nasdaq cross-check below; gold cross-checked against FRED gold fix "
         "when FRED is up.")

    print("=== DATA CHALLENGES REPORT (S1-S4 tags; degraded legs explicit) ===")
    for metric, status, detail in rows:
        print(f"  [{status:9s}] {metric}")
        if detail:
            print(f"             {detail}")
    return rows


def pool_all_results():
    """Re-run every historical sim on the corrected data layer and pool the
    results: Phase-1/2, Phase-3, and Discovery. Prints the data-challenges
    report first, then one pooled table. Writes the pooled CSV to OUT."""
    pd.set_option("display.width", 240)

    out, prices, rets, gold_fix, baa10y, div_hist, div_info, meta = run_sim()
    data_status(prices, rets, gold_fix, baa10y, div_hist, meta)

    print("\n--- Multi-source price checks (S2) ---")
    _nasdaq_crosscheck(prices, rets)
    if not gold_fix.empty:
        gld = rets.get("GLD", pd.Series(dtype=float))
        gf = gold_fix.pct_change(fill_method=None)
        joined = pd.concat([gld.rename("gld"), gf.rename("fix")], axis=1).dropna()
        if len(joined) > 30:
            print(f"  GLD vs FRED gold-fix return corr: "
                  f"{float(joined['gld'].corr(joined['fix'])):.3f} "
                  f"(n={len(joined)})")

    out3, prices3, rets3, gold_fix3, baa10y3, div_hist3, infos3, meta3 = run_sim_phase3()
    outd, pricesd, retsd, gold_fixd, baa10yd, div_histd, infosd, metad = run_sim_discovery()

    print("\n--- SEC XBRL stable-dividend cross-check (live) ---")
    try:
        from valuation_alpha.datastore import xbrl_financials
        from valuation_alpha.universe import cik_resolver
        rows = xbrl_crosscheck_all(
            DIVIDEND_CANDIDATES, div_hist, pd.Timestamp(END),
            resolve_cik=cik_resolver.resolve_cik,
            fetch_companyfacts=xbrl_financials.fetch_companyfacts,
            extract=xbrl_financials.extract_quarterly_financials,
        )
        n_na = sum(1 for _, s, _ in rows if s == "NA")
        if rows and n_na == len(rows):
            print("  EDGAR CIK resolution unavailable - XBRL cross-check SKIPPED (DEGRADED)")
        else:
            print("  ticker | status | detail")
            for name, status, detail in rows:
                print(f"  {name:6s} | {status:4s} | {detail}")
    except Exception as e:
        print(f"  XBRL cross-check failed: {type(e).__name__}: {e} (DEGRADED)")

    pool = pd.concat([out, out3, outd], ignore_index=True, sort=False)
    pool["phase"] = (["P1/P2"] * len(out)) + (["P3"] * len(out3)) + (["DISCOVERY"] * len(outd))
    pool["div_src"] = "real-events+static-fallback"

    d_ref = DEGRADED_BASELINE_DISCOVERY
    pool["degraded_end_value"] = pool["strategy"].map(d_ref)
    pool["delta_vs_degraded_%"] = (pool["end_value"] / pool["degraded_end_value"] - 1) * 100

    pd.set_option("display.max_rows", None)
    cols = ["phase", "strategy", "end_value", "gain", "total_return", "ann_return",
            "sharpe", "maxdd", "ir", "fees", "fees_pct_of_gain", "dividends",
            "coverage", "trades", "delta_vs_degraded_%"]
    show = pool[cols].copy()
    show["delta_vs_degraded_%"] = show["delta_vs_degraded_%"].round(2)
    print("\n=== POOLED RESULTS — every historical test on the corrected "
          "data layer ===")
    print(show.to_string(float_format=lambda x: f"{x:,.2f}",
                         formatters={"delta_vs_degraded_%": lambda x:
                                     (f"{x:+.2f}" if x == x else "n/a")}))

    pool.to_csv(f"{OUT}\\pooled_results.csv", index=False)
    return pool, meta, meta3, metad


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "pool":
        pool_all_results()
    else:
        main()
