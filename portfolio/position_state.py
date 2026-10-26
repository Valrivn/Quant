"""
Position state persistence with dwell tracking.
(Workstream B — feeds ``HoldingDwellTracker`` in Workstream A.)

WHY THIS EXISTS
---------------
The Arm A turnover fix rests on a 60-trading-day minimum dwell
(``turnover_controls.min_holding_days``) and a 15% hysteresis deadband.  Both
are *stateful*: "may I exit MSFT?" cannot be answered from today's prices, it
depends on how many days the position has been held and what the composite
score was at entry.  That state has to survive a process restart, otherwise a
daily run silently resets every dwell counter to zero on each invocation and
the 60-day rule quietly stops holding anything.

WHY PARQUET AND NOT JSON
------------------------
``days_held`` and ``entry_composite_score`` are compared and summed
continuously.  Round-tripping those through JSON strings invites the classic
bug where ``days_held`` comes back as ``"59"`` and ``59 < "60"`` raises
TypeError, or worse silently compares as a string.  Parquet keeps the dtypes,
and it is also what the rest of this repo persists state with.

SCHEMA IS FIXED AND EXPLICIT
----------------------------
The on-disk schema is declared in :data:`_SCHEMA` and written identically on
every save, including the empty case.  A store that writes different columns
when it happens to be empty and different columns when it happens to be full
is a store that fails to load one of those two cases.

WRITE ATOMICITY
---------------
Saves go to a temporary file in the same directory and are then moved into
place.  A crash mid-write leaves the previous good state intact instead of a
truncated Parquet file that fails to parse on the next run — and a state file
that cannot be parsed is indistinguishable from "the strategy held nothing",
which is the most expensive possible default.

CALL ORDERING
-------------
:meth:`PositionStateStore.increment_all_days` must be called ONCE at the start
of a trading day, BEFORE any new positions are added via :meth:`set`.  A
position opened today would otherwise have its first day counted as a day
already held.  The store does not silently skip same-day positions, because a
counter that quietly disagrees with ``entry_date`` is harder to audit than one
that is simply documented as call-order-dependent.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional

import pandas as pd

__all__ = ["PositionState", "PositionStateStore"]

#: Fixed on-disk schema.  Column order and dtypes are identical for empty and
#: populated stores so both cases load through the same code path.
_SCHEMA: List[tuple] = [
    ("ticker", "string"),
    ("weight", "float64"),
    ("days_held", "int64"),
    ("entry_date", "datetime64[ns]"),
    ("entry_composite_score", "float64"),
    ("entry_price", "float64"),
    ("current_price", "float64"),
    ("max_weight_seen", "float64"),
    ("embargo_days_remaining", "int64"),
]


@dataclass
class PositionState:
    """
    A single held position plus the dwell bookkeeping the exit rules need.

    Attributes:
        ticker: identifier, stored upper-case.
        weight: current portfolio weight (fraction, e.g. 0.05 for 5%).
        days_held: trading days since ``entry_date``.  The 60-day minimum-dwell
            rule reads this.
        entry_date: date the position was opened.
        entry_composite_score: the composite at entry.  Held so the hysteresis
            deadband can be evaluated against the score the position was bought
            on rather than only against a live score that has since drifted.
        entry_price: price at entry, for realised-P&L attribution.
        current_price: last observed price.
        max_weight_seen: largest weight this position has held.  Distinguishes
            "trimmed from 8% to 5%" from "never above 5%", which the turnover
            accounting needs and which ``weight`` alone cannot express after the
            trim has happened.
    """

    ticker: str
    weight: float
    days_held: int
    entry_date: pd.Timestamp
    entry_composite_score: float
    entry_price: float
    current_price: float = 0.0
    max_weight_seen: float = 0.0
    embargo_days_remaining: int = 0

    def __post_init__(self) -> None:
        self.ticker = str(self.ticker).upper()
        self.entry_date = pd.Timestamp(self.entry_date)
        # A position opened at 5% has, by definition, been seen at 5%.
        if self.max_weight_seen <= 0.0:
            self.max_weight_seen = float(self.weight)

    def update_price(self, price: float) -> None:
        """Record a new price and fold the current weight into the high-water."""
        p = float(price)
        if not pd.notna(p):
            # A NaN price is missing data, not a price.  Storing it would
            # propagate NaN into every downstream P&L calculation.
            return
        self.current_price = p
        self.max_weight_seen = max(self.max_weight_seen, float(self.weight))

    def increment_day(self) -> None:
        """Advance the dwell counter by one trading day."""
        self.days_held = int(self.days_held) + 1

    @property
    def unrealized_return(self) -> float:
        """Price return since entry, or NaN if either price is unusable."""
        if self.entry_price and pd.notna(self.current_price) and self.entry_price > 0:
            return float(self.current_price / self.entry_price - 1.0)
        return float("nan")

    def meets_min_dwell(self, min_holding_days: int) -> bool:
        """
        Whether the pre-registered minimum dwell has elapsed.

        Takes the threshold as an argument so it comes from the registry
        (``turnover_controls.min_holding_days``) rather than being hardcoded
        here.
        """
        return int(self.days_held) >= int(min_holding_days)

    def exceeds_deadband(self, target_weight: float,
                         hysteresis_deadband: float) -> bool:
        """
        Whether a rebalance to ``target_weight`` is large enough to trade.

        The deadband is relative: ``|actual - target| / target``.  An absolute
        band would treat a move from 5% to 6% and a move from 0.5% to 1.5% as
        equally significant, which is wrong — the second is a tripling of the
        position and the first is noise.

        Returns True (trade) when the relative deviation *exceeds* the band.
        A non-positive ``target_weight`` has no meaningful relative deviation,
        so it returns False: the correct action there is to close the position,
        which is a lifecycle decision, not a deadband decision.
        """
        target = float(target_weight)
        if not pd.notna(target) or target <= 0.0:
            return False
        current = float(self.weight)
        return abs(current - target) / target > float(hysteresis_deadband)

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for persistence.  Timestamps become ISO-8601 strings."""
        d = asdict(self)
        d["entry_date"] = pd.Timestamp(self.entry_date).isoformat()
        d["ticker"] = str(self.ticker).upper()
        return d

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PositionState":
        """Deserialise from persistence, ignoring unknown keys."""
        payload = {k: v for k, v in dict(data).items()
                   if k in {f.name for f in cls.__dataclass_fields__.values()}}
        if "entry_date" in payload and payload["entry_date"] is not None:
            payload["entry_date"] = pd.Timestamp(payload["entry_date"])
        if payload.get("ticker") is None:
            raise ValueError("PositionState payload has no ticker")
        payload.setdefault("weight", 0.0)
        payload.setdefault("days_held", 0)
        payload.setdefault("entry_composite_score", float("nan"))
        payload.setdefault("entry_price", float("nan"))
        payload.setdefault("current_price", 0.0)
        payload.setdefault("max_weight_seen", 0.0)
        payload.setdefault("embargo_days_remaining", 0)
        return cls(**payload)


class PositionStateStore:
    """
    Persists :class:`PositionState` to Parquet on disk and to memory.

    Used by ``HoldingDwellTracker`` in Workstream A.  The in-memory dict is the
    working copy; every mutation writes through so that a crash between two
    mutations cannot lose the state that the dwell rules depend on.
    """

    def __init__(self, storage_path: str = "data/position_state.parquet",
                 autosave: bool = True) -> None:
        self.storage_path = str(storage_path)
        self.autosave = bool(autosave)
        self.positions: Dict[str, PositionState] = {}
        self._load()

    # -- persistence -------------------------------------------------------

    @property
    def path(self) -> Path:
        return Path(self.storage_path)

    def _empty_frame(self) -> pd.DataFrame:
        """An empty frame with the exact persisted schema."""
        return pd.DataFrame({
            "ticker": pd.Series(dtype="string"),
            "weight": pd.Series(dtype="float64"),
            "days_held": pd.Series(dtype="int64"),
            "entry_date": pd.Series(dtype="datetime64[ns]"),
            "entry_composite_score": pd.Series(dtype="float64"),
            "entry_price": pd.Series(dtype="float64"),
            "current_price": pd.Series(dtype="float64"),
            "max_weight_seen": pd.Series(dtype="float64"),
            "embargo_days_remaining": pd.Series(dtype="int64"),
        })

    def _frame(self) -> pd.DataFrame:
        """The current state as a schema-conforming frame, sorted by ticker."""
        if not self.positions:
            return self._empty_frame()
        rows = [p.to_dict() for p in self.positions.values()]
        df = pd.DataFrame(rows, columns=[name for name, _ in _SCHEMA])
        for col, dtype in _SCHEMA:
            if col == "entry_date":
                df[col] = pd.to_datetime(df[col])
            elif dtype == "int64":
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype("int64")
            elif dtype == "float64":
                df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
            else:
                df[col] = df[col].astype("string")
        return df.sort_values("ticker").reset_index(drop=True)

    def _load(self) -> None:
        """Load from Parquet if it exists.  A missing file means no positions."""
        p = self.path
        if not p.exists():
            self.positions = {}
            return
        df = pd.read_parquet(p)
        if df is None or len(df) == 0:
            self.positions = {}
            return
        missing = [c for c, _ in _SCHEMA if c not in df.columns]
        if missing:
            raise ValueError(
                f"position state {p} is missing columns {missing}; expected "
                f"{[c for c, _ in _SCHEMA]}. Refusing to guess — a state file "
                f"read with the wrong schema produces dwell counters that are "
                f"plausible and wrong.")
        loaded: Dict[str, PositionState] = {}
        for rec in df[[c for c, _ in _SCHEMA]].to_dict(orient="records"):
            state = PositionState.from_dict(rec)
            loaded[state.ticker] = state
        self.positions = loaded

    def _save(self) -> None:
        """Write to Parquet atomically (temp file in the same dir, then move)."""
        p = self.path
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            self._frame().to_parquet(tmp, index=False)
            os.replace(str(tmp), str(p))       # atomic on POSIX and Windows
        finally:
            if tmp.exists():
                tmp.unlink()

    def _maybe_save(self) -> None:
        if self.autosave:
            self._save()

    # -- accessors ---------------------------------------------------------

    def get(self, ticker: str) -> Optional[PositionState]:
        """The state for ``ticker``, or None if not held."""
        return self.positions.get(str(ticker).upper())

    def has(self, ticker: str) -> bool:
        return str(ticker).upper() in self.positions

    def set(self, state: PositionState) -> None:
        """Insert or replace a position and persist."""
        key = str(state.ticker).upper()
        self.positions[key] = state
        self._maybe_save()

    def remove(self, ticker: str) -> None:
        """Drop a position if held and persist."""
        key = str(ticker).upper()
        if key in self.positions:
            del self.positions[key]
            self._maybe_save()

    def get_all(self) -> Dict[str, PositionState]:
        """A shallow copy of the mapping (the PositionState objects are shared)."""
        return dict(self.positions)

    def tickers(self) -> List[str]:
        return sorted(self.positions)

    def __len__(self) -> int:
        return len(self.positions)

    def __contains__(self, ticker: object) -> bool:
        return str(ticker).upper() in self.positions

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(self.positions))

    # -- mutations ---------------------------------------------------------

    def increment_all_days(self, as_of: Optional[pd.Timestamp] = None) -> None:
        """
        Advance every dwell counter by one trading day.

        Call once at the start of each trading day, BEFORE adding new
        positions.  A position whose ``entry_date`` is today is skipped: it
        has been open for zero days, and counting its first day here is what
        would make the 60-day minimum dwell expire on day 59.

        Args:
            as_of: the trading date.  When given, positions entered on this
                date are left alone.
        """
        if as_of is None:
            for pos in self.positions.values():
                pos.increment_day()
        else:
            day = pd.Timestamp(as_of).normalize()
            for pos in self.positions.values():
                if pd.Timestamp(pos.entry_date).normalize() < day:
                    pos.increment_day()
        self._maybe_save()

    def update_prices(self, prices: Mapping[str, float]) -> None:
        """Refresh ``current_price`` (and the weight high-water) for many names."""
        touched = False
        for ticker, px in (prices or {}).items():
            pos = self.get(ticker)
            if pos is not None:
                pos.update_price(float(px))
                touched = True
        if touched:
            self._maybe_save()

    def update_weights(self, weights: Mapping[str, float]) -> None:
        """Set new weights, maintaining each position's high-water mark."""
        touched = False
        for ticker, w in (weights or {}).items():
            pos = self.get(ticker)
            if pos is None:
                continue
            pos.weight = float(w)
            pos.max_weight_seen = max(pos.max_weight_seen, float(w))
            touched = True
        if touched:
            self._maybe_save()

    def set_embargo(self, ticker: str, days: int) -> None:
        """Set embargo cooldown days for a ticker."""
        key = str(ticker).upper()
        pos = self.get(key)
        if pos is not None:
            pos.embargo_days_remaining = max(0, int(days))
            self._maybe_save()
        else:
            # Create dummy/placeholder record if non-held ticker is embargoed
            state = PositionState(
                ticker=key,
                weight=0.0,
                days_held=0,
                entry_date=pd.Timestamp.now(),
                entry_composite_score=0.0,
                entry_price=0.0,
                embargo_days_remaining=max(0, int(days)),
            )
            self.set(state)

    def decrement_embargoes(self) -> None:
        """Decrement all embargo_days_remaining > 0 by 1."""
        touched = False
        for pos in self.positions.values():
            if pos.embargo_days_remaining > 0:
                pos.embargo_days_remaining -= 1
                touched = True
        if touched:
            self._maybe_save()

    def is_embargoed(self, ticker: str) -> bool:
        """True if the ticker has embargo_days_remaining > 0."""
        pos = self.get(ticker)
        if pos is None:
            return False
        return pos.embargo_days_remaining > 0

    def eligible_for_exit(self, min_holding_days: int) -> List[str]:
        """Tickers that have served their minimum dwell (not embargoed)."""
        return sorted(t.ticker for t in self.positions.values()
                      if t.meets_min_dwell(min_holding_days))

    # -- lifecycle ---------------------------------------------------------

    def snapshot(self) -> pd.DataFrame:
        """A schema-conforming frame of the current state (no write)."""
        return self._frame()

    def clear(self, persist: bool = True) -> None:
        """Drop every position.  Used when a book is fully liquidated."""
        self.positions = {}
        if persist:
            self._save()

    def save(self) -> None:
        """Force a write (for callers running with ``autosave=False``)."""
        self._save()

    def summary(self) -> Dict[str, Any]:
        """Provenance block for a report header."""
        days = [p.days_held for p in self.positions.values()]
        return {
            "storage_path": self.storage_path,
            "exists": self.path.exists(),
            "n_positions": len(self.positions),
            "total_weight": float(sum(p.weight for p in self.positions.values())),
            "min_days_held": int(min(days)) if days else None,
            "max_days_held": int(max(days)) if days else None,
            "median_days_held": float(pd.Series(days).median()) if days else None,
        }
