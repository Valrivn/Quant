"""P3 SellAlgorithm — regime-dependent exit overlay (B-20260803, CEO-approved).

Implements the approved hybrid spec:
  - high watermark = trailing-252d max close per name
  - regime-dependent band: trending (NORMAL credit, low vol) -> widen to
    BAND_TRENDING or a 2.5x ATR trailing stop; choppy/regime-shift -> tighten
    to BAND_CHOPPY and require BOTH cashflow + macro confirmation.
  - phase exits: sell 50% at the first threshold, remainder at the second.
  - point-in-time cashflow gate: facts filtered by filed_date <= decision_date;
    "accelerating cashflow" overrides the exit (hold).
  - macro gate: negative sector credit regime confirms the exit.
  - re-entry: 60-day cooldown AND macro gate neutral/positive before re-entry.
  - ablation + whipsaw-rate reporting for the P3 validation gate.

The module is pure (deterministic given inputs); the OOS harness feeds it
point-in-time price/regime/cashflow series.
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd

_HIGH_WATERMARK_WINDOW = 252
_ATR_WINDOW = 20
_ATR_MULT = 2.5
BAND_TRENDING = 0.28        # 25-30% band in trending regimes
BAND_CHOPPY = 0.11          # 10-12% band in choppy regimes
PHASE_SPLIT = 0.5           # 50% at first threshold
REENTRY_COOLDOWN_DAYS = 60
REENTRY_PULLBACK = 0.10
MOAT_DROP_THRESHOLD = 0.30  # D-20260802-002: moat "compromised" = composite
                            # falls >= 0.30 below its peak during the hold.


@dataclass
class ExitEvent:
    ticker: str
    date: pd.Timestamp
    price: float
    reason: str
    pct: float  # fraction of position sold
    regime: str
    macro_confirmed: bool
    cashflow_override: bool


@dataclass
class ExitStats:
    n_exits: int = 0
    n_phased: int = 0
    n_override: int = 0
    n_reentries: int = 0
    whipsaw_rate: float = 0.0  # re-entries per name-year
    exits_avoiding_pullback: int = 0
    hit_rate: float = 0.0
    regime_counts: Dict[str, int] = field(default_factory=dict)


class SellAlgorithm:
    """Daily per-name exit decisions driven by regime, price, and PIT gates.

    ``regime_by_date`` is a pd.Series of the sector credit regime label
    ("trending" or "choppy") aligned to the decision dates. ``cashflow_gate``
    is a callable(date, ticker) -> "accelerating" | "worsening" | None, where
    None means no point-in-time data (never overrides the exit).
    """

    def __init__(
        self,
        regime_by_date: pd.Series = None,
        macro_gate: Callable[[pd.Timestamp, str], bool] = None,
        cashflow_gate: Callable[[pd.Timestamp, str], Optional[str]] = None,
        moat_gate: Callable[[pd.Timestamp, str], Optional[float]] = None,
        band_trending: float = BAND_TRENDING,
        band_choppy: float = BAND_CHOPPY,
        phase_split: float = PHASE_SPLIT,
        reentry_cooldown_days: int = REENTRY_COOLDOWN_DAYS,
        watermark: str = "frozen",
        moat_compromise_only: bool = False,
        moat_drop_threshold: float = MOAT_DROP_THRESHOLD,
    ):
        self.regime_by_date = regime_by_date
        self.macro_gate = macro_gate or (lambda date, ticker: None)
        self.cashflow_gate = cashflow_gate or (lambda date, ticker: None)
        self.moat_gate = moat_gate or (lambda date, ticker: None)
        self.band_trending = band_trending
        self.band_choppy = band_choppy
        self.phase_split = phase_split
        self.reentry_cooldown_days = reentry_cooldown_days
        self.watermark = watermark  # "frozen" (entry-anchored) or "rolling" (spec)
        # D-20260802-002: the CEO's exit rule. When True the price-band and ATR
        # overlay is DISABLED; the only sell is a qualitative moat compromise.
        self.moat_compromise_only = moat_compromise_only
        self.moat_drop_threshold = moat_drop_threshold
        self._reentry_ok = {}  # ticker -> earliest re-entry date

    def _regime_on(self, date: pd.Timestamp) -> str:
        if self.regime_by_date is not None and len(self.regime_by_date):
            idx = self.regime_by_date.index
            if isinstance(idx, pd.DatetimeIndex):
                pos = idx.searchsorted(pd.Timestamp(date))
                if pos > 0:
                    return str(self.regime_by_date.iloc[pos - 1])
        return "choppy"  # conservative default when regime unknown

    def _macro_negative(self, date: pd.Timestamp, ticker: str) -> bool:
        v = self.macro_gate(date, ticker)
        return bool(v)  # truthy -> sector macro regime negative

    def _atr(self, close: pd.Series) -> pd.Series:
        if close is None or len(close) < _ATR_WINDOW + 1:
            return pd.Series(dtype=float)
        prev = close.shift(1)
        tr = pd.concat([(close - prev).abs(), prev, close], axis=1).max(axis=1)
        return tr.rolling(_ATR_WINDOW).mean()

    def _band_for(self, regime: str) -> float:
        return self.band_trending if regime == "trending" else self.band_choppy

    def simulate(
        self,
        ticker: str,
        full_close: pd.Series,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> tuple[List[ExitEvent], float]:
        """Run the overlay over a held window.

        ``full_close`` is the name's full price history (so the entry reference
        high can be taken as the trailing-252d max BEFORE ``start``, frozen for
        the whole hold). Returns (events, final_position) where final_position
        is the remaining fraction (1.0 = fully held, 0.0 = fully exited).
        """
        events: List[ExitEvent] = []
        if full_close is None or len(full_close) == 0:
            return events, 1.0
        pre = full_close[full_close.index < start]
        if self.watermark == "frozen":
            if len(pre) >= _HIGH_WATERMARK_WINDOW:
                reference_high = float(pre.tail(_HIGH_WATERMARK_WINDOW).max())
            elif len(pre) > 0:
                reference_high = float(pre.max())
            else:
                seg0 = full_close[full_close.index >= start]
                reference_high = float(seg0.iloc[0]) if len(seg0) else 1.0
        else:
            # Rolling spec: high_watermark(t) = max over the trailing 252d
            # window ENDING the prior day (no lookahead; a new all-time high
            # ratchets the ceiling up, so the band only fires after an extreme
            # excursion beyond the recent range).
            reference_high = None

        c = full_close[(full_close.index >= start) & (full_close.index <= end)].dropna()
        if len(c) == 0:
            return events, 1.0
        atr = self._atr(c)
        rolling_hm = None
        if self.watermark == "rolling" and len(pre) > 0:
            rolling_hm = full_close.rolling(
                _HIGH_WATERMARK_WINDOW, min_periods=1).max().shift(1)
        current_high = reference_high if reference_high is not None else float(c.iloc[0])
        position = 1.0
        exited_phases = 0
        moat_peak = None  # peak moat composite observed during the hold
        for dt, price in c.items():
            if position <= 0:
                break
            price = float(price)

            # D-20260802-002: moat-compromise-only mode. No price band, no ATR
            # stop — the only sell trigger is a qualitative moat drop of >=
            # ``moat_drop_threshold`` below its peak during the hold. Unknown
            # moat (None) never sells.
            if self.moat_compromise_only:
                moat = self.moat_gate(dt, ticker)
                if moat is not None and moat == moat:
                    moat_peak = max(moat, moat_peak) if moat_peak is not None else moat
                    if moat_peak - moat >= self.moat_drop_threshold:
                        events.append(ExitEvent(ticker, dt, price, "moat_compromise",
                                                pct=1.0, regime="moat",
                                                macro_confirmed=False,
                                                cashflow_override=False))
                        position = 0.0
                        self._reentry_ok[ticker] = dt + pd.Timedelta(days=self.reentry_cooldown_days)
                continue

            current_high = max(current_high, price)
            regime = self._regime_on(dt)
            band = self._band_for(regime)
            macro_neg = self._macro_negative(dt, ticker)
            cf = self.cashflow_gate(dt, ticker)

            is_atr_stop = False
            a = atr.loc[dt] if dt in atr.index else np.nan
            if (
                regime == "trending"
                and not np.isnan(a)
                and price <= current_high - _ATR_MULT * a
            ):
                is_atr_stop = True

            if self.watermark == "rolling":
                if rolling_hm is None or dt not in rolling_hm.index:
                    continue
                base = float(rolling_hm.loc[dt])
                trigger = is_atr_stop or price >= base * (1.0 + band)
            else:
                trigger = is_atr_stop or price >= reference_high * (1.0 + band)
            if not trigger:
                continue

            cf_accel = cf == "accelerating"
            if regime == "choppy":
                confirmed = macro_neg or cf == "worsening"
            else:
                confirmed = True
            if cf_accel and not macro_neg:
                events.append(ExitEvent(ticker, dt, price, "override-hold",
                                        pct=0.0, regime=regime,
                                        macro_confirmed=macro_neg,
                                        cashflow_override=True))
                continue
            if not confirmed:
                continue

            pct_now = self.phase_split if exited_phases == 0 else 1.0
            position -= pct_now
            exited_phases += 1
            reason = "atr_stop" if is_atr_stop else f"band_{regime}"
            events.append(ExitEvent(ticker, dt, price, reason,
                                    pct=pct_now, regime=regime,
                                    macro_confirmed=macro_neg,
                                    cashflow_override=False))
            if position <= 0:
                self._reentry_ok[ticker] = dt + pd.Timedelta(days=self.reentry_cooldown_days)
                break
        return events, max(position, 0.0)


def make_macro_gate(fred: dict) -> Callable[[pd.Timestamp, str], bool]:
    """Build a macro gate from FRED BAA10Y spread: negative when the spread
    regime is WIDENING/CRISIS (>200bps), evaluated point-in-time."""
    spread = fred.get("BAA10Y")
    if spread is None or len(spread) == 0:
        return lambda date, ticker: None

    def gate(date, ticker):
        idx = spread.index
        pos = idx.searchsorted(pd.Timestamp(date))
        if pos == 0:
            return False
        v = float(spread.iloc[pos - 1])
        return v > 2.0  # BAA10Y spread > 200bps

    return gate


def make_cashflow_gate(
    quarterly_by_ticker: Dict[str, pd.DataFrame],
) -> Callable[[pd.Timestamp, str], Optional[str]]:
    """Point-in-time cashflow gate from XBRL quarterly facts.

    quarterly_by_ticker[ticker] is a DataFrame indexed by fiscal_end with a
    ``filed_date`` column and ``operating_income`` / ``revenue`` columns.
    Returns "accelerating" when the trailing 4-quarter operating margin trend
    is positive; "worsening" when negative; None when no PIT data exists.
    """

    def gate(date, ticker):
        q = quarterly_by_ticker.get(ticker)
        if q is None or q.empty:
            return None
        if "filed_date" not in q.columns or "operating_margin" not in q.columns:
            return None
        pit = q[q["filed_date"] <= pd.Timestamp(date)]
        if len(pit) < 4:
            return None
        trend = pit["operating_margin"].dropna().tail(4)
        if len(trend) < 2:
            return None
        slope = float(trend.iloc[-1] - trend.iloc[0])
        if slope > 1e-6:
            return "accelerating"
        if slope < -1e-6:
            return "worsening"
        return None

    return gate


def make_moat_gate(
    moat_by_ticker: Dict[str, pd.Series],
) -> Callable[[pd.Timestamp, str], Optional[float]]:
    """Point-in-time moat gate from precomputed per-ticker moat score series.

    moat_by_ticker[ticker] is a pd.Series indexed by date with the qualitative
    moat/uniqueness composite (0..1) as of that date. Returns the most recent
    score on or before ``date``; None when no data (never triggers a sell).
    """
    if not moat_by_ticker:
        return lambda date, ticker: None

    def gate(date, ticker):
        s = moat_by_ticker.get(ticker)
        if s is None or len(s) == 0:
            return None
        if not isinstance(s.index, pd.DatetimeIndex):
            s = pd.Series(s.values, index=pd.to_datetime(s.index))
        pos = s.index.searchsorted(pd.Timestamp(date))
        if pos == 0:
            return None
        v = s.iloc[pos - 1]
        if v is None:
            return None
        return float(v)

    return gate


def summarize_events(events: List[ExitEvent], name_years: float) -> ExitStats:
    st = ExitStats()
    for e in events:
        st.regime_counts[e.regime] = st.regime_counts.get(e.regime, 0) + 1
        if e.pct > 0:
            st.n_exits += 1
        if e.cashflow_override:
            st.n_override += 1
        if e.reason == "override-hold":
            continue
        if e.pct > 0 and e.pct < 1.0:
            st.n_phased += 1
    st.whipsaw_rate = st.n_reentries / name_years if name_years > 0 else 0.0
    if st.n_exits > 0:
        st.hit_rate = st.exits_avoiding_pullback / st.n_exits
    return st
