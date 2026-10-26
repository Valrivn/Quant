"""
Regime state persistence: triggers, graded de-risking, re-entry embargo.
(Workstream B — feeds ``MarketEventDetector``, ``GradedDerisking`` and
``ReEntryEmbargo`` in Workstream A.)

WHY THIS EXISTS
---------------
The 3-stage framework is a state machine, and a state machine whose state lives
only in process memory resets on every restart.  That failure mode is
particularly nasty here: a restart in the middle of a de-risking sequence
would clear ``current_equity_cap`` back to 1.0 and hand the strategy full
equity on the worst possible morning — precisely while the persistence gate
that justified de-risking is still firing.  So the state is written to disk on
every transition.

WHAT IS PERSISTED
-----------------
  - which trigger confirmed, and when
  - days elapsed since confirmation (drives the graded de-risking steps)
  - the embargo state and days remaining
  - the equity cap currently in force

``days_since_confirmation`` is stored rather than recomputed from
``confirmation_date`` because the two can legitimately disagree: a trigger can
be confirmed on a date that is not a trading day, and recomputing from
calendar days would advance the de-risking ladder during a weekend.  The stored
counter is incremented explicitly, once per trading day, so the ladder advances
on trading days only.

WHY JSON HERE BUT PARQUET FOR POSITIONS
---------------------------------------
This is a single small record, not a table, and it is meant to be readable in a
terminal when something has gone wrong at 3am.  The dtype hazard that motivated
Parquet for positions does not apply: the only integer that matters
(``days_since_remaining``) is compared against other integers, and
:func:`RegimeState.from_dict` coerces it back to ``int`` explicitly rather than
trusting whatever JSON produced.

WRITES ARE ATOMIC
-----------------
Same rationale as the position store: a half-written JSON file fails to parse,
and a regime state that fails to parse is indistinguishable from "no market
event", which is the most dangerous possible default.

DUAL SOURCE OF TRUTH WITH ``regime.embargo.ReEntryEmbargo``
----------------------------------------------------------
``ReEntryEmbargo`` keeps its own in-memory ``state`` / ``days_in_state`` /
``confirmation_date``.  So does :class:`RegimeState`.  Two copies of a state
machine is two answers to "where are we in the cooldown?".  The rule adopted
here:

  * :class:`RegimeState` is the PERSISTED MIRROR and the reporting source.
  * ``ReEntryEmbargo`` remains the TRANSITION LOGIC for a single process.
  * :meth:`RegimeStateStore.sync_from_embargo` copies the authority's answer
    into the mirror after every ``embargo.update(...)`` call.

The mirror never computes cooldown transitions itself; it only records them.  If
Workstream A would rather make :class:`RegimeState` authoritative, that is a
small refactor of ``embargo.py`` and the right call for multi-process runs —
see the integration questions in the Workstream B report.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import pandas as pd

__all__ = [
    "RegimeState",
    "RegimeStateStore",
    "TRIGGER_TYPES",
    "EMBARGO_STATES",
]

#: Trigger identifiers, matching ``MarketEventDetector.update()['trigger_type']``.
TRIGGER_TYPES = ("spread_velocity", "bocpd", "spy_trend")

#: Valid embargo states.
EMBARGO_STATES = ("inactive", "active", "cooling_down")


@dataclass
class RegimeState:
    """
    The persisted mirror of the 3-stage market-event state machine.

    Attributes:
        market_event_triggered: whether a trigger is currently confirmed.
        trigger_type: which one — one of :data:`TRIGGER_TYPES`.
        confirmation_date: when it confirmed.
        confirmation_day: the 1-based day of the persistence run, as reported
            by ``MarketEventDetector`` (0 when not triggered).
        days_since_confirmation: trading days elapsed since confirmation.
            0 on the confirmation day itself.  This is the value
            ``GradedDerisking.get_equity_cap`` consumes.
        embargo_state: one of :data:`EMBARGO_STATES`.
        embargo_days_remaining: trading days of cooldown still owed.
        current_equity_cap: equity fraction currently permitted, 1.0 when no
            event is active.
        cash_instruments: where the disallowed residual is parked.
        last_update: when this record was last written.
    """

    market_event_triggered: bool = False
    trigger_type: Optional[str] = None
    confirmation_date: Optional[pd.Timestamp] = None
    confirmation_day: int = 0
    days_since_confirmation: int = 0
    embargo_state: str = "inactive"
    embargo_days_remaining: int = 0
    current_equity_cap: float = 1.0
    cash_instruments: List[str] = field(default_factory=lambda: ["BIL", "SHY"])
    last_update: pd.Timestamp = field(default_factory=pd.Timestamp.now)

    def __post_init__(self) -> None:
        if self.trigger_type is not None and self.trigger_type not in TRIGGER_TYPES:
            raise ValueError(
                f"unknown trigger_type {self.trigger_type!r}; expected one of "
                f"{list(TRIGGER_TYPES)}. These identifiers are shared with "
                f"MarketEventDetector, so a typo here would silently never "
                f"match a real trigger.")
        if self.embargo_state not in EMBARGO_STATES:
            raise ValueError(
                f"unknown embargo_state {self.embargo_state!r}; expected one of "
                f"{list(EMBARGO_STATES)}")
        if self.confirmation_date is not None:
            self.confirmation_date = pd.Timestamp(self.confirmation_date)

    # -- derived -----------------------------------------------------------

    @property
    def is_de_risking(self) -> bool:
        """True while the equity cap is below 1.0."""
        return float(self.current_equity_cap) < 1.0

    @property
    def can_reenter(self) -> bool:
        """True when no embargo is in force."""
        return self.embargo_state == "inactive"

    def effective_equity_cap(self) -> float:
        """The cap to apply, never above 1.0 and never below 0."""
        return float(min(1.0, max(0.0, self.current_equity_cap)))

    # -- transitions -------------------------------------------------------

    def is_global_gate_open(self, is_expansion: bool = False, confirmation_unresolved: bool = False) -> bool:
        """
        False when market_event_triggered and not is_expansion,
        or when embargo_days_remaining > 0 and confirmation_unresolved.
        """
        if self.market_event_triggered and not is_expansion:
            return False
        if self.embargo_days_remaining > 0 and confirmation_unresolved:
            return False
        return True

    def advance_day(self, is_crisis: bool = False) -> None:
        """
        Advance one trading day.

        Only counts while an event is actually triggered.
        When regime IS CRISIS (is_crisis=True), do NOT decrement embargo_days_remaining (freeze macro clock).
        When regime is NOT CRISIS, decrement embargo_days_remaining by 1 each trading day if > 0.
        Fix existing bug: tie embargo decrement to the same global clock and regime state.
        """
        if self.market_event_triggered:
            self.days_since_confirmation = int(self.days_since_confirmation) + 1
        
        if not is_crisis:
            if self.embargo_days_remaining > 0:
                self.embargo_days_remaining = int(self.embargo_days_remaining) - 1
                if self.embargo_days_remaining == 0:
                    self.embargo_state = "inactive"

    def clear_event(self) -> None:
        """Retract the trigger and restore full equity."""
        self.market_event_triggered = False
        self.trigger_type = None
        self.confirmation_date = None
        self.confirmation_day = 0
        self.days_since_confirmation = 0
        self.current_equity_cap = 1.0

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a JSON-safe dict.  Timestamps become ISO-8601."""
        d = asdict(self)
        for k, v in list(d.items()):
            if isinstance(v, pd.Timestamp):
                d[k] = v.isoformat() if v is not None else None
        d["cash_instruments"] = list(self.cash_instruments or [])
        d["is_de_risking"] = self.is_de_risking
        d["can_reenter"] = self.can_reenter
        return d

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RegimeState":
        """
        Deserialise, tolerating unknown and missing keys.

        Derived keys written by :meth:`to_dict` (``is_de_risking``,
        ``can_reenter``) are dropped rather than passed to the constructor.
        Unknown keys are ignored so a future version can add fields without
        breaking an older reader.
        """
        payload = {k: v for k, v in dict(data).items()
                   if k in {f.name for f in fields(cls)}}
        for key in ("confirmation_date", "last_update"):
            if key in payload and payload[key] is not None:
                payload[key] = pd.Timestamp(payload[key])
        # Coerce numerics explicitly: JSON round-tripping should never be able
        # to leave an int field holding a string.
        for key in ("confirmation_day", "days_since_confirmation",
                    "embargo_days_remaining"):
            if payload.get(key) is not None:
                payload[key] = int(payload[key])
        for key in ("current_equity_cap",):
            if payload.get(key) is not None:
                payload[key] = float(payload[key])
        if payload.get("market_event_triggered") is not None:
            payload["market_event_triggered"] = bool(payload["market_event_triggered"])
        if payload.get("cash_instruments") is not None:
            payload["cash_instruments"] = [str(x) for x in payload["cash_instruments"]]
        if payload.get("last_update") is None:
            payload.pop("last_update")
        return cls(**payload)


class RegimeStateStore:
    """
    Persists :class:`RegimeState` to JSON on disk and to memory.

    Used by ``MarketEventDetector``, ``GradedDerisking`` and
    ``ReEntryEmbargo`` in Workstream A via :meth:`sync_from_detector`,
    :meth:`sync_from_derisking` and :meth:`sync_from_embargo`.
    """

    def __init__(self, storage_path: str = "data/regime_state.json",
                 autosave: bool = True) -> None:
        self.storage_path = str(storage_path)
        self.autosave = bool(autosave)
        self.state = RegimeState()
        self._load()

    @property
    def path(self) -> Path:
        return Path(self.storage_path)

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        """Load from JSON if it exists; a missing file means a clean slate."""
        p = self.path
        if not p.exists():
            self.state = RegimeState()
            return
        raw = p.read_text(encoding="utf-8").strip()
        if not raw:
            self.state = RegimeState()
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"regime state {p} is not valid JSON: {exc}. Refusing to "
                f"silently reset — a corrupt regime file read as 'no market "
                f"event' restores full equity mid-crisis.") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"regime state {p} did not contain a JSON object")
        self.state = RegimeState.from_dict(payload)

    def _save(self) -> None:
        """Write JSON atomically (temp file in the same dir, then move)."""
        p = self.path
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            tmp.write_text(
                json.dumps(self.state.to_dict(), indent=2, sort_keys=True,
                           default=str),
                encoding="utf-8")
            os.replace(str(tmp), str(p))
        finally:
            if tmp.exists():
                tmp.unlink()

    def _maybe_save(self) -> None:
        if self.autosave:
            self._save()

    # -- accessors ---------------------------------------------------------

    def get(self) -> RegimeState:
        """The live state object.  Mutating it does NOT persist — use update()."""
        return self.state

    def update(self, **kwargs: Any) -> RegimeState:
        """
        Set fields on the state and persist.

        Unknown field names are ignored (matching the brief's ``hasattr``
        behaviour) but the write still happens, so a caller passing a typo gets
        a persisted record that quietly lacks the field they meant to set.
        """
        for k, v in kwargs.items():
            if hasattr(self.state, k):
                setattr(self.state, k, v)
        self.state.last_update = pd.Timestamp.now()
        self._maybe_save()
        return self.state

    def save(self) -> None:
        """Force a write (for callers running with ``autosave=False``)."""
        self._save()

    def reset(self) -> RegimeState:
        """Return to a clean slate and persist."""
        self.state = RegimeState()
        self._maybe_save()
        return self.state

    def summary(self) -> Dict[str, Any]:
        """Provenance block for a report header."""
        return {
            "storage_path": self.storage_path,
            "exists": self.path.exists(),
            "market_event_triggered": self.state.market_event_triggered,
            "trigger_type": self.state.trigger_type,
            "days_since_confirmation": self.state.days_since_confirmation,
            "embargo_state": self.state.embargo_state,
            "embargo_days_remaining": self.state.embargo_days_remaining,
            "current_equity_cap": self.state.current_equity_cap,
            "last_update": self.state.last_update,
        }

    # -- Workstream A integration -----------------------------------------

    def advance_day(self, is_crisis: bool = False) -> RegimeState:
        """Advance the trading-day counters and persist."""
        self.state.advance_day(is_crisis=is_crisis)
        self.state.last_update = pd.Timestamp.now()
        self._maybe_save()
        return self.state

    def sync_from_detector(self, detector_output: Mapping[str, Any]) -> RegimeState:
        """
        Mirror ``MarketEventDetector.update()`` output into the persisted state.

        The detector's return dict is the authority on WHETHER a trigger fired;
        this store only records it.  Called once per day after
        ``detector.update(market_data)``.

        A rising trigger transitions latch: once ``confirmation_date`` is set it
        is not overwritten by a later detector call that reports no trigger,
        because ``days_since_confirmation`` is measured from the original
        confirmation and re-dating it would restart the de-risking ladder
        mid-crisis.  Clearing happens through :meth:`clear_event`, driven by the
        embargo's own exit condition.
        """
        d = dict(detector_output)
        triggered = bool(d.get("triggered", False))
        if triggered:
            self.state.market_event_triggered = True
            self.state.trigger_type = d.get("trigger_type", self.state.trigger_type)
            self.state.confirmation_day = int(d.get("confirmation_day", 0) or 0)
            self.state.days_since_confirmation = int(
                d.get("days_since_confirmation", 0) or 0)
            if self.state.confirmation_date is None:
                self.state.confirmation_date = pd.Timestamp.now().normalize()
        self.state.last_update = pd.Timestamp.now()
        self._maybe_save()
        return self.state

    def sync_from_derisking(self, equity_cap: float) -> RegimeState:
        """Mirror the cap that ``GradedDerisking.get_equity_cap`` computed."""
        self.state.current_equity_cap = float(min(1.0, max(0.0, float(equity_cap))))
        self.state.last_update = pd.Timestamp.now()
        self._maybe_save()
        return self.state

    def sync_from_embargo(self, embargo_output: Mapping[str, Any]) -> RegimeState:
        """
        Mirror ``ReEntryEmbargo.update()`` output into the persisted state.

        ``ReEntryEmbargo`` is the transition authority for the cooldown; this
        copies its answer so the value survives a restart.  See the module
        docstring on the dual source of truth.
        """
        e = dict(embargo_output)
        state = e.get("state")
        if state is not None:
            if state not in EMBARGO_STATES:
                raise ValueError(
                    f"ReEntryEmbargo reported unknown state {state!r}; expected "
                    f"one of {list(EMBARGO_STATES)}")
            self.state.embargo_state = state
        remaining = e.get("days_remaining")
        if remaining is not None:
            self.state.embargo_days_remaining = max(0, int(remaining))
        self.state.last_update = pd.Timestamp.now()
        self._maybe_save()
        return self.state
